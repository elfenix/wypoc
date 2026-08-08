"""Tests for `wyrm --compile` (wypoc/compiler_c/, see its DESIGN.md).

Two kinds of check, and the second is the one that carries the weight:

- structural assertions on the generated C for each supported fixture under
  wypoc/samples/, and one CompileError case per documented scope cut;
- `test_generated_c_compiles`, which runs the generated C through a C
  compiler against test/native/lang_internal.h - a stub of exactly the
  interpreter surface the output is allowed to depend on. That catches the
  mistakes string assertions miss (a mismatched brace, a value read through
  the wrong union field, a call with the wrong arity) and keeps the
  dependency explicit: widening what the compiler emits means widening that
  header first.
"""
import os
import shutil
import subprocess

import pytest

from conftest import compile_sample, sample_source
from wypoc.compiler_c import CompileError, compile_module
from wypoc.parse import parse

NATIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")

COMPILING_SAMPLES = [
    "compile_leaf.wy",
    "compile_tail_call.wy",
    "compile_non_tail_call.wy",
    "compile_nested_calls.wy",
    "compile_call_in_loop.wy",
    "compile_floats.wy",
    "compile_class_def.wy",
    "compile_native_block.wy",
]

ERROR_SAMPLES = {
    "compile_err_missing_native.wy": "import native",
    "compile_err_for_loop.wy": "statement not supported by --compile",
    "compile_err_class_def.wy": "methods declared inside the class body",
    "compile_err_set_if_unset.wy": r"'\?=' not supported by --compile",
    "compile_err_float_modulo.wy": "has no float form in C",
    "compile_err_slot_default.wy": "must be a constant expression",
}


# --- the calling convention ------------------------------------------------

def test_a_fn_is_one_c_function_in_the_native_convention():
    c_src = compile_sample("compile_leaf.wy")
    assert (
        "bool w_compile_leaf_clamp(wyrm_lang_vm* vm, wyrm_value* args, "
        "wyrm_uword argc, wyrm_value* out)"
    ) in c_src
    # No chunk graph, no resumable state machine - that was the old target's
    # convention, and control flow compiles straight across without it.
    assert "wyrm_exec_state" not in c_src
    assert "wyrm_state_call_continue" not in c_src
    assert "__wyrm_forward_result" not in c_src


def test_a_function_checks_its_arity_and_argument_tags():
    """Arguments arrive dynamically typed, so a compiled function cannot
    trust the caller - it is the one place compiled code meets values it did
    not produce."""
    c_src = compile_sample("compile_leaf.wy")
    assert 'lang_vm_runtime_error(vm, "clamp() takes 3 argument(s), not %u"' in c_src
    assert "if (args[0].type != WYRM_TYPE_TAG_WORD) {" in c_src
    assert "must be a int" in c_src


def test_parameters_and_locals_are_plain_c_variables():
    c_src = compile_sample("compile_non_tail_call.wy")
    assert "wyrm_word x = ((wyrm_word)(args[0]).data.word);" in c_src
    assert "wyrm_word y = 0;" in c_src, "a body local is declared and zeroed up front"


def test_falling_off_the_end_answers_nil():
    """The same value the interpreter gives a function with no `return`."""
    src = "import native\n\nfn effect(a: int):\n    pass\n"
    c_src = compile_module(parse(src), "m")
    assert "*out = lang_value_nil();\n    return true;" in c_src


def test_control_flow_compiles_to_c_control_flow():
    c_src = compile_sample("compile_call_in_loop.wy")
    assert "while ((i < n)) {" in c_src
    assert "if ((x < 0)) {" in c_src
    assert "} else {" in c_src


# --- calls -----------------------------------------------------------------

def test_a_call_is_an_ordinary_c_call_that_propagates_failure():
    c_src = compile_sample("compile_tail_call.wy")
    assert "wyrm_value __args1[2] = { lang_value_int" in c_src
    assert "if (!w_compile_tail_call_add(vm, __args1, 2, &__t2)) { return false; }" in c_src


def test_a_call_may_appear_anywhere_in_an_expression():
    """The old backend could only place a call as a whole statement or a bare
    `return`, because a call there split its enclosing block in two."""
    c_src = compile_sample("compile_nested_calls.wy")
    # Each call becomes a statement assigning a temporary; the arithmetic
    # around them stays ordinary C.
    assert c_src.count("if (!w_compile_nested_calls_inc(") == 7
    assert c_src.count("if (!w_compile_nested_calls_add(") == 3
    assert "*out = lang_value_int((wyrm_word)((((wyrm_word)(__t8).data.word) + 1)));" in c_src


def test_a_call_in_a_while_condition_is_re_evaluated_each_iteration():
    """Hoisting the condition out of `while (...)` would evaluate it once, so
    the loop becomes `for (;;)` with the hoist and an explicit break inside."""
    c_src = compile_sample("compile_nested_calls.wy")
    assert "for (;;) {" in c_src
    assert "{ break; }" in c_src


def test_short_circuit_survives_a_call_on_the_right():
    """`a and f(b)` must not call `f` when `a` is false - a hoisted call
    would run unconditionally, so the branch is rebuilt explicitly."""
    c_src = compile_sample("compile_nested_calls.wy")
    assert "bool __sc" in c_src
    assert "&& " not in c_src.split("short_circuit")[-1], (
        "the short-circuiting case does not fall back to C's &&"
    )


# --- types -----------------------------------------------------------------

def test_floats_compile():
    c_src = compile_sample("compile_floats.wy")
    assert "wyrm_float a = ((wyrm_float)(args[0]).data.fp);" in c_src
    assert "*out = lang_value_float((wyrm_float)((a * b)));" in c_src


def test_mixed_arithmetic_widens_to_float():
    c_src = compile_sample("compile_floats.wy")
    assert "lang_value_float((wyrm_float)((a + b)))" in c_src


def test_a_bool_result_boxes_as_a_bool():
    c_src = compile_sample("compile_floats.wy")
    assert "*out = lang_value_bool((bool)((x < 1.0)));" in c_src


# --- classes ---------------------------------------------------------------

def test_class_builds_the_interpreters_class_object():
    c_src = compile_sample("compile_class_def.wy")
    assert "bool w_compile_class_def_Point(wyrm_lang_vm* vm, wyrm_patch_class** out)" in c_src
    assert 'wyrm_patch_symtab_intern(&vm->symtab, "Point", &sym)' in c_src
    assert "cls->base.sym_name.symtab_entry = sym + 1;" in c_src
    assert "wyrm_patch_class_add_slot(vm->state->context, cls, sym + 1, " in c_src


def test_a_slot_carries_its_declared_default():
    """The slot API takes a default *value*, which is what makes a declared
    default compile at all."""
    c_src = compile_sample("compile_class_def.wy")
    assert "sym + 1, lang_value_bool((bool)(true))" in c_src
    assert "sym + 1, lang_value_float((wyrm_float)(2.5))" in c_src
    assert "sym + 1, lang_value_int((wyrm_word)(0))" in c_src, (
        "a slot with no default gets its type's zero value"
    )


# --- native blocks and registration ---------------------------------------

def test_native_block_splices_verbatim_and_needs_no_marshalling():
    """A wyrm local is a C local of the same name under this convention, so
    the spliced code reads and writes them directly."""
    c_src = compile_sample("compile_native_block.wy")
    assert "#include <stdio.h>" in c_src
    assert "x = a + b;" in c_src
    assert "y = b - c;" in c_src
    assert "/* native: reads a, b, c, writes x, y */" in c_src


def test_a_module_ends_with_a_registration_table():
    c_src = compile_sample("compile_tail_call.wy")
    assert "const lang_builtin COMPILE_TAIL_CALL_BUILTINS[] = {" in c_src
    assert '{ "add", BI_PLAIN, 2, 2, w_compile_tail_call_add },' in c_src
    assert "COMPILE_TAIL_CALL_BUILTIN_COUNT" in c_src


# --- failing loudly --------------------------------------------------------

@pytest.mark.parametrize("name,message", ERROR_SAMPLES.items(), ids=list(ERROR_SAMPLES))
def test_error_paths(name, message):
    tree = parse(sample_source(name))
    with pytest.raises(CompileError, match=message):
        compile_module(tree, os.path.splitext(name)[0])


def test_an_untyped_local_is_refused_by_name():
    src = "import native\n\nfn f() -> int:\n    x := 1\n    return x\n"
    with pytest.raises(CompileError, match="needs an explicit type"):
        compile_module(parse(src), "m")


def test_a_message_fn_is_refused():
    src = "import native\n\nfn [Box] describe() -> int:\n    return 1\n"
    with pytest.raises(CompileError, match="message fns"):
        compile_module(parse(src), "m")


def test_narrowing_a_float_into_an_int_is_refused():
    src = "import native\n\nfn f(a: float) -> int:\n    return a\n"
    with pytest.raises(CompileError, match="does not fit"):
        compile_module(parse(src), "m")


# --- the C compiler has the last word --------------------------------------

CC = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")


@pytest.mark.skipif(CC is None, reason="no C compiler available")
@pytest.mark.parametrize("name", COMPILING_SAMPLES)
def test_generated_c_compiles(name, tmp_path):
    """Every generated fixture is real C11 against the documented surface
    (test/native/lang_internal.h). `-Wall -Werror` is deliberate: the point of
    generated code is that nobody reads it, so anything a compiler can notice
    should be a failure here rather than noise in a host build."""
    out_path = tmp_path / (os.path.splitext(name)[0] + ".c")
    out_path.write_text(compile_sample(name))
    result = subprocess.run(
        [CC, "-fsyntax-only", "-std=c11", "-Wall", "-Werror",
         "-I", NATIVE_DIR, str(out_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
