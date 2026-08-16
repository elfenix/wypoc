"""Parses samples/eval_assignments.wy and checks the resulting scope."""
import pytest

from conftest import eval_sample
from wypoc import wyrm_builtins
from wypoc.wyrm_eval_parse_tree import Variable

EXPECTED = {
    "x": 5,
    "y": 10,
    "z": 15,
    "name": "Wyrm",
    "greeting": "Hello, Wyrm",
    "pi_ish": 3.14,
    "negated": -5,
    "a": 1,
    "b": 2,
    "arr": [99, 2, 3, 4],
    "grown": [1, 2, 3, 4, 5],
    "pruned": [1],
    "expanded": [1, 0, 0, 0],
    "appended": [1, 2],
    "d": {"a": 100},
    "removed": 2,
    "grid": [[1, 42], [3, 4]],
}


@pytest.fixture(scope="module")
def ctx():
    ctx: dict = {}
    wyrm_builtins.install(ctx)
    return eval_sample("eval_assignments.wy", ctx)


@pytest.mark.parametrize("name,expected", EXPECTED.items(), ids=list(EXPECTED))
def test_assignment(ctx, name, expected):
    var = ctx.get(name)
    assert isinstance(var, Variable), f"{name} missing from context (got {var!r})"
    assert var.value == expected


def test_index_assignment_through_slot(ctx):
    box = ctx["box"].value
    assert box.attrs["cells"].value == [1, 55, 3]


def test_index_assignment_rejects_immutable_types():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    for src in ('t := (1, 2)\nt[0] = 9\n', 's := "abc"\ns[0] = "z"\n'):
        with pytest.raises(TypeError, match="does not support item assignment"):
            eval_program(parse(src), {})


def test_dict_remove_missing_key_raises():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    ctx: dict = {}
    wyrm_builtins.install(ctx)
    with pytest.raises(KeyError):
        eval_program(parse('d := { "a": 1 }\nd!remove("missing")\n'), ctx)


def test_dict_remove_rejects_non_dict_receiver():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    ctx: dict = {}
    wyrm_builtins.install(ctx)
    with pytest.raises(TypeError, match="not a dict"):
        eval_program(parse('arr := [1, 2]\narr!remove(0)\n'), ctx)


def test_list_resize_grows_with_unset(ctx):
    from wypoc.wyrm_eval_parse_tree import UNSET

    grown = ctx["grown"].value
    assert grown[3] == 4 and grown[4] == 5, "grown items were overwritten after resize"

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    fresh: dict = {}
    wyrm_builtins.install(fresh)
    eval_program(parse("a := [1, 2, 3]\na!resize(5)\n"), fresh)
    a = fresh["a"].value
    assert a[:3] == [1, 2, 3]
    assert a[3] is UNSET and a[4] is UNSET


def test_list_expand_is_relative_and_shares_the_fill_value():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    ctx: dict = {}
    wyrm_builtins.install(ctx)
    eval_program(parse("a := [1]\na!expand(0, 3)\na!expand(9, 0)\n"), ctx)
    assert ctx["a"].value == [1, 0, 0, 0], "expand grows by count, not to count"

    # A mutable fill is stored by reference, one object shared per slot.
    fresh: dict = {}
    wyrm_builtins.install(fresh)
    eval_program(parse("row := [0]\ngrid := []\ngrid!expand(row, 2)\n"), fresh)
    grid = fresh["grid"].value
    assert grid[0] is grid[1] is fresh["row"].value


def test_list_expand_rejects_negative_count():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    ctx: dict = {}
    wyrm_builtins.install(ctx)
    with pytest.raises(ValueError, match="count must be >= 0"):
        eval_program(parse("a := [1, 2, 3]\na!expand(0, -1)\n"), ctx)


def test_list_append_grows_by_one_and_chains():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    ctx: dict = {}
    wyrm_builtins.install(ctx)
    eval_program(parse('a := []\na!append(1)!append("two")\n'), ctx)
    assert ctx["a"].value == [1, "two"]


def test_list_expand_and_append_reject_non_list_receivers():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    for src, message in (
        ('d := { "a": 1 }\nd!expand(0, 2)\n', "expand: not a list"),
        ('d := { "a": 1 }\nd!append(0)\n', "append: not a list"),
    ):
        ctx: dict = {}
        wyrm_builtins.install(ctx)
        with pytest.raises(TypeError, match=message):
            eval_program(parse(src), ctx)


def test_list_resize_rejects_negative_count():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    ctx: dict = {}
    wyrm_builtins.install(ctx)
    with pytest.raises(ValueError, match="count must be >= 0"):
        eval_program(parse("a := [1, 2, 3]\na!resize(-1)\n"), ctx)


def test_list_resize_rejects_non_list_receiver():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    ctx: dict = {}
    wyrm_builtins.install(ctx)
    with pytest.raises(TypeError, match="not a list"):
        eval_program(parse('d := { "a": 1 }\nd!resize(2)\n'), ctx)


# --------------------------------------------------------------------------
# The bitwise family: unary `~`/`+` (which bind like unary `-`), and the
# `<<`/`>>` shifts (which bind tighter than `&` and looser than `+`).
# --------------------------------------------------------------------------

def run(src: str) -> dict:
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    ctx: dict = {}
    wyrm_builtins.install(ctx)
    eval_program(parse(src if src.endswith("\n") else src + "\n"), ctx)
    return {name: var.value for name, var in ctx.items()
            if isinstance(var, Variable)}


@pytest.mark.parametrize("src,expected", [
    ("x := ~5", -6),
    ("x := ~-1", 0),
    ("x := +5", 5),
    ("x := +-5", -5),
    ("x := -~5", 6),
    ("x := 1 << 4", 16),
    ("x := 256 >> 4", 16),
    ("x := 1 << 2 << 3", 32),            # left-associative: (1 << 2) << 3
    ("x := 1 + 1 << 2", 8),              # `+` binds tighter than `<<`
    ("x := 3 & 1 << 1", 2),              # `<<` binds tighter than `&`
    ("x := ~0 & 0xff", 255),             # unary binds tighter than either
    ("x := 2 ** 3 << 1", 16),            # `**` too
])
def test_the_bitwise_operators_and_how_tightly_they_bind(src, expected):
    assert run(src)["x"] == expected


def test_a_negative_shift_count_is_an_error_value_not_a_crash():
    # The same treatment `/` gives division by zero, so wyrm code can catch it.
    assert wyrm_builtins.is_error(run("x := 1 << -1")["x"])
    assert wyrm_builtins.is_error(run("x := 1 >> -1")["x"])


def test_a_name_may_contain_a_dollar():
    values = run("reg$0 := 7\n$total := reg$0 * 2\nx := $total + 1")
    assert values["reg$0"] == 7 and values["$total"] == 14 and values["x"] == 15


@pytest.mark.parametrize("src,expected", [
    ("x := 1 is int", True),
    ("x := 1 is not int", False),
    ("x := 1 is not str", True),
    ('x := "s" is not int | float', True),
    ("x := 1.5 is not int | float", False),
    ("x := (1 is not str) and (1 is int)", True),
])
def test_is_not_negates_the_type_check(src, expected):
    assert run(src)["x"] is expected


# --------------------------------------------------------------------------
# `and`/`or` short-circuit: the right operand must not be evaluated once
# the left operand alone decides the result.
# --------------------------------------------------------------------------

def test_and_short_circuits_on_a_falsy_left_operand():
    values = run(
        "fn boom():\n"
        "    side := true\n"
        "    side = false\n"
        "    return true\n"
        "x := false and boom()\n"
    )
    assert values["x"] is False


def test_or_short_circuits_on_a_truthy_left_operand():
    values = run(
        "fn boom() -> bool:\n"
        "    1 / 0\n"
        "    return true\n"
        "x := true or boom()\n"
    )
    assert values["x"] is True


def test_and_still_evaluates_the_right_operand_when_needed():
    values = run("x := true and false\ny := true and true\n")
    assert values["x"] is False and values["y"] is True


def test_or_still_evaluates_the_right_operand_when_needed():
    values = run("x := false or false\ny := false or true\n")
    assert values["x"] is False and values["y"] is True


# --------------------------------------------------------------------------
# `if` as an expression: its value is that of the last statement executed
# in whichever branch ran, same as `do:`.
# --------------------------------------------------------------------------

def test_if_expression_takes_the_then_branch_value():
    values = run("check := true\na := if check { 3 } else { 4 }\n")
    assert values["a"] == 3


def test_if_expression_takes_the_else_branch_value():
    values = run("check := false\na := if check { 3 } else { 4 }\n")
    assert values["a"] == 4


def test_if_expression_takes_an_elif_branch_value():
    values = run("n := 2\na := if n == 1 { 1 } elif n == 2 { 2 } else { 3 }\n")
    assert values["a"] == 2


def test_if_expression_with_no_matching_branch_and_no_else_is_none():
    values = run("check := false\na := if check { 3 }\n")
    assert values["a"] is None


def test_if_used_as_a_statement_still_yields_the_taken_branch_value():
    # An `if` written as an ordinary statement (not through if_expr's
    # `if COND { ... }` expression form) must still answer the value of
    # whichever branch ran, so it can be the tail of a `do:`/fn body.
    values = run("a := do {\n    if true {\n        5\n    }\n}\n")
    assert values["a"] == 5


def test_if_used_as_a_statement_with_no_matching_branch_is_none():
    values = run("a := do {\n    if false {\n        5\n    }\n}\n")
    assert values["a"] is None
