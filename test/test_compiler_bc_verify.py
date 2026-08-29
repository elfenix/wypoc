"""The structural verifier (wypoc/compiler_bc/verify.py).

`compile_module` runs the verifier on every image it produces, so the rest of
the suite already asserts that well-formed images pass.  What is left to prove
is the other half: that a *broken* image is caught.  Each test here compiles a
real module, corrupts one thing in it, and checks the verifier says which.

Register machines rot exactly here - a jump landing mid-instruction, a window
running off the frame, a `u` set drifting out of step with the code - and none
of it is visible in a listing that still looks plausible.
"""

import pytest

from wypoc.compiler_bc import CompileError, compile_module, opcodes, verify
from wypoc.parse import parse

L = opcodes.L
P = opcodes.P


def build(source, module_name="m"):
    """Compile without verifying, so a test can break the result on purpose."""
    return compile_module(parse(source), module_name, check=False)


def word_of(image, mnemonic):
    """The index of the first word of the named instruction."""
    offset = 0
    while offset < len(image.code):
        entry, _wide, _fields, nwords = opcodes.unpack(image.code, offset)
        if entry.name == mnemonic:
            return offset
        offset += nwords
    raise AssertionError(f"no {mnemonic} in this image")


def test_a_well_formed_image_verifies():
    verify(build("fn f(a):\n    return a + 1\n\nprintln(f(1))\n"))


def test_a_register_past_the_end_of_the_frame_is_caught():
    image = build("fn f(a):\n    return a + 1\n")
    image.functions[0].nlocals = 1  # the body actually uses L0 and L1
    with pytest.raises(CompileError, match=r"is L1, but the frame holds 1 slot"):
        verify(image)


def test_a_parameter_reference_past_the_p_frame_is_caught():
    image = build("fn f(a):\n    return a\n")
    image.functions[0].params = []  # the body still reads P0
    with pytest.raises(CompileError, match=r"is P0, but the P frame holds 0 slot"):
        verify(image)


def test_a_jump_off_the_end_of_the_body_is_caught():
    image = build("fn f(n):\n    if n > 0:\n        n = 1\n")
    offset = word_of(image, "jf")
    # Compact conditional jumps carry their offset in a0 (spec 2.2).
    image.code[offset] = (image.code[offset] & 0x0000FFFF) | (200 << 16)
    with pytest.raises(CompileError, match="outside the body"):
        verify(image)


def test_a_jump_into_the_middle_of_an_instruction_is_caught():
    image = build("fn f(n):\n    if n > 0:\n        n = n + 1\n")
    offset = word_of(image, "jf")
    # +3 from the jump lands on the second word of the two-word `add`.
    image.code[offset] = (image.code[offset] & 0x0000FFFF) | (3 << 16)
    with pytest.raises(CompileError, match="not an instruction boundary"):
        verify(image)


def test_a_call_window_running_off_the_frame_is_caught():
    """The window is the callee plus its arguments, and no register operand
    names its far end - only `argc` does, so only the window check finds it."""
    image = build("fn f():\n    return println(1)\n")
    offset = word_of(image, "call")
    image.code[offset] = (image.code[offset] & 0xFFFF00FF) | (60 << 8)
    with pytest.raises(CompileError, match="runs past the"):
        verify(image)


def test_a_table_index_out_of_range_is_caught():
    image = build('fn f():\n    return "x"\n')
    offset = word_of(image, "lconst")
    image.code[offset] = (image.code[offset] & 0x0000FFFF) | (9 << 16)
    with pytest.raises(CompileError, match="that table has 1 entr"):
        verify(image)


def test_a_missing_referenced_name_is_caught():
    """A relocation the body reads has to be in the scope's `u` set, or the VM
    never binds it before the read (spec 6.2).

    Only messages reach the relocation table now - every variable is a global
    slot filled by whatever supplies it (doc/addendum.md) - so the check needs
    a body that actually sends one."""
    image = build("class C:\n    slot v: int = 0\n\nfn [C] go():\n    return 1\n")
    assert image.module_uses, "a message identity is still a relocation"
    image.module_uses = []
    with pytest.raises(CompileError, match="without listing them"):
        verify(image)


def test_a_referenced_name_that_is_never_read_is_caught():
    image = build("println(1)\n")
    image.add_message(["unused"])
    image.module_uses = [0, 1]
    with pytest.raises(CompileError, match="never reads them"):
        verify(image)


def test_an_instruction_running_past_the_end_of_a_body_is_caught():
    """A one-word op overwritten with a two-word one would have the VM read
    the next function's first word as its operands."""
    image = build("fn f():\n    return 1\n\nfn g():\n    return 2\n")
    end = image.functions[1].code_offset - 1
    image.code[end] = opcodes.pack("iter", a0=L(0), a1=L(0))[0]
    with pytest.raises(CompileError, match="runs past the end of the body"):
        verify(image)


def test_an_empty_window_base_is_not_range_checked():
    """`dict dst, base, 0` reads no registers, so its base may point anywhere -
    including one past the frame, which is where the compiler naturally leaves
    it. Checking it would reject correct code."""
    image = build("fn f(obj):\n    obj.seen = {}\n")
    offset = word_of(image, "dict")
    assert opcodes.unpack(image.code, offset)[2]["f"] == 0
    verify(image)
    # With a non-zero count the same base is checked, and caught.
    image.code[offset] = (image.code[offset] & 0xFFFF00FF) | (1 << 8)
    with pytest.raises(CompileError, match="but the frame holds"):
        verify(image)


def test_a_zero_value_return_needs_no_base_register():
    """Module init returns nothing and may hold no locals at all, so
    `return count=0`'s base is meaningless - the disassembler leaves it out
    for the same reason."""
    image = build("pass\n")
    # Nothing in this init needs a slot, and `return count=0` is all it emits.
    assert image.init_nlocals == 0
    assert opcodes.unpack(image.code, 0)[1] is False
    verify(image)
