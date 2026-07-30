"""Parses wypoc/samples/eval_control_flow.wy and checks while/for/break/
continue/for-else evaluated correctly.

Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_eval_control_flow.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import Variable, eval_program

SAMPLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "samples", "eval_control_flow.wy"
)

EXPECTED = {
    "i": 5,       # while loop breaks once i hits 5
    "seen": 7,    # 1 + 2 + 4 (3 is skipped via continue, 5 breaks before adding)
    "total": 6,   # 1 + 2 + 3, then break at x == 4 (for/else's else is skipped)
    "summed": 106,  # 1 + 2 + 3, loop completes -> else adds 100
    "found_even": 4,  # first_even returns as soon as it finds an even item
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
