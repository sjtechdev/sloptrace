"""Near-duplicate functions: same logic, not quite the same code.

Exact clone detection (clones.py) hashes a whole normalised body, so one
added or removed statement defeats it. Here each function is reduced to the
set of its normalised statements and two functions are compared by Jaccard
similarity: shared statements / all distinct statements between them.

A statement contributes its own "shape" (its type plus its normalised
header expressions: an `if` counts for its test, not its body). Identifiers
and literals are erased exactly as for exact clones, so renames are free.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from collections import Counter, defaultdict
from typing import NamedTuple

from sloptrace.clones import _Normaliser, body_fingerprint
from sloptrace.config import Config

_BLOCK_FIELDS = {"body", "orelse", "finalbody", "handlers", "cases", "name"}
_MAX_DOC_FREQ = 50   # a statement shape in more functions than this is too common to find candidates with


class FunctionShape(NamedTuple):
    key: str                 # "rel::qualname", same keys as the exact-clone fingerprints
    shingles: frozenset
    rel: str
    lo: int
    hi: int
    fp: str | None = None    # exact-body fingerprint (any size); equal fp means an exact copy, not a near one


def _h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:12], 16)


def _dump(value) -> str:
    if isinstance(value, ast.AST):
        return ast.dump(_Normaliser().visit(copy.deepcopy(value)))
    if isinstance(value, list):
        return "[" + ",".join(_dump(v) for v in value) + "]"
    return repr(value)


def statement_shingles(func: ast.AST) -> frozenset:
    out: set[int] = set()

    def walk(stmts) -> None:
        for st in stmts:
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                continue    # docstring / bare string
            parts = [type(st).__name__]
            parts += [_dump(v) for k, v in ast.iter_fields(st) if k not in _BLOCK_FIELDS]
            out.add(_h("|".join(parts)))
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue    # a nested def is compared as a function of its own, not as part of this one
            for f in ("body", "orelse", "finalbody"):
                sub = getattr(st, f, None)
                if isinstance(sub, list):
                    walk(sub)
            for h in getattr(st, "handlers", None) or []:
                out.add(_h("ExceptHandler|" + _dump(h.type)))
                walk(h.body)
            for c in getattr(st, "cases", None) or []:
                walk(c.body)

    walk(func.body)
    return frozenset(out)


def collect_shapes(tree: ast.AST, rel: str, config: Config) -> dict[str, FunctionShape]:
    """One FunctionShape per function big enough to compare (scoped
    qualnames, each function visited once, as in clones.collect_fingerprints)."""
    out: dict[str, FunctionShape] = {}

    def walk(nodes, prefix: str) -> None:
        for n in nodes:
            if isinstance(n, ast.ClassDef):
                walk(n.body, f"{prefix}{n.name}.")
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                shingles = statement_shingles(n)
                if len(shingles) >= config.near_dup_min_statements:
                    key = f"{rel}::{prefix}{n.name}"
                    out[key] = FunctionShape(key, shingles, rel, n.lineno,
                                             getattr(n, "end_lineno", n.lineno) or n.lineno,
                                             body_fingerprint(n, 1))
                walk(n.body, f"{prefix}{n.name}.")

    walk(getattr(tree, "body", []), "")
    return out


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def find_similar(own: list[FunctionShape], others: list[FunctionShape],
                 threshold: float) -> list[tuple[float, FunctionShape, FunctionShape]]:
    """For each function in `own`, its most similar function (>= threshold)
    in `others`, skipping exact copies (equal body fingerprint). Candidates come from an inverted index on statement
    shapes, ignoring shapes too common to be informative, so this is an
    approximation that cannot find a match made up only of ubiquitous shapes.
    """
    index: dict[int, list[int]] = defaultdict(list)
    for i, o in enumerate(others):
        for s in o.shingles:
            index[s].append(i)

    out = []
    for a in own:
        overlap: Counter[int] = Counter()
        for s in a.shingles:
            docs = index.get(s, ())
            if len(docs) <= _MAX_DOC_FREQ:
                overlap.update(docs)
        best = None
        for i, n in overlap.items():
            b = others[i]
            if n < 2 or min(len(a.shingles), len(b.shingles)) < threshold * max(len(a.shingles), len(b.shingles)) - 1e-9:
                continue    # cheap size bound: Jaccard <= min/max
            if a.fp is not None and a.fp == b.fp:
                continue
            sim = jaccard(a.shingles, b.shingles)
            if sim >= threshold and (best is None or (sim, b.key) > (best[0], best[1].key)):
                best = (sim, b)
        if best:
            out.append((best[0], a, best[1]))
    out.sort(key=lambda t: (-t[0], t[1].key))
    return out
