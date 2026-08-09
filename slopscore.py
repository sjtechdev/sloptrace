#!/usr/bin/env python3
"""
slopscore -- deterministic complexity-erosion metrics for a Python codebase.

Four things, each earning its place independently:

  1. EROSION -- a single slow-moving trend line: the share of the codebase's
     total complexity mass sitting in functions above a cyclomatic-complexity
     threshold (SCBench arXiv:2603.24755 Eq.2-3). Backed by a ranked list of
     the actual functions driving it, each with its cyclomatic AND cognitive
     complexity, so the number is never just an assertion.
  2. CLONES -- duplicated code, at both whole-function and statement-block
     granularity, reported WITH EVIDENCE (file:line for every member) so a
     hit is something you can go look at, not just a ratio.
  3. REPEATED LITERALS -- a string literal repeated 4+ times in one module,
     or the same list/tuple of literals (a column/field schema) reused
     verbatim across the repo: usually a hard-coded thing that wants to be
     a shared constant.
  4. IMPORT CYCLES -- strongly-connected components of the intra-repo import
     graph: modules that cannot be understood independently of each other.

Deltas between snapshots (via --history) matter only for erosion -- one
codebase has no population to compare its absolute level against, so watch
the slope, not the value.

Usage:
    python slopscore.py /path/to/repo                     # HEAD only
    python slopscore.py /path/to/repo --history --every 20 --max-commits 15
    python slopscore.py /path/to/repo --history --since 2024-01-01 -o out.json
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

from radon.complexity import cc_visit
from radon.raw import analyze as raw_analyze

try:
    import complexipy   # cognitive complexity (Campbell / SonarSource algorithm)
    HAVE_COGNITIVE = True
except ImportError:
    HAVE_COGNITIVE = False

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

DEFAULT_EXCLUDES = [
    "test", "tests", "testing", "conftest.py", "migrations", "vendor",
    "vendored", "third_party", "_vendor", "node_modules", "build", "dist",
    ".venv", "venv", "site-packages", "docs", "examples", "setup.py",
]

CC_HIGH = 10       # radon's "moderate risk" cutoff
CC_SEVERE = 30     # reported separately as the subset that's especially bad
COG_HIGH = 15      # SonarCloud's default cognitive-complexity threshold

# Cognitive complexity correlates ~0.93 with cyclomatic, so it is mostly
# redundant -- it earns its place on the residual. It catches deeply nested
# code that CC scores as simple (requests' Response.iter_content: CC 7,
# cognitive 29) and forgives flat dispatch that CC punishes. Shown alongside
# CC on every offender, not folded into the erosion number itself.

EROSION_TOP_N = 15         # ranked offender list length in the printed report
MIN_LITERAL_REPEATS = 4    # a literal must recur this many times in one file
MIN_COLUMN_LIST_LEN = 3        # elements; shorter lists (flag pairs, ("GET","POST")) are rarely schemas
MIN_COLUMN_LIST_REPEATS = 2    # same list reused this many times, anywhere in the repo
MIN_CLONE_STATEMENTS = 3   # whole-function clones: below this, "duplicates" are idioms
MIN_CLONE_WINDOW = 3       # block clones: consecutive statements before a match counts
# DOMINANT LEVER on the block-clone count. Window 2 matches any two-statement
# idiom and is meaningless; 3 is a defensible minimum clone size. Treat it as
# calibrated, not derived, and re-check on your own repo with --clone-window.
CLONE_EVIDENCE_TOP_N = 8   # how many clone/literal/cycle groups to print evidence for


# --------------------------------------------------------------------------
# repeated literals: individual strings, and column-list schemas
# --------------------------------------------------------------------------


def repeated_literals(tree: ast.AST) -> list[dict]:
    """The same non-trivial string literal appearing MIN_LITERAL_REPEATS+
    times in one module -- usually a hard-coded field/column name that wants
    to be a shared constant. Returns evidence: value, count, and line numbers.

    Literals used only as a subscript key -- df["username"],
    df[["username", "email"]] -- are schema/column references by
    definition, not narrative repetition, so they're excluded from the count.
    (A whole subscript key LIST that recurs is instead caught by
    find_literal_lists below -- that's the stronger, more specific signal.)
    """
    subscript_keys: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript):
            keys = n.slice.elts if isinstance(n.slice, (ast.Tuple, ast.List)) else [n.slice]
            for k in keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    subscript_keys.add(id(k))

    occurrences: dict[str, list[int]] = defaultdict(list)
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in subscript_keys:
            v = n.value
            if 3 <= len(v) <= 60 and not v.startswith(("\n", " ")):
                occurrences[v].append(n.lineno)

    return [
        {"value": v, "count": len(lines), "lines": sorted(lines)}
        for v, lines in occurrences.items()
        if len(lines) >= MIN_LITERAL_REPEATS
    ]


def find_literal_lists(tree: ast.AST, rel: str) -> list[tuple[tuple[str, ...], str, int]]:
    """List/tuple literals of MIN_COLUMN_LIST_LEN+ string constants -- the
    shape of a hard-coded column/field schema (df[["id", "name", "email"]],
    a serializer's `fields = (...)`). Returns one (values, file, lineno) row
    per literal found; whether it's REPEATED is checked repo-wide by the
    caller, since the same schema copy-pasted into a different module is the
    common case, not just repetition within one file.
    """
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.List, ast.Tuple)) and len(n.elts) >= MIN_COLUMN_LIST_LEN:
            if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in n.elts):
                out.append((tuple(e.value for e in n.elts), rel, n.lineno))
    return out


def build_column_list_repeats(hits: list[tuple[tuple[str, ...], str, int]]) -> list[dict]:
    """The same literal list appearing at MIN_COLUMN_LIST_REPEATS+ distinct
    locations anywhere in the repo -- evidence: the values, and every
    file:line it was found at."""
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for values, rel, lineno in hits:
        groups[values].append(f"{rel}:{lineno}")
    out = [
        {"values": list(values), "count": len(locs), "locations": locs}
        for values, locs in groups.items()
        if len(locs) >= MIN_COLUMN_LIST_REPEATS
    ]
    out.sort(key=lambda g: -g["count"])
    return out


# --------------------------------------------------------------------------
# structure: import cycles
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# clones: whole-function fingerprints  +  statement-block windows
# --------------------------------------------------------------------------


class _Normaliser(ast.NodeTransformer):
    """Erase local identifiers and literals so renamed copy-paste still
    matches -- but keep attribute names, and the name of a directly-called
    function, intact. Erasing those too made unrelated statements collide
    on shape alone (`results.append(x)` and `results.update(x)` both
    became `V.A(V)`); preserving them means a match now requires the same
    *operation*, not just the same syntactic silhouette.
    """

    def visit_Call(self, n: ast.Call) -> ast.Call:
        if not isinstance(n.func, ast.Name):
            n.func = self.visit(n.func)
        n.args = [self.visit(a) for a in n.args]
        n.keywords = [self.visit(k) for k in n.keywords]
        return n

    def visit_Name(self, n):
        return ast.copy_location(ast.Name(id="V", ctx=n.ctx), n)

    def visit_arg(self, n):
        n.arg, n.annotation = "V", None
        return n

    def visit_Constant(self, n):
        return ast.copy_location(ast.Constant(value="C"), n)


def body_fingerprint(node) -> str | None:
    """Structural hash of a function body, or None if too small to be a clone."""
    body = [x for x in node.body
            if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant))]
    if len(body) < MIN_CLONE_STATEMENTS:
        return None
    try:
        mod = ast.parse(ast.unparse(ast.Module(body=body, type_ignores=[])))
        norm = _Normaliser().visit(mod)
        return hashlib.md5(ast.dump(norm).encode()).hexdigest()[:16]
    except (SyntaxError, ValueError, RecursionError, AttributeError):
        return None


def collect_fingerprints(tree: ast.AST, rel: str) -> dict[str, tuple]:
    """Fingerprint every function exactly once, under a scoped qualname.

    A single ast.walk would visit a method twice -- once via its class and once
    on its own -- registering two qualnames with the same hash and making every
    method a false 'clone' of itself.
    """
    out: dict[str, tuple] = {}

    def walk(nodes, prefix: str) -> None:
        for n in nodes:
            if isinstance(n, ast.ClassDef):
                walk(n.body, f"{prefix}{n.name}.")
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fp = body_fingerprint(n)
                if fp:
                    lo, hi = n.lineno, getattr(n, "end_lineno", n.lineno) or n.lineno
                    out[f"{rel}::{prefix}{n.name}"] = (fp, rel, lo, hi)
                walk(n.body, f"{prefix}{n.name}.")   # nested defs

    walk(getattr(tree, "body", []), "")
    return out


def clone_groups(fingerprints: dict[str, tuple]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for qual, rec in fingerprints.items():
        groups[rec[0]].append(qual)
    return {fp: sorted(v) for fp, v in groups.items() if len(v) > 1}


def build_function_clone_evidence(fingerprints: dict[str, tuple]) -> list[dict]:
    """Whole-function clone groups with a file:line-range for every member,
    largest groups first."""
    out = []
    for members in clone_groups(fingerprints).values():
        locations = [f"{fingerprints[m][1]}:{fingerprints[m][2]}-{fingerprints[m][3]}" for m in members]
        out.append({"members": members, "locations": locations})
    out.sort(key=lambda g: -len(g["members"]))
    return out


def find_divergent_clones(prev: "Snapshot", cur: "Snapshot") -> list[dict]:
    """Functions that were identical last snapshot and are not any more.

    This is the case the clone literature actually agrees is harmful: clones
    maintained consistently are mostly benign, but INCONSISTENT edits to
    duplicated code induce faults. A copy-paste that has started to drift is a
    bug waiting to be found in whichever copy nobody updated.
    """
    out = []
    for members in clone_groups(prev.func_fingerprints).values():
        still = {m: cur.func_fingerprints[m][0] for m in members if m in cur.func_fingerprints}
        variants = {v for v in still.values() if v}
        if len(still) >= 2 and len(variants) > 1:
            out.append({"members": sorted(still), "variants": len(variants)})
    return out


def block_clone_windows(tree: ast.AST, rel: str) -> dict[str, list[tuple[str, int, int]]]:
    """Structural (Type-2) clone detection over statement windows.

    Whole-function matching misses the common case: a duplicated block *inside*
    two otherwise-different functions. This slides a window of consecutive
    statements over every block in the module, normalises identifiers and
    literals away, and hashes. Windows sharing a hash are clones of each other.

    Returns hash -> one (file, first_line, last_line) entry per matching
    window, so a clone can be reported with evidence instead of just a ratio.
    """
    windows: dict[str, list[tuple[str, int, int]]] = defaultdict(list)

    def norm_stmt(st: ast.stmt) -> str | None:
        try:
            mod = ast.parse(ast.unparse(st))
            return ast.dump(_Normaliser().visit(mod))
        except (SyntaxError, ValueError, RecursionError, AttributeError):
            return None

    def scan(body: list, windowable: bool = True) -> None:
        stmts = [x for x in body if isinstance(x, ast.stmt)]
        if windowable and len(stmts) >= MIN_CLONE_WINDOW:
            normed = [norm_stmt(x) for x in stmts]
            for i in range(len(stmts) - MIN_CLONE_WINDOW + 1):
                chunk = normed[i:i + MIN_CLONE_WINDOW]
                if any(c is None for c in chunk):
                    continue
                h = hashlib.md5("|".join(chunk).encode()).hexdigest()[:16]
                lo = stmts[i].lineno
                hi = getattr(stmts[i + MIN_CLONE_WINDOW - 1], "end_lineno", lo) or lo
                windows[h].append((rel, lo, hi))
        for st in stmts:
            for f in ("body", "orelse", "finalbody"):
                sub = getattr(st, f, None)
                if isinstance(sub, list):
                    scan(sub)
            for h in getattr(st, "handlers", []) or []:
                scan(h.body)

    # The module's own top level is never windowed directly -- a run of
    # imports or top-level constants is boilerplate, not a clone, and
    # matched two unrelated files' import blocks against each other. Nested
    # bodies (functions, classes, control flow) are still fully scanned.
    scan(getattr(tree, "body", []), windowable=False)
    return windows


def _merge_spans(occurrences: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """Collapse overlapping/adjacent (file, lo, hi) spans within the same
    file into one.

    Sliding a window by 1 statement over a genuine N-statement duplicate
    produces N-window+1 overlapping windows for what is really ONE location;
    left unmerged, that inflates the reported occurrence count, and can even
    manufacture a fake clone out of a single location whose windows merely
    overlap THEMSELVES -- e.g. four similarly-shaped `self.x = None` lines in
    a row normalise to the same statement, so windows [0:3] and [1:4] hash
    equal even though nothing was ever copy-pasted anywhere else. Requiring
    2+ occurrences AFTER this merge is what tells a real second location
    apart from that kind of self-overlap.
    """
    by_file: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for rel, lo, hi in occurrences:
        by_file[rel].append((lo, hi))
    merged: list[tuple[str, int, int]] = []
    for rel, spans in by_file.items():
        spans.sort()
        cur_lo, cur_hi = spans[0]
        for lo, hi in spans[1:]:
            if lo <= cur_hi + 1:      # overlapping or touching -> same location
                cur_hi = max(cur_hi, hi)
            else:
                merged.append((rel, cur_lo, cur_hi))
                cur_lo, cur_hi = lo, hi
        merged.append((rel, cur_lo, cur_hi))
    return merged


def _spans_touch(a: tuple[str, int, int], b: tuple[str, int, int]) -> bool:
    ar, alo, ahi = a
    br, blo, bhi = b
    return ar == br and blo <= ahi + 1 and bhi >= alo - 1


def _coalesce_groups(groups: list[list[tuple[str, int, int]]]) -> list[list[tuple[str, int, int]]]:
    """Merge block-clone groups that are windowed views of the same longer
    duplicate.

    A real 4-statement duplicate, seen through a 3-statement window,
    produces TWO different hashes -- stmts[0:3] and stmts[1:4] -- each a
    legitimate 2+-location group on its own, but describing overlapping
    slices of one clone rather than two separate ones. Two groups merge
    when they have the same number of locations and every location in one
    pairs up, file-for-file, with a touching/overlapping location in the
    other -- i.e. they're the same set of instances, just windowed
    differently.
    """
    groups = [list(g) for g in groups]
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            a_sorted = sorted(groups[i])
            for j in range(i + 1, len(groups)):
                b_sorted = sorted(groups[j])
                if len(a_sorted) != len(b_sorted):
                    continue
                if all(_spans_touch(x, y) for x, y in zip(a_sorted, b_sorted)):
                    groups[i] = [
                        (xr, min(xlo, ylo), max(xhi, yhi))
                        for (xr, xlo, xhi), (_yr, ylo, yhi) in zip(a_sorted, b_sorted)
                    ]
                    del groups[j]
                    changed = True
                    break
            if changed:
                break
    return groups


def build_block_clone_evidence(
    windows: dict[str, list[tuple[str, int, int]]],
) -> tuple[list[dict], set[tuple[str, int]]]:
    """Hashes that occurred at 2+ distinct locations across the repo (after
    merging overlapping/adjacent windows into one location each -- see
    _merge_spans), then coalesced across hashes so a longer duplicate isn't
    reported as several overlapping smaller ones (see _coalesce_groups).
    Each group carries every location's file:line range, largest
    (occurrences) first."""
    raw_groups: list[list[tuple[str, int, int]]] = []
    for occurrences in windows.values():
        locations = _merge_spans(occurrences)
        if len(locations) >= 2:
            raw_groups.append(locations)

    groups = []
    clone_lines: set[tuple[str, int]] = set()
    for locations in _coalesce_groups(raw_groups):
        locations = sorted(locations)
        groups.append({
            "occurrences": len(locations),
            "locations": [f"{r}:{lo}-{hi}" for r, lo, hi in locations],
        })
        for r, lo, hi in locations:
            clone_lines.update((r, ln) for ln in range(lo, hi + 1))
    groups.sort(key=lambda g: -g["occurrences"])
    return groups, clone_lines


# --------------------------------------------------------------------------
# snapshot analysis
# --------------------------------------------------------------------------


@dataclass
class Snapshot:
    ref: str
    date: str = ""
    subject: str = ""   # row label for humans reading the JSON, not a metric
    lloc: int = 0
    functions: int = 0
    parse_errors: int = 0

    # -- erosion ------------------------------------------------------------
    cc_gt_10: int = 0
    cc_gt_30: int = 0
    cog_gt_15: int = 0
    erosion_mass: float = 0.0                    # Eq.3: share of complexity mass in CC>10 funcs
    erosion_offenders: list = field(default_factory=list)   # ranked functions behind it

    # -- clones ---------------------------------------------------------------
    clone_ratio: float = 0.0                      # clone lines / LLOC
    clone_line_count: int = 0
    function_clones: list = field(default_factory=list)     # whole-body dup groups, w/ evidence
    block_clones: list = field(default_factory=list)        # dup statement-window groups, w/ evidence
    func_fingerprints: dict = field(default_factory=dict)   # qualname -> (hash, file, lo, hi)
    divergent_clones: list = field(default_factory=list)    # drift since prev snapshot (history mode)

    # -- repeated literals ------------------------------------------------------
    literal_repeats: list = field(default_factory=list)        # [{value, count, file, lines}]
    column_list_repeats: list = field(default_factory=list)    # [{values, count, locations}], repo-wide

    # -- import cycles --------------------------------------------------------
    modules: int = 0
    cyclic_modules: int = 0
    type_only_imports: int = 0
    sccs: list = field(default_factory=list)      # import cycles (size > 1)

    # -- deltas, filled in later (history mode only) ---------------------------
    delta_erosion_pct: float | None = None


def iter_python_files(root: Path, excludes: list[str]) -> list[Path]:
    out = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root)
        parts = {s.lower() for s in rel.parts}
        stem = rel.parts[-1].lower()
        if parts & set(excludes) or stem in excludes:
            continue
        if any(stem.startswith("test_") or stem.endswith("_test.py") for _ in [0]):
            continue
        out.append(p)
    return sorted(out)


def analyse_snapshot(root: Path, ref: str, excludes: list[str]) -> Snapshot:
    snap = Snapshot(ref=ref)
    files = iter_python_files(root, excludes)

    total_mass = 0.0
    high_mass = 0.0
    offenders: list[dict] = []
    literal_repeats: list[dict] = []
    column_list_hits: list[tuple[tuple[str, ...], str, int]] = []
    import_nodes: dict[str, list] = {}
    block_windows: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    function_count = 0

    for f in files:
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except (SyntaxError, ValueError, OSError):
            snap.parse_errors += 1
            continue

        rel = str(f.relative_to(root))

        try:
            snap.lloc += raw_analyze(src).lloc
        except Exception:
            pass

        cog_by_line: dict[int, int] = {}
        if HAVE_COGNITIVE:
            try:
                for fn in complexipy.file_complexity(str(f)).functions:
                    cog_by_line[fn.line_start] = fn.complexity
            except Exception:
                pass

        try:
            # Function blocks only: cc_visit also yields Class blocks whose
            # complexity is the aggregate of their methods, which would double
            # count against a mass-weighted total.
            for block in cc_visit(src):
                if type(block).__name__ != "Function":
                    continue
                function_count += 1
                endline = block.endline or block.lineno
                sloc = max(1, endline - block.lineno + 1)
                mass = block.complexity * math.sqrt(sloc)   # Eq.2
                total_mass += mass
                if block.complexity > CC_SEVERE:
                    snap.cc_gt_30 += 1
                cog = cog_by_line.get(block.lineno)
                if cog is not None and cog > COG_HIGH:
                    snap.cog_gt_15 += 1
                if block.complexity > CC_HIGH:
                    snap.cc_gt_10 += 1
                    high_mass += mass
                    qualname = f"{block.classname}.{block.name}" if block.classname else block.name
                    offenders.append({
                        "qualname": qualname, "file": rel, "lineno": block.lineno,
                        "cc": block.complexity, "cog": cog, "mass": mass,
                    })
        except Exception:
            pass

        modname = module_name(rel)
        import_nodes[modname], n_type_only = collect_imports(tree)
        snap.type_only_imports += n_type_only

        snap.func_fingerprints.update(collect_fingerprints(tree, rel))
        for h, occurrences in block_clone_windows(tree, rel).items():
            block_windows[h].extend(occurrences)

        for hit in repeated_literals(tree):
            literal_repeats.append({**hit, "file": rel})
        column_list_hits.extend(find_literal_lists(tree, rel))

    # -- import cycles --
    known = set(import_nodes)
    edges: dict[str, set[str]] = {m: set() for m in known}
    for mod, nodes in import_nodes.items():
        for n in nodes:
            for tgt in resolve_import(mod, n, known):
                if tgt != mod:
                    edges[mod].add(tgt)
    snap.modules = len(known)
    snap.sccs = strongly_connected(edges, sorted(known))
    snap.cyclic_modules = sum(len(c) for c in snap.sccs)

    # -- clones --
    snap.function_clones = build_function_clone_evidence(snap.func_fingerprints)
    snap.block_clones, clone_lines = build_block_clone_evidence(block_windows)
    _lloc = max(snap.lloc, 1)
    snap.clone_line_count = len(clone_lines)
    snap.clone_ratio = round(min(1.0, len(clone_lines) / _lloc), 4)

    # -- erosion: rank offenders by their share of total complexity mass --
    snap.erosion_mass = round(high_mass / total_mass, 4) if total_mass else 0.0
    offenders.sort(key=lambda o: -o["mass"])
    for o in offenders:
        o["mass_pct"] = round(100 * o["mass"] / total_mass, 2) if total_mass else 0.0
        del o["mass"]
    snap.erosion_offenders = offenders

    # -- repeated literals --
    literal_repeats.sort(key=lambda h: -h["count"])
    snap.literal_repeats = literal_repeats
    snap.column_list_repeats = build_column_list_repeats(column_list_hits)

    snap.functions = function_count
    return snap


# --------------------------------------------------------------------------
# git history walking
# --------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def pick_commits(repo: Path, every: int, max_commits: int, since: str | None) -> list[tuple[str, str, str]]:
    args = ["log", "--first-parent", "--format=%H\x1f%cs\x1f%s"]
    if since:
        args.append(f"--since={since}")
    lines = [l for l in git(repo, *args).splitlines() if l]
    rows = [tuple(l.split("\x1f", 2)) for l in lines]
    rows = rows[::every] if every > 1 else rows
    rows = rows[:max_commits]
    return list(reversed(rows))  # oldest -> newest


def checkout_to_temp(repo: Path, sha: str, dest: Path) -> None:
    """git archive is cheaper and safer than a worktree: no index, no locks."""
    p = subprocess.run(["git", "-C", str(repo), "archive", sha], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
        tf.extractall(dest)


def add_deltas(snaps: list[Snapshot]) -> None:
    for prev, cur in zip(snaps, snaps[1:]):
        cur.divergent_clones = find_divergent_clones(prev, cur)
        if prev.erosion_mass:
            cur.delta_erosion_pct = round(100 * (cur.erosion_mass - prev.erosion_mass) / prev.erosion_mass, 1)


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else "…" + s[-(n - 1):]


def print_report(snaps: list[Snapshot], show_literals: bool = False) -> None:
    w = sys.stdout.write
    last = snaps[-1]

    # ---- erosion: the headline, plus what's behind it ----
    w(f"\nEROSION    {last.erosion_mass:.3f}   complexity mass sitting in CC>{CC_HIGH} functions\n")
    cog_note = f", {last.cog_gt_15} cognitive>{COG_HIGH}" if HAVE_COGNITIVE else " (pip install slop_score[cognitive] for cognitive complexity)"
    w(f"           {last.cc_gt_10} of {last.functions} functions CC>{CC_HIGH} "
      f"({last.cc_gt_30} of those CC>{CC_SEVERE}){cog_note}\n")

    if last.erosion_offenders:
        w("\nFUNCTIONS DRIVING EROSION\n")
        hdr = f"  {'function':<30} {'file:line':<30} {'CC':>4} {'COG':>4} {'mass%':>6}\n"
        w(hdr)
        w("  " + "-" * (len(hdr) - 4) + "\n")
        for o in last.erosion_offenders[:EROSION_TOP_N]:
            loc = f"{o['file']}:{o['lineno']}"
            cog = "-" if o["cog"] is None else o["cog"]
            w(f"  {_clip(o['qualname'], 30):<30} {_clip(loc, 30):<30} {o['cc']:>4} {cog:>4} {o['mass_pct']:>6.1f}\n")
        rest = len(last.erosion_offenders) - EROSION_TOP_N
        if rest > 0:
            w(f"  ... {rest} more in JSON output (-o)\n")

    # ---- movement: erosion trend only (history mode) ----
    if len(snaps) > 1:
        w("\nMOVEMENT\n")
        hdr = f"  {'ref':<10} {'date':<11} {'erosion':>8} {'dEros%':>8} {'CC>10':>6} {'CC>30':>6}\n"
        w(hdr)
        w("  " + "-" * (len(hdr) - 4) + "\n")
        for s_ in snaps:
            d = "       -" if s_.delta_erosion_pct is None else format(s_.delta_erosion_pct, ">8.1f")
            w(f"  {s_.ref[:9]:<10} {s_.date:<11} {s_.erosion_mass:>8.3f} {d} {s_.cc_gt_10:>6} {s_.cc_gt_30:>6}\n")

    # ---- clones, with evidence ----
    if last.function_clones or last.block_clones:
        w(f"\nCLONES     {last.clone_ratio:.3f}   duplicated lines / LLOC "
          f"({last.clone_line_count} of {max(last.lloc, 1)} lines)\n")
        if last.function_clones:
            w(f"  whole-function duplicates, {len(last.function_clones)} group(s):\n")
            for g in last.function_clones[:CLONE_EVIDENCE_TOP_N]:
                extra = len(g["locations"]) - 4
                w(f"    {len(g['members']):>2}x  " + "  ".join(g["locations"][:4])
                  + (f"  +{extra} more" if extra > 0 else "") + "\n")
        if last.block_clones:
            w(f"  duplicated statement blocks, {len(last.block_clones)} group(s):\n")
            for g in last.block_clones[:CLONE_EVIDENCE_TOP_N]:
                extra = len(g["locations"]) - 4
                w(f"    {g['occurrences']:>2}x  " + "  ".join(g["locations"][:4])
                  + (f"  +{extra} more" if extra > 0 else "") + "\n")
    div = [d for s_ in snaps for d in s_.divergent_clones]
    if div:
        w(f"  divergent ({len(div)}): identical last snapshot, not any more -- "
          + "; ".join(" / ".join(m.split("::")[-1] for m in d["members"][:3]) for d in div[:4]) + "\n")

    # ---- repeated literals: noisy on most codebases, so JSON-only unless
    # --show-literals is passed. Repeated COLUMN LISTS (below) is the
    # sharper signal and always prints. ----
    if show_literals and last.literal_repeats:
        w(f"\nREPEATED LITERALS  {len(last.literal_repeats)} literal(s) repeated {MIN_LITERAL_REPEATS}+ times in one file\n")
        for h in last.literal_repeats[:CLONE_EVIDENCE_TOP_N]:
            lines = ", ".join(str(l) for l in h["lines"][:6])
            extra = len(h["lines"]) - 6
            w(f"  {h['count']:>3}x  {h['value']!r:<28} {h['file']}:{lines}"
              + (f" +{extra} more" if extra > 0 else "") + "\n")
    elif last.literal_repeats:
        w(f"\n  {len(last.literal_repeats)} repeated literal(s) found, hidden by default "
          f"(noisy) -- see -o JSON or pass --show-literals\n")
    if last.column_list_repeats:
        w(f"\nREPEATED COLUMN LISTS  {len(last.column_list_repeats)} list(s) of "
          f"{MIN_COLUMN_LIST_LEN}+ literals reused {MIN_COLUMN_LIST_REPEATS}+ times\n")
        for g in last.column_list_repeats[:CLONE_EVIDENCE_TOP_N]:
            vextra = len(g["values"]) - 6
            vals = ", ".join(repr(v) for v in g["values"][:6]) + (", …" if vextra > 0 else "")
            lextra = len(g["locations"]) - 4
            locs = "  ".join(g["locations"][:4])
            w(f"  {g['count']:>3}x  [{vals}]  {locs}" + (f"  +{lextra} more" if lextra > 0 else "") + "\n")

    # ---- import cycles ----
    if last.sccs:
        pct = 100 * last.cyclic_modules / max(last.modules, 1)
        w(f"\nIMPORT CYCLES  {last.cyclic_modules}/{last.modules} modules ({pct:.0f}%) tangled\n")
        for c in sorted(last.sccs, key=len, reverse=True)[:CLONE_EVIDENCE_TOP_N]:
            tail = " -> ..." if len(c) > 8 else ""
            w("  " + " -> ".join(x.split(".")[-1] for x in c[:8]) + tail + "\n")
        if last.type_only_imports:
            w(f"  ({last.type_only_imports} additional import(s) are TYPE_CHECKING-guarded and don't run)\n")

    if last.parse_errors:
        w(f"\n  {last.parse_errors} file(s) failed to parse, skipped.\n")
    w("\n")


def main() -> int:
    global MIN_CLONE_WINDOW
    ap = argparse.ArgumentParser(description="Deterministic complexity-erosion metrics for Python repos.")
    ap.add_argument("repo", type=Path)
    ap.add_argument("--history", action="store_true", help="walk git history instead of scoring the working tree")
    ap.add_argument("--every", type=int, default=1, help="sample every Nth commit (default 1)")
    ap.add_argument("--max-commits", type=int, default=10)
    ap.add_argument("--since", default=None, help="e.g. 2024-01-01")
    ap.add_argument("--exclude", nargs="*", default=None, help="path segments to skip (replaces defaults)")
    ap.add_argument("--clone-window", type=int, default=MIN_CLONE_WINDOW,
                    help=f"min consecutive statements for a block clone (default {MIN_CLONE_WINDOW}; "
                         "dominant lever on the clone count")
    ap.add_argument("--show-literals", action="store_true",
                    help="print the REPEATED LITERALS section (hidden by default -- noisy; "
                         "always in the -o JSON regardless)")
    ap.add_argument("-o", "--output", type=Path, default=None, help="write JSON here")
    args = ap.parse_args()

    excludes = args.exclude if args.exclude is not None else DEFAULT_EXCLUDES
    MIN_CLONE_WINDOW = args.clone_window
    repo = args.repo.resolve()

    if not args.history:
        snaps = [analyse_snapshot(repo, "worktree", excludes)]
    else:
        commits = pick_commits(repo, args.every, args.max_commits, args.since)
        if not commits:
            print("no commits matched", file=sys.stderr)
            return 1
        snaps = []
        for i, (sha, date, subject) in enumerate(commits, 1):
            print(f"[{i}/{len(commits)}] {sha[:9]} {date} {subject[:50]}", file=sys.stderr)
            tmp = Path(tempfile.mkdtemp(prefix="slopscore-"))
            try:
                checkout_to_temp(repo, sha, tmp)
                s = analyse_snapshot(tmp, sha, excludes)
                s.date, s.subject = date, subject
                snaps.append(s)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        add_deltas(snaps)

    print_report(snaps, show_literals=args.show_literals)
    if args.output:
        payload = {"snapshots": [asdict(s) for s in snaps]}
        args.output.write_text(json.dumps(payload, indent=2, default=str))
        print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
