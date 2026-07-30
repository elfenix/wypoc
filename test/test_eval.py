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
}


@pytest.fixture(scope="module")
def ctx():
    return eval_sample("eval_assignments.wy")


@pytest.mark.parametrize("name,expected", EXPECTED.items(), ids=list(EXPECTED))
def test_assignment(ctx, name, expected):
    var = ctx.get(name)
    assert isinstance(var, Variable), f"{name} missing from context (got {var!r})"
    assert var.value == expected
