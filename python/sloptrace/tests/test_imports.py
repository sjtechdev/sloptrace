import json
import os
import subprocess
import sys

import pytest


def cycles(snap):
    return [c["modules"] for c in snap.sccs]


@pytest.mark.parametrize("a_src, b_src", [
    ("import pkg.b", "import pkg.a"),                 # plain `import`
    ("from pkg import b", "from pkg import a"),       # from-import of a submodule
    ("from . import b", "from . import a"),           # relative, submodule by name
    ("from .b import x", "from .a import y"),         # relative, name from module
    ("from pkg.b import x", "from pkg.a import y"),   # absolute, name from module
])
def test_two_module_cycle_is_found(score, a_src, b_src):
    snap = score({"pkg/__init__.py": "", "pkg/a.py": a_src, "pkg/b.py": b_src})
    assert cycles(snap) == [["pkg/a.py", "pkg/b.py"]]


def test_cycle_through_subpackage(score):
    snap = score({
        "pkg/__init__.py": "",
        "pkg/e.py": "from .sub import f",
        "pkg/sub/__init__.py": "",
        "pkg/sub/f.py": "from pkg import e",
    })
    assert cycles(snap) == [["pkg/e.py", "pkg/sub/f.py"]]


def test_relative_import_from_package_init(score):
    # in pkg/__init__.py, `from . import a` means pkg.a -- the package is
    # the module itself, not its parent
    snap = score({"pkg/__init__.py": "from . import a", "pkg/a.py": "from pkg import thing"})
    assert cycles(snap) == [["pkg/__init__.py", "pkg/a.py"]]


def test_src_layout(score):
    snap = score({
        "src/pkg/__init__.py": "",
        "src/pkg/a.py": "from pkg.b import x",
        "src/pkg/b.py": "from pkg.a import y",
    })
    assert cycles(snap) == [["src/pkg/a.py", "src/pkg/b.py"]]


def test_cycle_breaking_idioms_are_not_edges(score):
    snap = score({
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg.b import x",
        "pkg/b.py": """
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from pkg.a import y
            def later():
                from pkg.a import y
                return y
            class C:
                def method(self):
                    import pkg.a
            if __name__ == "__main__":
                from pkg.a import demo
        """,
    })
    assert snap.sccs == []
    assert snap.type_only_imports == 1
    assert snap.deferred_imports == 3


def test_import_time_code_paths_are_edges(score):
    # try/except, class bodies and plain `if` all run at import time
    snap = score({
        "pkg/__init__.py": "",
        "pkg/a.py": "try:\n    from pkg.b import x\nexcept ImportError:\n    x = None\n",
        "pkg/b.py": "class C:\n    from pkg.a import x\n",
    })
    assert cycles(snap) == [["pkg/a.py", "pkg/b.py"]]


def test_name_from_package_init_is_an_edge_to_init(score):
    snap = score({"pkg/__init__.py": "from pkg.a import f\nhelper = 1",
                  "pkg/a.py": "from pkg import helper\ndef f(): pass"})
    assert cycles(snap) == [["pkg/__init__.py", "pkg/a.py"]]


def test_ambiguous_absolute_import_is_not_guessed(score):
    # `from utils import f` in a/ doesn't refer to a/utils.py or b/utils.py:
    # both live inside packages, so `utils` isn't a top-level module at all
    snap = score({
        "a/__init__.py": "", "a/main.py": "from utils import f", "a/utils.py": "from a.main import x",
        "b/__init__.py": "", "b/utils.py": "",
    })
    assert snap.sccs == []


def test_same_root_preferred_over_other_roots(score):
    # two script directories, each with its own utils.py; each script means
    # the utils next to it
    snap = score({
        "tools/run.py": "import utils", "tools/utils.py": "import run",
        "scripts/run.py": "import utils", "scripts/utils.py": "",
    })
    assert cycles(snap) == [["tools/run.py", "tools/utils.py"]]


def test_report_identical_across_hash_seeds(make_tree, tmp_path):
    """Set iteration order changes with PYTHONHASHSEED; the output must not."""
    root = make_tree({
        "x/__init__.py": "", "x/utils.py": "from x import m", "x/m.py": "from x.utils import f",
        "y/__init__.py": "", "y/utils.py": "", "y/m.py": "from y import utils",
        "s/run.py": "from utils import f", "s/utils.py": "import run",
    })
    outputs = []
    for seed in ("1", "2", "3", "4", "5", "6"):
        out = tmp_path / f"out{seed}.json"
        env = {**os.environ, "PYTHONHASHSEED": seed}
        subprocess.run([sys.executable, "-m", "sloptrace", str(root), "-o", str(out)],
                       env=env, check=True, capture_output=True)
        outputs.append(json.loads(out.read_text()))
    assert all(o == outputs[0] for o in outputs)
    sccs = outputs[0]["snapshots"][0]["sccs"]
    assert [c["modules"] for c in sccs] == [["s/run.py", "s/utils.py"], ["x/m.py", "x/utils.py"]]
    assert sccs[0]["cycle"] == ["s/run.py", "s/utils.py", "s/run.py"]
