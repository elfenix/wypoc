"""wyrm: run a wyrm source file, the way `python script.py args...` runs a
Python one.

Usage:
    wyrm [interpreter options] script.wy [script args...]
    wyrm [interpreter options] -c "code" [args...]

Interpreter options (must come before the script path):
    -c code         eval `code` directly, instead of reading a script file
    -v, --verbose   turn on the pegen parser's verbose trace
    -h, --help      show this message and exit

Everything after the script path (or, for -c, after the code string) is left
untouched (dashes and all) and packed into a __ARGS tuple of strings,
visible to the script - the wyrm equivalent of sys.argv[1:] for a Python
script.

Installed as the `wyrm` console script (see pyproject.toml's
[project.scripts]); also runnable directly via `python -m wypoc.cli`.
"""
import sys
import traceback

from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import eval_program, expose, populate_globals


def main(argv: list = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    verbose = False
    code = None
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "-":
        opt = argv[i]
        if opt in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        elif opt in ("-v", "--verbose"):
            verbose = True
        elif opt == "-c":
            if i + 1 >= len(argv):
                print("wyrm: -c requires a string argument", file=sys.stderr)
                return 2
            code = argv[i + 1]
            i += 2
            break  # -c's own value is consumed; everything after is __ARGS, not more options
        else:
            print(f"wyrm: unknown option {opt!r}", file=sys.stderr)
            return 2
        i += 1

    if code is not None:
        src = code
        filename = "<string>"
        script_args = argv[i:]
    else:
        if i >= len(argv):
            print(__doc__.strip(), file=sys.stderr)
            return 2

        script_path = argv[i]
        script_args = argv[i + 1:]

        try:
            with open(script_path) as f:
                src = f.read()
        except OSError as e:
            print(f"wyrm: can't open file {script_path!r}: {e.strerror}", file=sys.stderr)
            return 2
        filename = script_path

    try:
        tree = parse(src, verbose=verbose, filename=filename)
    except SyntaxError as e:
        traceback.print_exception(type(e), e, None)
        return 1

    ctx: dict = {}
    populate_globals(ctx)
    expose(ctx, "__ARGS", tuple(script_args))

    try:
        eval_program(tree, ctx)
    except Exception as e:
        print(f"wyrm: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
