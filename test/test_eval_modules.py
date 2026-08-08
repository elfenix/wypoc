"""Exercises the wyrm module/import system: WYRM_PATH-based resolution
(default corelib/), package __init__.wy loading, `::`-qualified access,
from-import, and import's wildcard/aliased-single-name forms (the old
`using` keyword's replacement)."""
import io
import os
import sys

import pytest

from conftest import eval_sample
from wypoc import wyrm_io, wyrm_modules
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import (
    Class,
    ClassInstance,
    Module,
    clear_module_cache,
    eval_program,
)


def test_default_corelib_path():
    assert wyrm_modules.DEFAULT_COREPATH.endswith(os.path.join("wypoc", "corelib")), (
        "default corelib path is derived from wypoc's repo location"
    )


@pytest.fixture(scope="module")
def ctx():
    clear_module_cache()

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
    result = {}
    try:
        eval_sample("eval_modules.wy", result)
    finally:
        result["_stdout"] = fake_stdout.getvalue()
        sys.stdout = old_stdout
        wyrm_io._reset_std_handles()
    return result


def test_println_reaches_stdout(ctx):
    assert ctx["_stdout"] == "hi\ndirect\nbulk\naliased\n", (
        "println() (via all four import forms) actually reached stdout"
    )


@pytest.mark.parametrize("name", ["greeted", "direct", "bulk", "aliased"])
def test_println_returns_none(ctx, name):
    assert ctx[name].value is None, "println() returns None (it's I/O, not string-building)"


def test_module_and_package_objects(ctx):
    shapes = ctx["shapes"].value
    std = ctx["std"].value
    assert isinstance(shapes, Module) and shapes.name == "shapes", "import shapes -> a single-file module"
    assert isinstance(std, Module) and std.is_package, "import std::io -> std is a package (has __init__.wy)"
    assert "io" in std.submodules, "importing std::io registers io as a submodule of std"


def test_qualified_type_path(ctx):
    c = ctx["c"].value
    assert isinstance(c, ClassInstance) and c.cls.name == "Circle", (
        "shapes::Circle() works via qualified type path"
    )
    assert isinstance(ctx["c_area_fn"].value, Class), "shapes::Circle (via `::`) resolves to the Class object"


def test_wyrm_path_override(tmp_path, monkeypatch):
    (tmp_path / "extra.wy").write_text("value := 123\n")
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))

    paths = wyrm_modules.search_paths()
    assert paths[0] == str(tmp_path) and paths[-1] == wyrm_modules.DEFAULT_COREPATH, (
        "WYRM_PATH entries are searched before the corelib fallback"
    )

    clear_module_cache()
    ctx2: dict = {}
    eval_program(parse("import extra\nv := extra::value\n"), ctx2)
    assert ctx2["v"].value == 123, "a module found via WYRM_PATH imports correctly"

    # corelib is still reachable even with WYRM_PATH set (it's a fallback, not a replacement).
    clear_module_cache()
    ctx3: dict = {}
    eval_program(parse("import shapes\n"), ctx3)
    assert isinstance(ctx3["shapes"].value, Module), "corelib is still reachable as a fallback"
