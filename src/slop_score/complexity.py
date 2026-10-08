"""Per-function complexity: cyclomatic (radon), cognitive (complexipy, optional)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from radon.complexity import cc_visit

from slop_score.sloc import count_in_span

try:
    import complexipy   # cognitive complexity (Campbell / SonarSource algorithm)
    HAVE_COGNITIVE = True
except ImportError:
    HAVE_COGNITIVE = False


@dataclass(frozen=True)
class FunctionStat:
    qualname: str
    lineno: int
    cc: int
    cog: int | None
    sloc: int          # size term of Eq.2

    @property
    def mass(self) -> float:
        """SCBench Eq.2: CC weighted by the square root of SLOC."""
        return self.cc * math.sqrt(self.sloc)


def no_cognitive(name: str, lineno: int) -> int | None:
    return None


def cognitive_lookup(src: str):
    """complexipy and radon disagree on where a function starts: complexipy
    reports a decorated function from its first decorator, radon from the
    `def` line. Matching on the start line therefore silently dropped the
    score of every decorated function. Match instead on (name, the radon
    `def` line falls inside complexipy's span), taking the innermost span.
    Functions complexipy doesn't score (conditional defs, __main__ demos)
    come back None."""
    if not HAVE_COGNITIVE:
        return no_cognitive
    spans: dict[str, list[tuple[int, int, int]]] = {}
    for fn in complexipy.code_complexity(src).functions:
        spans.setdefault(fn.name.split("::")[-1], []).append((fn.line_start, fn.line_end, fn.complexity))

    def lookup(name: str, lineno: int) -> int | None:
        hits = [(start, cog) for start, end, cog in spans.get(name, ()) if start <= lineno <= end]
        return max(hits)[1] if hits else None
    return lookup


def function_stats(src: str, code_lines: tuple[int, ...], cognitive=no_cognitive) -> list[FunctionStat]:
    out = []
    # Function blocks only: cc_visit also yields Class blocks whose complexity
    # is the aggregate of their methods, which would double count against a
    # mass-weighted total.
    for block in cc_visit(src):
        if type(block).__name__ != "Function":
            continue
        endline = block.endline or block.lineno
        qualname = f"{block.classname}.{block.name}" if block.classname else block.name
        out.append(FunctionStat(
            qualname=qualname, lineno=block.lineno, cc=block.complexity,
            cog=cognitive(block.name, block.lineno),
            sloc=max(1, count_in_span(code_lines, block.lineno, endline)),
        ))
    return out
