"""Exercises the wyrm module/import system: WYRM_PATH-based resolution
(default corelib/), package __init__.wy loading, `::`-qualified access,
from-import, and using (bulk + aliased single-name).

Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_eval_modules.py
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc import wyrm_io, wyrm_modules
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import (
    Class,
    ClassInstance,
    Module,
    Variable,
    clear_module_cache,
    eval_program,
)

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "eval_modules.wy")


def check(cond, msg, failures):
    print(f"{'OK  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures[0] += 1


def main() -> int:
    failures = [0]

    check(
        wyrm_modules.DEFAULT_COREPATH.endswith(os.path.join("wyrm", "corelib")),
        f"default corelib path is derived from wypoc's location: {wyrm_modules.DEFAULT_COREPATH}",
        failures,
    )

    clear_module_cache()
    with open(SAMPLE) as f:
        src = f.read()
    tree = parse(src)
    ctx: dict = {}

    # println (corelib/std/io.wy) does real I/O - __write(__STDOUT, ...) -
    # rather than returning a string, so capture stdout to check what it
    # actually wrote. This also exercises populate_globals(): std::io's own
    # module scope (a fresh dict per import_module call) needs __write/
    # __STDOUT seeded into it directly, the same way the top-level script's
    # scope gets them from cli.py - without that, importing any module that
    # calls a __-primitive raises NameError (see populate_globals's
    # docstring in wyrm_eval_parse_tree.py for why).
    fake_stdout = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = fake_stdout
    wyrm_io._reset_std_handles()
    try:
        eval_program(tree, ctx)
    finally:
        sys.stdout = old_stdout
        wyrm_io._reset_std_handles()

    check(fake_stdout.getvalue() == "hidirectbulkaliased",
          f"println() (via all four import forms) actually reached stdout: {fake_stdout.getvalue()!r}", failures)
    for name in ("greeted", "direct", "bulk", "aliased"):
        check(ctx[name].value is None, f"println() returns None (it's I/O, not string-building): {name}", failures)

    shapes = ctx["shapes"].value
    std = ctx["std"].value
    check(isinstance(shapes, Module) and shapes.name == "shapes", "import shapes -> a single-file module", failures)
    check(isinstance(std, Module) and std.is_package, "import std::io -> std is a package (has __init__.wy)", failures)
    check("io" in std.submodules, "importing std::io registers io as a submodule of std", failures)

    c = ctx["c"].value
    check(isinstance(c, ClassInstance) and c.cls.name == "Circle", "new shapes::Circle() works via qualified type path", failures)
    check(isinstance(ctx["c_area_fn"].value, Class), "shapes::Circle (via `::`) resolves to the Class object", failures)

    # WYRM_PATH override: a module in a temp dir should resolve ahead of/
    # alongside the default corelib fallback.
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "extra.wy"), "w") as f:
            f.write("value = 123\n")
        old_env = os.environ.get("WYRM_PATH")
        os.environ["WYRM_PATH"] = tmp
        try:
            paths = wyrm_modules.search_paths()
            check(paths[0] == tmp and paths[-1] == wyrm_modules.DEFAULT_COREPATH,
                  "WYRM_PATH entries are searched before the corelib fallback", failures)

            clear_module_cache()
            ctx2: dict = {}
            eval_program(parse("import extra\nv = extra::value\n"), ctx2)
            check(ctx2["v"].value == 123, "a module found via WYRM_PATH imports correctly", failures)

            # corelib is still reachable even with WYRM_PATH set (it's a fallback, not a replacement).
            clear_module_cache()
            ctx3: dict = {}
            eval_program(parse("import shapes\n"), ctx3)
            check(isinstance(ctx3["shapes"].value, Module), "corelib is still reachable as a fallback", failures)
        finally:
            if old_env is None:
                del os.environ["WYRM_PATH"]
            else:
                os.environ["WYRM_PATH"] = old_env

    if failures[0]:
        print(f"\n{failures[0]} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
