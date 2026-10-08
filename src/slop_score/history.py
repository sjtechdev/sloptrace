"""Walking git history: pick commits, materialise each, score it."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from slop_score.clones import find_divergent_clones
from slop_score.config import Config
from slop_score.snapshot import Snapshot, analyse_tree


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def pick_commits(repo: Path, every: int, max_commits: int, since: str | None) -> list[tuple[str, str, str]]:
    args = ["log", "--first-parent", "--format=%H\x1f%cs\x1f%s"]
    if since:
        args.append(f"--since={since}")
    lines = [l for l in git(repo, *args).splitlines() if l]
    rows = [tuple(l.split("\x1f", 2)) for l in lines]
    rows = rows[::every] if every > 1 else rows
    rows = rows[:max_commits]
    return list(reversed(rows))  # oldest -> newest


def checkout_to_temp(repo: Path, sha: str, dest: Path) -> None:
    """git archive is cheaper and safer than a worktree: no index, no locks."""
    p = subprocess.run(["git", "-C", str(repo), "archive", sha], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
        tf.extractall(dest)


def add_deltas(snaps: list[Snapshot]) -> None:
    for prev, cur in zip(snaps, snaps[1:]):
        cur.divergent_clones = find_divergent_clones(prev.func_fingerprints, cur.func_fingerprints)
        if prev.erosion_mass:
            cur.delta_erosion_pct = round(100 * (cur.erosion_mass - prev.erosion_mass) / prev.erosion_mass, 1)




def analyse_history(repo: Path, config: Config, every: int, max_commits: int,
                    since: str | None) -> list[Snapshot]:
    commits = pick_commits(repo, every, max_commits, since)
    snaps = []
    for i, (sha, date, subject) in enumerate(commits, 1):
        print(f"[{i}/{len(commits)}] {sha[:9]} {date} {subject[:50]}", file=sys.stderr)
        tmp = Path(tempfile.mkdtemp(prefix="slopscore-"))
        try:
            checkout_to_temp(repo, sha, tmp)
            s = analyse_tree(tmp, sha, config, root_name=repo.name)
            s.date, s.subject = date, subject
            snaps.append(s)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    add_deltas(snaps)
    return snaps
