"""Regenerates the pegen parser from wyrm.gram and parses every sample under
wypoc/samples/, failing loudly on any syntax error. Run with:

    .venv/bin/python -m pegen wypoc/wyrm.gram -o wypoc/parser.py -q
    PYTHONPATH=. .venv/bin/python wypoc/test_grammar.py
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc.parse import parse

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def main() -> int:
    failures = 0
    for path in sorted(glob.glob(os.path.join(SAMPLES_DIR, "*.wy"))):
        name = os.path.basename(path)
        with open(path) as f:
            src = f.read()
        try:
            tree = parse(src)
        except SyntaxError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        else:
            print(f"OK   {name} ({len(tree.body)} top-level statements)")
    if failures:
        print(f"\n{failures} sample(s) failed to parse")
        return 1
    print("\nall samples parsed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
