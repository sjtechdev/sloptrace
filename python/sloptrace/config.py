"""Tunable thresholds, gathered in one immutable object.

Every analysis function takes a Config rather than reading module globals,
so a run's settings are explicit and two runs in one process can't leak
settings into each other.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_EXCLUDES = (
    "test", "tests", "testing", "conftest.py", "migrations", "vendor",
    "vendored", "third_party", "_vendor", "node_modules", "build", "dist",
    ".venv", "venv", "site-packages", "docs", "examples", "setup.py",
)


@dataclass(frozen=True)
class Config:
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES

    # -- erosion --
    cc_high: int = 10        # radon's "moderate risk" cutoff; SCBench Eq.3
    cc_severe: int = 30      # reported separately as the especially-bad subset
    cog_high: int = 15       # SonarCloud's default cognitive-complexity threshold
    # Cognitive complexity correlates ~0.93 with cyclomatic, so it is mostly
    # redundant -- it earns its place on the residual: deeply nested code that
    # CC scores as simple, and flat dispatch that CC punishes. Shown alongside
    # CC on every offender, not folded into the erosion number itself.

    # -- clones --
    min_clone_statements: int = 3   # whole-function clones: below this, "duplicates" are idioms
    clone_window: int = 3
    # DOMINANT LEVER on the block-clone count. Window 2 matches any
    # two-statement idiom and is meaningless; 3 is a defensible minimum clone
    # size. Calibrated, not derived -- re-check on your own repo.

    # -- near-duplicate functions (cross-repo, --with/--base) --
    near_dup_threshold: float = 0.7      # Jaccard similarity of statement sets; 0 turns it off
    near_dup_min_statements: int = 5     # distinct statement shapes a function needs to be compared
    collect_shapes: bool = False         # set by crossrepo when needed; costs a little per function

    # -- repeated literals --
    min_literal_repeats: int = 4      # same string this many times in one file
    min_column_list_len: int = 3      # shorter lists (flag pairs, ("GET","POST")) are rarely schemas
    min_column_list_repeats: int = 2  # same list reused this many times, anywhere

    # -- reporting --
    erosion_top_n: int = 15
    evidence_top_n: int = 8
