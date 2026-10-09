import json

import pytest

from sloptrace.cli import main
from sloptrace.config import Config
from sloptrace.crossrepo import analyse_with_references, label_repos

DUP = """
    def normalise(rows, key):
        out = []
        for r in rows:
            if r.get(key) is None:
                continue
            out.append(r[key].strip().lower())
        return sorted(set(out))
"""
OTHER = "def unrelated(x):\n    return x + 1\n"


def _repos(make_tree, **repos):
    root = make_tree({f"{name}/pkg/mod.py": src for name, src in repos.items()})
    return {name: root / name for name in repos}


def test_function_clone_between_repos_is_reported_with_repo_labels(make_tree):
    r = _repos(make_tree, C=DUP, A=DUP)
    snap = analyse_with_references(r["C"], [r["A"]], Config())
    (group,) = snap.cross_repo["function_clones"]
    assert group["repos"] == ["A", "C"]
    assert [l.rsplit(":", 1)[0] for l in group["locations"]] == ["A:pkg/mod.py", "C:pkg/mod.py"]


def test_other_repos_are_not_scored(make_tree):
    r = _repos(make_tree, C=OTHER, A=DUP)
    alone = analyse_with_references(r["C"], [], Config())
    with_a = analyse_with_references(r["C"], [r["A"]], Config())
    assert (with_a.sloc, with_a.functions, with_a.modules) == (alone.sloc, alone.functions, alone.modules)
    assert with_a.cross_repo["function_clones"] == []


def test_clones_only_inside_another_repo_are_not_reported(make_tree):
    root = make_tree({"C/pkg/m.py": OTHER, "A/pkg/a.py": DUP, "A/pkg/b.py": DUP})
    snap = analyse_with_references(root / "C", [root / "A"], Config())
    assert snap.cross_repo["function_clones"] == []


def test_same_relative_paths_in_two_repos_do_not_collide(make_tree):
    # both repos have pkg/mod.py; with unlabelled paths one would overwrite the other
    r = _repos(make_tree, C=DUP, A=DUP, B=OTHER)
    snap = analyse_with_references(r["C"], [r["A"], r["B"]], Config())
    assert len(snap.cross_repo["function_clones"][0]["locations"]) == 2


def test_labels_are_made_unique(tmp_path):
    labels = label_repos("app", [tmp_path / "x" / "app", tmp_path / "y" / "app"])
    assert [l for l, _ in labels] == ["app-2", "app-3"]


def test_cli_with_prints_section_and_writes_json(make_tree, tmp_path, capsys):
    r = _repos(make_tree, C=DUP, A=DUP)
    out = tmp_path / "o.json"
    assert main([str(r["C"]), "--with", str(r["A"]), "-o", str(out)]) == 0
    assert "SHARED WITH A" in capsys.readouterr().out
    assert json.loads(out.read_text())["snapshots"][0]["cross_repo"]["repos"] == ["A"]


def test_cli_rejects_with_plus_history(make_tree):
    r = _repos(make_tree, C=DUP, A=DUP)
    with pytest.raises(SystemExit):
        main([str(r["C"]), "--with", str(r["A"]), "--history"])


# ---- --base: classification and layering ----

LIB = "def helper(x):\n    return x\n"


def _layered(make_tree, c_extra="", a_extra="", b_extra=""):
    return make_tree({
        "A/alib/__init__.py": "", "A/alib/core.py": LIB + a_extra,
        "B/blib/__init__.py": "", "B/blib/svc.py": "from alib.core import helper\n" + b_extra,
        "C/capp/__init__.py": "", "C/capp/main.py": "from alib.core import helper\n" + c_extra,
    })


def test_import_edges_and_summary(make_tree):
    root = _layered(make_tree, b_extra="from capp.main import helper\n")
    snap = analyse_with_references(root / "C", [root / "A", root / "B"], Config(), base=root / "A")
    deps = {(d["from"], d["to"]): d["count"] for d in snap.cross_repo["dependencies"]}
    assert deps == {("C", "A"): 1, ("B", "A"): 1, ("B", "C"): 1}
    assert snap.cross_repo["violations"] == []


def test_base_importing_a_dependent_is_a_violation(make_tree):
    root = _layered(make_tree, a_extra="from capp.main import helper as h\n")
    snap = analyse_with_references(root / "C", [root / "A"], Config(), base=root / "A")
    (v,) = snap.cross_repo["violations"]
    assert (v["kind"], v["from"], v["to"]) == ("base-imports-dependent", "A", "C")
    assert "A:alib/core.py:" in v["examples"][0]


def test_two_repos_importing_each_other_is_a_cycle_unless_one_is_base(make_tree):
    root = _layered(make_tree, c_extra="from blib.svc import helper as h\n",
                    b_extra="from capp.main import helper as g\n")
    snap = analyse_with_references(root / "C", [root / "A", root / "B"], Config(), base=root / "A")
    (v,) = snap.cross_repo["violations"]
    assert (v["kind"], v["from"], v["to"]) == ("repo-cycle", "B", "C")


def test_relative_and_in_repo_imports_are_not_cross_repo(make_tree):
    root = make_tree({"C/capp/__init__.py": "", "C/capp/a.py": "from . import b\nfrom capp import b as c\n",
                      "C/capp/b.py": "", "A/capp/__init__.py": "", "A/capp/b.py": ""})
    snap = analyse_with_references(root / "C", [root / "A"], Config())
    assert snap.cross_repo["dependencies"] == []


SECOND = """
    def second(a):
        b = []
        for x in a:
            if x > 1:
                b.append(x * 2)
        return b
"""


def test_clones_are_classified_against_the_base(make_tree):
    root = make_tree({"A/p/m.py": DUP, "B/p/m.py": DUP + SECOND, "C/p/m.py": DUP + SECOND})
    snap = analyse_with_references(root / "C", [root / "A", root / "B"], Config(), base=root / "A")
    kinds = {tuple(g["repos"]): g["kind"] for g in snap.cross_repo["function_clones"]}
    assert kinds == {("A", "B", "C"): "in_base", ("B", "C"): "lift_candidate"}


def test_cli_base_implies_with_and_prints_layering(make_tree, tmp_path, capsys):
    root = _layered(make_tree, a_extra="from capp.main import helper as h\n")
    assert main([str(root / "C"), "--base", str(root / "A")]) == 0
    out = capsys.readouterr().out
    assert "LAYERING VIOLATIONS" in out and "base A imports from C" in out
