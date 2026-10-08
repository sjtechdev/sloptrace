DUPLICATED_FUNCTIONS = {"m.py": """
    def a(x):
        y = x + 1
        z = y * 2
        return z

    def b(p):
        q = p + 1
        r = q * 2
        return r
"""}


def test_whole_function_clone_is_not_also_reported_as_block_clone(score):
    snap = score(DUPLICATED_FUNCTIONS)
    assert [g["members"] for g in snap.function_clones] == [["m.py::a", "m.py::b"]]
    assert snap.block_clones == []
    # but its lines still count towards clone_ratio: every code line here is
    # part of one of the two copies
    assert snap.clone_line_count == snap.sloc == 8
    assert snap.clone_ratio == 1.0


def test_block_clone_inside_different_functions_is_reported(score):
    snap = score({"m.py": """
        def a(x):
            print("start")
            y = x + 1
            z = y * 2
            w = z - 3
            return w

        def b(p):
            q = p + 1
            r = q * 2
            s = r - 3
            return [s]
    """})
    assert snap.function_clones == []
    assert [g["locations"] for g in snap.block_clones] == [["m.py:4-6", "m.py:10-12"]]


def test_clone_ratio_counts_code_lines_not_comments(score):
    snap = score({"m.py": """
        def a(x):
            # a long comment that is not code
            # and another one
            y = x + 1
            z = y * 2
            return z

        def b(p):
            q = p + 1
            r = q * 2
            return r

        def unrelated():
            return 0
    """})
    # 2 x (def + 3 statements) duplicated, of 10 code lines
    assert snap.clone_line_count == 8
    assert snap.sloc == 10
    assert snap.clone_ratio == 0.8
