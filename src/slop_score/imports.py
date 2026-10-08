"""Import cycles: strongly-connected components of the intra-repo import graph."""

from __future__ import annotations

import ast
import os


def module_name(rel: str) -> str:
    return rel.replace(os.sep, ".").replace("/", ".").removesuffix(".py").removesuffix(".__init__")


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def collect_imports(tree: ast.AST) -> tuple[list[ast.ImportFrom], int]:
    """Runtime `from ... import ...` nodes, plus a count of type-only ones.

    Imports under `if TYPE_CHECKING:` never execute. They are the standard
    idiom for BREAKING an import cycle, so counting them as cycles reports the
    fix as the defect. Kept separately because a type-only cycle is still worth
    knowing about -- it just costs nothing at runtime.
    """
    runtime: list[ast.ImportFrom] = []
    type_only = 0

    def walk(nodes, guarded: bool) -> None:
        nonlocal type_only
        for n in nodes:
            if isinstance(n, ast.If):
                g = guarded or _is_type_checking_guard(n.test)
                walk(n.body, g)
                walk(n.orelse, guarded)
                continue
            if isinstance(n, ast.ImportFrom):
                if guarded:
                    type_only += 1
                else:
                    runtime.append(n)
            for f in ("body", "orelse", "finalbody", "handlers"):
                sub = getattr(n, f, None)
                if isinstance(sub, list):
                    walk([x for x in sub if isinstance(x, ast.stmt)], guarded)

    walk(getattr(tree, "body", []), False)
    return runtime, type_only


def resolve_import(current: str, node: ast.ImportFrom, known: set[str]) -> list[str]:
    """Resolve one `from ... import ...` to internal module names.

    Relative imports are resolved properly against the importing module's
    package. Absolute ones fall back to a suffix match, because a repo laid out
    as src/pkg/mod.py yields module names like 'src.pkg.mod' while the code
    inside writes 'from pkg.mod import x'.
    """
    out = []
    if node.level:
        parts = current.split(".")
        pkg = ".".join(parts[: max(0, len(parts) - node.level)])
        if node.module:
            cand = f"{pkg}.{node.module}" if pkg else node.module
            if cand in known:
                out.append(cand)
        else:
            # `from . import mod` -- each alias may itself be a module
            for a in node.names:
                cand = f"{pkg}.{a.name}" if pkg else a.name
                if cand in known:
                    out.append(cand)
    elif node.module:
        if node.module in known:
            out.append(node.module)
        else:
            for k in known:
                if k.endswith("." + node.module):
                    out.append(k)
                    break
    return out


def strongly_connected(edges: dict[str, set[str]], nodes: list[str]) -> list[list[str]]:
    """Iterative Tarjan. Returns only components of size > 1, i.e. the cycles.

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

    for root in nodes:
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
    return result
