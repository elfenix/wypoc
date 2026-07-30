"""Exercises the installed `wyrm` console script (wypoc/cli.py, wired up via
pyproject.toml's [project.scripts]): argument packing into __ARGS, exit
codes, and error reporting.

Requires `pip install -e .` to have been run first (so the `wyrm` command
exists in this venv). Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_cli.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WYRM = shutil.which("wyrm") or os.path.join(REPO_ROOT, ".venv", "bin", "wyrm")
SAMPLE = os.path.join(REPO_ROOT, "wypoc", "samples", "eval_args.wy")


def check(cond, msg, failures):
    print(f"{'OK  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures[0] += 1


def run(*args):
    return subprocess.run([WYRM, *args], capture_output=True, text=True)


def main() -> int:
    if not os.path.isfile(WYRM):
        print(f"wyrm console script not found at {WYRM!r} - run `pip install -e .` first", file=sys.stderr)
        return 1

    failures = [0]

    r = run(SAMPLE, "foo", "bar", "--baz", "3")
    check(r.returncode == 0, f"running a script with args exits 0 (got {r.returncode}, stderr={r.stderr!r})", failures)
    check(r.stdout == "foo\nbar\n--baz\n3\n",
          f"script args (dashes and all) are packed into __ARGS in order: {r.stdout!r}", failures)

    r = run(SAMPLE)
    check(r.returncode == 0 and r.stdout == "", "no script args -> empty __ARGS, nothing printed", failures)

    r = run("-h")
    check(r.returncode == 0 and "Usage" in r.stdout, "-h prints usage and exits 0", failures)

    r = run()
    check(r.returncode == 2 and "Usage" in r.stderr, "no script path prints usage to stderr and exits 2", failures)

    r = run("/no/such/file.wy")
    check(r.returncode == 2 and "can't open file" in r.stderr, "a missing script file exits 2 with a clear message", failures)

    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "bad.wy")
        with open(bad, "w") as f:
            f.write("x = 1 +\n")
        r = run(bad)
        check(r.returncode == 1, "a syntax error exits 1", failures)
        check(bad in r.stderr and "SyntaxError" in r.stderr,
              "a syntax error's message names the actual script file", failures)

        boom = os.path.join(tmp, "boom.wy")
        with open(boom, "w") as f:
            f.write("x = undefined_name\n")
        r = run(boom)
        check(r.returncode == 1 and "NameError" in r.stderr, "a runtime error exits 1 with a clear message", failures)

    if failures[0]:
        print(f"\n{failures[0]} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
