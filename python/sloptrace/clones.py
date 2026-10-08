"""Clones: whole-function fingerprints plus statement-block windows.

Both are Type-2 (structural) clone detection: identifiers and literals are
normalised away, so renamed copy-paste still matches.
"""

from __future__ import annotations

import ast
import hashlib
from collections import defaultdict

from sloptrace.config import Config

Span = tuple[str, int, int]   # (file, first line, last line)


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


def body_fingerprint(node, min_statements: int) -> str | None:
    """Structural hash of a function body, or None if too small to be a clone."""
    body = [x for x in node.body
            if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant))]
    if len(body) < min_statements:
        return None
    try:
        mod = ast.parse(ast.unparse(ast.Module(body=body, type_ignores=[])))
        norm = _Normaliser().visit(mod)
        return hashlib.md5(ast.dump(norm).encode()).hexdigest()[:16]
    except (SyntaxError, ValueError, RecursionError, AttributeError):
        return None


def collect_fingerprints(tree: ast.AST, rel: str, config: Config) -> dict[str, tuple]:
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
                fp = body_fingerprint(n, config.min_clone_statements)
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


def find_divergent_clones(prev: dict[str, tuple], cur: dict[str, tuple]) -> list[dict]:
    """Functions that were identical last snapshot and are not any more.

    This is the case the clone literature actually agrees is harmful: clones
    maintained consistently are mostly benign, but INCONSISTENT edits to
    duplicated code induce faults. A copy-paste that has started to drift is a
    bug waiting to be found in whichever copy nobody updated.
    """
    out = []
    for members in clone_groups(prev).values():
        still = {m: cur[m][0] for m in members if m in cur}
        variants = {v for v in still.values() if v}
        if len(still) >= 2 and len(variants) > 1:
            out.append({"members": sorted(still), "variants": len(variants)})
    return out


def block_clone_windows(tree: ast.AST, rel: str, window: int) -> dict[str, list[tuple[str, int, int]]]:
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
        if windowable and len(stmts) >= window:
            normed = [norm_stmt(x) for x in stmts]
            for i in range(len(stmts) - window + 1):
                chunk = normed[i:i + window]
                if any(c is None for c in chunk):
                    continue
                h = hashlib.md5("|".join(chunk).encode()).hexdigest()[:16]
                lo = stmts[i].lineno
                hi = getattr(stmts[i + window - 1], "end_lineno", lo) or lo
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


def block_clone_groups(windows: dict[str, list[Span]]) -> list[list[Span]]:
    """Hashes that occurred at 2+ distinct locations across the repo (after
    merging overlapping/adjacent windows into one location each -- see
    _merge_spans), then coalesced across hashes so a longer duplicate isn't
    reported as several overlapping smaller ones (see _coalesce_groups)."""
    raw_groups: list[list[Span]] = []
    for occurrences in windows.values():
        locations = _merge_spans(occurrences)
        if len(locations) >= 2:
            raw_groups.append(locations)
    return [sorted(g) for g in _coalesce_groups(raw_groups)]


def function_clone_spans(fingerprints: dict[str, tuple]) -> list[list[Span]]:
    """Each whole-function clone group, as the spans of its members."""
    return [[tuple(fingerprints[m][1:4]) for m in members]
            for members in clone_groups(fingerprints).values()]


def _contains(outer: Span, inner: Span) -> bool:
    return outer[0] == inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2]


def subsumed_by_function_clone(group: list[Span], function_groups: list[list[Span]]) -> bool:
    """True if every location of a block clone sits inside members of one
    and the same whole-function clone group. Such a block is just a slice of
    a duplicate already reported whole, so listing it again double-counts
    the evidence."""
    common: set[int] | None = None
    for loc in group:
        hits = {i for i, fg in enumerate(function_groups) if any(_contains(s, loc) for s in fg)}
        common = hits if common is None else common & hits
        if not common:
            return False
    return True


def build_block_clone_evidence(groups: list[list[Span]]) -> list[dict]:
    """Every location's file:line range, largest groups first."""
    out = [{"occurrences": len(g), "locations": [f"{r}:{lo}-{hi}" for r, lo, hi in g]} for g in groups]
    out.sort(key=lambda g: -g["occurrences"])
    return out
