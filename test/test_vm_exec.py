"""Executing straight-line code (wypoc/vm/interp.py, wypoc/vm/frame.py).

M1 of gen/bytecode-vm-plan.md: one function body, no calls and no jumps. The
tests come in three kinds.

*Hand-assembled bodies* pin each opcode's meaning against doc/wyc-format.md
§6 directly, encoded with the compiler's own `opcodes.pack`, so a test says
what an instruction does without a source program in the way.

*Compiled bodies* prove the loop runs what the compiler actually emits -
including `greet` from the Appendix A image, the fixture the whole toolchain
is anchored to.

*Equivalence* is the milestone's real gate: a body compiled from source
answers what the tree walker answers for the same call. The runtime under
both is the same code, and these tests are what keeps it that way.
"""

import struct

import pytest
from conftest import compile_bytecode_fixture as compile_fixture
from conftest import compile_bytecode_source as compile_source

from wypoc import wyrm_builtins
from wypoc import wyrm_eval_parse_tree as ev
from wypoc.compiler_bc import opcodes
from wypoc.compiler_bc.image import ModuleImage
from wypoc.parse import parse
from wypoc.vm import Frame, LoadedModule, TrapError, call_function, execute, load

L = opcodes.L
P = opcodes.P


# --------------------------------------------------------------------------
# harness


def module_of(*instructions, statics=(), symbols=(), nlocals=8):
    """A loadable image whose init routine is the given instructions.

    Hand-assembled, but through the real container: `to_wyc()` and `load()`
    are in the path, so a test body executes bytes that went through the same
    encode/decode the compiler and loader use on everything else.
    """
    image = ModuleImage("t")
    for value in statics:
        image.add_static(value)
    for name in symbols:
        image.add_symbol(name)
    image.init_nlocals = nlocals
    for words in instructions:
        image.emit(words)
    return LoadedModule(load(image.to_wyc()))


def run(*instructions, statics=(), symbols=(), nlocals=8, pframe=()):
    """Execute a hand-assembled body; answer `(returned values, frame)`."""
    module = module_of(*instructions, statics=statics, symbols=symbols, nlocals=nlocals)
    frame = Frame(nlocals, pframe)
    return execute(module, frame), frame


def ret(*instructions, base=0, count=1, **options):
    """Execute a body that ends by returning `count` values from `base`."""
    values, _frame = run(*instructions, opcodes.pack("return", a0=base, f=count), **options)
    return values[0] if count == 1 else values


# --------------------------------------------------------------------------
# loads (§6.1, §6.2, §6.3)


def test_lnil_lbool_and_lunset_load_the_runtime_s_own_values():
    values, _ = run(
        opcodes.pack("lnil", a0=L(0)),
        opcodes.pack("lbool", a0=L(1), f=1),
        opcodes.pack("lbool", a0=L(2), f=0),
        opcodes.pack("lunset", a0=L(3)),
        opcodes.pack("return", a0=L(0), f=4),
    )
    assert values[0] is wyrm_builtins.NIL
    assert values[1] is True and values[2] is False
    assert values[3] is ev.UNSET
    # Unset is an error value, and the VM must hand back the same one the
    # walker's `is_error` recognizes - §1.2, and what `jerr` will test.
    assert wyrm_builtins.is_error(values[3])


@pytest.mark.parametrize("value", [0, 1, -1, 127, -128])
def test_i8_loads_a_sign_extended_byte(value):
    assert ret(opcodes.pack_pairable("i8", L(0), value)) == value


@pytest.mark.parametrize("value", [128, -129, 70000, -70000, 2147483647, -2147483648])
def test_i32_carries_what_does_not_fit_in_a_byte(value):
    words = opcodes.pack_pairable("i8", L(0), value)
    assert len(words) == 2  # the wide form: i32
    assert ret(words) == value


def test_f32_is_the_single_precision_value_not_the_source_decimal():
    """§1.2: the machine's native float is binary32, so the literal was
    rounded when it was compiled, not here."""
    (bits,) = struct.unpack("<I", struct.pack("<f", 3.4))
    got = ret(opcodes.pack("f32", a0=L(0), w1=bits))
    assert got == struct.unpack("<f", struct.pack("<f", 3.4))[0]
    assert got != 3.4  # and the difference is real, not a rounding of nothing


def test_lconst_reads_the_static_pool():
    assert ret(opcodes.pack_pairable("lconst", 1, L(0)), statics=["first", "second"]) == "second"


def test_lsym_interns_a_symbol_not_a_string():
    got = ret(opcodes.pack_pairable("lsym", 0, L(0)), symbols=["name"])
    assert got == wyrm_builtins.Symbol("name")
    assert got != "name"


# --------------------------------------------------------------------------
# registers (§5.1)


def test_move_copies_between_l_slots():
    assert ret(
        opcodes.pack_pairable("i8", L(1), 42),
        opcodes.pack_pairable("move", L(0), L(1)),
    ) == 42


def test_a_p_register_reads_the_parameter_frame():
    assert ret(opcodes.pack_pairable("move", L(0), P(1)), pframe=["a", "b"]) == "b"


def test_a_p_register_is_writable_too():
    _values, frame = run(
        opcodes.pack_pairable("i8", P(0), 9),
        opcodes.pack("return", f=0),
        pframe=[None],
    )
    assert frame.p == [9]


def test_the_wide_form_reaches_registers_a_reg8_cannot():
    """A compact operand addresses L0-L127; above that the compiler must emit
    the wide form, and the two forms must mean the same thing (§5)."""
    words = opcodes.pack_pairable("move", L(0), L(200))
    assert len(words) == 2
    assert ret(
        opcodes.pack_pairable("i8", L(200), 7),
        words,
        nlocals=256,
    ) == 7


# --------------------------------------------------------------------------
# arithmetic and comparison (§6.3) - all of it the runtime's own


def binary(name, left, right):
    """`name` applied to two values, delivered through the P frame - which is
    the one way to hand the loop an arbitrary runtime value (a list, a class)
    without a pool that only holds the eight static types."""
    return ret(opcodes.pack(name, a0=L(0), a1=P(0), a2=P(1)), pframe=[left, right])


@pytest.mark.parametrize(
    "name, left, right, want",
    [
        ("add", 2, 3, 5),
        ("add", "ab", "cd", "abcd"),  # §6.3: `add` is concatenation too
        ("sub", 2, 3, -1),
        ("mul", 4, 5, 20),
        ("div", 7, 2, 3.5),
        ("mod", 7, 2, 1),
        ("pow", 2, 10, 1024),
        ("band", 6, 3, 2),
        ("bor", 6, 3, 7),
        ("bxor", 6, 3, 5),
        ("shl", 1, 4, 16),
        ("shr", 16, 3, 2),
        ("eq", 2, 2, True),
        ("ne", 2, 2, False),
        ("lt", 2, 3, True),
        ("le", 3, 3, True),
        ("gt", 2, 3, False),
        ("ge", 3, 3, True),
    ],
)
def test_binops_are_the_interpreter_s_binops(name, left, right, want):
    assert binary(name, left, right) == want


def test_a_failing_operation_yields_an_error_value_rather_than_raising():
    """§1.2: errors are values. Division by zero is `_safe_div`'s WyrmError
    here exactly as it is in interpreted source."""
    got = binary("div", 1, 0)
    assert wyrm_builtins.is_error(got)


def test_cmp3_is_three_way():
    assert binary("cmp3", 2, 5) == -1
    assert binary("cmp3", 5, 5) == 0
    assert binary("cmp3", 5, 2) == 1


def test_in_tests_membership_of_the_container():
    assert binary("in", 2, [1, 2, 3]) is True
    assert binary("in", 9, [1, 2, 3]) is False


def test_is_checks_a_primitive_type_value():
    assert binary("is", 3, wyrm_builtins.INT) is True
    assert binary("is", "3", wyrm_builtins.INT) is False


def test_is_accepts_a_tuple_of_types_for_a_sum_type():
    """§6.3: `a2` holds a class value, or a tuple of them for a sum type."""
    types = (wyrm_builtins.INT, wyrm_builtins.STR)
    assert binary("is", "x", types) is True
    assert binary("is", 1.5, types) is False


def test_is_checks_a_primitive_named_by_a_string():
    """§6.3: a primitive type reaches `is` as its *name*, which is how the
    compiler emits every one of them - `error` is a Class, `tuple` a
    constructor function and `list` unbound, so the bindings cannot answer
    what the walker's name table answers."""
    assert binary("is", 3, "int") is True
    assert binary("is", "3", "int") is False
    assert binary("is", ev.UNSET, "error") is True
    assert binary("is", [1], "list") is True
    assert binary("is", (1, 2), "tuple") is True
    assert binary("is", 3, "nosuchtype") is False


def test_is_accepts_a_sum_type_mixing_a_class_and_a_primitive_name():
    assert binary("is", 3, (wyrm_builtins.STR, "int")) is True
    assert binary("is", 1.5, (wyrm_builtins.STR, "int")) is False


@pytest.mark.parametrize(
    "name, value, want",
    [("neg", 5, -5), ("inv", 5, -6), ("not", 5, False), ("not", 0, True)],
)
def test_unary_ops(name, value, want):
    assert ret(
        opcodes.pack_pairable("i8", L(1), value),
        opcodes.pack_pairable(name, L(0), L(1)),
    ) == want


# --------------------------------------------------------------------------
# return (§6.1) - the window, not yet the backfill


def test_return_hands_back_its_whole_window():
    values, _ = run(
        opcodes.pack_pairable("i8", L(1), 1),
        opcodes.pack_pairable("i8", L(2), 2),
        opcodes.pack("return", a0=L(1), f=2),
    )
    assert values == [1, 2]


def test_a_zero_count_return_ignores_its_base():
    """§1.3: an empty window reads nothing, and its base may point anywhere -
    including one past the frame."""
    values, _ = run(opcodes.pack("return", a0=L(99), f=0), nlocals=2)
    assert values == []


# --------------------------------------------------------------------------
# stopping loudly


def test_trap_zero_names_the_uncompiled_body_it_usually_means():
    with pytest.raises(TrapError) as caught:
        run(opcodes.pack("trap", f=0))
    assert "could not lower" in str(caught.value)
    assert caught.value.offset == 0


def test_trap_reports_an_unknown_code_as_itself():
    with pytest.raises(TrapError, match="trap 7"):
        run(opcodes.pack("noop"), opcodes.pack("trap", f=7))


@pytest.mark.parametrize("name, words", [
    ("super", opcodes.pack("super", a0=L(0), f=0, a1=1)),
    ("return_cps", opcodes.pack("return_cps", a0=L(0), f=0, a1=1)),
    ("new_primitive", opcodes.pack("new_primitive", a0=L(0), f=0)),
])
def test_an_opcode_this_milestone_does_not_implement_stops_the_program(name, words):
    """The cross-cutting rule: never a silent no-op. Wrong output costs far
    more than a stop, and these are all opcodes later milestones fill in."""
    with pytest.raises(TrapError) as caught:
        run(opcodes.pack("noop"), words)
    assert name in str(caught.value)
    assert caught.value.offset == 1


@pytest.mark.parametrize("word", [0x0000003F, 0x00000051])
def test_a_reserved_opcode_is_invalid_rather_than_ignored(word):
    with pytest.raises(TrapError, match="invalid opcode"):
        run([word])


def test_a_two_word_instruction_at_the_end_of_the_code_section_stops():
    """The verifier keeps this out of any image the compiler writes; the loop
    still refuses to read a word that is not there."""
    first, _second = opcodes.pack("add", a0=L(0), a1=L(1), a2=L(2))
    with pytest.raises(TrapError, match="past the end"):
        run([first])


# --------------------------------------------------------------------------
# compiled bodies


def test_greet_from_the_appendix_a_image_runs():
    """The anchor fixture. `greet` takes one parameter in P0, loads a static
    and concatenates - which is the whole of M1 in four words."""
    module = LoadedModule(load(compile_fixture("hello.wy").to_wyc()))
    assert call_function(module, 0, ["World"]) == ["Hello World"]


def test_calling_a_body_with_the_wrong_number_of_arguments_is_refused():
    module = LoadedModule(load(compile_fixture("hello.wy").to_wyc()))
    with pytest.raises(TrapError, match="missing required argument: 'name'"):
        call_function(module, 0, [])


ARITHMETIC = [
    ("fn f(a, b):\n    return a * b + 1\n", [6, 7]),
    ("fn f(a, b):\n    return (a - b) / 2\n", [9, 3]),
    ("fn f(a, b):\n    return a % b == 0\n", [9, 3]),
    ("fn f(a, b):\n    return -a + ~b\n", [5, 2]),
    ("fn f(a, b):\n    return a <=> b\n", [5, 2]),
    ("fn f(a):\n    return \"n=\" + a\n", ["x"]),
    ("fn f(a):\n    return not a\n", [0]),
]


@pytest.mark.parametrize("source, args", ARITHMETIC)
def test_a_compiled_body_answers_what_the_interpreter_answers(source, args):
    """The milestone's gate: same source, same arguments, same value - one
    reached through the tree walker, the other through the dispatch loop."""
    ctx = {}
    ev.eval_program(parse(source), ctx)
    expected = ev.call_value(ev.unwrap(ctx["f"]), list(args), {})

    module = LoadedModule(load(compile_source(source).to_wyc()))
    assert call_function(module, 0, args) == [expected]


# --------------------------------------------------------------------------
# jumps (§5.2)


def test_a_forward_jump_skips_the_instructions_it_counts():
    """An offset counts words from the instruction *after* the jump, so a
    `jmp +2` steps over one two-word instruction."""
    assert ret(
        opcodes.pack_pairable("i8", L(0), 1),
        opcodes.pack_pairable("jmp", 0, 2),
        opcodes.pack("add", a0=L(0), a1=L(0), a2=L(0)),  # skipped: two words
    ) == 1


def test_a_backward_jump_loops():
    """`while i < 3: i = i + 1`, hand-assembled: the loop body ends with a
    negative offset back to the test."""
    body = [
        opcodes.pack_pairable("i8", L(0), 0),   # i
        opcodes.pack_pairable("i8", L(1), 3),   # limit
        # loop test (word 2):
        opcodes.pack("lt", a0=L(2), a1=L(0), a2=L(1)),
        opcodes.pack_pairable("jf", L(2), 4),   # exit past the body
        opcodes.pack_pairable("i8", L(3), 1),
        opcodes.pack("add", a0=L(0), a1=L(0), a2=L(3)),
        opcodes.pack_pairable("jmp", 0, -7),    # back to the test
    ]
    assert ret(*body) == 3


@pytest.mark.parametrize(
    "name, value, jumps",
    [
        ("jf", 0, True), ("jf", 1, False),
        ("jt", 1, True), ("jt", 0, False),
    ],
)
def test_the_conditional_jumps_test_truthiness(name, value, jumps):
    taken = ret(
        opcodes.pack_pairable("i8", L(1), value),
        opcodes.pack_pairable(name, L(1), 2),
        opcodes.pack_pairable("i8", L(0), 0),   # not taken lands here
        opcodes.pack_pairable("jmp", 0, 1),
        opcodes.pack_pairable("i8", L(0), 1),   # taken lands here
    )
    assert taken == (1 if jumps else 0)


@pytest.mark.parametrize("name, jumps_on_error", [("jerr", True), ("jnerr", False)])
@pytest.mark.parametrize("erroneous", [True, False])
def test_jerr_and_jnerr_test_error_ness(name, jumps_on_error, erroneous):
    """§1.2: errors are ordinary values, and these are the only instructions
    that inspect that property."""
    value = wyrm_builtins.error("nope") if erroneous else "fine"
    taken = ret(
        opcodes.pack_pairable(name, P(0), 2),
        opcodes.pack_pairable("i8", L(0), 0),
        opcodes.pack_pairable("jmp", 0, 1),
        opcodes.pack_pairable("i8", L(0), 1),
        pframe=[value],
    )
    assert taken == (1 if erroneous == jumps_on_error else 0)


# --------------------------------------------------------------------------
# data construction (§6.3)


def loaded_window(*values):
    """Instructions loading `values` into L1.. through the P frame."""
    return [opcodes.pack_pairable("move", L(1 + i), P(i)) for i in range(len(values))]


def built(name, *values, count=None):
    count = len(values) if count is None else count
    return ret(
        *loaded_window(*values),
        opcodes.pack(name, a0=L(0), a1=L(1), f=count),
        pframe=list(values),
    )


def test_tuple_and_list_take_a_window_of_values():
    assert built("tuple", 1, 2, 3) == (1, 2, 3)
    assert built("list", 1, 2, 3) == [1, 2, 3]
    assert built("tuple") == ()


def test_dict_reads_its_window_as_key_value_pairs():
    """§6.3: `f` is the *pair* count, so the window is twice as wide."""
    assert built("dict", "a", 1, "b", 2, count=2) == {"a": 1, "b": 2}


def test_plist_builds_a_proper_pair_list():
    got = built("plist", 6, 7)
    assert isinstance(got, wyrm_builtins.Pair)
    assert (got.car, got.cdr.car, got.cdr.cdr) == (6, 7, wyrm_builtins.NIL)


def test_a_pair_list_cell_is_the_capture_box_the_compiler_builds():
    """A mutated captured variable becomes a one-element `$[...]` the closure
    reads and writes through `getidx`/`setidx` - no VM support at all, which
    is the point. Asserted here because nothing else in the loop proves the
    box is writable."""
    values, _ = run(
        opcodes.pack_pairable("i8", L(1), 0),          # the index
        opcodes.pack_pairable("move", L(3), P(0)),
        opcodes.pack("plist", a0=L(2), a1=L(3), f=1),  # the cell
        opcodes.pack_pairable("i8", L(4), 9),
        opcodes.pack("setidx", a0=L(2), a1=L(1), a2=L(4)),
        opcodes.pack("getidx", a0=L(0), a1=L(2), a2=L(1)),
        opcodes.pack("return", a0=L(0), f=1),
        pframe=["before"],
    )
    assert values == [9]


# --------------------------------------------------------------------------
# unpack (§6.3)


def test_unpack_spreads_an_indexable_across_registers():
    values, _ = run(
        opcodes.pack_pairable("move", L(4), P(0)),
        opcodes.pack("unpack", a0=L(0), a1=L(4), f=3),
        opcodes.pack("return", a0=L(0), f=3),
        pframe=[[1, 2, 3]],
    )
    assert values == [1, 2, 3]


@pytest.mark.parametrize("source", [[1, 2], 7])
def test_a_botched_unpack_fills_every_register_with_the_error(source):
    """§6.3 is explicit about this: a length *or* type mismatch writes the
    error to every destination, so no register is left holding a stale value
    that would read as a real one."""
    values, _ = run(
        opcodes.pack_pairable("move", L(4), P(0)),
        opcodes.pack("unpack", a0=L(0), a1=L(4), f=3),
        opcodes.pack("return", a0=L(0), f=3),
        pframe=[source],
    )
    assert len(values) == 3
    assert all(wyrm_builtins.is_error(value) for value in values)


# --------------------------------------------------------------------------
# object access (§6.3)


def index_read(container, key):
    return ret(
        opcodes.pack("getidx", a0=L(0), a1=P(0), a2=P(1)),
        pframe=[container, key],
    )


def test_getidx_is_the_interpreter_s_own_index_rules():
    assert index_read([10, 20], 1) == 20
    assert index_read("A", 0) == ord("A")          # a string indexes to a codepoint
    assert index_read({"k": 1}, "k") == 1
    assert index_read({"k": 1}, "absent") is ev.UNSET  # a missing key is Unset
    assert wyrm_builtins.is_error(index_read([1], 9))  # and a bad index is an error value


def test_setidx_writes_through_the_container():
    items = [1, 2, 3]
    values, _ = run(
        opcodes.pack("setidx", a0=P(0), a1=P(1), a2=P(2)),
        opcodes.pack("return", f=0),
        pframe=[items, 1, 99],
    )
    assert values == [] and items == [1, 99, 3]


def instance():
    ctx = ev.Scope()
    ev.populate_globals(ctx)
    ev.eval_program(parse("class Box:\n    slot v: int = 1\n\nb := Box()\n"), ctx)
    return ev.unwrap(ctx["b"])


def test_getattr_and_setattr_address_the_property_namespace():
    box = instance()
    assert ret(opcodes.pack("getattr", a0=L(0), a1=P(0), a2=0),
               symbols=["v"], pframe=[box]) == 1

    run(
        opcodes.pack("setattr", a0=P(0), a1=0, a2=P(1)),
        opcodes.pack("return", f=0),
        symbols=["v"],
        pframe=[box, 42],
    )
    assert ev.unwrap(box.attrs["v"]) == 42


# --------------------------------------------------------------------------
# iteration (§6.3)


def test_iter_and_itnext_walk_a_value_and_jump_when_it_is_spent():
    """The `for` loop's shape: `itnext` writes the next value or takes its
    exhaustion jump, and the sum comes out at the end."""
    values, _ = run(
        opcodes.pack_pairable("i8", L(0), 0),           # total
        opcodes.pack("iter", a0=L(1), a1=P(0)),
        # loop head (word 3):
        opcodes.pack("itnext", a0=L(2), a1=L(1), a2=3),  # done -> past the jump
        opcodes.pack("add", a0=L(0), a1=L(0), a2=L(2)),
        opcodes.pack_pairable("jmp", 0, -5),
        opcodes.pack("return", a0=L(0), f=1),
        pframe=[[1, 2, 3, 4]],
    )
    assert values == [10]


def test_iteration_goes_through_the_interpreter_s_own_contract():
    """`iter` is `_iter_values`, so anything the walker can iterate - a
    string, a dict, a pair list - iterates identically here, down to a string
    yielding one-character strings rather than the codepoints `getidx` gives.
    Matching the walker is the whole point; agreeing with `getidx` is not."""
    assert ret(
        opcodes.pack("iter", a0=L(1), a1=P(0)),
        opcodes.pack("itnext", a0=L(0), a1=L(1), a2=0),
        pframe=["xyz"],
    ) == "x"


# --------------------------------------------------------------------------
# defers (§6.3, §10)


def test_a_defer_runs_when_the_frame_returns():
    ran, values = _deferred(mode=0, result=1)
    assert values == [1] and ran == [True]


def test_a_defer_on_error_runs_only_when_the_frame_leaves_with_an_error():
    """§6.3 mode 1. The condition is the one `run_scoped_block` applies in the
    tree walker: an error value on the way out."""
    ran, _values = _deferred(mode=1, result=1)
    assert ran == []

    ran, values = _deferred(mode=1, result=wyrm_builtins.error("bad"))
    assert wyrm_builtins.is_error(values[0]) and ran == [True]


def test_defers_run_most_recently_armed_first():
    order = []
    module = module_of(
        opcodes.pack("defer_reg", a0=P(0), f=0),
        opcodes.pack("defer_reg", a0=P(1), f=0),
        opcodes.pack("return", f=0),
    )
    frame = Frame(4, [lambda: order.append("first"), lambda: order.append("second")])
    execute(module, frame)
    assert order == ["second", "first"]


def test_a_defer_runs_even_when_the_frame_leaves_by_a_trap():
    ran = []
    module = module_of(
        opcodes.pack("defer_reg", a0=P(0), f=0),
        opcodes.pack("trap", f=0),
    )
    with pytest.raises(TrapError):
        execute(module, Frame(4, [lambda: ran.append(True)]))
    assert ran == [True]


def _deferred(mode, result):
    """Arm one defer over a Python callable, then return `result`; answer what
    the defer recorded and what the frame returned."""
    ran = []
    module = module_of(
        opcodes.pack("defer_reg", a0=P(0), f=mode),
        opcodes.pack_pairable("move", L(0), P(1)),
        opcodes.pack("return", a0=L(0), f=1),
    )
    values = execute(module, Frame(4, [lambda: ran.append(True), result]))
    return ran, values


# --------------------------------------------------------------------------
# slots by number (§8.6)


SLOTTED = (
    "class Base:\n"
    "    slot first: int = 1\n"
    "    slot second: int = 2\n"
    "\n"
    "class Derived(Base):\n"
    "    slot third: int = 3\n"
)


def derived_instance():
    """An instance of a compiled two-level class, plus its module."""
    from wypoc.vm import load_module

    module = load_module(load(compile_source(SLOTTED, name="slotted").to_wyc()))
    return module, ev.new_instance(module.namespace()["Derived"])


@pytest.mark.parametrize("slot, want", [(0, 1), (1, 2), (2, 3)])
def test_getslot_numbers_slots_base_first(slot, want):
    """§8.6: "instances lay slots out base-first: every ancestor's slots in
    order from the root, then the class's own". Nothing emits `getslot` yet -
    the compiler ships the optimization disabled - so this is what pins the
    numbering the format promises."""
    _module, instance = derived_instance()
    assert ret(
        opcodes.pack("getslot", a0=L(0), a1=P(0), a2=slot),
        pframe=[instance],
    ) == want


def test_setslot_writes_the_same_slot_getslot_reads():
    _module, instance = derived_instance()
    values, _ = run(
        opcodes.pack("setslot", a0=P(0), a1=2, a2=P(1)),
        opcodes.pack("getslot", a0=L(0), a1=P(0), a2=2),
        opcodes.pack("return", a0=L(0), f=1),
        pframe=[instance, 99],
    )
    assert values == [99]
    assert ev.unwrap(instance.attrs["third"]) == 99


# --------------------------------------------------------------------------
# the `::` namespace (§7.3) - the only lookup done at execution time


def scope_module():
    """A compiled module with a global and a class static to reach into."""
    from wypoc.vm import load_module

    return load_module(load(compile_source(
        "UNITS := \"cm\"\n"
        "\n"
        "class Counter:\n"
        "    static made: int = 0\n",
        name="scoped",
    ).to_wyc()))


def test_getscope_reads_a_module_member_by_name():
    module = scope_module()
    assert ret(
        opcodes.pack("getscope", a0=L(0), a1=P(0), a2=0),
        symbols=["UNITS"],
        pframe=[module.as_module()],
    ) == "cm"


def test_setscope_writes_through_the_binding():
    module = scope_module()
    run(
        opcodes.pack("setscope", a0=P(0), a1=0, a2=P(1)),
        opcodes.pack("return", f=0),
        symbols=["UNITS"],
        pframe=[module.as_module(), "m"],
    )
    assert module.namespace()["UNITS"] == "m"


def test_a_class_static_is_reached_through_the_class(monkeypatch):
    """§8.6: a class static lives in a module global slot, and the class is
    what names it - so `Counter::made` is a `::` lookup like a module's."""
    module = scope_module()
    counter = module.namespace()["Counter"]
    from wypoc.vm import link

    binding = link.scope_member(module, counter, "made")
    assert binding is not link.MISSING
    binding.value = 7
    assert module.namespace()["Counter::made"] == 7


def test_getscope_stops_when_the_namespace_has_no_such_member():
    module = scope_module()
    with pytest.raises(TrapError, match="nosuch"):
        run(
            opcodes.pack("getscope", a0=L(0), a1=P(0), a2=0),
            symbols=["nosuch"],
            pframe=[module.as_module()],
        )


# --------------------------------------------------------------------------
# the spread forms and the write-through name (§6.2, §6.3)
#
# Nothing in the corpus emits these yet - `f(*a, **k)` needs a call the
# samples do not make, and `mod::x = e` has no source spelling at all
# (doc/llm-bytecode.md §9) - so they are pinned here rather than by a fixture.


def module_with_reloc(*instructions, kind=None, path=("name",), nlocals=8):
    """A module whose relocation 0 names `path`, plus a hand-assembled init."""
    image = ModuleImage("relocs")
    image.add_message(list(path))
    image.init_nlocals = nlocals
    for words in instructions:
        image.emit(words)
    return LoadedModule(load(image.to_wyc()))


def test_call_va_reads_a_positional_tuple_and_a_keyword_dict():
    """§6.3: callee, then the tuple and dict the compiler already joined."""
    def joined(*args, sep="-"):
        return sep.join(args)

    module = module_of(
        opcodes.pack_pairable("move", L(0), P(0)),
        opcodes.pack_pairable("move", L(1), P(1)),
        opcodes.pack_pairable("move", L(2), P(2)),
        opcodes.pack("call_va", a0=L(0), a1=1),
        opcodes.pack("return", a0=L(0), f=1),
    )
    values = execute(module, Frame(8, [joined, ("a", "b"), {"sep": "+"}]))
    assert values == ["a+b"]


def test_msg_va_dispatches_with_the_same_two_registers():
    module = module_with_reloc(
        opcodes.pack_pairable("move", L(0), P(0)),
        opcodes.pack_pairable("move", L(1), P(1)),
        opcodes.pack_pairable("move", L(2), P(2)),
        opcodes.pack("msg_va", a0=L(0), a1=0, a2=1),
        opcodes.pack("return", a0=L(0), f=1),
        path=("greet",),
    )
    # Reading the entry is what creates the identity now - a message binds on
    # first read rather than in a batch (doc/addendum.md).
    module.message("greet").add_overload(
        (None,), ev.NativeBody(lambda this, *args, **named: (this, args, named)), {}
    )
    values = execute(module, Frame(8, ["me", ("x",), {"loudly": True}]))
    assert values == [("me", ("x",), {"loudly": True})]


def test_a_store_through_something_that_is_not_a_namespace_faults():
    """§10, decided: a failed store stops the program at the instruction that
    could not perform it - it never becomes a value some later `jerr` may or
    may not look at.

    `rset` used to be the example, as the write-through twin of a relocation
    read. Both are retired: a module writes its own globals with `gset`, and
    a name it does not define is a free slot nothing in this module assigns
    (doc/addendum.md). `setscope` is the remaining store with no result
    register, so it is where the rule is checked."""
    module = module_with_reloc(
        opcodes.pack_pairable("i8", L(0), 7),
        opcodes.pack("setscope", a0=L(0), a1=L(0), a2=0),
        opcodes.pack("return", f=0),
        path=("target",),
    )
    with pytest.raises(TrapError):
        execute(module, Frame(8, []))

def test_new_instance_builds_an_uninitialised_instance():
    """§6.3's runtime-internal constructor: slots at their defaults, no init.
    No compiler emits it - script code constructs by calling the class."""
    module, _instance = derived_instance()
    values, _ = run(
        opcodes.pack("new_instance", a0=L(0), a1=P(0)),
        opcodes.pack("getslot", a0=L(1), a1=L(0), a2=2),
        opcodes.pack("return", a0=L(1), f=1),
        pframe=[module.namespace()["Derived"]],
    )
    assert values == [3]
