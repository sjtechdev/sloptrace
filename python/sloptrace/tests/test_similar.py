from sloptrace.config import Config
from sloptrace.crossrepo import analyse_with_references
from sloptrace.cli import main

ORIG = """
    def clean(rows, key):
        out = []
        for r in rows:
            if r.get(key) is None:
                continue
            value = r[key].strip().lower()
            out.append(value)
        total = len(out)
        return sorted(set(out)), total
"""
# same function with an extra statement and an extra branch, and renamed identifiers
EDITED = """
    def tidy(items, field):
        res = []
        for it in items:
            if it.get(field) is None:
                continue
            v = it[field].strip().lower()
            res.append(v)
        print("done")
        count = len(res)
        return sorted(set(res)), count
"""
UNRELATED = """
    def parse(text):
        parts = text.split(",")
        head, *rest = parts
        mapping = {}
        for p in rest:
            k, v = p.split("=")
            mapping[k] = int(v)
        return head, mapping
"""


def _near(make_tree, c, a, **config):
    root = make_tree({"C/p/m.py": c, "A/p/m.py": a})
    snap = analyse_with_references(root / "C", [root / "A"], Config(**config), base=root / "A")
    return snap.cross_repo


def test_edited_copy_is_a_near_duplicate_not_an_exact_clone(make_tree):
    x = _near(make_tree, EDITED, ORIG)
    assert x["function_clones"] == []
    (g,) = x["near_duplicates"]
    assert g["kind"] == "in_base" and g["repos"] == ["A", "C"]
    assert 0.7 <= g["similarity"] < 1.0
    assert g["locations"][0].startswith("C:p/m.py:") and g["locations"][1].startswith("A:p/m.py:")


def test_exact_copies_are_not_repeated_as_near_duplicates(make_tree):
    x = _near(make_tree, ORIG, ORIG)
    assert len(x["function_clones"]) == 1 and x["near_duplicates"] == []


def test_unrelated_functions_do_not_match(make_tree):
    assert _near(make_tree, UNRELATED, ORIG)["near_duplicates"] == []


def test_similarity_zero_turns_it_off_and_threshold_is_respected(make_tree):
    assert _near(make_tree, EDITED, ORIG, near_dup_threshold=0)["near_duplicates"] == []
    assert _near(make_tree, EDITED, ORIG, near_dup_threshold=0.99)["near_duplicates"] == []


def test_cli_prints_near_duplicates_and_validates_similarity(make_tree, capsys):
    root = make_tree({"C/p/m.py": EDITED, "A/p/m.py": ORIG})
    assert main([str(root / "C"), "--base", str(root / "A")]) == 0
    out = capsys.readouterr().out
    assert "near-duplicate" in out and "LIFT" not in out and "ALREADY IN BASE A" in out
    import pytest
    with pytest.raises(SystemExit):
        main([str(root / "C"), "--base", str(root / "A"), "--similarity", "2"])


def test_a_function_is_not_matched_through_its_nested_function(make_tree):
    outer = """
        def outer(rows):
            def inner(r):
                out = []
                for x in r:
                    if x:
                        out.append(x.strip())
                return sorted(out), len(out)
            return inner(rows)
    """
    standalone = """
        def inner2(r):
            out = []
            for x in r:
                if x:
                    out.append(x.strip())
            return sorted(out), len(out)
    """
    x = _near(make_tree, outer, standalone)
    # `outer` starts at line 2 of C's file; only the nested `inner` (line 3) may pair with A's copy
    starts = [g["locations"][0].split(":")[2].split("-")[0] for g in x["near_duplicates"]]
    assert "2" not in starts
