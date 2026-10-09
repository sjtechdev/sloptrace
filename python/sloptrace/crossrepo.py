"""Relating the scanned repo to other repos passed with --with / --base.

The other repos are indexed (their files go through the same per-file
analysis) but never scored: they don't contribute to the scanned repo's
SLOC, erosion, clone ratio or import cycles. They are a pool to match the
scanned repo's code against, and a set of targets for its imports.

With a base repo (the shared lowest layer, "A"), a match is classified:
already in the base (reuse it), or shared with a sibling but absent from
the base (a candidate to lift into it). Imports that run the wrong way, the
base importing from a repo that depends on it, are reported as violations.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sloptrace import clones, literals
from sloptrace.config import Config
from sloptrace.discover import ModuleIndex
from sloptrace.imports import resolve_import
from sloptrace.snapshot import FileFacts, Snapshot, aggregate, collect_facts


@dataclass
class Repo:
    label: str            # unique name used in reports
    root_name: str        # the directory's real name (how it would be imported if a package)
    facts: list[FileFacts]


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


def _kind(repos: set[str], base: str | None) -> str:
    if base is None:
        return "shared"
    return "in_base" if base in repos else "lift_candidate"


def find_shared(own: str, repos_: list[Repo], base: str | None, config: Config) -> dict:
    """Groups of identical code with at least one copy in the scanned repo
    and at least one in another repo."""
    repos = _Repos(own, {r.label for r in repos_ if r.label != own})
    facts = [f for r in repos_ for f in r.facts]

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
        out = []
        for g in groups:
            present = {repos.of(r) for r, _, _ in g}
            out.append({"kind": _kind(present, base), "repos": sorted(present),
                        "locations": [repos.show(r, f"{lo}-{hi}") for r, lo, hi in sorted(g)]})
        out.sort(key=lambda g: (-len(g["locations"]), g["locations"]))
        return out

    column_lists = []
    for g in literals.build_column_list_repeats(
            [h for f in facts for h in f.literal_lists], config):
        if repos.crosses(g["locations"]):
            present = {repos.of(loc) for loc in g["locations"]}
            column_lists.append({
                "kind": _kind(present, base), "values": g["values"], "count": g["count"],
                "repos": sorted(present),
                "locations": [repos.show(*loc.rsplit(":", 1)) for loc in g["locations"]],
            })
    return {"function_clones": evidence(function_shared),
            "block_clones": evidence(block_shared), "column_lists": column_lists}


def import_edges(repos_: list[Repo]) -> list[dict]:
    """Imports whose target lives in a different repo.

    An absolute import is resolved in its own repo first; only if nothing
    there matches is it tried against the other repos, and it counts only
    when exactly one of them defines the name (ambiguity is not guessed
    at). Relative imports never leave their repo.
    """
    def path(r: Repo, f: FileFacts) -> str:
        return f.rel.removeprefix(f"{r.label}:") if f.rel.startswith(f"{r.label}:") else f.rel

    indexes = {r.label: ModuleIndex([path(r, f) for f in r.facts], r.root_name) for r in repos_}
    edges = []
    for r in repos_:
        own_index = indexes[r.label]
        for f in r.facts:
            importer = own_index.by_rel[path(r, f)]
            for ref in f.imports.runtime:
                if ref.level or resolve_import(importer, ref, own_index):
                    continue
                hits = {o.label: t for o in repos_ if o.label != r.label
                        if (t := resolve_import(importer, ref, indexes[o.label]))}
                if len(hits) == 1:
                    (to, targets), = hits.items()
                    edges.append({"from": r.label, "to": to, "file": f"{r.label}:{path(r, f)}",
                                  "line": ref.lineno, "imports": targets[0].name})
    return edges


def summarise_dependencies(edges: list[dict], base: str | None) -> tuple[list[dict], list[dict]]:
    """Per-direction import counts, and the layering problems among them."""
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in edges:
        by_pair[(e["from"], e["to"])].append(e)

    def example(e):
        return f"{e['file']}:{e['line']} -> {e['imports']}"

    dependencies = [{"from": a, "to": b, "count": len(es), "examples": [example(e) for e in es[:3]]}
                    for (a, b), es in sorted(by_pair.items())]

    violations = []
    for (a, b), es in sorted(by_pair.items()):
        if a == base:
            violations.append({"kind": "base-imports-dependent", "from": a, "to": b,
                               "examples": [example(e) for e in es[:3]]})
        elif a < b and (b, a) in by_pair and base not in (a, b):
            back = by_pair[(b, a)]
            violations.append({"kind": "repo-cycle", "from": a, "to": b,
                               "examples": [example(e) for e in es[:2] + back[:2]]})
    return dependencies, violations


def analyse_with_references(root: Path, references: list[Path], config: Config,
                            base: Path | None = None, ref: str = "worktree") -> Snapshot:
    """Score `root` as usual, then relate it to the other repos. `base`, if
    given, must be one of `references`."""
    own = root.name
    own_facts, parse_errors = collect_facts(root, config)
    snap = aggregate(own_facts, ref, config, parse_errors, own)

    repos_ = [Repo(own, own, own_facts)]
    base_label = None
    for label, path in label_repos(own, references):
        repos_.append(Repo(label, path.name, collect_facts(path, config, label)[0]))
        if base is not None and path == base:
            base_label = label

    shared = find_shared(own, repos_, base_label, config)
    dependencies, violations = summarise_dependencies(import_edges(repos_), base_label)
    snap.cross_repo = {"repos": [r.label for r in repos_[1:]], "base": base_label, **shared,
                       "dependencies": dependencies, "violations": violations}
    return snap
