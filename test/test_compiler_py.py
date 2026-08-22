"""Tests for `wyrm --compile-py` (wypoc/compiler_py/) - see the
implementation plan's staged rollout. This file grows incrementally, one
section per stage, rather than arriving all at once.

Like test_compiler_c.py, string-matching the generated source isn't enough
on its own - `run_py_source` actually writes the generated file to disk and
runs it with a real Python interpreter (PYTHONPATH pointed at the repo root
so `wypoc.compiler_py.engine` is importable), asserting on real stdout.
"""
import os
import subprocess
import sys

import pytest

from conftest import (
    REPO_ROOT, compile_py_decorated_sample, compile_py_entry_sample,
    compile_py_sample, compile_py_tree_sample, eval_sample,
    eval_sample_with_builtins, exec_compiled_py_module, run_py_do_import,
    sample_source,
)
from wypoc.compiler_py import CompileError, compile_entry_module
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import unwrap

# --- Stage 1: literals/Name/UnaryOp/BinOp/Call expressions, plain fn defs,
# Assign/VarDecl/If/While/Return/ExprStmt statements. --------------------

COMPILING_SAMPLES = [
    "compile_py_basics.wy",
]


def run_py_source(tmp_path, source: str, filename: str = "entry.py") -> subprocess.CompletedProcess:
    script = tmp_path / filename
    script.write_text(source)
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, env=env, timeout=30,
    )


@pytest.mark.parametrize("name", COMPILING_SAMPLES)
def test_compiling_samples_produce_valid_python(name):
    source = compile_py_entry_sample(name)
    compile(source, name, "exec")  # syntax-checks the generated source directly


def test_compile_py_basics_runs_and_prints_expected_output(tmp_path):
    source = compile_py_entry_sample("compile_py_basics.wy")
    result = run_py_source(tmp_path, source)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "clamped= 10  summed= 10"


def test_every_wyrm_fn_compiles_to_an_async_def():
    source = compile_py_entry_sample("compile_py_basics.wy")
    assert "async def wy_clamp(" in source
    assert "async def wy_count_up_to(" in source
    assert "async def do_import(ctx: Context) -> None:" in source


def test_do_import_is_idempotency_guarded():
    source = compile_py_entry_sample("compile_py_basics.wy")
    assert "if not ctx.machine.mark_loaded(__name__):" in source


def test_main_block_runs_an_asyncio_loop():
    source = compile_py_entry_sample("compile_py_basics.wy")
    assert 'if __name__ == "__main__":' in source
    assert "asyncio.run(do_import(ctx))" in source


def test_unsupported_top_level_statement_raises_compile_error():
    src = "return 1\n"
    with pytest.raises(CompileError, match="not supported by --compile-py"):
        compile_entry_module(parse(src), "err")


def test_coroutine_def_compiles_to_a_cursor_returning_async_def():
    src = "co foo():\n    yield 1\n"
    source = compile_entry_module(parse(src), "err")
    assert "async def wy_foo() -> Cursor:" in source
    assert "async def _body(_cursor):" in source
    assert "return Cursor(_body)" in source


def test_dollar_identifier_is_escaped():
    from wypoc.compiler_py.naming import py_ident
    assert py_ident("reg$0") == "wy_regDWY0"
    assert py_ident("xDWYy") == "wy_xDWY_0_y"
    assert py_ident("a$DWYb") == "wy_aDWYDWY_0_b"


# --- Stage 2: classes (@dataclass + init + inheritance), message dispatch
# (`!`), Attr vs Message, TypeCheck, plain-fn-as-wildcard-message
# promotion, multi-receiver dispatch. All within one module (no imports
# yet - that's stage 3). ----------------------------------------------

MESSAGE_ORACLE_NAMES = [
    "circle_area", "square_area", "shape_area", "bound_area",
    "circle_desc", "square_desc", "cc", "cs", "gen",
]


def test_eval_messages_matches_the_interpreter():
    """Cross-checks --compile-py's output against the tree-walking
    interpreter's own evaluation of the same sample - the oracle
    comparison the plan calls for at this stage."""
    interp_ctx = eval_sample("eval_messages.wy")
    py_ns = exec_compiled_py_module(compile_py_sample("eval_messages.wy"))
    run_py_do_import(py_ns)

    for name in MESSAGE_ORACLE_NAMES:
        expected = unwrap(interp_ctx[name])
        actual = py_ns[f"wy_{name}"]
        assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"


def test_class_compiles_to_a_dataclass_plus_async_constructor():
    source = compile_py_sample("eval_messages.wy")
    assert "@dataclass" in source
    assert "class _wy_Shape_fields:" in source
    assert "class _wy_Circle_fields(_wy_Shape_fields):" in source
    assert "async def wy_Circle(*args, **kwargs):" in source


def test_single_inheritance_sets_wy_bases_for_dispatch_distance():
    source = compile_py_sample("eval_messages.wy")
    assert "__wy_bases__: ClassVar[tuple] = (_wy_Shape_fields,)" in source


def test_bare_message_send_returns_a_closure_call_is_awaited():
    source = compile_py_sample("eval_messages.wy")
    assert "wy_bound = dispatch(wy_c, 'area', _TABLE)" in source
    assert "wy_bound_area = (await wy_bound())" in source


def test_multi_receiver_dispatch_passes_a_receiver_list():
    source = compile_py_sample("eval_messages.wy")
    assert "'collide', _TABLE" in source
    assert "_TABLE.register('collide', (_wy_Circle_fields, _wy_Circle_fields)" in source


def test_plain_fn_is_promoted_as_a_wildcard_overload():
    source = compile_py_sample("eval_messages.wy")
    assert "_TABLE.register('describe', (None,), _wy_wildcard_describe)" in source
    assert "_TABLE.register('collide', (None, None), _wy_wildcard_collide)" in source


def test_attr_and_message_never_collide():
    """`.name` (Attr) always compiles to plain attribute access, never a
    dispatch() call - structurally distinct from `!name` (Message)."""
    src = "class Foo:\n    slot x: int = 0\nf := Foo()\ny := f.x\n"
    source = compile_entry_module(parse(src), "err")
    assert "y = (wy_f).wy_x" in source or "wy_y = (wy_f).wy_x" in source


def test_multiple_inheritance_rejected():
    src = ("class A:\n    slot x: int = 0\n"
           "class B:\n    slot y: int = 0\n"
           "class C(A, B):\n    slot z: int = 0\n")
    with pytest.raises(CompileError, match="single inheritance"):
        compile_entry_module(parse(src), "err")


def test_signal_member_rejected():
    src = "class Foo:\n    signal changed(v: int)\n"
    with pytest.raises(CompileError, match="unsupported class-body member"):
        compile_entry_module(parse(src), "err")


# --- Stage 3: imports / multi-file output tree / do_import chaining. Two
# import forms exercised together: a bare `import static shapes` (corelib,
# reached via wyrm_modules' DEFAULT_COREPATH fallback - std::io itself
# isn't compilable yet, since it needs stage 6's for/try/defer, so a
# plainer corelib module stands in for it here) and an item-list `import
# compile_py_lib::(double)` pulling one name out of a sibling sample. -----

def test_compile_tree_writes_the_expected_output_layout(tmp_path):
    entry_path = compile_py_tree_sample("compile_py_importer.wy", tmp_path / "out")
    out = tmp_path / "out"
    assert os.path.isfile(entry_path)
    assert os.path.isfile(out / "wyrm" / "__init__.py")
    assert os.path.isfile(out / "wyrm" / "shapes.py")
    assert os.path.isfile(out / "wyrm" / "compile_py_lib.py")
    # the entry script is a sibling of wyrm/, not inside it
    assert not (out / "wyrm" / "compile_py_importer.py").exists()


def test_compile_tree_runs_and_matches_the_interpreter(tmp_path):
    interp_ctx = eval_sample("compile_py_importer.wy")
    entry_path = compile_py_tree_sample("compile_py_importer.wy", tmp_path / "out")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path / "out"), REPO_ROOT])
    result = subprocess.run(
        [sys.executable, "-c",
         "import asyncio, importlib.util, sys\n"
         f"spec = importlib.util.spec_from_file_location('compile_py_importer', {entry_path!r})\n"
         "m = importlib.util.module_from_spec(spec)\n"
         "spec.loader.exec_module(m)\n"
         "asyncio.run(m.do_import(m.Machine().root_context()))\n"
         "print(m.wy_area)\n"
         "print(m.wy_doubled)\n"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    from wypoc.wyrm_eval_parse_tree import unwrap
    got_area, got_doubled = result.stdout.splitlines()
    assert float(got_area) == unwrap(interp_ctx["area"])
    assert int(got_doubled) == unwrap(interp_ctx["doubled"])


def _compile_importer_entry_source() -> str:
    """compile_py_entry_sample doesn't thread a graph through, and
    compile_py_importer.wy needs one (it has real imports) - build one
    directly, the way compile_tree does internally."""
    from wypoc.compiler_py.graph import CompileGraph
    graph = CompileGraph()
    return compile_entry_module(parse(sample_source("compile_py_importer.wy")),
                                 "compile_py_importer", graph=graph)


def test_scope_operator_compiles_to_attribute_access_on_the_bound_module():
    source = _compile_importer_entry_source()
    assert "(wyrm.shapes).wy_Circle" in source


def test_static_import_merges_the_message_table():
    source = _compile_importer_entry_source()
    assert "_TABLE.merge(wyrm.shapes._TABLE)" in source


def test_item_list_import_binds_the_qualified_reference():
    source = _compile_importer_entry_source()
    assert "wy_doubled = (await wyrm.compile_py_lib.wy_double(21))" in source


def test_do_import_chaining_awaits_every_imported_module_first():
    source = _compile_importer_entry_source()
    assert "await wyrm.shapes.do_import(ctx)" in source
    assert "await wyrm.compile_py_lib.do_import(ctx)" in source


def test_bare_non_static_message_send_is_not_visible_across_a_plain_import():
    """A plain (non-static) `import mod` doesn't bring mod's message table
    with it - matches the interpreter's eval_import exactly (only
    `import static` calls _adopt_messages); confirmed directly against the
    interpreter in this test, not just asserted from the compiler side."""
    src = "import shapes\nc := shapes::Circle()\narea := c!area()\n"
    with pytest.raises(Exception, match="no message named 'area'"):
        eval_sample_inline(src)


def eval_sample_inline(src: str):
    from wypoc.wyrm_eval_parse_tree import eval_program
    eval_program(parse(src), {})


def test_unknown_module_raises_compile_error():
    from wypoc.compiler_py.graph import CompileGraph
    src = "import totally::nonexistent::module\n"
    with pytest.raises(CompileError, match="cannot find module"):
        compile_entry_module(parse(src), "err", graph=CompileGraph())


def test_import_without_a_graph_raises_a_clear_compile_error():
    src = "import shapes\n"
    with pytest.raises(CompileError, match="multi-file driver"):
        compile_entry_module(parse(src), "err")


# --- Stage 4: coroutines - lazy-start Cursor, yield/yield from/next/send/
# .value, message-dispatched co [Cls] methods, `catch` (needed by this
# fixture's `.value catch "expected"`), and `is <primitive>` TypeChecks
# (needed by term_adder's `while term is int:`). All within one module -
# eval_coroutines.wy has no imports. ------------------------------------

COROUTINE_ORACLE_NAMES = [
    "first_next", "value_while_active", "value_after_finish",
    "mirror_start", "mirror_echo_1", "mirror_echo_2",
    "delegate_1", "delegate_2", "delegate_3",
    "sum_start", "sum_after_5", "sum_after_10",
]
# These four end the driving coroutine (an exhausted next()/send() call, or
# one that hits an internal error) - both sides represent that as "some
# error value", but the interpreter's is a StopIteration-classed
# ClassInstance and ours is an engine.WyrmError, so these are compared by
# "is this an error" rather than by exact value/representation.
COROUTINE_ORACLE_ERROR_NAMES = ["second_next", "delegate_4", "sum_stopped"]


def test_eval_coroutines_matches_the_interpreter():
    from wypoc.wyrm_builtins import is_error as interp_is_error
    from wypoc.wyrm_eval_parse_tree import unwrap
    from wypoc.compiler_py.engine import is_error as py_is_error

    interp_ctx = eval_sample_with_builtins("eval_coroutines.wy")
    py_ns = exec_compiled_py_module(compile_py_sample("eval_coroutines.wy"))
    run_py_do_import(py_ns)

    for name in COROUTINE_ORACLE_NAMES:
        expected = unwrap(interp_ctx[name])
        actual = py_ns[f"wy_{name}"]
        assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"

    for name in COROUTINE_ORACLE_ERROR_NAMES:
        assert interp_is_error(unwrap(interp_ctx[name])), f"{name} should be an interpreter error"
        assert py_is_error(py_ns[f"wy_{name}"]), f"{name} should be a compiled-side error"


def test_calling_a_coroutine_does_not_run_its_body():
    source = compile_py_sample("eval_coroutines.wy")
    assert "async def wy_do_1x() -> Cursor:" in source
    assert "    async def _body(_cursor):" in source
    assert "    return Cursor(_body)" in source


def test_yield_from_forwards_via_the_raw_advance_primitive():
    """`yield from` must drive the delegate with Cursor._advance, not the
    public .next()/.send() - those discard the delegate's real completion
    value in favor of a StopIteration sentinel (see engine.Cursor.next's
    docstring), but yield-from needs that real value."""
    source = compile_py_sample("eval_coroutines.wy")
    assert "_yf_inner1._advance(_yf_sent2)" in source


def test_message_dispatched_coroutine_returns_a_cursor():
    source = compile_py_sample("eval_coroutines.wy")
    assert "_TABLE.register('term_adder', (_wy_summation_fields,), _wy_msg_term_adder__on_summation)" in source
    assert "async def _wy_msg_term_adder__on_summation(wy_this) -> Cursor:" in source


def test_next_and_send_builtins_compile_to_cursor_methods():
    source = compile_py_sample("eval_coroutines.wy")
    assert "await (wy_cofun).next()" in source
    assert "await (wy_mirrored).send(20)" in source


def test_this_attr_target_assignment_is_supported():
    source = compile_py_sample("eval_coroutines.wy")
    assert "(wy_this).wy_total = ((wy_this).wy_total + wy_term)" in source


def test_yield_outside_coroutine_raises_compile_error():
    src = "fn foo():\n    yield 1\n"
    with pytest.raises(CompileError, match="'yield' used outside a coroutine body"):
        compile_entry_module(parse(src), "err")


# --- Stage 5: decorator pre-pass. compile_py_decorated.wy exercises only
# the native @__dump/@__identity decorators (no import needed) - a
# wyrm-written decorator library (like samples/decolib.wy) still needs
# ordinary codegen support for its own body (sexpr/car/cdr/`$[...]`
# manipulation), which this compiler doesn't have yet, so it's not
# exercised here; see decorators_pass.py's module docstring. ------------

DECORATED_ORACLE_NAMES = ["sum_result", "twice_result", "msg_result"]


def test_decorated_sample_matches_the_interpreter():
    from wypoc.wyrm_eval_parse_tree import Scope, populate_globals, unwrap

    interp_ctx = Scope()
    populate_globals(interp_ctx)
    from wypoc.wyrm_eval_parse_tree import eval_program
    eval_program(parse(sample_source("compile_py_decorated.wy")), interp_ctx)

    py_ns = exec_compiled_py_module(compile_py_decorated_sample("compile_py_decorated.wy"))
    run_py_do_import(py_ns)

    for name in DECORATED_ORACLE_NAMES:
        expected = unwrap(interp_ctx[name])
        actual = py_ns[f"wy_{name}"]
        assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"


def test_decorated_source_has_no_decorator_traces():
    """After expansion, the generated source is indistinguishable from
    what the undecorated originals would compile to - no Decorated node
    reaches codegen, and no decorator-specific machinery leaks into the
    output."""
    source = compile_py_decorated_sample("compile_py_decorated.wy")
    assert "async def wy_add(wy_a, wy_b):" in source
    assert "return (wy_a + wy_b)" in source
    assert "async def wy_twice(wy_n):" in source
    assert "return (wy_n * 2)" in source
    assert "__dump" not in source
    assert "__identity" not in source


def test_decorator_pre_pass_mutes_io():
    """@__dump prints the s-expression it receives as a real side effect
    of running the decorated code - the pre-pass must not let that reach
    real stdout (it would otherwise print once during compilation and
    again whenever the generated code actually runs)."""
    import contextlib
    import io as _io

    from wypoc.compiler_py.decorators_pass import expand_all_decorators

    tree = parse(sample_source("compile_py_decorated.wy"))
    captured = _io.StringIO()
    with contextlib.redirect_stdout(captured):
        expand_all_decorators(tree, ())
    assert captured.getvalue() == ""


def test_module_with_no_decorators_skips_the_pre_pass_entirely():
    """A quick `any(isinstance(n, ast.Decorated) ...)` check means a
    module without decorators never invokes the interpreter at all - the
    returned tree is the exact same object, not a copy."""
    from wypoc.compiler_py.decorators_pass import expand_all_decorators
    tree = parse(sample_source("compile_py_basics.wy"))
    assert expand_all_decorators(tree, ()) is tree


def test_compile_tree_expands_decorators_in_the_entry_script(tmp_path):
    entry_path = compile_py_tree_sample("compile_py_decorated.wy", tmp_path / "out")
    source = open(entry_path).read()
    assert "__dump" not in source
    assert "__identity" not in source
    assert "async def wy_add(wy_a, wy_b):" in source


def test_unreached_decorator_is_skipped_not_hard_errored():
    """A Decorated node *nested inside a fn/co body* (as against one
    wrapping a whole top-level `fn`/`co` definition, which is a top-level
    statement in its own right and so always runs during the pre-pass's
    eager execution) that the pre-pass's eager run never reached - a
    [Cls] method whose body's decorator only fires when the method
    itself is *called*, and nothing at module top level calls it -
    doesn't abort the whole module. It's silently skipped, the same way
    a `$`-named macro-template definition is (see module.py's
    _is_macro_only)."""
    src = ("class Box:\n"
           "    slot v: int = 0\n"
           "    fn init(v: int):\n"
           "        this.v = v\n"
           "fn [Box] doubled_value() -> int:\n"
           "    return @__identity this.v * 2\n"
           "fn [Box] never_called() -> int:\n"
           "    return @__identity this.v * 3\n"
           "b := Box(5)\n"
           "msg_result := b!doubled_value()\n")
    from wypoc.compiler_py.decorators_pass import expand_all_decorators
    tree = expand_all_decorators(parse(src), ())
    source = compile_entry_module(tree, "err")
    assert "doubled_value" in source
    assert "never_called" not in source


# --- Stage 6: remaining control flow (For/Try/Catch/Defer/With) + CLI
# wiring. eval_control_flow.wy and eval_defer_with_do.wy are reused
# unmodified as oracle fixtures; error_handling gets a reduced sample
# (compile_py_error_handling.wy) since inheriting a user class from the
# builtin `error` type isn't supported yet - see that file's header
# comment. -----------------------------------------------------------

CONTROL_FLOW_ORACLE_NAMES = [
    "seen", "total", "summed", "found_even",
    "coroutine_total", "class_iter_total",
]


def test_eval_control_flow_matches_the_interpreter():
    interp_ctx = eval_sample_with_builtins("eval_control_flow.wy")
    py_ns = exec_compiled_py_module(compile_py_sample("eval_control_flow.wy"))
    run_py_do_import(py_ns)

    for name in CONTROL_FLOW_ORACLE_NAMES:
        expected = unwrap(interp_ctx[name])
        actual = py_ns[f"wy_{name}"]
        assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"


def test_for_loop_compiles_to_async_for_over_wy_aiter():
    source = compile_py_sample("eval_control_flow.wy")
    assert "_wy_aiter(" in source
    assert "async for " in source


def test_for_else_maps_onto_pythons_own_for_else():
    src = "total := 0\nfor x in [1, 2, 3]:\n    total = total + x\nelse:\n    total = total + 100\n"
    source = compile_entry_module(parse(src), "err")
    assert "async for wy_x in engine._wy_aiter(" in source
    assert "else:" in source


DEFER_ORACLE_NAMES = [
    "ordering_log", "per_iteration_log", "defer_on_error_log", "with_sum",
]


def test_eval_defer_with_do_matches_the_interpreter():
    interp_ctx = eval_sample_with_builtins("eval_defer_with_do.wy")
    py_ns = exec_compiled_py_module(compile_py_sample("eval_defer_with_do.wy"))
    run_py_do_import(py_ns)

    for name in DEFER_ORACLE_NAMES:
        expected = unwrap(interp_ctx[name])
        actual = py_ns[f"wy_{name}"]
        assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"


def test_defer_compiles_to_a_synthesized_try_finally():
    source = compile_py_sample("eval_defer_with_do.wy")
    assert "try:" in source
    assert "finally:" in source


def test_defer_on_error_guards_cleanup_with_the_error_exit_flag():
    src = ("fn risky():\n"
           "    defer on error:\n"
           "        print(\"cleanup\")\n"
           "    return error(\"boom\")\n")
    source = compile_entry_module(parse(src), "err")
    assert "_wy_error_exit = False" in source
    assert "if is_error(" in source
    assert "except BaseException:" in source
    assert "if _wy_error_exit:" in source


def test_with_block_compiles_to_plain_assignment():
    src = "with:\n    x = 1\n    y = 2\nz := x + y\n"
    source = compile_entry_module(parse(src), "err")
    assert "wy_x = 1" in source
    assert "wy_y = 2" in source


ERROR_HANDLING_ORACLE_NAMES = [
    "try_result", "passthrough_result", "f", "not_an_error",
    "catch_return_result", "index_error_result",
    "demo_ok", "demo_div_by_zero",
]


def test_compile_py_error_handling_matches_the_interpreter():
    from wypoc.compiler_py.engine import is_error as py_is_error
    from wypoc.wyrm_builtins import is_error as interp_is_error

    interp_ctx = eval_sample_with_builtins("compile_py_error_handling.wy")
    py_ns = exec_compiled_py_module(compile_py_sample("compile_py_error_handling.wy"))
    run_py_do_import(py_ns)

    for name in ERROR_HANDLING_ORACLE_NAMES:
        expected = unwrap(interp_ctx[name])
        actual = py_ns[f"wy_{name}"]
        # WyrmError is a distinct dataclass on each side (the interpreter's
        # own error-value type vs. engine.WyrmError) - compare by
        # is-error-ness (and the message, when there is one) rather than by
        # dataclass equality, which would always be False across types.
        if interp_is_error(expected):
            assert py_is_error(actual), f"{name}: expected an error, got {actual!r}"
            expected_what = getattr(expected, "what", None) or getattr(expected, "message", None)
            actual_what = getattr(actual, "what", None)
            if expected_what is not None and actual_what is not None:
                assert str(actual_what) == str(expected_what), \
                    f"{name}: expected error {expected_what!r}, got {actual_what!r}"
        elif name in ("demo_ok", "demo_div_by_zero"):
            # `demo(...) catch 'div0` yields either the constructed instance
            # (a distinct dataclass type on each side, so compared by
            # is-error-ness only) or the fallback symbol 'div0 (compared for
            # real, since Symbol reprs match across sides).
            assert not py_is_error(actual), f"{name}: expected {expected!r}, got error {actual!r}"
            if name == "demo_div_by_zero":
                assert actual == py_ns["engine"].Symbol("div0")
            else:
                assert actual is not None
        else:
            assert not py_is_error(actual), f"{name}: expected {expected!r}, got error {actual!r}"
            assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"


def test_div_and_mod_by_zero_produce_catchable_errors():
    source = compile_py_sample("compile_py_error_handling.wy")
    assert "engine.wy_div(" in source


def test_index_error_is_catchable():
    source = compile_py_sample("compile_py_error_handling.wy")
    assert "engine.wy_index(" in source


def test_slot_without_default_gets_a_type_aware_zero_value():
    source = compile_py_sample("compile_py_error_handling.wy")
    assert "wy_result: Any = 0" in source


def test_native_block_raises_compile_error():
    src = "native::block('HEADER, $[], $[], R\"tag(int x = 1;)tag\")\n"
    with pytest.raises(CompileError):
        compile_entry_module(parse(src), "err")


def test_cli_compile_py_flag_writes_a_runnable_tree(tmp_path):
    out_dir = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, "-m", "wypoc.cli", "--compile-py", str(out_dir),
         str(os.path.join(REPO_ROOT, "wypoc", "samples", "compile_py_basics.wy"))],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    entry = out_dir / "compile_py_basics.py"
    assert entry.is_file()

    run_result = subprocess.run(
        [sys.executable, str(entry)],
        capture_output=True, text=True, timeout=30,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert run_result.stdout == "clamped= 10  summed= 10"


def test_cli_compile_py_mutually_exclusive_with_compile():
    result = subprocess.run(
        [sys.executable, "-m", "wypoc.cli", "--compile-py", "/tmp/x",
         "--compile", "foo.wy"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=30,
    )
    assert result.returncode == 2
    assert "--compile-py cannot be used with" in result.stderr


def test_cli_compile_py_requires_output_dir_argument():
    result = subprocess.run(
        [sys.executable, "-m", "wypoc.cli", "--compile-py"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=30,
    )
    assert result.returncode == 2
    assert "requires an output directory" in result.stderr
