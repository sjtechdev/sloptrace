from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sloptrace.config import Config
from sloptrace.snapshot import analyse_tree


@pytest.fixture
def make_tree(tmp_path):
    """Write {relative path: source} into a temp dir and return its root."""
    def _make(files: dict[str, str]) -> Path:
        for rel, src in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(src))
        return tmp_path
    return _make


@pytest.fixture
def score(make_tree):
    def _score(files: dict[str, str], **config):
        return analyse_tree(make_tree(files), "worktree", Config(**config))
    return _score
