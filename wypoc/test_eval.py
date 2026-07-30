"""Parses wypoc/samples/eval_assignments.wy and checks the resulting scope.

Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_eval.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import Variable, eval_program

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "eval_assignments.wy")

EXPECTED = {
    "x": 5,
    "y": 10,
    "z": 15,
    "name": "Wyrm",
    "greeting": "Hello, Wyrm",
    "pi_ish": 3.14,
    "negated": -5,
    "a": 1,
    "b": 2,
}


def main() -> int:
    with open(SAMPLE) as f:
        src = f.read()
    tree = parse(src)
    ctx: dict = {}
    eval_program(tree, ctx)

    failures = 0
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
