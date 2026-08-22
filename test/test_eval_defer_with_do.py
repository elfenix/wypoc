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


def test_defer_still_fires_once_per_level_through_a_deep_trampolined_recursion():
    """Each level of a deep, trampolined (see wyrm_eval_parse_tree.py's
    _run_driver/call_function) tail-recursive call registers its own
    `defer` - this checks two things at once: that a defer still fires
    exactly once per call level (not zero times, not once total) even
    though the call chain no longer recurses natively, and that they still
    fire in the right order (deepest call's scope tears down first, as its
    ReturnSignal propagates outward one level at a time - see
    run_scoped_block's docstring) rather than e.g. all at once at the
    outermost call. `log` encodes the visitation order as a big base-100
    Horner value (n can exceed 99, so "digits" overlap, but the encoding is
    still injective for a fixed-length ordered sequence - any reordering or
    omission changes the result) so a wrong order or a missed/duplicated
    defer both fail the assertion, not just the count."""
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import Scope, eval_program

    depth = 300
    ctx = Scope()
    eval_program(
        parse(
            "var count: int = 0\n"
            "var log: int = 0\n"
            "fn count_down(n: int) -> int:\n"
            "    defer:\n"
            "        count = count + 1\n"
            "        log = log * 100 + n\n"
            "    if n <= 0:\n"
            "        return 0\n"
            "    else:\n"
            "        return count_down(n - 1)\n"
            "\n"
            f"var result = count_down({depth})\n"
        ),
        ctx,
    )
    assert ctx["count"].value == depth + 1
    expected_log = 0
    for n in range(depth + 1):  # innermost (n=0) tears down first
        expected_log = expected_log * 100 + n
    assert ctx["log"].value == expected_log


def test_defer_still_fires_once_per_level_through_deep_non_tail_recursion():
    """Same proof as test_defer_still_fires_once_per_level_through_a_deep_
    trampolined_recursion, but for `return count_down(n - 1) + 0` - the
    call wrapped in a BinOp - which only trampolines via _eval_expr_gen's
    general Call handling."""
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import Scope, eval_program

    depth = 300
    ctx = Scope()
    eval_program(
        parse(
            "var count: int = 0\n"
            "var log: int = 0\n"
            "fn count_down(n: int) -> int:\n"
            "    defer:\n"
            "        count = count + 1\n"
            "        log = log * 100 + n\n"
            "    if n <= 0:\n"
            "        return 0\n"
            "    else:\n"
            "        return count_down(n - 1) + 0\n"
            "\n"
            f"var result = count_down({depth})\n"
        ),
        ctx,
    )
    assert ctx["count"].value == depth + 1
    expected_log = 0
    for n in range(depth + 1):
        expected_log = expected_log * 100 + n
    assert ctx["log"].value == expected_log


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
