"""Exercises wypoc.lsp.diagnostics_for_source directly - no JSON-RPC/stdio
server involved, since that logic is deliberately factored out as a plain
function for testability. Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_lsp.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc.lsp import diagnostics_for_source

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def check(cond, msg, failures):
    print(f"{'OK  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures[0] += 1


def main() -> int:
    failures = [0]

    check(diagnostics_for_source("x = 1\n") == [], "clean source produces no diagnostics", failures)

    diags = diagnostics_for_source("x = 1 +\n")
    check(len(diags) == 1, "a syntax error produces exactly one diagnostic", failures)
    if diags:
        d = diags[0]
        check(d.range.start.line == 0, f"diagnostic is on line 0 (0-indexed): {d.range.start.line}", failures)
        check(d.severity is not None, "diagnostic has a severity set", failures)
        check(bool(d.message), f"diagnostic has a non-empty message: {d.message!r}", failures)

    multiline = "fn foo():\n    x = 1 +\n"
    diags = diagnostics_for_source(multiline)
    check(len(diags) == 1 and diags[0].range.start.line == 1,
          f"a later-line error is reported on the right (0-indexed) line: {diags}", failures)

    # Every bundled sample is known-good (test_grammar.py covers this too,
    # from the grammar side); the LSP server should agree there's nothing
    # to report for any of them.
    for name in sorted(os.listdir(SAMPLES_DIR)):
        if not name.endswith(".wy"):
            continue
        with open(os.path.join(SAMPLES_DIR, name)) as f:
            src = f.read()
        check(diagnostics_for_source(src) == [], f"samples/{name} produces no diagnostics", failures)

    if failures[0]:
        print(f"\n{failures[0]} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
