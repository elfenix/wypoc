"""Parses samples/eval_coroutines.wy and drives next()/send() to check
coroutine suspension/resumption, 'yield from' delegation, the '.value'
attribute, and message-dispatched coroutines - see
doc/language-spec.md's Coroutines section."""
import pytest

from conftest import eval_sample
from wypoc import wyrm_builtins


@pytest.fixture(scope="module")
def ctx():
    ctx: dict = {}
    wyrm_builtins.install(ctx)
    return eval_sample("eval_coroutines.wy", ctx)


def value(ctx, name):
    return ctx[name].value


def test_next_yields_then_finishes(ctx):
    assert value(ctx, "first_next") == 1
    assert value(ctx, "value_while_active") == "expected", (
        "accessing .value before the coroutine finishes is an error, caught here"
    )
    assert wyrm_builtins.is_error(value(ctx, "second_next")), (
        "next() past the end reports StopIteration"
    )
    assert value(ctx, "value_after_finish") == 5, ".value holds the `return` value once finished"


def test_send_mirrors_values_back(ctx):
    assert value(ctx, "mirror_start") == 10
    assert value(ctx, "mirror_echo_1") == 20
    assert value(ctx, "mirror_echo_2") == 30


def test_yield_from_delegates(ctx):
    assert value(ctx, "delegate_1") == 1
    assert value(ctx, "delegate_2") == 2
    assert value(ctx, "delegate_3") == 3
    assert wyrm_builtins.is_error(value(ctx, "delegate_4")), (
        "the delegating coroutine finishes once its sub-coroutine does"
    )


def test_message_dispatched_coroutine(ctx):
    assert value(ctx, "sum_start") == 0
    assert value(ctx, "sum_after_5") == 5
    assert value(ctx, "sum_after_10") == 15
    assert wyrm_builtins.is_error(value(ctx, "sum_stopped")), (
        "sending nil ends term_adder's while loop, finishing the coroutine"
    )


def test_deeply_recursive_coroutine_body_does_not_overflow():
    """A coroutine body's own thread used to start with whatever
    threading.stack_size() defaults to, and no recursion-limit bump - so a
    recursive call chain deep enough to need CoroutineInstance._run's paired
    stack-size/recursion-limit increase (see its docstring) would previously
    hit Python's default ~1000-frame RecursionError well before this depth.
    Run entirely inside the co body (not at module top level) so it
    exercises the coroutine's own thread, not the main one."""
    import textwrap

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    source = textwrap.dedent("""\
        fn depth(n: int) -> int:
            if n <= 0:
                return 0
            else:
                return depth(n - 1) + 1

        co deep_sum(n: int) -> int:
            return depth(n)

        cofun := deep_sum(500)
        finished := next(cofun)
        result := cofun.value
        """)
    ctx: dict = {}
    wyrm_builtins.install(ctx)
    eval_program(parse(source), ctx)
    assert wyrm_builtins.is_error(value(ctx, "finished")), (
        "a co with no yield runs to completion on the first next()"
    )
    # 500 Wyrm-level recursive calls is well past what the same recursive fn
    # can reach at module top level (~60-70 deep, given how many native
    # Python frames wyrm_eval_parse_tree.py spends per Wyrm-level call) -
    # this depth only succeeds inside a coroutine body thanks to
    # CoroutineInstance._run's paired stack-size/recursion-limit increase.
    assert value(ctx, "result") == 500


def test_tail_recursive_call_from_a_coroutine_body_trampolines_like_anywhere_else():
    """CoroutineInstance._run still calls the native run_scoped_block for
    the co body's own top-level statements (see _run, and Phase 4 of the
    wyrm-recursion-depth plan) - but any call it makes to a plain Function
    (via eval_expr's ordinary Call handling -> call_value -> call_function)
    goes through _run_driver regardless of whether the caller is native
    top-level code or a coroutine's own thread, since call_function itself
    is the trampoline entry point (see call_function). So a tail-recursive
    fn called from inside a co body gets the *same* effectively-unbounded
    depth as anywhere else - not just the bigger-but-still-finite headroom
    Phase 0's stack-size/recursion-limit pairing bought it. 100000 is well
    past what even Phase 0's fix alone could reach."""
    import textwrap

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    source = textwrap.dedent("""\
        fn depth(n: int, acc: int) -> int:
            if n <= 0:
                return acc
            else:
                return depth(n - 1, acc + 1)

        co deep_sum(n: int) -> int:
            return depth(n, 0)

        cofun := deep_sum(100000)
        finished := next(cofun)
        result := cofun.value
        """)
    ctx: dict = {}
    wyrm_builtins.install(ctx)
    eval_program(parse(source), ctx)
    assert wyrm_builtins.is_error(value(ctx, "finished"))
    assert value(ctx, "result") == 100000
