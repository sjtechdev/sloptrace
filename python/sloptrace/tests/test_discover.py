import shutil
import subprocess

import pytest

from sloptrace.config import DEFAULT_EXCLUDES
from sloptrace.discover import ModuleIndex, list_python_files


def test_excludes_tests_hidden_dirs_and_default_segments(make_tree):
    root = make_tree({
        "pkg/__init__.py": "", "pkg/a.py": "",
        "pkg/test_a.py": "", "pkg/a_test.py": "", "tests/t.py": "",
        ".tox/py312/lib/x.py": "", "venv/lib/y.py": "", "docs/conf.py": "",
    })
    assert list_python_files(root, DEFAULT_EXCLUDES) == ["pkg/__init__.py", "pkg/a.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_respects_gitignore_and_includes_untracked(make_tree):
    root = make_tree({
        ".gitignore": "generated/\n",
        "pkg/__init__.py": "", "pkg/tracked.py": "", "pkg/untracked.py": "",
        "generated/out.py": "",
    })
    git = ["git", "-C", str(root)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", ".gitignore", "pkg/__init__.py", "pkg/tracked.py"], check=True)
    assert list_python_files(root, ()) == ["pkg/__init__.py", "pkg/tracked.py", "pkg/untracked.py"]


def test_module_names():
    ix = ModuleIndex([
        "src/pkg/__init__.py", "src/pkg/a.py",
        "src/pkg/ns/b.py",          # implicit namespace subpackage inside pkg
        "scripts/run.py",           # loose script dir is its own import root
    ])
    names = {rel: (m.name, m.import_root) for rel, m in ix.by_rel.items()}
    assert names == {
        "src/pkg/__init__.py": ("pkg", "src"),
        "src/pkg/a.py": ("pkg.a", "src"),
        "src/pkg/ns/b.py": ("pkg.ns.b", "src"),
        "scripts/run.py": ("run", "scripts"),
    }


def test_scanned_root_that_is_itself_a_package():
    ix = ModuleIndex(["__init__.py", "models.py"], root_name="requests")
    assert ix.by_rel["models.py"].name == "requests.models"
    assert ix.by_rel["__init__.py"].name == "requests"
