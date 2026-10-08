"""Which files make up the codebase being scored."""

from __future__ import annotations

from pathlib import Path


def is_excluded(rel: Path, excludes: tuple[str, ...]) -> bool:
    parts = {s.lower() for s in rel.parts}
    name = rel.parts[-1].lower()
    if parts & set(excludes) or name in excludes:
        return True
    return name.startswith("test_") or name.endswith("_test.py")


def iter_python_files(root: Path, excludes: tuple[str, ...]) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if not is_excluded(p.relative_to(root), excludes))
