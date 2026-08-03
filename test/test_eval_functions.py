"""Parses samples/eval_functions.wy and checks function calls + if/elif/else
control flow evaluated correctly."""
import pytest

from conftest import eval_sample
from wypoc.wyrm_eval_parse_tree import Function, Variable

EXPECTED = {
    "sum_direct": 5,
    "greeting_default": "Hello, Wyrm",
    "greeting_custom": "Hey, Wyrm",
    "classify_neg": "negative",
    "classify_zero": "zero",
    "classify_pos": "positive",
    "packed": (1, 2, 3),
    "lambda_result": 9,
    "call_count_1": 1,
    "call_count_2": 2,
    "call_count_3": 3,
    "call_count_declared_1": 1,
    "call_count_declared_2": 2,
}


@pytest.fixture(scope="module")
def ctx():
    return eval_sample("eval_functions.wy")


@pytest.mark.parametrize("name", ["add", "greet", "classify", "pack"])
def test_defines_function(ctx, name):
    var = ctx.get(name)
    assert isinstance(var, Variable) and isinstance(var.value, Function), (
        f"{name}: expected a Function, got {var!r}"
    )


@pytest.mark.parametrize("name,expected", EXPECTED.items(), ids=list(EXPECTED))
def test_result(ctx, name, expected):
    var = ctx.get(name)
    assert isinstance(var, Variable), f"{name} missing from context (got {var!r})"
    assert var.value == expected
