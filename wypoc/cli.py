"""wyrm: run a wyrm source file, the way `python script.py args...` runs a
Python one - or, given no script at all, start the interactive REPL, the
way plain `python` does.

Usage:
    wyrm                                    (interactive REPL)
    wyrm --tui                              (interactive REPL, full screen)
    wyrm [interpreter options] script.wy [script args...]
    wyrm [interpreter options] -c "code" [args...]
    wyrm --compile [-o out.c] module.wy

Interpreter options (must come before the script path):
    -c code         eval `code` directly, instead of reading a script file
    -t, --tui       start the REPL in its full-screen Textual UI rather
                     than the default readline prompt
    --compile       translate `module.wy` (must `import native`) to C source
                     targeting the real wyrm VM calling convention, instead
                     of running it; prints to stdout unless -o is given
    -o path         with --compile, write the generated C to `path` instead
                     of stdout
    -v, --verbose   turn on the pegen parser's verbose trace
    -h, --help      show this message and exit

Everything after the script path (or, for -c, after the code string) is left
untouched (dashes and all) and packed into a __ARGS tuple of strings,
visible to the script - the wyrm equivalent of sys.argv[1:] for a Python
script. --compile ignores script args (a module being compiled isn't run).

Installed as the `wyrm` console script (see pyproject.toml's
[project.scripts]); also runnable directly via `python -m wypoc.cli`.
"""
import os
import sys
import traceback

from wypoc import wyrm_modules
from wypoc.compiler_c import CompileError, compile_module
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import Scope, eval_program, expose, populate_globals


def run_repl(tui: bool) -> int:
    """Starts an interactive session. The full-screen UI needs both a real
    terminal and `textual`, so `--tui` falls back to the readline prompt
    (with a note saying why) rather than failing outright - the REPL still
    works in a pipe, over a dumb terminal, or with only the `rich` half of
    the optional dependencies installed."""
    try:
        from wypoc.repl import Session, run_readline
    except ImportError as e:
        print(f"wyrm: the REPL needs the 'repl' extra installed "
              f"(pip install 'wypoc[repl]'): {e}", file=sys.stderr)
        return 2

    session = Session()
    if tui:
        reason = None
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            reason = "not running on a terminal"
        else:
            try:
                from wypoc.repl_tui import run_tui
            except ImportError as e:
                reason = f"textual is not available ({e})"
        if reason is None:
            return run_tui(session)
        print(f"wyrm: --tui unavailable ({reason}); using the readline REPL",
              file=sys.stderr)
    return run_readline(session)


def main(argv: list = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    verbose = False
    code = None
    compile_mode = False
    tui = False
    output_path = None
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "-":
        opt = argv[i]
        if opt in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        elif opt in ("-v", "--verbose"):
            verbose = True
        elif opt == "--compile":
            compile_mode = True
        elif opt in ("-t", "--tui"):
            tui = True
        elif opt == "-o":
            if i + 1 >= len(argv):
                print("wyrm: -o requires a path argument", file=sys.stderr)
                return 2
            output_path = argv[i + 1]
            i += 1
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

    if compile_mode and code is not None:
        print("wyrm: --compile cannot be used with -c", file=sys.stderr)
        return 2

    if tui and (compile_mode or code is not None or i < len(argv)):
        print("wyrm: --tui starts the interactive REPL; it takes no script",
              file=sys.stderr)
        return 2

    # No script and no -c: this is an interactive session, not a usage
    # error - `wyrm` alone means the REPL, like `python` alone does.
    if code is None and not compile_mode and i >= len(argv):
        return run_repl(tui)

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

    if compile_mode:
        module_name = os.path.splitext(os.path.basename(filename))[0]
        try:
            c_src = compile_module(tree, module_name)
        except CompileError as e:
            print(f"wyrm: compile error: {e}", file=sys.stderr)
            return 1
        if output_path:
            with open(output_path, "w") as f:
                f.write(c_src)
        else:
            sys.stdout.write(c_src)
        return 0

    # A script's own directory is a search root, so a module sitting next to
    # it - a decorator library, say - is importable with no WYRM_PATH set.
    if code is None:
        wyrm_modules.set_script_root(os.path.dirname(os.path.abspath(filename)))

    ctx = Scope()
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
