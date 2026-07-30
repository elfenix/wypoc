"""Exercises the wyrm_io primitives (__open/__read/__write/__lseek/__dup2/
__close/__flush, __STDIN/__STDOUT/__STDERR) exposed into wyrm via
wyrm_io.install().

Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_eval_io.py
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc import wyrm_io
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import eval_program, expose

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "eval_io.wy")


def check(cond, msg, failures):
    # Printed via sys.__stdout__ since this test temporarily swaps
    # sys.stdout itself to test __write(__STDOUT, ...).
    print(f"{'OK  ' if cond else 'FAIL'} {msg}", file=sys.__stdout__)
    if not cond:
        failures[0] += 1


def main() -> int:
    failures = [0]

    check(wyrm_io.STDIN == 0 and wyrm_io.STDOUT == 1 and wyrm_io.STDERR == 2,
          "STDIN/STDOUT/STDERR are 0/1/2", failures)

    fake_stdout = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = fake_stdout
    wyrm_io._reset_std_handles()  # repoint handle 1 at the fake stdout

    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "io_test.txt")

            ctx: dict = {}
            wyrm_io.install(ctx)
            expose(ctx, "path", path)

            with open(SAMPLE) as f:
                src = f.read()
            tree = parse(src)
            eval_program(tree, ctx)

            check(ctx["written"].value == len("hello wyrm io"), "__write returns the byte/char count written", failures)
            check(ctx["content"].value == "hello wyrm io", "__read(fd2) reads the full file back", failures)
            check(ctx["pos"].value == 2, "__lseek(fd2, 2, 0) returns the new position", failures)
            check(ctx["partial"].value == "llo wyrm io", "__read after seeking to 2 gets the tail", failures)
            check(ctx["new_handle"].value == 42, "__dup2(fd2, 42) returns the new handle", failures)
            check(ctx["remaining_via_dup"].value == "",
                  "dup2'd handle shares fd2's file position (already at EOF)", failures)

            check(ctx["to_stdout"].value == len("printed via __STDOUT\n"),
                  "__write(__STDOUT, ...) returns the char count", failures)
            check(fake_stdout.getvalue() == "printed via __STDOUT\n",
                  "__write(__STDOUT, ...) actually reached Python's stdout", failures)

            with open(path) as f:
                on_disk = f.read()
            check(on_disk == "hello wyrm io", "the file on disk matches what __write wrote", failures)

            check(ctx["flushed"].value == 0, "__flush(fd) returns 0", failures)
            check(ctx["closed"].value == 0, "__close(fd2) returns 0", failures)
            check(ctx["close_again"].value == 0,
                  "__close on new_handle (dup2'd, already-closed file) is still a clean 0", failures)

            try:
                wyrm_io.wyrm_read(ctx["fd2"].value)
            except OSError:
                check(True, "reading a closed handle raises (bad file handle)", failures)
            else:
                check(False, "reading a closed handle should have raised", failures)
    finally:
        sys.stdout = old_stdout
        wyrm_io._reset_std_handles()

    if failures[0]:
        print(f"\n{failures[0]} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
