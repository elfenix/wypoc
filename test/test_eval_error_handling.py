"""Parses samples/eval_error_handling.wy and checks try/catch/error evaluated
correctly."""
import pytest

from conftest import eval_sample
from wypoc import wyrm_builtins
from wypoc.wyrm_builtins import WyrmError
from wypoc.wyrm_eval_parse_tree import Variable

EXPECTED = {
    "try_result": WyrmError("Hello World"),  # try returns the error immediately
    "passthrough_result": 11,  # try on a non-error value just passes it through
    "f": 5,  # error("Hello") catch 5 -> the handler's value
    "not_an_error": 10,  # catch's handler is never evaluated when there's no error
    "catch_return_result": 0,  # `catch return 0` forces an actual function return
}


@pytest.fixture(scope="module")
def ctx():
    ctx: dict = {}
    wyrm_builtins.install(ctx)
    return eval_sample("eval_error_handling.wy", ctx)


@pytest.mark.parametrize("name,expected", EXPECTED.items(), ids=list(EXPECTED))
def test_error_handling(ctx, name, expected):
    var = ctx.get(name)
    assert isinstance(var, Variable), f"{name} missing from context (got {var!r})"
    assert var.value == expected
