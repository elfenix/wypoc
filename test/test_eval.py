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
        eval_program(parse('d := ${ "a": 1 }\nd!remove("missing")\n'), ctx)


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
        eval_program(parse('d := ${ "a": 1 }\nd!resize(2)\n'), ctx)
