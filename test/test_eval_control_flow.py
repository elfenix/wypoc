"""Parses samples/eval_control_flow.wy and checks while/for/break/continue/
for-else evaluated correctly."""
import pytest

from conftest import eval_sample
from wypoc.wyrm_eval_parse_tree import Variable

EXPECTED = {
    "i": 5,       # while loop breaks once i hits 5
    "seen": 7,    # 1 + 2 + 4 (3 is skipped via continue, 5 breaks before adding)
    "total": 6,   # 1 + 2 + 3, then break at x == 4 (for/else's else is skipped)
    "summed": 106,  # 1 + 2 + 3, loop completes -> else adds 100
    "found_even": 4,  # first_even returns as soon as it finds an even item
}


@pytest.fixture(scope="module")
def ctx():
    return eval_sample("eval_control_flow.wy")


@pytest.mark.parametrize("name,expected", EXPECTED.items(), ids=list(EXPECTED))
def test_control_flow(ctx, name, expected):
    var = ctx.get(name)
    assert isinstance(var, Variable), f"{name} missing from context (got {var!r})"
    assert var.value == expected
