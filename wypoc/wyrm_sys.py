"""Process-level primitives exposed to wyrm code as the `sys` module: the
script's command-line arguments and its environment variables. Meant to
back a `sys` module written in wyrm itself (see corelib/), the same way
wyrm_io.py backs corelib/std/io.wy.

`argv` mirrors __ARGS (see cli.py's own docstring on packing script_args
into it) - the same list, just reachable from any module's scope, not only
the top-level script's, since __ARGS is bound directly into the top ctx
rather than installed by populate_globals. cli.py calls set_argv() right
alongside its `expose(ctx, "__ARGS", ...)` so the two never disagree.
"""
import os

_argv: list = []


def set_argv(args) -> None:
    """Registers the script's own arguments (script_args in cli.py, or ()
    for the REPL/no script) - called once, alongside `expose(ctx,
    "__ARGS", ...)`, so __argv() has something to answer before any wyrm
    code (top-level or a module reached via `import`) asks for it."""
    global _argv
    _argv = list(args)


def argv() -> list:
    """__argv() -> the script's own arguments, as a fresh list each call
    (so wyrm code mutating what it gets back can't corrupt the shared
    state)."""
    return list(_argv)


def environ() -> dict:
    """__environ() -> a snapshot dict of the process environment at call
    time (like Python's dict(os.environ)), not a live view - mutating it
    doesn't change the real environment, matching wyrm's other dicts,
    which are always plain values, never handles."""
    return dict(os.environ)


def install(ctx: dict) -> None:
    from wypoc.wyrm_eval_parse_tree import expose_all

    expose_all(
        ctx,
        __argv=argv,
        __environ=environ,
    )
