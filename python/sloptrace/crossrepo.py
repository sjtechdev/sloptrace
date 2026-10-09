"""Code shared between the scanned repo and other repos passed with --with.

The other repos are indexed (their files go through the same per-file
analysis) but never scored: they don't contribute to the scanned repo's
SLOC, erosion, clone ratio or import cycles. They only act as a pool to
match the scanned repo's code against, so a clone of something that
already lives in repo A, or a block that B and C both carry, shows up.
"""

from __future__ import annotations

from pathlib import Path

from sloptrace import clones, literals
from sloptrace.config import Config
from sloptrace.snapshot import FileFacts, Snapshot, aggregate, collect_facts


def label_repos(own: str, paths: list[Path]) -> list[tuple[str, Path]]:
    """One distinct label per repo, taken from its directory name."""
    taken = {own}
    out = []
    for p in paths:
        label, n = p.name, 2
        while label in taken:
            label, n = f"{p.name}-{n}", n + 1
        taken.add(label)
        out.append((label, p))
    return out


class _Repos:
    """Tells which repo a file path (or "path:line" location) belongs to."""

    def __init__(self, own: str, labels: set[str]):
        self.own, self.labels = own, labels

    def of(self, rel: str) -> str:
        head, sep, _ = rel.partition(":")
        return head if sep and head in self.labels else self.own

    def show(self, rel: str, tail: str) -> str:
        """repo:path:tail, with the scanned repo's own files labelled too."""
        return f"{rel}:{tail}" if self.of(rel) != self.own else f"{self.own}:{rel}:{tail}"

    def crosses(self, rels) -> bool:
        repos = {self.of(r) for r in rels}
        return self.own in repos and len(repos) > 1


def find_shared(own: str, own_facts: list[FileFacts], ref_facts: list[FileFacts],
                labels: set[str], config: Config) -> dict:
    """Groups of identical code with at least one copy in the scanned repo
    and at least one in another repo."""
    repos = _Repos(own, labels)
    facts = own_facts + ref_facts

    fingerprints: dict[str, tuple] = {}
    windows: dict[str, list] = {}
    for f in facts:
        fingerprints.update(f.fingerprints)
        for h, occ in f.windows.items():
            windows.setdefault(h, []).extend(occ)

    function_groups = clones.function_clone_spans(fingerprints)
    function_shared = [g for g in function_groups if repos.crosses(s[0] for s in g)]
    block_shared = [g for g in clones.block_clone_groups(windows)
                    if repos.crosses(s[0] for s in g)
                    and not clones.subsumed_by_function_clone(g, function_groups)]

    def evidence(groups):
        out = [{"repos": sorted({repos.of(r) for r, _, _ in g}),
                "locations": [repos.show(r, f"{lo}-{hi}") for r, lo, hi in sorted(g)]}
               for g in groups]
        out.sort(key=lambda g: (-len(g["locations"]), g["locations"]))
        return out

    column_lists = []
    for g in literals.build_column_list_repeats(
            [h for f in facts for h in f.literal_lists], config):
        if repos.crosses(g["locations"]):
            column_lists.append({
                "values": g["values"], "count": g["count"],
                "repos": sorted({repos.of(loc) for loc in g["locations"]}),
                "locations": [repos.show(*loc.rsplit(":", 1)) for loc in g["locations"]],
            })

    return {"repos": sorted(labels), "function_clones": evidence(function_shared),
            "block_clones": evidence(block_shared), "column_lists": column_lists}


def analyse_with_references(root: Path, references: list[Path], config: Config,
                            ref: str = "worktree") -> Snapshot:
    """Score `root` as usual, then match it against the other repos."""
    own = root.name
    own_facts, parse_errors = collect_facts(root, config)
    snap = aggregate(own_facts, ref, config, parse_errors, own)

    ref_facts: list[FileFacts] = []
    labelled = label_repos(own, references)
    for label, path in labelled:
        ref_facts += collect_facts(path, config, label)[0]
    snap.cross_repo = find_shared(own, own_facts, ref_facts, {l for l, _ in labelled}, config)
    return snap
