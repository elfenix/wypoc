"""Parses samples/eval_error_handling.wy and checks try/catch/error evaluated
correctly."""
import pytest

from conftest import eval_sample
from wypoc import wyrm_builtins
from wypoc.wyrm_builtins import Symbol, WyrmError
from wypoc.wyrm_eval_parse_tree import ClassInstance, Variable

EXPECTED = {
    "try_result": WyrmError("Hello World"),  # try returns the error immediately
    "passthrough_result": 11,  # try on a non-error value just passes it through
    "f": 5,  # error("Hello") catch 5 -> the handler's value
    "not_an_error": 10,  # catch's handler is never evaluated when there's no error
    "catch_return_result": 0,  # `catch return 0` forces an actual function return
    # demo(10, 0)'s init hit a division error -> catch fired. A symbol, not
    # the string of the same name: `'div0 != "div0"` (see Symbol).
    "demo_div_by_zero": Symbol("div0"),
    "index_error_result": 0,  # out-of-range list index catch 0 -> 0, not a crash
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


def test_error_subclass(ctx):
    hw = ctx["hardware_error"].value
    assert isinstance(hw, ClassInstance) and hw.cls.name == "DetectedHardwareFailure"
    assert wyrm_builtins.is_error(hw), "a subclass of `error` is still recognized as an error"


def test_construction_with_init_raii(ctx):
    ok = ctx["demo_ok"].value
    assert isinstance(ok, ClassInstance) and ok.cls.name == "demo"
    assert ok.attrs["result"].value == 5.0, "demo(10, 2) -> init ran, this.result = 10 / 2"
