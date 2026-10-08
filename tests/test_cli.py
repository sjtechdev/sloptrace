import json

from slop_score.cli import main


def test_json_output_is_versioned_and_omits_internal_fields(make_tree, tmp_path, capsys):
    root = make_tree({"pkg/__init__.py": "", "pkg/a.py": "def f(x):\n    return x\n"})
    out = tmp_path / "out.json"
    assert main([str(root), "-o", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["schema_version"] == 2
    snap = payload["snapshots"][0]
    assert snap["functions"] == 1
    assert "func_fingerprints" not in snap
    assert "EROSION" in capsys.readouterr().out
