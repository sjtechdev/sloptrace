"""Per-function complexity: cyclomatic (radon), cognitive (complexipy, optional)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from radon.complexity import cc_visit

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
    size: int          # lines used for the size term of Eq.2

    @property
    def mass(self) -> float:
        """SCBench Eq.2: complexity weighted by the square root of size."""
        return self.cc * math.sqrt(self.size)


def _cognitive_by_line(path: str) -> dict[int, int]:
    if not HAVE_COGNITIVE:
        return {}
    return {fn.line_start: fn.complexity for fn in complexipy.file_complexity(path).functions}


def function_stats(src: str, path: str) -> list[FunctionStat]:
    try:
        cog = _cognitive_by_line(path)
    except Exception:
        cog = {}
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
            cog=cog.get(block.lineno), size=max(1, endline - block.lineno + 1),
        ))
    return out
