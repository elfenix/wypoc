"""Parses samples/eval_range.wy and checks `range` (corelib/prelude.wy,
seeded into every scope by populate_globals - no import needed)."""
import pytest

from conftest import eval_sample
from wypoc import wyrm_builtins
from wypoc.wyrm_eval_parse_tree import Variable

EXPECTED = {
    "total": 10,  # sum(range(0, 5)) == 0+1+2+3+4
    "count": 0,  # range(3, 3) is empty (begin >= end)
    "first": 2,  # first value out of range(2, 6)
    "c1": 0,
    "c2": 1,
    "c3": 2,
}


@pytest.fixture(scope="module")
def ctx():
    from wypoc.wyrm_eval_parse_tree import Scope, populate_globals

    ctx = Scope()
    populate_globals(ctx)
    return eval_sample("eval_range.wy", ctx)


@pytest.mark.parametrize("name,expected", EXPECTED.items(), ids=list(EXPECTED))
def test_result(ctx, name, expected):
    var = ctx.get(name)
    assert isinstance(var, Variable), f"{name} missing from context (got {var!r})"
    assert var.value == expected


def test_exhausted_range_yields_stop_iteration(ctx):
    assert wyrm_builtins.is_error(ctx["c4"].value)
