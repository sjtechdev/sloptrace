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
