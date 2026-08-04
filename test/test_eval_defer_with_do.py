"""Parses samples/eval_defer_with_do.wy and checks basic-use `do`/`defer`/
`with` semantics - see doc/language-spec.md's "do" and "Errors / RAII"
sections, and wyrm_eval_parse_tree.py's run_scoped_block/Scope.defers for
the POC-level implementation (defer is tied to whichever block Scope it's
lexically written in, not just the enclosing function call)."""
import pytest

from conftest import eval_sample
from wypoc import wyrm_builtins
from wypoc.wyrm_eval_parse_tree import Variable

EXPECTED = {
    "do_value": 3,
    "nested_do": 10,
    "ordering_log": "body second-registered-first-run first-registered-last-run ",
    "per_iteration_log": "iter iter-cleanup iter iter-cleanup iter iter-cleanup ",
    "ok_result": 1,
    "defer_on_error_log": "on-error-ran ",
    "pi_ish": 3.14,
    "e_ish": 2.72,
    "speed_of_light": 299_792_458.0,
    "with_sum": pytest.approx(5.86),
}


@pytest.fixture(scope="module")
def ctx():
    ctx: dict = {}
    wyrm_builtins.install(ctx)
    return eval_sample("eval_defer_with_do.wy", ctx)


@pytest.mark.parametrize("name,expected", EXPECTED.items(), ids=list(EXPECTED))
def test_result(ctx, name, expected):
    var = ctx.get(name)
    assert isinstance(var, Variable), f"{name} missing from context (got {var!r})"
    assert var.value == expected


def test_failing_call_returns_an_error(ctx):
    from wypoc.wyrm_builtins import is_error

    assert is_error(ctx["failing_result"].value)


def test_with_binding_is_immutable():
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    with pytest.raises(TypeError, match="immutable"):
        eval_program(parse("with x: int = 5\nx = 6\n"), {})


def test_defer_fires_per_loop_iteration_not_once():
    """defer is tied to the block Scope it's written in (basic-use choice -
    see wyrm_eval_parse_tree.py's run_scoped_block), so a defer inside a
    `while` body fires at the end of every iteration."""
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import Scope, eval_program

    ctx = Scope()
    eval_program(
        parse(
            "var n = 0\nvar count: int = 0\n"
            "while n < 4:\n    defer:\n        count = count + 1\n    n = n + 1\n"
        ),
        ctx,
    )
    assert ctx["count"].value == 4
