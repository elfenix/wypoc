"""Parses wypoc/samples/eval_functions.wy and checks function calls +
if/elif/else control flow evaluated correctly.

Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_eval_functions.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import Function, Variable, eval_program

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "eval_functions.wy")

EXPECTED = {
    "sum_direct": 5,
    "greeting_default": "Hello, Wyrm",
    "greeting_custom": "Hey, Wyrm",
    "classify_neg": "negative",
    "classify_zero": "zero",
    "classify_pos": "positive",
    "packed": (1, 2, 3),
    "lambda_result": 9,
}


def main() -> int:
    with open(SAMPLE) as f:
        src = f.read()
    tree = parse(src)
    ctx: dict = {}
    eval_program(tree, ctx)

    failures = 0

    for name in ("add", "greet", "classify", "pack"):
        var = ctx.get(name)
        if not (isinstance(var, Variable) and isinstance(var.value, Function)):
            print(f"FAIL {name}: expected a Function, got {var!r}")
            failures += 1
        else:
            print(f"OK   {name} = {var.value!r}")

    for name, expected in EXPECTED.items():
        var = ctx.get(name)
        if not isinstance(var, Variable):
            print(f"FAIL {name}: missing from context (got {var!r})")
            failures += 1
            continue
        if var.value != expected:
            print(f"FAIL {name}: expected {expected!r}, got {var.value!r}")
            failures += 1
        else:
            print(f"OK   {name} = {var.value!r}")

    if failures:
        print(f"\n{failures} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
