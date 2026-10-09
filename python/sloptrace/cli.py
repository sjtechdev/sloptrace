"""Command-line entry point."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from sloptrace.config import DEFAULT_EXCLUDES, Config
from sloptrace.crossrepo import analyse_with_references
from sloptrace.history import analyse_history
from sloptrace.report import print_report
from sloptrace.snapshot import Snapshot, analyse_tree

JSON_SCHEMA_VERSION = 2


def snapshot_to_json(s: Snapshot) -> dict:
    """Public fields only: internal bookkeeping (fingerprint tables) is
    marked with metadata={"internal": True} and left out of the output."""
    return {
        f.name: dataclasses.asdict(s)[f.name]
        for f in dataclasses.fields(s)
        if not f.metadata.get("internal")
    }


def build_parser() -> argparse.ArgumentParser:
    defaults = Config()
    ap = argparse.ArgumentParser(prog="sloptrace",
                                 description="Deterministic complexity-erosion metrics for Python repos.")
    ap.add_argument("repo", type=Path)
    ap.add_argument("--with", dest="with_repos", action="append", type=Path, default=[], metavar="REPO",
                    help="another repo to match this one against (repeatable). It is indexed, not scored: "
                         "code shared with it is reported, its own metrics are not")
    ap.add_argument("--base", type=Path, default=None, metavar="REPO",
                    help="the shared lowest-layer repo (implies --with). Shared code is then split into "
                         "already-in-base vs lift candidates, and base importing other repos is flagged")
    ap.add_argument("--similarity", type=float, default=defaults.near_dup_threshold, metavar="T",
                    help="with --with/--base: also report functions at least this similar (0-1, by shared "
                         f"statements) to one in another repo, not only exact copies (default {defaults.near_dup_threshold}; "
                         "0 turns it off)")
    ap.add_argument("--history", action="store_true", help="walk git history instead of scoring the working tree")
    ap.add_argument("--every", type=int, default=1, help="sample every Nth commit (default 1)")
    ap.add_argument("--max-commits", type=int, default=10)
    ap.add_argument("--since", default=None, help="e.g. 2024-01-01")
    ap.add_argument("--exclude", nargs="*", default=None,
                    help="path segments to skip (replaces the defaults: %s)" % " ".join(DEFAULT_EXCLUDES))
    ap.add_argument("--clone-window", type=int, default=defaults.clone_window,
                    help=f"min consecutive statements for a block clone (default {defaults.clone_window}; "
                         "dominant lever on the clone count)")
    ap.add_argument("--show-literals", action="store_true",
                    help="print the REPEATED LITERALS section (hidden by default -- noisy; "
                         "always in the -o JSON regardless)")
    ap.add_argument("-o", "--output", type=Path, default=None, help="write JSON here")
    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    base = args.base.resolve() if args.base else None
    references = list(dict.fromkeys([p.resolve() for p in args.with_repos] + ([base] if base else [])))
    if references and args.history:
        parser.error("--with/--base can't be combined with --history yet")
    if not 0 <= args.similarity <= 1:
        parser.error("--similarity must be between 0 and 1")
    for p in references:
        if not p.is_dir():
            parser.error(f"--with: {p} is not a directory")
    config = Config(
        excludes=tuple(args.exclude) if args.exclude is not None else DEFAULT_EXCLUDES,
        clone_window=args.clone_window,
        near_dup_threshold=args.similarity,
    )
    repo = args.repo.resolve()

    if args.history:
        snaps = analyse_history(repo, config, args.every, args.max_commits, args.since)
        if not snaps:
            print("no commits matched", file=sys.stderr)
            return 1
    elif references:
        snaps = [analyse_with_references(repo, references, config, base)]
    else:
        snaps = [analyse_tree(repo, "worktree", config)]

    print_report(snaps, config, show_literals=args.show_literals)
    if args.output:
        payload = {"schema_version": JSON_SCHEMA_VERSION,
                   "snapshots": [snapshot_to_json(s) for s in snaps]}
        args.output.write_text(json.dumps(payload, indent=2, default=str))
        print(f"wrote {args.output}", file=sys.stderr)
    return 0
