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
    Scope,
    SignalValue,
    clear_module_cache,
    eval_program,
    populate_globals,
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


# --- __dynamic__ (see import_module's docstring) ---------------------------
# The stopgap escape hatch for `static` vs. plain `import`: this interpreter
# has no real load/run split, so `__dynamic__` is exposed to a module's own
# top level (false for `import static`, true otherwise) as something the
# module can check by hand.

def test_dynamic_flag_is_true_for_a_plain_import(tmp_path, monkeypatch):
    (tmp_path / "flagged.wy").write_text("x := __dynamic__\n")
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    clear_module_cache()
    ctx: dict = {}
    eval_program(parse("import flagged\n"), ctx)
    mod = ctx["flagged"].value
    assert mod.ctx["x"].value is True, "seen as true from the module's own top level"
    assert mod.ctx["__dynamic__"].value is True


def test_dynamic_flag_is_false_for_a_static_import(tmp_path, monkeypatch):
    (tmp_path / "flagged.wy").write_text("x := __dynamic__\n")
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    clear_module_cache()
    ctx: dict = {}
    eval_program(parse("import static flagged\n"), ctx)
    mod = ctx["flagged"].value
    assert mod.ctx["x"].value is False, "seen as false from the module's own top level"
    assert mod.ctx["__dynamic__"].value is False


def test_a_static_then_plain_import_runs_the_module_once_but_refreshes_the_flag(
        tmp_path, monkeypatch):
    (tmp_path / "flagged.wy").write_text("x := __dynamic__\n")
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    clear_module_cache()
    ctx: dict = {}
    eval_program(parse("import static flagged\nimport flagged\n"), ctx)
    mod = ctx["flagged"].value
    assert mod.ctx["x"].value is False, (
        "the module only ran once, during the static import - the later "
        "plain import is a cache hit, not a re-run, so top-level code never "
        "saw __dynamic__ flip to true"
    )
    assert mod.ctx["__dynamic__"].value is True, (
        "but __dynamic__ itself is refreshed on every import, cached or "
        "not, so code that reads it lazily (inside a fn, say) sees the "
        "latest import's kind rather than the one that actually ran the body"
    )


# --- __name__ (mirrors Python's __name__, always fully qualified) ----------

def test_top_level_script_sees_dunder_main():
    ctx: dict = {}
    populate_globals(ctx)
    assert ctx["__name__"].value == "__main__"


def test_imported_module_sees_its_own_fully_qualified_name(tmp_path, monkeypatch):
    (tmp_path / "flagged.wy").write_text("x := __name__\n")
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    clear_module_cache()
    ctx: dict = {}
    eval_program(parse("import flagged\n"), ctx)
    mod = ctx["flagged"].value
    assert mod.ctx["x"].value == "flagged", "seen as its own name from the module's own top level"
    assert mod.ctx["__name__"].value == "flagged"


def test_imported_submodule_sees_its_full_double_colon_path(ctx):
    std_io = ctx["std"].value.submodules["io"]
    assert std_io.ctx["__name__"].value == "std::io", (
        "__name__ is always fully qualified, unlike Python's (where the "
        "entry script can end up with an unqualified __name__)"
    )


# --- module-level signals & messages ---------------------------------------
# `signal name(...)` and `fn [] name(...)` at a module's own top level - the
# module-scoped counterparts of a class's per-instance signal and tagged
# `fn [Class] name(...)` message handler (see wyrm_eval_parse_tree.py's
# ast.SignalDef/ast.FnDef cases in _eval_stmt_impl, and _resolve_message's
# Module branch).

def test_signal_at_module_top_level_creates_one_shared_signal_value(tmp_path, monkeypatch):
    (tmp_path / "svc.wy").write_text("signal a_signal(x: int)\n")
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    clear_module_cache()
    ctx: dict = {}
    eval_program(parse("import svc\n"), ctx)
    mod = ctx["svc"].value
    assert isinstance(mod.ctx["a_signal"].value, SignalValue)


def test_module_signal_emit_and_connect_work_unchanged(tmp_path, monkeypatch):
    (tmp_path / "svc.wy").write_text(
        "signal a_signal(x: int)\n"
        "fn [] a_method():\n"
        "    emit a_signal(5)\n"
    )
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    clear_module_cache()
    ctx = Scope()
    populate_globals(ctx)  # "connect" is a wildcard message registered here, not in a bare dict
    eval_program(
        parse(
            "import svc\n"
            "seen := []\n"
            "svc::a_signal ! connect(fn(v) { seen ! append(v) })\n"
            "svc ! a_method()\n"
        ),
        ctx,
    )
    assert list(ctx["seen"].value) == [5]


def test_module_message_dispatch_does_not_need_import_static(tmp_path, monkeypatch):
    """`mod ! name(...)` resolves against the module's own message table
    (message_table(mod.ctx)), not the sender's - unlike a decorator, it
    doesn't need `import static`'s message-table adoption to be reachable."""
    (tmp_path / "svc.wy").write_text(
        "fn [] a_method():\n"
        "    return 42\n"
    )
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    clear_module_cache()
    ctx: dict = {}
    eval_program(parse("import svc\nresult := svc ! a_method()\n"), ctx)
    assert ctx["result"].value == 42


def test_two_modules_own_message_handlers_do_not_collide(tmp_path, monkeypatch):
    """Each module's `fn []` handlers live in that module's own
    message_table (a fresh root Scope per import_module call) - two modules
    declaring the same handler name must resolve independently rather than
    fighting over one shared wildcard overload."""
    (tmp_path / "a.wy").write_text("fn [] greet():\n    return \"a\"\n")
    (tmp_path / "b.wy").write_text("fn [] greet():\n    return \"b\"\n")
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    clear_module_cache()
    ctx: dict = {}
    eval_program(
        parse(
            "import a\n"
            "import b\n"
            "ra := a ! greet()\n"
            "rb := b ! greet()\n"
        ),
        ctx,
    )
    assert ctx["ra"].value == "a"
    assert ctx["rb"].value == "b"


def test_an_untagged_fn_is_not_a_module_message(tmp_path, monkeypatch):
    """A plain `fn name()` (class_target=None) is an ordinary module-level
    function, not a `fn []` module message handler - `mod ! name()` should
    still fail for it, the same as it would for any undeclared message."""
    (tmp_path / "svc.wy").write_text("fn plain():\n    return 1\n")
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    clear_module_cache()
    ctx: dict = {}
    with pytest.raises(NameError):
        eval_program(parse("import svc\nsvc ! plain()\n"), ctx)
