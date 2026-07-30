"""Exercises the `!` message operator: overload storage/promotion, class-body
methods registering at module scope, `this` binding (single receiver vs. a
tuple), bare-slot-name access inside a method body, and left-to-right
most-specific-wins overload resolution (wypoc/samples/eval_messages.wy).

Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_eval_messages.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import BoundMessage, Method, dispatch_message, eval_program

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "eval_messages.wy")


def check(cond, msg, failures):
    print(f"{'OK  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures[0] += 1


def main() -> int:
    failures = [0]

    with open(SAMPLE) as f:
        src = f.read()
    tree = parse(src)
    ctx: dict = {}
    eval_program(tree, ctx)

    area = ctx["area"].value
    check(isinstance(area, Method) and len(area.overloads) == 3,
          "class-body area() methods register a 3-overload 'area' Method at module scope", failures)

    check(ctx["circle_area"].value == 12.56636, "c!area() picks Circle's override over Shape's", failures)
    check(ctx["square_area"].value == 9.0, "s!area() picks Square's override over Shape's", failures)
    check(ctx["shape_area"].value == 0.0, "plain!area() (a bare Shape) uses Shape's own area()", failures)
    check(ctx["bound_area"].value == 12.56636,
          "c!area (no parens) yields a callable bound to c, called separately", failures)

    describe = ctx["describe"].value
    check(isinstance(describe, Method) and len(describe.overloads) == 2,
          "a plain fn + a later fn [Circle] promotes 'describe' into a 2-overload Method", failures)
    check(ctx["circle_desc"].value == "a circle", "c!describe() prefers the Circle-specific overload", failures)
    check(ctx["square_desc"].value == "generic thing",
          "s!describe() falls back to the promoted wildcard overload (Square doesn't match Circle)", failures)

    check(ctx["cc"].value == "circle-circle collision", "(c, c2) ! collide() matches [Circle, Circle] exactly", failures)
    check(ctx["cs"].value == "circle-square collision", "(c, s) ! collide() matches [Circle, Square] exactly", failures)
    check(ctx["gen"].value == "generic collision",
          "(s, s) ! collide() falls back to the 2-receiver wildcard (arity taken from the triggering [Cls, Cls] form)",
          failures)

    # `this` binding: single receiver -> the object itself; tuple receiver -> the tuple.
    c = ctx["c"].value
    result = dispatch_message("area", [c], [], ctx)
    check(result == 12.56636, "dispatch_message('area', [c], ...) matches c!area()", failures)

    bound = ctx["bound"].value
    check(isinstance(bound, BoundMessage) and bound.receivers == [c],
          "c!area (no call) is a BoundMessage holding [c] as receivers", failures)

    if failures[0]:
        print(f"\n{failures[0]} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
