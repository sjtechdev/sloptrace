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
