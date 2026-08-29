"""wyrm: run a wyrm source file, the way `python script.py args...` runs a
Python one - or, given no script at all, start the interactive REPL, the
way plain `python` does.

Usage:
    wyrm                                    (interactive REPL)
    wyrm --tui                              (interactive REPL, full screen)
    wyrm [interpreter options] script.wy [script args...]
    wyrm --vm [interpreter options] script.wy [script args...]
    wyrm [interpreter options] module.wyc [script args...]
    wyrm [interpreter options] -c "code" [args...]
    wyrm [interpreter options] -m mod::sub [script args...]
    wyrm --dump-wys [-o out.wys] module.wy
    wyrm --build-bc [-o dir] [--emit wya,c,wyc] module.wy
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
    --dump-wys      translate `module.wy` to its canonical .wys s-expression
                     form (see wypoc/wys.py), with every decorator fully
                     expanded, instead of running it; prints to stdout
                     unless -o is given. A `.wys` file is a compiled unit,
                     not source - `wyrm script.wys` runs one directly,
                     without re-parsing or re-expanding anything.
    --build-bc      compile `module.wy` to bytecode (see doc/llm-bytecode.md)
                     instead of running it, writing the three containers of
                     one image - `module.wy_a` (the ASCII listing),
                     `module.c` (C arrays) and `module.wyc` (binary) - next
                     to the source. The compiler is fail-loud: a construct
                     it does not support is an error naming the construct
                     and its line, never wrong bytes. The one exception is a
                     whole function body that will not lower - a template
                     never meant to be called, say - which becomes a stub
                     that traps if it is called, reported on stderr
    --vm            run `script.wy` on the bytecode VM instead of the tree
                     walker: compile it to a module image, cache that image
                     as `<script_dir>/__wycache__/script.wyc`, and run it -
                     and on a later run, if the cached image is newer than
                     the source, skip both the parse and the compile and
                     just run it, the way Python reuses a `__pycache__`
                     `.pyc`. An unwritable cache directory costs the cache,
                     not the run. Compiling is fail-loud the same way
                     --build-bc is
    --emit list     with --build-bc, emit only these containers: a comma-
                     separated subset of `wya,c,wyc` (default: all three)
    --strip         with --build-bc, leave out the debug section (the source
                     file name and the line table). The VM ignores it either
                     way; the disassembler and the DAP adapter are what read
                     it, so strip only what ships
    -o path         with --dump-wys, write the output to `path` instead of
                     stdout; with --build-bc, the directory to write the
                     compiled files into instead of alongside the source
    --dbus-session  connect to the D-Bus session bus before running the
                     script (needs the `dbus` extra - see wypoc/wyrm_dbus.py
                     and corelib/std/dbus.wy), so `dbus::register_object`/
                     `dbus::run()` have a live connection from the start
    -v, --verbose   turn on the pegen parser's verbose trace
    -h, --help      show this message and exit

Everything after the script path (or, for -c, after the code string) is left
untouched (dashes and all) and packed into a __ARGS tuple of strings,
visible to the script - the wyrm equivalent of sys.argv[1:] for a Python
script. --dump-wys ignores script args (a module being translated isn't run).

Running a `.wy` script (not `-c`, not a `.wys` file, which is already a
compiled unit) checks an AST cache first: `<script_dir>/__wycache__/` by
default, created automatically like Python's __pycache__, or the
`global_cache` directory from ~/.wyrm/config instead, if one is set - see
wypoc/cache.py. A hit skips parsing entirely; a miss parses as usual and
populates the cache for next time (silently skipped if the cache directory
can't be created or written to). `--vm` caches in the same directory, but
a compiled `script.wyc` image rather than a pickled tree - see the flag
above and wypoc/cache.py.

Every mode that has a script file at all - a plain run, --dump-wys,
--check - honors ~/.wyrm/config the same way the
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
import sys
import time
import traceback

from wypoc import ast_nodes as ast
from wypoc import cache as cache_mod
from wypoc import config as config_mod
from wypoc import project as project_mod
from wypoc import wyrm_dbus
from wypoc import wyrm_modules
from wypoc import wyrm_sys
from wypoc import wys
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
    """`wyrm --config name=value ...`: a mode of its own, like --dump-wys -
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
                 visited_files: set, errors: list, stack: list) -> None:
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
    check_imports(file_path, tree, roots, visited_files, errors, stack)


def check_imports(filename: str, tree, roots: "list | None",
                   visited_files: set, errors: list,
                   stack: "list | None" = None) -> None:
    """`--check`'s "recursively identify and check any imported files":
    walks `tree` (ast_nodes.Node.walk - every node, depth-first) for every
    Import/FromImport/ThreadSpawn, resolves each to a file the way actually
    running the script would, and recursively parses and walks that file
    too. `errors` collects one message per problem found rather than
    stopping at the first, so a single `--check` run surfaces everything
    wrong at once, the way a compiler's error list does.

    Two sets of bookkeeping, doing different jobs. `visited_files` is every
    file already expanded (by resolved absolute path, not by the import path
    spelling used to reach it), so a diamond import - two files both
    importing `std::io` - is only ever parsed once. `stack` is the chain of
    files currently being walked, and an import that lands on one of *those*
    has closed a cycle, which the addendum makes illegal: the walk reports it
    and does not recurse, rather than relying on `visited_files` to stop the
    recursion and letting the cycle through unremarked.

    The cycle test comes first for that reason. Everything on the stack is
    also in `visited_files`, so checking membership in the other order would
    dismiss a back edge as an already-seen file.

    `import`'s own path is ambiguous the way eval_import documents: the
    whole path may name a module, or its last segment may instead be a
    symbol pulled out of the module named by the rest of the path - resolved
    here as it is there, by trying the whole path first and falling back to
    path[:-1]. `from`-imports and `thread`-spawns have no such ambiguity
    (see ast_nodes.FromImport/ThreadSpawn) - their whole path always names a
    module."""
    if stack is None:
        stack = [(filename, _module_spelling(filename))]
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
        spelling = "::".join(path_segments)
        depth = _stack_index(stack, file_path)
        if depth is not None:
            errors.append(_cycle_message(stack, depth, spelling))
            continue
        if file_path in visited_files:
            continue
        visited_files.add(file_path)
        stack.append((file_path, spelling))
        _check_file(file_path, roots, visited_files, errors, stack)
        stack.pop()


def _module_spelling(file_path: str) -> str:
    """How to name a file in a cycle report. The root of the walk was reached
    as a path on the command line rather than as an import, so it has no
    import spelling of its own and its stem is the closest thing to one."""
    return os.path.splitext(os.path.basename(file_path))[0]


def _stack_index(stack: list, file_path: str) -> "int | None":
    for index, (seen, _spelling) in enumerate(stack):
        if seen == file_path:
            return index
    return None


def _cycle_message(stack: list, depth: int, spelling: str) -> str:
    """Go's shape: the cycle as a chain of "a imports b" lines, starting and
    ending at the module the back edge reached. `stack[depth]` is that module
    as it was first entered, so slicing from there and closing the loop with
    the edge just found spells the cycle in the order the walk found it."""
    names = [name for _path, name in stack[depth:]] + [spelling]
    lines = ["import cycle not allowed:"]
    lines += [f"\t{a} imports {b}" for a, b in zip(names, names[1:])]
    return "\n".join(lines)


def build_bytecode(tree, filename: str, output_dir, emit: "str | None",
                   debug: bool = True) -> int:
    """--build-bc: compile one module to its three bytecode containers.

    The module name is the source file's stem, which is what a single-file
    build has to assume until `import` lowering gives the compiler a real
    module path to work from.
    """
    from wypoc.compiler_bc import CompileError, compile_module

    containers = ("wya", "c", "wyc")
    if emit is not None:
        chosen = [part.strip() for part in emit.split(",") if part.strip()]
        unknown = [part for part in chosen if part not in containers]
        if unknown:
            print(f"wyrm: --emit: unknown container(s) {', '.join(unknown)}; "
                  f"expected a subset of {','.join(containers)}", file=sys.stderr)
            return 2
        containers = tuple(part for part in containers if part in chosen)

    module_name = os.path.splitext(os.path.basename(filename))[0]
    try:
        image = compile_module(tree, module_name, filename, debug=debug)
    except CompileError as e:
        print(f"wyrm: {filename}: {e}", file=sys.stderr)
        return 1

    directory = output_dir or os.path.dirname(os.path.abspath(filename))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    stem = os.path.join(directory, module_name)
    written = []
    for container in containers:
        path = f"{stem}.{'wy_a' if container == 'wya' else container}"
        if container == "wyc":
            with open(path, "wb") as f:
                f.write(image.to_wyc())
        else:
            with open(path, "w") as f:
                f.write(image.to_wya() if container == "wya" else image.to_c())
        written.append(path)
    for name, reason in image.unlowered:
        # Not swallowed: a body that would not lower became a stub that traps
        # if it is ever called, and every one of them is named here.
        print(f"wyrm: {filename}: {name}() will not compile, so calling it "
              f"traps: {reason}", file=sys.stderr)
    for path in written:
        print(f"wyrm: wrote {path}")
    return 0


def run_compiled_script(tree, filename: str, script_args: list) -> int:
    """--vm: compile a source script to a module image and run it on the
    bytecode VM, caching the image in `__wycache__/` the way a plain run
    caches the parsed tree - `foo.wy` -> `__wycache__/foo.wyc`.

    This is the compile step only; a run that found a fresh image already
    sitting in the cache never gets here (see main, which hands that
    straight to run_image_file). If the cache can't be written, the run
    still happens - the image is right here in memory - it just isn't
    saved for next time.
    """
    from wypoc import vm
    from wypoc.compiler_bc import CompileError, compile_module

    module_name = os.path.splitext(os.path.basename(filename))[0]
    try:
        image = compile_module(tree, module_name, filename)
    except CompileError as e:
        print(f"wyrm: {filename}: {e}", file=sys.stderr)
        return 1
    for name, reason in image.unlowered:
        # Same fail-loud courtesy --build-bc extends: a body that would not
        # lower is a stub that traps if it is ever called, and it is named.
        print(f"wyrm: {filename}: {name}() will not compile, so calling it "
              f"traps: {reason}", file=sys.stderr)
    blob = image.to_wyc()
    cache_mod.save_image(filename, blob)
    return run_image(vm.load(blob), filename, script_args)


def run_image(image, path: str, script_args: list) -> int:
    """Run an already-loaded image, turning what the VM raises into the
    exit codes and one-line messages a script run answers with."""
    from wypoc import vm

    wyrm_modules.set_script_root(os.path.dirname(os.path.abspath(path)))
    try:
        vm.load_module(image, path=path, argv=script_args)
    except vm.VMError as e:
        print(f"wyrm: {e}", file=sys.stderr)
        return 1
    except ExitSignal as e:
        return e.code
    except EndSignal:
        return 0
    except Exception as e:
        print(f"wyrm: {_format_runtime_error(e, path)}", file=sys.stderr)
        return 1
    return 0


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


def run_image_file(path: str, script_args: list) -> int:
    """`wyrm module.wyc`: run a compiled module image.

    The mirror of running a `.wys` file - both are compiled units rather than
    source - except that a `.wyc` is executed by the bytecode VM instead of
    being decoded back into a tree for the interpreter. The module's own
    directory is a search root, the same courtesy a `.wy` script gets, so an
    image can import a module sitting next to it.
    """
    from wypoc import vm

    try:
        with open(path, "rb") as f:
            image = vm.load(f.read())
    except OSError as e:
        print(f"wyrm: can't open file {path!r}: {e.strerror}", file=sys.stderr)
        return 2
    except vm.VMError as e:
        print(f"wyrm: {e}", file=sys.stderr)
        return 1
    return run_image(image, path, script_args)


def main(argv: list = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    verbose = False
    code = None
    dump_wys_mode = False
    dbus_session = False
    tui = None  # None: whatever ~/.wyrm/config says; -t/--no-tui decide it
    config_mode = False
    config_assignments = []
    check_mode = False
    build_bc_mode = False
    vm_mode = False
    emit_containers = None
    strip_debug = False
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
        elif opt == "--dump-wys":
            dump_wys_mode = True
        elif opt == "--check":
            check_mode = True
        elif opt == "--build-bc":
            build_bc_mode = True
        elif opt == "--vm":
            vm_mode = True
        elif opt == "--strip":
            strip_debug = True
        elif opt == "--emit":
            if i + 1 >= len(argv):
                print("wyrm: --emit requires a container list", file=sys.stderr)
                return 2
            emit_containers = argv[i + 1]
            i += 1
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
        if check_mode or code is not None or tui is not None or i < len(argv):
            print("wyrm: --config sets options and exits; it takes nothing "
                  "else to run", file=sys.stderr)
            return 2
        return write_config(config_assignments)

    if dump_wys_mode and code is not None:
        print("wyrm: --dump-wys cannot be used with -c", file=sys.stderr)
        return 2

    if check_mode and (dump_wys_mode or code is not None):
        print("wyrm: --check cannot be used with --dump-wys or -c", file=sys.stderr)
        return 2

    if build_bc_mode and (dump_wys_mode or check_mode or code is not None):
        print("wyrm: --build-bc cannot be used with --dump-wys, --check, or -c",
              file=sys.stderr)
        return 2

    if vm_mode and (dump_wys_mode or check_mode or build_bc_mode or code is not None):
        print("wyrm: --vm cannot be used with --dump-wys, --check, --build-bc, or -c",
              file=sys.stderr)
        return 2

    if (emit_containers is not None or strip_debug) and not build_bc_mode:
        print("wyrm: --emit and --strip only apply to --build-bc", file=sys.stderr)
        return 2

    if module_arg is not None and (dump_wys_mode or check_mode or code is not None):
        print("wyrm: -m cannot be used with --dump-wys, --check, or -c", file=sys.stderr)
        return 2

    if dbus_session and (dump_wys_mode or check_mode):
        print("wyrm: --dbus-session only applies to running a script, -c, or -m",
              file=sys.stderr)
        return 2

    if tui and (dump_wys_mode or check_mode or code is not None or module_arg is not None
                or i < len(argv)):
        print("wyrm: --tui starts the interactive REPL; it takes no script",
              file=sys.stderr)
        return 2

    # No script and no -c: this is an interactive session, not a usage
    # error - `wyrm` alone means the REPL, like `python` alone does.
    if (code is None and module_arg is None and not dump_wys_mode
            and not check_mode and not build_bc_mode and i >= len(argv)):
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

        if script_path.endswith(".wyc"):
            # A `.wyc` is a compiled module image, not source: it is loaded
            # and run by the bytecode VM (wypoc/vm/), not parsed. Handled
            # before the read below, which opens the file as text.
            return run_image_file(script_path, script_args)

        try:
            with open(script_path) as f:
                src = f.read()
        except OSError as e:
            print(f"wyrm: can't open file {script_path!r}: {e.strerror}", file=sys.stderr)
            return 2
        filename = script_path

        if vm_mode:
            # A cached image that is newer than its source is the whole
            # point of --vm: it skips the parse *and* the compile below,
            # exactly the way a fresh `__pycache__/foo.pyc` skips both for
            # Python. A miss falls through and compiles at the bottom.
            fresh_image = cache_mod.fresh_image_for(filename)
            if fresh_image is not None:
                return run_image_file(fresh_image, script_args)

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

    if build_bc_mode:
        return build_bytecode(tree, filename, output_path, emit_containers,
                              debug=not strip_debug)

    if vm_mode:
        if dbus_session:
            try:
                wyrm_dbus.connect_session()
            except wyrm_dbus.DbusError as e:
                print(f"wyrm: --dbus-session: {e}", file=sys.stderr)
                return 1
        return run_compiled_script(tree, filename, script_args)

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
    wyrm_sys.set_argv(script_args)

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
