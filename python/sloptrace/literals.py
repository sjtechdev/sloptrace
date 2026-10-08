"""Repeated literals: individual strings, and column-list schemas."""

from __future__ import annotations

import ast
from collections import defaultdict

from sloptrace.config import Config


def repeated_literals(tree: ast.AST, config: Config) -> list[dict]:
    """The same non-trivial string literal appearing min_literal_repeats+
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
        if len(lines) >= config.min_literal_repeats
    ]


def find_literal_lists(tree: ast.AST, rel: str, config: Config) -> list[tuple[tuple[str, ...], str, int]]:
    """List/tuple literals of min_column_list_len+ string constants -- the
    shape of a hard-coded column/field schema (df[["id", "name", "email"]],
    a serializer's `fields = (...)`). Returns one (values, file, lineno) row
    per literal found; whether it's REPEATED is checked repo-wide by
    build_column_list_repeats, since the same schema copy-pasted into a
    different module is the common case.
    """
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.List, ast.Tuple)) and len(n.elts) >= config.min_column_list_len:
            if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in n.elts):
                out.append((tuple(e.value for e in n.elts), rel, n.lineno))
    return out


def build_column_list_repeats(hits: list[tuple[tuple[str, ...], str, int]], config: Config) -> list[dict]:
    """The same literal list appearing at min_column_list_repeats+ distinct
    locations anywhere in the repo -- evidence: the values, and every
    file:line it was found at."""
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for values, rel, lineno in hits:
        groups[values].append(f"{rel}:{lineno}")
    out = [
        {"values": list(values), "count": len(locs), "locations": locs}
        for values, locs in groups.items()
        if len(locs) >= config.min_column_list_repeats
    ]
    out.sort(key=lambda g: -g["count"])
    return out
