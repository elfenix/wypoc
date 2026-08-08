"""Exercises the `!` message operator: overload storage/promotion, class-body
methods registering at module scope, `this` binding (single receiver vs. a
tuple), bare-slot-name access inside a method body, and left-to-right
most-specific-wins overload resolution (samples/eval_messages.wy)."""
import pytest

from conftest import eval_sample
from wypoc.wyrm_eval_parse_tree import BoundMessage, Method, dispatch_message, message_table


@pytest.fixture(scope="module")
def ctx():
    return eval_sample("eval_messages.wy")


def test_area_overloads_registered(ctx):
    area = message_table(ctx)["area"]
    assert isinstance(area, Method) and len(area.overloads) == 3, (
        "class-body area() methods register a 3-overload 'area' Method at module scope"
    )
    assert "area" not in ctx, "messages live in their own namespace, never as a plain ctx binding"


def test_area_dispatch(ctx):
    assert ctx["circle_area"].value == 12.56636, "c!area() picks Circle's override over Shape's"
    assert ctx["square_area"].value == 9.0, "s!area() picks Square's override over Shape's"
    assert ctx["shape_area"].value == 0.0, "plain!area() (a bare Shape) uses Shape's own area()"
    assert ctx["bound_area"].value == 12.56636, (
        "c!area (no parens) yields a callable bound to c, called separately"
    )


def test_describe_promotion(ctx):
    describe = message_table(ctx)["describe"]
    assert isinstance(describe, Method) and len(describe.overloads) == 2, (
        "a plain fn + a later fn [Circle] promotes 'describe' into a 2-overload Method"
    )
    assert isinstance(ctx["describe"].value, Method) is False, (
        "promotion copies the plain fn into the message's wildcard overload; "
        "the original 'describe' variable binding is untouched, in its own namespace"
    )
    assert ctx["circle_desc"].value == "a circle", "c!describe() prefers the Circle-specific overload"
    assert ctx["square_desc"].value == "generic thing", (
        "s!describe() falls back to the promoted wildcard overload (Square doesn't match Circle)"
    )


def test_multi_receiver_dispatch(ctx):
    assert ctx["cc"].value == "circle-circle collision", "(c, c2) ! collide() matches [Circle, Circle] exactly"
    assert ctx["cs"].value == "circle-square collision", "(c, s) ! collide() matches [Circle, Square] exactly"
    assert ctx["gen"].value == "generic collision", (
        "(s, s) ! collide() falls back to the 2-receiver wildcard "
        "(arity taken from the triggering [Cls, Cls] form)"
    )


def test_this_binding(ctx):
    # `this` binding: single receiver -> the object itself; tuple receiver -> the tuple.
    c = ctx["c"].value
    result = dispatch_message("area", [c], [], ctx)
    assert result == 12.56636, "dispatch_message('area', [c], ...) matches c!area()"

    bound = ctx["bound"].value
    assert isinstance(bound, BoundMessage) and bound.receivers == [c], (
        "c!area (no call) is a BoundMessage holding [c] as receivers"
    )
