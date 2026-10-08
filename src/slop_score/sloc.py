"""Source lines of code: lines carrying code, not blanks, comments or docstrings.

SCBench sizes functions in SLOC (Eq.2) and normalises clone lines by it, so
both the erosion mass and clone_ratio use this definition.
"""

from __future__ import annotations

import ast
import io
import tokenize
from bisect import bisect_left, bisect_right

_NON_CODE = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
             tokenize.DEDENT, tokenize.ENDMARKER}


def _docstring_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            first = n.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def code_lines(src: str, tree: ast.AST) -> tuple[int, ...]:
    """Sorted line numbers that carry code."""
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type not in _NON_CODE:
                lines.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, SyntaxError):
        # can't tokenize what ast could parse only in odd edge cases;
        # fall back to non-blank lines rather than dropping the file
        lines = {i for i, line in enumerate(src.splitlines(), 1) if line.strip()}
    return tuple(sorted(lines - _docstring_lines(tree)))


def lines_in_span(lines: tuple[int, ...], lo: int, hi: int) -> tuple[int, ...]:
    """The sorted `lines` that fall in [lo, hi]."""
    return lines[bisect_left(lines, lo):bisect_right(lines, hi)]


def count_in_span(lines: tuple[int, ...], lo: int, hi: int) -> int:
    return bisect_right(lines, hi) - bisect_left(lines, lo)
