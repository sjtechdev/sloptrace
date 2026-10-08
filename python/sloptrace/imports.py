"""Import cycles: strongly-connected components of the intra-repo import graph."""

from __future__ import annotations

import ast
from typing import NamedTuple

from sloptrace.discover import Module, ModuleIndex


class ImportRef(NamedTuple):
    """One import statement, reduced to what resolution needs.

    `import a.b, c`        -> module=None,  names=("a.b", "c"), level=0
    `from ..x import y, z` -> module="x",   names=("y", "z"),   level=2
    """
    module: str | None
    names: tuple[str, ...]
    level: int
    lineno: int


class CollectedImports(NamedTuple):
    runtime: list[ImportRef]
    type_only: int    # under `if TYPE_CHECKING:` -- never executes
    deferred: int     # in a function body or `if __name__ == "__main__":` -- not run on import


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _is_main_guard(test: ast.expr) -> bool:
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
            and test.left.id == "__name__" and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")


def _count_imports(nodes) -> int:
    return sum(isinstance(n, (ast.Import, ast.ImportFrom)) for x in nodes for n in ast.walk(x))


def _ref(n: ast.Import | ast.ImportFrom) -> ImportRef:
    if isinstance(n, ast.Import):
        return ImportRef(None, tuple(a.name for a in n.names), 0, n.lineno)
    return ImportRef(n.module, tuple(a.name for a in n.names), n.level, n.lineno)


def collect_imports(tree: ast.AST) -> CollectedImports:
    """Imports that execute when the module is imported.

    Two idioms exist precisely to BREAK an import cycle, and counting them as
    cycle edges would report the fix as the defect:
      - `if TYPE_CHECKING:` imports never run at all;
      - imports inside a function body run only when it's called.
    Imports under `if __name__ == "__main__":` likewise only run when the
    file is executed as a script. All three are counted separately instead.
    Class bodies do run at import time, so their imports count (but their
    methods' don't).
    """
    runtime: list[ImportRef] = []
    type_only = deferred = 0

    def walk(stmts: list[ast.stmt]) -> None:
        nonlocal type_only, deferred
        for n in stmts:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                runtime.append(_ref(n))
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                deferred += _count_imports(n.body)
            elif isinstance(n, ast.If) and _is_type_checking_guard(n.test):
                type_only += _count_imports(n.body)
                walk(n.orelse)
            elif isinstance(n, ast.If) and _is_main_guard(n.test):
                deferred += _count_imports(n.body)
                walk(n.orelse)
            else:
                for f in ("body", "orelse", "finalbody"):
                    sub = getattr(n, f, None)
                    if isinstance(sub, list):
                        walk([x for x in sub if isinstance(x, ast.stmt)])
                for h in getattr(n, "handlers", None) or []:   # try / except
                    walk(h.body)
                for c in getattr(n, "cases", None) or []:      # match / case
                    walk(c.body)

    walk(getattr(tree, "body", []))
    return CollectedImports(runtime, type_only, deferred)


def _relative_base(importer: Module, level: int) -> str | None:
    """The package a level-N relative import is relative to, or None if it
    climbs above the importer's top-level package."""
    parts = importer.name.split(".") if importer.name else []
    if not importer.is_package:
        parts = parts[:-1]          # a plain module's package is its parent
    up = level - 1
    if up > len(parts):
        return None
    return ".".join(parts[: len(parts) - up])


def resolve_import(importer: Module, ref: ImportRef, index: ModuleIndex) -> list[Module]:
    """The internal modules an import statement depends on.

    Follows grimp's convention: an import is an edge to the most specific
    module it names. `import a.b.c` -> a.b.c; `from a import b` -> a.b if
    that's a module, else a (b is a name defined in a/__init__.py).
    Relative imports only resolve inside the importer's own import root;
    absolute ones prefer it, and are skipped rather than guessed if they're
    ambiguous between other roots.
    """
    if ref.module is None and ref.level == 0:           # import a.b, c
        found = (index.lookup(name, importer) for name in ref.names)
        return [m for m in found if m is not None]

    if ref.level:
        base = _relative_base(importer, ref.level)
        if base is None:
            return []
        if ref.module:
            base = f"{base}.{ref.module}" if base else ref.module
        same_root_only = True
    else:
        base, same_root_only = ref.module, False

    out: list[Module] = []
    for name in ref.names:
        target = None
        if name != "*":
            sub = f"{base}.{name}" if base else name
            target = index.lookup(sub, importer, same_root_only)
        if target is None and base:
            target = index.lookup(base, importer, same_root_only)
        if target is not None and target not in out:
            out.append(target)
    return out


def import_graph(index: ModuleIndex, refs: dict[str, list[ImportRef]]) -> dict[str, set[str]]:
    """Edges between file paths (not module names: two import roots may
    share a name, paths never collide)."""
    edges: dict[str, set[str]] = {rel: set() for rel in index.by_rel}
    for rel, rs in refs.items():
        importer = index.by_rel[rel]
        for ref in rs:
            for target in resolve_import(importer, ref, index):
                if target.rel != rel:
                    edges[rel].add(target.rel)
    return edges


def strongly_connected(edges: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan. Returns only components of size > 1, i.e. the cycles,
    each sorted, in a deterministic order.

    An SCC is the honest unit here: naively enumerating cycles double counts
    every sub-loop of the same tangle. What you want to know is how many
    modules cannot be understood independently of each other.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in sorted(edges):
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    result.append(sorted(comp))
    return sorted(result)


def shortest_cycle(edges: dict[str, set[str]], component: list[str]) -> list[str]:
    """One concrete loop through the component, as evidence: the shortest
    cycle through its first member, found by BFS restricted to the
    component. Returned as [a, b, ..., a]."""
    members = set(component)
    start = component[0]
    parent: dict[str, str] = {}
    frontier = [start]
    while frontier:
        nxt = []
        for v in frontier:
            for w in sorted(edges[v] & members):
                if w == start:
                    path = [v]
                    while path[-1] != start:
                        path.append(parent[path[-1]])
                    return path[::-1] + [start]
                if w not in parent:
                    parent[w] = v
                    nxt.append(w)
        frontier = nxt
    return component  # unreachable for a genuine SCC
