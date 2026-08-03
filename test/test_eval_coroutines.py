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
