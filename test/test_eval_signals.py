"""`signal`/`emit` (wypoc/samples/eval_signals.wy): grammar plus the runtime
pieces in wyrm_eval_parse_tree.py/wyrm_builtins.py - Class.signals/
all_signals, SignalValue, connect/disconnect, and Emit dispatch."""
import pytest

from conftest import eval_sample_with_builtins
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import (
    Class, ClassInstance, Scope, SignalValue, eval_program, lookup,
    populate_globals,
)


@pytest.fixture(scope="module")
def ctx():
    return eval_sample_with_builtins("eval_signals.wy")


def test_signal_is_class_metadata():
    Counter = None
    ctx = eval_sample_with_builtins("eval_signals.wy")
    Counter = ctx["Counter"].value
    assert isinstance(Counter, Class)
    assert "changed" in Counter.signals
    assert "count" in Counter.slots and "changed" not in Counter.slots


def test_each_instance_gets_its_own_subscriber_list(ctx):
    c = ctx["c"].value
    assert isinstance(c, ClassInstance)
    sig = lookup("changed", c.attrs)
    assert isinstance(sig, SignalValue)


def test_emit_calls_subscribers_synchronously_with_args(ctx):
    assert ctx["after_two_emits"].value == [[0, 5], [5, 8]]


def test_disconnect_stops_further_calls(ctx):
    # One more increment happened after disconnecting on_change - the log
    # should be unchanged from the two-emit snapshot.
    assert ctx["after_disconnect"].value == ctx["after_two_emits"].value


def test_signal_is_inherited():
    ctx = eval_sample_with_builtins("eval_signals.wy")
    assert isinstance(ctx["inherited_signal"].value, SignalValue)


def _run(src: str):
    ctx = Scope()
    populate_globals(ctx)
    eval_program(parse(src), ctx)
    return ctx


def test_connect_on_non_signal_raises_type_error():
    with pytest.raises(TypeError, match="not a signal"):
        _run("5 ! connect(fn(x): pass)")


def test_emit_on_non_signal_raises_type_error():
    with pytest.raises(TypeError, match="not a signal"):
        _run("x := 5\nemit x(1)\n")


def test_emit_on_undefined_name_raises_name_error():
    with pytest.raises(NameError):
        _run("emit nope(1)\n")


def test_disconnect_unknown_callback_is_a_no_op():
    ctx = _run(
        "class Foo:\n"
        "    signal s()\n"
        "f := Foo()\n"
        "fn cb(): pass\n"
        "f.s ! disconnect(cb)\n"
    )
    assert isinstance(lookup("f", ctx).attrs["s"].value, SignalValue)


def test_a_slot_and_signal_of_the_same_name_is_rejected():
    # Both live in one instance's attrs namespace (see SignalValue's
    # docstring) - allowing it would mean whichever of the two seeding
    # loops in _instantiate_gen runs last silently wins.
    with pytest.raises(TypeError, match="both a slot and a signal"):
        _run(
            "class Bad:\n"
            "    slot changed: int = 0\n"
            "    signal changed()\n"
            "Bad()\n"
        )
