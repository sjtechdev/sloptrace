"""Human-readable text report."""

from __future__ import annotations

import sys
from typing import TextIO

from sloptrace.complexity import HAVE_COGNITIVE
from sloptrace.config import Config
from sloptrace.snapshot import Snapshot


def _module_label(rel: str) -> str:
    """src/pkg/sub/__init__.py -> sub,  src/pkg/mod.py -> mod"""
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return parts[-1]


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else "…" + s[-(n - 1):]


_KIND_TITLES = {
    "in_base": "ALREADY IN BASE {base}   reuse it instead of keeping a copy",
    "lift_candidate": "LIFT INTO {base}?   shared with another repo, absent from the base",
    "shared": "SHARED WITH {repos}",
}


def _print_cross_repo(x: dict, config: Config, w) -> None:
    base = x["base"]
    groups = [("whole functions", g) for g in x["function_clones"]] + \
             [("statement blocks", g) for g in x["block_clones"]] + \
             [("column list", g) for g in x["column_lists"]] + \
             [("near-duplicate", g) for g in x["near_duplicates"]]
    if not groups:
        w(f"\nSHARED WITH {', '.join(x['repos'])}  nothing found\n")
    for kind in ("in_base", "lift_candidate", "shared"):
        mine = [(label, g) for label, g in groups if g["kind"] == kind]
        if not mine:
            continue
        w("\n" + _KIND_TITLES[kind].format(base=base, repos=", ".join(x["repos"])) + "\n")
        for label in dict.fromkeys(label for label, _ in mine):
            of_type = [g for l, g in mine if l == label]
            for g in of_type[:config.evidence_top_n]:
                extra = len(g["locations"]) - 4
                shown = f"[{', '.join(repr(v) for v in g['values'][:5])}]  " if "values" in g else ""
                lead = f"{g['similarity']:>3.0%}" if "similarity" in g else f"{len(g['locations']):>2}x"
                w(f"  {lead}  {shown}" + "  ".join(g["locations"][:4])
                  + (f"  +{extra} more" if extra > 0 else "") + f"   ({label})\n")
            if len(of_type) > config.evidence_top_n:
                w(f"  ... {len(of_type) - config.evidence_top_n} more {label} in JSON output (-o)\n")

    if x["dependencies"]:
        w("\nREPO DEPENDENCIES\n")
        for d in x["dependencies"]:
            w(f"  {d['from']} -> {d['to']}   {d['count']} import(s), e.g. {d['examples'][0]}\n")
    if x["violations"]:
        w("\nLAYERING VIOLATIONS\n")
        for v in x["violations"]:
            what = (f"base {v['from']} imports from {v['to']}" if v["kind"] == "base-imports-dependent"
                    else f"{v['from']} and {v['to']} import each other")
            w(f"  {what}\n")
            for e in v["examples"]:
                w(f"      {e}\n")


def print_report(snaps: list[Snapshot], config: Config, show_literals: bool = False,
                 out: TextIO | None = None) -> None:
    w = (out or sys.stdout).write
    last = snaps[-1]

    # ---- erosion: the headline, plus what's behind it ----
    w(f"\nEROSION    {last.erosion_mass:.3f}   complexity mass sitting in CC>{config.cc_high} functions\n")
    cog_note = f", {last.cog_gt_15} cognitive>{config.cog_high}" if HAVE_COGNITIVE else ""
    w(f"           {last.cc_gt_10} of {last.functions} functions CC>{config.cc_high} "
      f"({last.cc_gt_30} of those CC>{config.cc_severe}){cog_note}\n")

    if last.erosion_offenders:
        w("\nFUNCTIONS DRIVING EROSION\n")
        hdr = f"  {'function':<30} {'file:line':<30} {'CC':>4} {'COG':>4} {'mass%':>6}\n"
        w(hdr)
        w("  " + "-" * (len(hdr) - 4) + "\n")
        for o in last.erosion_offenders[:config.erosion_top_n]:
            loc = f"{o['file']}:{o['lineno']}"
            cog = "-" if o["cog"] is None else o["cog"]
            w(f"  {_clip(o['qualname'], 30):<30} {_clip(loc, 30):<30} {o['cc']:>4} {cog:>4} {o['mass_pct']:>6.1f}\n")
        rest = len(last.erosion_offenders) - config.erosion_top_n
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
        w(f"\nCLONES     {last.clone_ratio:.3f}   duplicated lines / SLOC "
          f"({last.clone_line_count} of {max(last.sloc, 1)} lines)\n")
        if last.function_clones:
            w(f"  whole-function duplicates, {len(last.function_clones)} group(s):\n")
            for g in last.function_clones[:config.evidence_top_n]:
                extra = len(g["locations"]) - 4
                w(f"    {len(g['members']):>2}x  " + "  ".join(g["locations"][:4])
                  + (f"  +{extra} more" if extra > 0 else "") + "\n")
        if last.block_clones:
            w(f"  duplicated statement blocks, {len(last.block_clones)} group(s):\n")
            for g in last.block_clones[:config.evidence_top_n]:
                extra = len(g["locations"]) - 4
                w(f"    {g['occurrences']:>2}x  " + "  ".join(g["locations"][:4])
                  + (f"  +{extra} more" if extra > 0 else "") + "\n")
    div = [d for s_ in snaps for d in s_.divergent_clones]
    if div:
        w(f"  divergent ({len(div)}): identical last snapshot, not any more -- "
          + "; ".join(" / ".join(m.split("::")[-1] for m in d["members"][:3]) for d in div[:4]) + "\n")

    # ---- other repos (--with / --base) ----
    if last.cross_repo:
        _print_cross_repo(last.cross_repo, config, w)

    # ---- repeated literals: noisy on most codebases, so JSON-only unless
    # --show-literals is passed. Repeated COLUMN LISTS (below) is the
    # sharper signal and always prints. ----
    if show_literals and last.literal_repeats:
        w(f"\nREPEATED LITERALS  {len(last.literal_repeats)} literal(s) repeated {config.min_literal_repeats}+ times in one file\n")
        for h in last.literal_repeats[:config.evidence_top_n]:
            lines = ", ".join(str(l) for l in h["lines"][:6])
            extra = len(h["lines"]) - 6
            w(f"  {h['count']:>3}x  {h['value']!r:<28} {h['file']}:{lines}"
              + (f" +{extra} more" if extra > 0 else "") + "\n")
    elif last.literal_repeats:
        w(f"\n  {len(last.literal_repeats)} repeated literal(s) found, hidden by default "
          f"(noisy) -- see -o JSON or pass --show-literals\n")
    if last.column_list_repeats:
        w(f"\nREPEATED COLUMN LISTS  {len(last.column_list_repeats)} list(s) of "
          f"{config.min_column_list_len}+ literals reused {config.min_column_list_repeats}+ times\n")
        for g in last.column_list_repeats[:config.evidence_top_n]:
            vextra = len(g["values"]) - 6
            vals = ", ".join(repr(v) for v in g["values"][:6]) + (", …" if vextra > 0 else "")
            lextra = len(g["locations"]) - 4
            locs = "  ".join(g["locations"][:4])
            w(f"  {g['count']:>3}x  [{vals}]  {locs}" + (f"  +{lextra} more" if lextra > 0 else "") + "\n")

    # ---- import cycles ----
    if last.sccs:
        pct = 100 * last.cyclic_modules / max(last.modules, 1)
        w(f"\nIMPORT CYCLES  {last.cyclic_modules}/{last.modules} modules ({pct:.0f}%) tangled\n")
        for c in sorted(last.sccs, key=lambda c: -len(c["modules"]))[:config.evidence_top_n]:
            more = len(c["modules"]) - (len(c["cycle"]) - 1)
            w("  " + " -> ".join(_module_label(x) for x in c["cycle"])
              + (f"   (+{more} more module(s) in this tangle)" if more > 0 else "") + "\n")
    not_run = []
    if last.type_only_imports:
        not_run.append(f"{last.type_only_imports} TYPE_CHECKING-guarded")
    if last.deferred_imports:
        not_run.append(f"{last.deferred_imports} function-local or __main__-guarded")
    if last.sccs and not_run:
        w(f"  (not counted: {' and '.join(not_run)} import(s), which don't run at import time)\n")

    if last.parse_errors:
        w(f"\n  {last.parse_errors} file(s) failed to parse, skipped.\n")
    if last.warnings:
        w(f"\n  {len(last.warnings)} file(s) only partly analysed -- see 'warnings' in the -o JSON.\n")
    w("\n")
