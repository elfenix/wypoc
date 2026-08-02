"""Tests for wyrm --compile (wypoc/compiler_c.py): structural checks on the
generated C for the supported fixtures under wypoc/samples/, plus one
CompileError case per documented v1 scope cut.

Mostly string/structure assertions, not full semantic execution - wypoc has
no C toolchain integration today. The one exception is
test_generated_c_passes_gcc_syntax_check, which shells out to gcc against
the real wyrm repo's headers when that sibling repo is present, to catch
real header/type mismatches pure string assertions would miss.
"""
import os
import shutil
import subprocess

import pytest

from conftest import compile_sample, sample_source
from wypoc.compiler_c import CompileError, compile_module
from wypoc.parse import parse

ERROR_SAMPLES = {
    "compile_err_missing_native.wy": "import native",
    "compile_err_float_param.wy": "unsupported type 'float'",
    "compile_err_non_tail_call.wy": "calls nested inside larger expressions",
    "compile_err_for_loop.wy": "statement not supported by --compile",
    "compile_err_class_def.wy": "top-level statement not supported by --compile",
}


def test_leaf_function_structure():
    c_src = compile_sample("compile_leaf.wy")
    assert "wyrm_exec_state w_compile_leaf_clamp(wyrm_state* state)" in c_src
    assert "wyrm_exec_state w_compile_leaf_count_up_to(wyrm_state* state)" in c_src
    assert "wyrm_state_push_return" in c_src
    # No calls between functions in this fixture -> no forwarder needed.
    assert "__wyrm_forward_result" not in c_src


def test_tail_call_structure():
    c_src = compile_sample("compile_tail_call.wy")
    assert "wyrm_exec_state w_compile_tail_call_add(wyrm_state* state)" in c_src
    assert "wyrm_exec_state w_compile_tail_call_dispatch(wyrm_state* state)" in c_src
    assert "static wyrm_exec_state __wyrm_forward_result(wyrm_state* state);" in c_src
    assert "wyrm_state_call_continue(state, __wyrm_forward_result, w_compile_tail_call_add," in c_src
    assert "return WYRM_EXEC_CONTINUE;" in c_src


def test_non_tail_call_structure():
    c_src = compile_sample("compile_non_tail_call.wy")
    # combo() has two non-tail calls -> entry + 2 static continuation chunks.
    assert "wyrm_exec_state w_compile_non_tail_call_combo(wyrm_state* state)" in c_src
    assert "static wyrm_exec_state compile_non_tail_call_combo_chunk_1(wyrm_state* state)" in c_src
    assert "static wyrm_exec_state compile_non_tail_call_combo_chunk_2(wyrm_state* state)" in c_src
    assert (
        "wyrm_state_call_continue(state, compile_non_tail_call_combo_chunk_1, "
        "w_compile_non_tail_call_inc," in c_src
    )
    assert (
        "wyrm_state_call_continue(state, compile_non_tail_call_combo_chunk_2, "
        "w_compile_non_tail_call_double," in c_src
    )
    # log_and_use() discards its call's result but still needs a chunk split.
    assert "static wyrm_exec_state compile_non_tail_call_log_and_use_chunk_1(wyrm_state* state)" in c_src
    assert "return WYRM_EXEC_CONTINUE;" in c_src


def test_native_block_splice():
    c_src = compile_sample("compile_native_block.wy")
    assert "#include <stdio.h>" in c_src
    assert "x = a + b;" in c_src
    assert "y = b - c;" in c_src
    assert "wyrm_exec_state w_compile_native_block_combine(wyrm_state* state)" in c_src


@pytest.mark.parametrize("name,message", ERROR_SAMPLES.items(), ids=list(ERROR_SAMPLES))
def test_error_paths(name, message):
    tree = parse(sample_source(name))
    with pytest.raises(CompileError, match=message):
        compile_module(tree, os.path.splitext(name)[0])


WYRM_INCLUDE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "wyrm", "include")
)


@pytest.mark.skipif(
    not (shutil.which("gcc") and os.path.isdir(WYRM_INCLUDE_DIR)),
    reason="gcc and/or the sibling wyrm repo's include/ directory aren't available",
)
@pytest.mark.parametrize(
    "name", ["compile_leaf.wy", "compile_tail_call.wy", "compile_native_block.wy", "compile_non_tail_call.wy"],
)
def test_generated_c_passes_gcc_syntax_check(name, tmp_path):
    c_src = compile_sample(name)
    out_path = tmp_path / (os.path.splitext(name)[0] + ".c")
    out_path.write_text(c_src)
    result = subprocess.run(
        ["gcc", "-fsyntax-only", "-std=c11", "-I", WYRM_INCLUDE_DIR, str(out_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
