"""Scoring one tree: per-file facts, then repo-wide aggregation.

analyse_file is a pure function of (path, source, config); everything that
needs more than one file at a time (clone grouping, import cycles, erosion
totals, repeated column lists) happens in aggregate().
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from slop_score import clones, imports, literals, sloc
from slop_score.complexity import FunctionStat, function_stats
from slop_score.config import Config
from slop_score.discover import ModuleIndex, list_python_files


@dataclass
class FileFacts:
    rel: str
    code_lines: tuple[int, ...] = ()   # sorted; len() is the file's SLOC
    functions: list[FunctionStat] = field(default_factory=list)
    fingerprints: dict = field(default_factory=dict)
    windows: dict = field(default_factory=dict)
    literal_repeats: list = field(default_factory=list)
    literal_lists: list = field(default_factory=list)
    imports: imports.CollectedImports = field(
        default_factory=lambda: imports.CollectedImports([], 0, 0))


def analyse_file(rel: str, src: str, path: str, config: Config) -> FileFacts | None:
    """None if the file doesn't parse."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    facts = FileFacts(rel=rel)
    facts.code_lines = sloc.code_lines(src, tree)
    try:
        facts.functions = function_stats(src, path, facts.code_lines)
    except Exception:
        pass
    facts.imports = imports.collect_imports(tree)
    facts.fingerprints = clones.collect_fingerprints(tree, rel, config)
    facts.windows = clones.block_clone_windows(tree, rel, config.clone_window)
    facts.literal_repeats = [{**hit, "file": rel} for hit in literals.repeated_literals(tree, config)]
    facts.literal_lists = literals.find_literal_lists(tree, rel, config)
    return facts


@dataclass
class Snapshot:
    ref: str
    date: str = ""
    subject: str = ""   # row label for humans reading the JSON, not a metric
    sloc: int = 0
    functions: int = 0
    parse_errors: int = 0

    # -- erosion --
    cc_gt_10: int = 0
    cc_gt_30: int = 0
    cog_gt_15: int = 0
    erosion_mass: float = 0.0                    # Eq.3: share of complexity mass in CC>10 funcs
    erosion_offenders: list = field(default_factory=list)   # ranked functions behind it

    # -- clones --
    clone_ratio: float = 0.0
    clone_line_count: int = 0
    function_clones: list = field(default_factory=list)     # whole-body dup groups, w/ evidence
    block_clones: list = field(default_factory=list)        # dup statement-window groups, w/ evidence
    func_fingerprints: dict = field(default_factory=dict, metadata={"internal": True})
    divergent_clones: list = field(default_factory=list)    # drift since prev snapshot (history mode)

    # -- repeated literals --
    literal_repeats: list = field(default_factory=list)
    column_list_repeats: list = field(default_factory=list)

    # -- import cycles --
    modules: int = 0
    cyclic_modules: int = 0
    type_only_imports: int = 0   # under `if TYPE_CHECKING:`, never run
    deferred_imports: int = 0    # inside function bodies, run on call
    sccs: list = field(default_factory=list)      # [{modules: [paths], cycle: [a, b, ..., a]}]

    # -- deltas (history mode only) --
    delta_erosion_pct: float | None = None


def _aggregate_erosion(snap: Snapshot, facts: list[FileFacts], config: Config) -> None:
    total_mass = high_mass = 0.0
    offenders = []
    for f in facts:
        for fn in f.functions:
            snap.functions += 1
            total_mass += fn.mass
            if fn.cc > config.cc_severe:
                snap.cc_gt_30 += 1
            if fn.cog is not None and fn.cog > config.cog_high:
                snap.cog_gt_15 += 1
            if fn.cc > config.cc_high:
                snap.cc_gt_10 += 1
                high_mass += fn.mass
                offenders.append({"qualname": fn.qualname, "file": f.rel, "lineno": fn.lineno,
                                  "cc": fn.cc, "cog": fn.cog, "mass": fn.mass})
    snap.erosion_mass = round(high_mass / total_mass, 4) if total_mass else 0.0
    offenders.sort(key=lambda o: -o["mass"])
    for o in offenders:
        o["mass_pct"] = round(100 * o.pop("mass") / total_mass, 2) if total_mass else 0.0
    snap.erosion_offenders = offenders


def _aggregate_clones(snap: Snapshot, facts: list[FileFacts]) -> None:
    windows: dict[str, list] = defaultdict(list)
    for f in facts:
        snap.func_fingerprints.update(f.fingerprints)
        for h, occ in f.windows.items():
            windows[h].extend(occ)
    snap.function_clones = clones.build_function_clone_evidence(snap.func_fingerprints)
    snap.block_clones, clone_lines = clones.build_block_clone_evidence(windows)
    snap.clone_line_count = len(clone_lines)
    snap.clone_ratio = round(min(1.0, len(clone_lines) / max(snap.sloc, 1)), 4)


def _aggregate_imports(snap: Snapshot, facts: list[FileFacts], root_name: str) -> None:
    index = ModuleIndex([f.rel for f in facts], root_name)
    edges = imports.import_graph(index, {f.rel: f.imports.runtime for f in facts})
    snap.modules = len(index.by_rel)
    snap.type_only_imports = sum(f.imports.type_only for f in facts)
    snap.deferred_imports = sum(f.imports.deferred for f in facts)
    snap.sccs = [{"modules": c, "cycle": imports.shortest_cycle(edges, c)}
                 for c in imports.strongly_connected(edges)]
    snap.cyclic_modules = sum(len(c["modules"]) for c in snap.sccs)


def aggregate(facts: list[FileFacts], ref: str, config: Config, parse_errors: int = 0,
              root_name: str = "") -> Snapshot:
    snap = Snapshot(ref=ref, parse_errors=parse_errors)
    snap.sloc = sum(len(f.code_lines) for f in facts)
    _aggregate_erosion(snap, facts, config)
    _aggregate_clones(snap, facts)
    _aggregate_imports(snap, facts, root_name)
    repeats = [hit for f in facts for hit in f.literal_repeats]
    repeats.sort(key=lambda h: -h["count"])
    snap.literal_repeats = repeats
    snap.column_list_repeats = literals.build_column_list_repeats(
        [hit for f in facts for hit in f.literal_lists], config)
    return snap


def analyse_tree(root: Path, ref: str, config: Config, root_name: str | None = None) -> Snapshot:
    """Score the .py files under `root`. `root_name` is the name the root
    directory would be imported as, if it is itself a package (defaults to
    its directory name; history mode passes the repo's real name, since it
    scores a temp-dir copy)."""
    facts, parse_errors = [], 0
    for rel in list_python_files(root, config.excludes):
        p = root / rel
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            parse_errors += 1
            continue
        f = analyse_file(rel, src, str(p), config)
        if f is None:
            parse_errors += 1
        else:
            facts.append(f)
    return aggregate(facts, ref, config, parse_errors, root_name or root.name)
