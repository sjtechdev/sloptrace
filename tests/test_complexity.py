import ast
import textwrap
import time

import pytest

from slop_score.sloc import code_lines


def test_sloc_skips_blanks_comments_and_docstrings():
    src = textwrap.dedent('''\
        """Module docstring."""

        # a comment
        def f(x):
            """Docstring
            over two lines."""
            y = (x +    # trailing comment
                 1)

            return y
    ''')
    assert code_lines(src, ast.parse(src)) == (4, 7, 8, 10)


def test_function_mass_uses_sloc(score):
    snap = score({"m.py": '''
        def f(x):
            """A long docstring
            that should not
            make f look bigger."""
            # nor should comments
            if x:
                return 1
            return 2
    '''})
    # CC 2, SLOC 4 (def, if, return, return)
    assert snap.sloc == 4
    assert snap.functions == 1


def test_huge_literal_is_fast(score):
    # radon's raw analyser (previously used for LLOC) took ~29s on rich's
    # 3,600-line emoji table; a single big literal must stay cheap
    body = "\n".join(f'    "name_{i}": "value_{i}",' for i in range(4000))
    t = time.perf_counter()
    snap = score({"table.py": "TABLE = {\n" + body + "\n}\n"})
    assert time.perf_counter() - t < 3
    assert snap.sloc == 4002


def test_decorated_function_gets_a_cognitive_score(score):
    pytest.importorskip("complexipy")
    snap = score({"m.py": """
        import functools

        class C:
            @staticmethod
            @functools.lru_cache
            def nested(xs):
                for x in xs:
                    if x:
                        for y in x:
                            if y:
                                while y:
                                    if y > 1:
                                        y -= 1
                                    elif y:
                                        break
                return xs
    """})
    # previously None: complexipy reports this function from its first
    # decorator line, radon from the `def` line, and the lookup was by line
    assert snap.cog_gt_15 == 1


def test_analysis_failure_is_reported_not_swallowed(score, monkeypatch):
    import slop_score.snapshot as snapshot

    def boom(*a, **k):
        raise RuntimeError("radon exploded")
    monkeypatch.setattr(snapshot, "function_stats", boom)
    snap = score({"m.py": "def f():\n    return 1\n"})
    assert snap.functions == 0
    assert snap.warnings == ["m.py: cyclomatic complexity failed (RuntimeError: radon exploded)"]
