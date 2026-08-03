"""Parses samples/eval_assignments.wy and checks the resulting scope."""
import pytest

from conftest import eval_sample
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
    "d": {"a": 100, "b": 2},
    "grid": [[1, 42], [3, 4]],
}


@pytest.fixture(scope="module")
def ctx():
    return eval_sample("eval_assignments.wy")


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

    for src in ('t = (1, 2)\nt[0] = 9\n', 's = "abc"\ns[0] = "z"\n'):
        with pytest.raises(TypeError, match="does not support item assignment"):
            eval_program(parse(src), {})
