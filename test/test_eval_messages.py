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


def test_str_prefers_a_class_instances___str___message():
    """`str(elem)` asks first whether elem's class answers `__str__` (same
    "ask first, class's own answer is final" shape as `sexpr()`'s `__sexpr`
    hook - see wyrm_eval_parse_tree.str_value) and only falls back to the
    built-in bare rendering when it doesn't."""
    import textwrap

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program
    from wypoc import wyrm_builtins

    source = textwrap.dedent("""\
        class Point:
            slot x: int = 0
            slot y: int = 0

        fn [Point] __str__() -> str:
            return "Point(" + str(x) + ", " + str(y) + ")"

        class Plain:
            slot v: int = 0

        p := Point()
        p.x = 3
        p.y = 4
        p_str := str(p)

        plain := Plain()
        plain.v = 5
        plain_str := str(plain)
        """)
    ctx: dict = {}
    wyrm_builtins.install(ctx)
    eval_program(parse(source), ctx)
    assert ctx["p_str"].value == "Point(3, 4)", "str(p) dispatches to Point's __str__"
    assert ctx["plain_str"].value == "<Plain v=5>", (
        "a class with no __str__ overload falls back to the default bare rendering"
    )


def test_deep_tail_recursive_message_send_does_not_overflow_the_python_stack():
    """A `return this ! step(...)` tail call - an ordinary FnDef-backed
    overload, not NativeBody/CoDef - is trampolined by _run_driver exactly
    like call_function's plain-Call case (see wyrm_eval_parse_tree.py's
    call_overload/_make_overload_activation), so a self-recursive message
    send chain grows a Python list, not the C stack, same as
    test_eval_functions.py's plain-Call equivalent."""
    import textwrap

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program
    from wypoc import wyrm_builtins

    source = textwrap.dedent("""\
        class counter:
            slot n: int = 0

        fn [counter] step(acc: int) -> int:
            if n <= 0:
                return acc
            else:
                n = n - 1
                return this ! step(acc + 1)

        c := counter()
        c.n = 100000
        result := c ! step(0)
        """)
    ctx: dict = {}
    wyrm_builtins.install(ctx)
    eval_program(parse(source), ctx)
    assert ctx["result"].value == 100000
