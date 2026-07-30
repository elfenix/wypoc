"""Shows Python objects/functions being exposed into the wyrm ecosystem via
expose()/expose_all()/builtin(), then called from wyrm source
(wypoc/samples/eval_builtins.wy).

Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_eval_builtins.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import Variable, builtin, eval_program, expose, expose_all

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "eval_builtins.wy")


def main() -> int:
    ctx: dict = {}
    captured = []

    # expose(): register a single Python callable under a wyrm name.
    expose(ctx, "display", captured.append)

    # builtin(): decorator form - keeps `add_one` usable as normal Python too.
    @builtin(ctx, "add_one")
    def _add_one(n):
        return n + 1

    def double(a, b):
        return (a * 2, b * 2)

    # expose_all(): register several names (functions or plain values) at once.
    expose_all(ctx, pi=math.pi, double=double)

    with open(SAMPLE) as f:
        src = f.read()
    tree = parse(src)
    eval_program(tree, ctx)

    failures = 0

    def check(cond, msg):
        nonlocal failures
        print(f"{'OK  ' if cond else 'FAIL'} {msg}")
        if not cond:
            failures += 1

    check(captured == ["hello", 3], f"display() calls reached Python: {captured!r}")
    check(isinstance(ctx["r"], Variable) and ctx["r"].value == 42, "add_one(41) == 42")
    check(isinstance(ctx["p"], Variable) and ctx["p"].value == math.pi, "pi exposed as a plain value")
    check(isinstance(ctx["c"], Variable) and ctx["c"].value == (6, 8), "double(3, 4) == (6, 8)")

    if failures:
        print(f"\n{failures} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
