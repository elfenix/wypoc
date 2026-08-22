"""wyrm: run a wyrm source file, the way `python script.py args...` runs a
Python one - or, given no script at all, start the interactive REPL, the
way plain `python` does.

Usage:
    wyrm                                    (interactive REPL)
    wyrm --tui                              (interactive REPL, full screen)
    wyrm [interpreter options] script.wy [script args...]
    wyrm [interpreter options] -c "code" [args...]
    wyrm [interpreter options] -m mod::sub [script args...]
    wyrm --compile [-o out.c] module.wy
    wyrm --compile-py out_dir script.wy
    wyrm --check script.wy
    wyrm --config name=value [--config ...]  (set an option and exit)

Interpreter options (must come before the script path):
    -Ipath          add `path` to the module search directories, ahead of
                     WYRM_PATH and ~/.wyrm/config's own `path` option (`-I
                     ../wy`, detached, also works). Repeatable and ordered:
                     `wyrm -Ia -Ib script.wy` with WYRM_PATH=c searches a,
                     then b, then c.
    -c code         eval `code` directly, instead of reading a script file
    -m mod::sub     run a module found via the same search-path resolution
                     as `import mod::sub` (WYRM_PATH, corelib, etc.) as the
                     entry script, instead of reading a script file - the
                     wyrm equivalent of `python -m pkg.mod`. Unlike a real
                     `import`, the module's own top level sees `__name__ ==
                     "__main__"`, not `"mod::sub"` - it's being run
                     directly, not imported
    -t, --tui       start the REPL in its full-screen Textual UI rather
                     than the default readline prompt
    --no-tui        use the readline prompt even if ~/.wyrm/config asks
                     for the full-screen one
    --check         a basic sanity check, not a run: parse `script.wy`,
                     then recursively resolve and parse every file it
                     (transitively) imports (`import`, `from ... import`,
                     `thread`), without evaluating any of it. Prints how
                     long the whole parse took on success; on failure,
                     every problem found (a syntax error, or an import that
                     doesn't resolve to a file), not just the first, and
                     exits nonzero.
    --no-config     suppress every config file this run would otherwise
                     read - ~/.wyrm/config *and* any project .wyrm/config -
                     so the session runs on plain built-in defaults, with
                     no project root scan and no preamble. Applies to every
                     mode; cannot be combined with --config, which exists
                     to write to the file this suppresses.
    --config n=v    set option `n` to `v` in ~/.wyrm/config (creating the
                     file and its directory if needed) and exit, running
                     nothing - the command-line form of the REPL's
                     `:set config n`. Repeatable; `--config n` alone turns
                     a boolean option on, and bare `--config` lists them
    --compile       translate `module.wy` (must `import native`) to C source
                     targeting the real wyrm VM calling convention, instead
                     of running it; prints to stdout unless -o is given
    --dump-wys      translate `module.wy` to its canonical .wys s-expression
                     form (see wypoc/wys.py), with every decorator fully
                     expanded, instead of running it; prints to stdout
                     unless -o is given. A `.wys` file is a compiled unit,
                     not source - `wyrm script.wys` runs one directly,
                     without re-parsing or re-expanding anything.
    -o path         with --compile or --dump-wys, write the output to
                     `path` instead of stdout
    --dbus-session  connect to the D-Bus session bus before running the
                     script (needs the `dbus` extra - see wypoc/wyrm_dbus.py
                     and corelib/std/dbus.wy), so `dbus::register_object`/
                     `dbus::run()` have a live connection from the start
    --compile-py out_dir
                     translate `script.wy`, and everything it transitively
                     imports, into a tree of async Python source files
                     under `out_dir` (a `wyrm` namespace package mirroring
                     wyrm module-space, plus `script`'s own compiled form
                     as a sibling of it) instead of running it - see
                     wypoc/compiler_py/__init__.py. Unlike --compile, no
                     `import native`-style marker is required.
    --run-py out_dir entry.py [script args...]
                     runs Python source a previous `--compile-py out_dir
                     script.wy` produced: `entry.py` is that run's own
                     `script.py` (a bare filename resolves against
                     `out_dir`). Wires up PYTHONPATH so the generated
                     `wyrm.*` package and wypoc's own runtime
                     (wypoc.compiler_py.engine, which every generated
                     module imports) are importable, then runs it with
                     this same Python interpreter.
    -v, --verbose   turn on the pegen parser's verbose trace
    -h, --help      show this message and exit

Everything after the script path (or, for -c, after the code string) is left
untouched (dashes and all) and packed into a __ARGS tuple of strings,
visible to the script - the wyrm equivalent of sys.argv[1:] for a Python
script. --compile/--compile-py ignore script args (a module being compiled
isn't run).

Running a `.wy` script (not `-c`, not a `.wys` file, which is already a
compiled unit) checks an AST cache first: `<script_dir>/__wycache__/` by
default, created automatically like Python's __pycache__, or the
`global_cache` directory from ~/.wyrm/config instead, if one is set - see
wypoc/cache.py. A hit skips parsing entirely; a miss parses as usual and
populates the cache for next time (silently skipped if the cache directory
can't be created or written to).

Every mode that has a script file at all - a plain run, --compile,
--compile-py, --dump-wys, --check - honors ~/.wyrm/config the same way the
REPL and -c already did: its options apply, and a project's own
`.wyrm/config` overlays them (most usefully `path`, extra module search
directories) if a `.wyrm/` directory turns up scanning upward from the
*script's own directory* through each parent, stopping at the first one
that has it (or at the filesystem root, if none do) - see project.py's
find_project_root. `--no-config` skips all of this and runs on plain
defaults instead.

Installed as the `wyrm` console script (see pyproject.toml's
[project.scripts]); also runnable directly via `python -m wypoc.cli`.
"""
import os
import subprocess
import sys
import time
import traceback

from wypoc import ast_nodes as ast
from wypoc import cache as cache_mod
from wypoc import compiler_py
from wypoc import config as config_mod
from wypoc import project as project_mod
from wypoc import wyrm_dbus
from wypoc import wyrm_modules
from wypoc import wys
from wypoc.compiler_c import CompileError, compile_module
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import (
    EndSignal, ExitSignal, Scope, WyrmLocatedError, eval_program,
    expand_decorators, expose, populate_globals,
)


def _format_runtime_error(e: Exception, filename: "str | None") -> str:
    """`wyrm: ...` - the one-line message an uncaught exception out of
    eval_program gets printed as. A shadowed exception (WyrmLocatedError -
    see wyrm_eval_parse_tree.py) already renders its original type/message
    plus its wyrm source location via its own __str__ - this just also
    prefixes the script's filename, `python foo.py` traceback style,
    without stuttering the shadow class's own (dynamically-built, not very
    readable - e.g. "LocatedTypeError") type name in front of that.
    Anything else (a signal/exception the evaluator never shadows) falls
    back to the plain `Type: message` cli.py always printed."""
    if isinstance(e, WyrmLocatedError):
        where = f"{filename}:" if filename else ""
        return f"{where}{e}"
    return f"{type(e).__name__}: {e}"


def write_config(assignments: list) -> int:
    """`wyrm --config name=value ...`: a mode of its own, like --compile -
    it writes the options into ~/.wyrm/config and exits without running
    anything. Bare `--config` (no assignment at all) lists what can be set
    and what the file says now."""
    if not assignments:
        options = config_mod.load()
        print(f"config file: {config_mod.config_path()}")
        for name in sorted(config_mod.OPTIONS):
            value = "true" if options[name] else "false"
            print(f"  {name} = {value}    {config_mod.DESCRIPTIONS[name]}")
        return 0
    for text in assignments:
        try:
            name, value = config_mod.parse_assignment(text)
            config_mod.set_option(name, value)
        except config_mod.ConfigError as e:
            print(f"wyrm: {e}", file=sys.stderr)
            return 2
        print(f"{name} = {str(value).lower() if isinstance(value, bool) else value}"
              f"  ({config_mod.config_path()})")
    return 0


def _check_file(file_path: str, roots: "list | None",
                 visited_files: set, errors: list) -> None:
    """Parses one imported file for `--check` (cache-assisted, same as any
    other module load - see cache.py) and recurses into whatever *it*
    imports. Failures - can't open it, doesn't parse - are appended to
    `errors` and stop that one branch, not the whole check."""
    try:
        with open(file_path, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        errors.append(f"{file_path}: can't open: {e.strerror}")
        return
    tree = cache_mod.load(file_path)
    if tree is None:
        try:
            tree = parse(src, filename=file_path)
        except SyntaxError as e:
            errors.append(f"{file_path}: {e}")
            return
        cache_mod.save(file_path, tree)
    check_imports(file_path, tree, roots, visited_files, errors)


def check_imports(filename: str, tree, roots: "list | None",
                   visited_files: set, errors: list) -> None:
    """`--check`'s "recursively identify and check any imported files":
    walks `tree` (ast_nodes.Node.walk - every node, depth-first) for every
    Import/FromImport/ThreadSpawn, resolves each to a file the way actually
    running the script would, and recursively parses and walks that file
    too. `visited_files` is shared across the whole recursion (by resolved
    absolute file path, not by the import path spelling used to reach it),
    so a diamond import - two files both importing `std::io`, or a circular
    pair - is only ever parsed once. `errors` collects one message per
    problem found rather than stopping at the first, so a single `--check`
    run surfaces everything wrong at once, the way a compiler's error list
    does.

    `import`'s own path is ambiguous the way eval_import documents: the
    whole path may name a module, or its last segment may instead be a
    symbol pulled out of the module named by the rest of the path - resolved
    here as it is there, by trying the whole path first and falling back to
    path[:-1]. `from`-imports and `thread`-spawns have no such ambiguity
    (see ast_nodes.FromImport/ThreadSpawn) - their whole path always names a
    module."""
    for node in tree.walk():
        if isinstance(node, ast.Import):
            path_segments = list(node.path)
            resolved = wyrm_modules.resolve_module_file(path_segments, roots)
            if resolved is None and len(path_segments) > 1:
                resolved = wyrm_modules.resolve_module_file(path_segments[:-1], roots)
        elif isinstance(node, (ast.FromImport, ast.ThreadSpawn)):
            path_segments = list(node.path)
            resolved = wyrm_modules.resolve_module_file(path_segments, roots)
        else:
            continue
        if resolved is None:
            errors.append(f"{filename}: no module named "
                          f"{'::'.join(path_segments)!r}")
            continue
        file_path, _is_package = resolved
        if file_path in visited_files:
            continue
        visited_files.add(file_path)
        _check_file(file_path, roots, visited_files, errors)


def run_compiled_py(python_dir: str, script_path: str, script_args: list) -> int:
    """`wyrm --run-py <python_dir> <script.py> [args...]`: runs Python
    source a previous `--compile-py <python_dir> script.wy` produced.

    The one thing that source needs to actually start is that `wypoc`
    itself - specifically `wypoc.compiler_py.engine`, which every generated
    module imports (see compiler_py/module.py's PREAMBLE) - be importable
    by whichever interpreter runs it; the generated tree is not a
    standalone artifact the way a real AOT compiler's output would be. This
    runs it with `sys.executable`, the same interpreter `wyrm` itself is
    running under, which already satisfies that by construction, and
    additionally puts `python_dir` and wypoc's own package root onto
    PYTHONPATH as a belt-and-braces measure - the same trick
    test_compiler_py.py's run_py_source uses for an uninstalled dev
    checkout, generalized here to work whether wypoc is pip-installed or
    run from a source tree."""
    if not os.path.isdir(python_dir):
        print(f"wyrm: --run-py: not a directory: {python_dir!r}", file=sys.stderr)
        return 2

    if not os.path.isfile(script_path):
        candidate = os.path.join(python_dir, script_path)
        if os.path.isfile(candidate):
            script_path = candidate
        else:
            print(f"wyrm: --run-py: can't find script {script_path!r} "
                  f"(also tried {candidate!r})", file=sys.stderr)
            return 2

    import wypoc

    wypoc_root = os.path.dirname(os.path.dirname(os.path.abspath(wypoc.__file__)))
    path_entries = [os.path.abspath(python_dir), wypoc_root]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        path_entries.append(existing)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(path_entries)

    result = subprocess.run(
        [sys.executable, os.path.abspath(script_path), *script_args], env=env,
    )
    return result.returncode


def run_repl(tui: bool, options: "dict | None" = None,
             project_root: "str | None" = None,
             preamble: "str | None" = None) -> int:
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

    session = Session(options=options, project_root=project_root, preamble=preamble)
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
    dump_wys_mode = False
    dbus_session = False
    compile_py_mode = False
    compile_py_dir = None
    run_py_mode = False
    run_py_dir = None
    run_py_script = None
    tui = None  # None: whatever ~/.wyrm/config says; -t/--no-tui decide it
    config_mode = False
    config_assignments = []
    check_mode = False
    no_config = False
    include_paths = []
    output_path = None
    module_arg = None
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
        elif opt == "--dump-wys":
            dump_wys_mode = True
        elif opt == "--check":
            check_mode = True
        elif opt == "--no-config":
            no_config = True
        elif opt.startswith("-I"):
            # `-Ipath` (gcc-style, attached) or `-I path` (detached); either
            # way it stacks - `-Ia -Ib` adds both, in that order (see
            # wyrm_modules.set_include_paths).
            if opt == "-I":
                if i + 1 >= len(argv):
                    print("wyrm: -I requires a path argument", file=sys.stderr)
                    return 2
                include_paths.append(argv[i + 1])
                i += 1
            else:
                include_paths.append(opt[2:])
        elif opt == "--dbus-session":
            dbus_session = True
        elif opt == "--compile-py":
            if i + 1 >= len(argv):
                print("wyrm: --compile-py requires an output directory argument",
                      file=sys.stderr)
                return 2
            compile_py_mode = True
            compile_py_dir = argv[i + 1]
            i += 1
        elif opt == "--run-py":
            if i + 2 >= len(argv):
                print("wyrm: --run-py requires <python_dir> <script.py> arguments",
                      file=sys.stderr)
                return 2
            run_py_mode = True
            run_py_dir = argv[i + 1]
            run_py_script = argv[i + 2]
            i += 3
            break  # everything after is the compiled script's own args, not more options
        elif opt in ("-t", "--tui"):
            tui = True
        elif opt == "--no-tui":
            tui = False
        elif opt == "--config":
            config_mode = True
            # `--config` may be the whole command (list the options), so its
            # argument is optional - but a following `-something` is the next
            # option, not this one's value.
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                config_assignments.append(argv[i + 1])
                i += 1
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
        elif opt == "-m":
            if i + 1 >= len(argv):
                print("wyrm: -m requires a module path argument", file=sys.stderr)
                return 2
            module_arg = argv[i + 1]
            i += 2
            break  # -m's own value is consumed; everything after is __ARGS, not more options
        else:
            print(f"wyrm: unknown option {opt!r}", file=sys.stderr)
            return 2
        i += 1

    # --no-config takes effect before anything below reads a config file -
    # write_config included, so `--no-config --config ...` (caught just
    # below) never gets a chance to touch ~/.wyrm/config either.
    config_mod.set_disabled(no_config)
    # -I applies to every mode (REPL included - see repl.Session.__init__),
    # so it's registered once, up front, rather than threaded through each
    # mode branch's own set_extra_search_paths call.
    wyrm_modules.set_include_paths(include_paths)

    if no_config and config_mode:
        print("wyrm: --no-config suppresses the config file; --config writes "
              "to it, so the two can't be used together", file=sys.stderr)
        return 2

    if config_mode:
        # A mode of its own: set the options, say so, and stop. Combining it
        # with something to run would leave it ambiguous whether the new
        # settings applied to that run, so it's refused rather than guessed.
        if compile_mode or check_mode or code is not None or tui is not None or i < len(argv):
            print("wyrm: --config sets options and exits; it takes nothing "
                  "else to run", file=sys.stderr)
            return 2
        return write_config(config_assignments)

    if compile_mode and code is not None:
        print("wyrm: --compile cannot be used with -c", file=sys.stderr)
        return 2

    if dump_wys_mode and (compile_mode or code is not None):
        print("wyrm: --dump-wys cannot be used with --compile or -c", file=sys.stderr)
        return 2

    if compile_py_mode and (compile_mode or dump_wys_mode or code is not None):
        print("wyrm: --compile-py cannot be used with --compile, --dump-wys, or -c",
              file=sys.stderr)
        return 2

    if run_py_mode and (compile_mode or dump_wys_mode or compile_py_mode or code is not None):
        print("wyrm: --run-py cannot be used with --compile, --dump-wys, "
              "--compile-py, or -c", file=sys.stderr)
        return 2

    if check_mode and (compile_mode or dump_wys_mode or compile_py_mode or run_py_mode
                        or code is not None):
        print("wyrm: --check cannot be used with --compile, --dump-wys, "
              "--compile-py, --run-py, or -c", file=sys.stderr)
        return 2

    if module_arg is not None and (compile_mode or dump_wys_mode or compile_py_mode
                                    or run_py_mode or check_mode or code is not None):
        print("wyrm: -m cannot be used with --compile, --dump-wys, "
              "--compile-py, --run-py, --check, or -c", file=sys.stderr)
        return 2

    if dbus_session and (compile_mode or dump_wys_mode or compile_py_mode or run_py_mode
                          or check_mode):
        print("wyrm: --dbus-session only applies to running a script, -c, or -m",
              file=sys.stderr)
        return 2

    if tui and (compile_mode or dump_wys_mode or compile_py_mode or run_py_mode
                or check_mode or code is not None or module_arg is not None or i < len(argv)):
        print("wyrm: --tui starts the interactive REPL; it takes no script",
              file=sys.stderr)
        return 2

    if run_py_mode:
        return run_compiled_py(run_py_dir, run_py_script, argv[i:])

    # No script and no -c: this is an interactive session, not a usage
    # error - `wyrm` alone means the REPL, like `python` alone does.
    if (code is None and module_arg is None and not compile_mode and not dump_wys_mode
            and not compile_py_mode and not check_mode and i >= len(argv)):
        # The config file supplies the session's starting options - global,
        # then a project's own if one is found (see project.py) - and the
        # `tui` one among them stands in for --tui when neither -t nor
        # --no-tui said otherwise.
        options, project_root = project_mod.load_options()
        preamble = project_mod.load_preamble(project_root)
        return run_repl(options["tui"] if tui is None else tui, options,
                        project_root, preamble)

    if code is not None:
        src = code
        filename = "<string>"
        script_args = argv[i:]
    else:
        if module_arg is not None:
            # `-m mod::sub`: resolve it the same way `import mod::sub` would -
            # there's no script file on the command line yet to scan upward
            # from for a project root, so (like the `-c` case below) options
            # are loaded from the working directory first, just to learn the
            # `path` config's extra search roots before resolving the module.
            options, project_root = project_mod.load_options()
            wyrm_modules.set_extra_search_paths(
                project_mod.resolve_search_paths(options.get("path", ""), project_root))
            resolved = wyrm_modules.resolve_module_file(module_arg.split("::"))
            if resolved is None:
                print(f"wyrm: -m: no module named {module_arg!r} (searched: "
                      f"{', '.join(wyrm_modules.search_paths())})", file=sys.stderr)
                return 2
            script_path, _is_package = resolved
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

        # Every mode that runs, compiles, or checks an actual script file -
        # not just the REPL and -c, which already did this - honors
        # ~/.wyrm/config and a project's own .wyrm/config: scanning upward
        # from the *script's* directory (not the working directory) for a
        # .wyrm/ directory, per find_project_root, and feeding its `path`
        # option to the module search before anything below tries to
        # resolve an import. --no-config (config_mod.set_disabled, above)
        # short-circuits this to plain defaults and no project root at all.
        options, project_root = project_mod.load_options(
            start=os.path.dirname(os.path.abspath(filename)))
        wyrm_modules.set_extra_search_paths(
            project_mod.resolve_search_paths(options.get("path", ""), project_root))

    if compile_py_mode:
        wyrm_modules.set_script_root(os.path.dirname(os.path.abspath(filename)))
        try:
            compiler_py.compile_tree(filename, compile_py_dir)
        except compiler_py.CompileError as e:
            print(f"wyrm: compile error: {e}", file=sys.stderr)
            return 1
        return 0

    # A `.wys` file is already a compiled unit (see wypoc/wys.py) - loading
    # it means decoding its s-expression back into an ast.Program, not
    # parsing it as wyrm source. A plain `.wy` script, on the other hand,
    # checks the AST cache first (cache.py) - a hit skips parsing entirely;
    # a miss (including a `-c` snippet, which has no file to cache against)
    # parses as usual and, for a real script, populates the cache for next
    # time.
    check_start = time.perf_counter()  # only read by --check, below
    try:
        if code is None and filename.endswith(".wys"):
            tree = wys.loads(src, filename=filename)
        elif code is None:
            tree = cache_mod.load(filename)
            if tree is None:
                tree = parse(src, verbose=verbose, filename=filename)
                cache_mod.save(filename, tree)
        else:
            tree = parse(src, verbose=verbose, filename=filename)
    except SyntaxError as e:
        traceback.print_exception(type(e), e, None)
        return 1
    except wys.WysError as e:
        print(f"wyrm: {e}", file=sys.stderr)
        return 1

    # A script's own directory is a search root, so a module sitting next to
    # it - a decorator library, say - is importable with no WYRM_PATH set.
    if code is None:
        wyrm_modules.set_script_root(os.path.dirname(os.path.abspath(filename)))

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

    if dump_wys_mode:
        ctx = Scope()
        populate_globals(ctx)
        try:
            expand_decorators(tree, ctx)
            wys_src = wys.dumps(tree)
        except Exception as e:
            print(f"wyrm: dump-wys error: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        if output_path:
            with open(output_path, "w") as f:
                f.write(wys_src)
        else:
            sys.stdout.write(wys_src)
        return 0

    if check_mode:
        # A basic sanity check, not a real run: parse the script, then
        # recursively parse everything it (transitively) imports, without
        # evaluating a line of any of it - see check_imports. `roots=None`
        # in every resolve_module_file call inside that recursion means
        # "compute wyrm_modules.search_paths() fresh each time", which is
        # what we want since set_script_root was just called above.
        errors: list = []
        check_imports(filename, tree, None, set(), errors)
        elapsed = time.perf_counter() - check_start
        if errors:
            for message in errors:
                print(f"wyrm: {message}", file=sys.stderr)
            print(f"wyrm: check failed: {len(errors)} problem(s) found "
                  f"in {elapsed:.3f}s", file=sys.stderr)
            return 1
        print(f"wyrm: check OK - {filename} parsed cleanly in {elapsed:.3f}s")
        return 0

    # `-c` has no script of its own to scan upward from, but it should still
    # behave like a session started in the working directory: the project's
    # `path` config (on top of ~/.wyrm/config's) becomes extra search roots,
    # and `.wyrm/preamble.wy`, if there is one, runs first - same as the REPL
    # (see project.py, repl.Session.__init__).
    preamble_tree = None
    if code is not None:
        options, project_root = project_mod.load_options()
        wyrm_modules.set_extra_search_paths(
            project_mod.resolve_search_paths(options.get("path", ""), project_root))
        wyrm_modules.set_script_root(project_root or os.getcwd())
        preamble_src = project_mod.load_preamble(project_root)
        if preamble_src is not None:
            try:
                preamble_tree = parse(preamble_src,
                                      filename=project_mod.preamble_path(project_root))
            except SyntaxError as e:
                traceback.print_exception(type(e), e, None)
                return 1

    ctx = Scope()
    populate_globals(ctx)
    expose(ctx, "__ARGS", tuple(script_args))

    if dbus_session:
        try:
            wyrm_dbus.connect_session()
        except wyrm_dbus.DbusError as e:
            print(f"wyrm: --dbus-session: {e}", file=sys.stderr)
            return 1

    try:
        if preamble_tree is not None:
            eval_program(preamble_tree, ctx)
        eval_program(tree, ctx)
    except ExitSignal as e:
        # `exit()` - stop the whole program right now with its given code,
        # tearing down every `thread`-spawned process on the way out (see
        # wyrm_remote.terminate_all's docstring - otherwise one blocked on
        # its own empty call_q would leave the interpreter hanging at
        # shutdown, waiting to join a non-daemon child that's never coming
        # back).
        from wypoc import wyrm_remote
        wyrm_remote.terminate_all()
        return e.code
    except EndSignal:
        # `end()` - stop cleanly, *without* tearing down any spawned
        # processes - the one way main opts out of the implicit exit()
        # below, leaving them to keep serving their own queues.
        return 0
    except Exception as e:
        # An uncaught error also ends the script - same implicit-exit
        # teardown as falling off the end below, so a spawned process
        # doesn't outlive a script that crashed rather than finished.
        from wypoc import wyrm_remote
        wyrm_remote.terminate_all()
        print(f"wyrm: {_format_runtime_error(e, filename)}", file=sys.stderr)
        return 1

    # Falling off the end of the script's own top level, with no explicit
    # `end()`/`exit()`, is itself an implicit `exit()` - see EndSignal's
    # docstring - so it tears down spawned processes the same way the
    # explicit form above does.
    from wypoc import wyrm_remote
    wyrm_remote.terminate_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
