"""End-to-end tests for the bytecode compiler: source in, module image out.

The gate here is Appendix A of doc/llm-bytecode.md. `test/bytecode/hello.wy`
is the worked example's source, `test/bytecode/hello.wy_a` is the listing it
must compile to, and the appendix's own hex for the sections it writes out in
full is asserted separately - so the golden cannot drift from the spec by
being regenerated.

Every fixture under test/bytecode/ is also run by the POC interpreter, so the
semantics a golden encodes keep being exercised even before a VM exists.
"""

import os
import re
import shutil
import subprocess

import pytest
from conftest import REPO_ROOT

from wypoc import wyrm_io, wyrm_modules
from wypoc.compiler_bc import CompileError, assemble_wya, compile_module
from wypoc.compiler_bc.image import SECTION_IDS
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import eval_program, populate_globals

BYTECODE_DIR = os.path.join(REPO_ROOT, "test", "bytecode")

# The `code` section exactly as Appendix A writes it out, one instruction per
# line - the bytes a compiled hello.wy has to produce.
APPENDIX_A_CODE = bytes.fromhex(
    "A8 00 00 00 00 00 00 00"    # closure L0 <- fn#0, 0 caps
    "43 00 00 00"                # gset g0 <- L0        (greet)
    "42 00 01 00"                # gget L0 <- g1        (println)
    "42 01 00 00"                # gget L1 <- g0        (greet)
    "46 02 01 00"                # lconst L2 <- static#1 ("World")
    "A0 01 01 00 00 00 01 00"    # call base=L1 argc=1 nres=1
    "A0 01 00 00 00 00 01 00"    # call base=L0 argc=1 nres=1
    "02 00 00 00"                # return count=0
    "46 00 00 00"                # lconst L0 <- static#0 ("Hello ")
    "85 00 01 00 00 80 00 00"    # add L1 <- L0 + P0    (name)
    "02 01 01 00"                # return L1 count=1
)


def fixture_source(name: str) -> str:
    with open(os.path.join(BYTECODE_DIR, name)) as f:
        return f.read()


def compile_fixture(name: str, **options):
    """Compile one fixture by its path relative to test/bytecode/.

    The fixture's own directory is a module search root while it compiles, so
    one that imports a neighbour - a decorator library, say - resolves
    exactly as it would from the shell.
    """
    source = fixture_source(name)
    module_name = os.path.splitext(os.path.basename(name))[0]
    directory = os.path.dirname(os.path.join(BYTECODE_DIR, name))
    previous = wyrm_modules.set_script_root(directory)
    try:
        image = compile_module(parse(source, filename=name), module_name, name,
                               **options)
    finally:
        wyrm_modules.set_script_root(previous)
    assert not image.unlowered, f"{name} stubbed a function: {image.unlowered}"
    return image


def fixture_names(suffix=".wy"):
    """Every fixture under test/bytecode/, subdirectories included."""
    found = []
    for root, _dirs, files in os.walk(BYTECODE_DIR):
        for name in files:
            if name.endswith(suffix):
                path = os.path.join(root, name)
                found.append(os.path.relpath(path, BYTECODE_DIR))
    return sorted(found)


def compile_source(source: str, module_name: str = "m", **options):
    """Compile a snippet, strictly: a body that will not lower is a refusal
    here rather than a trapping stub, so a gap still fails a test."""
    options.setdefault("stub_unlowered", False)
    return compile_module(
        parse(source, filename=f"{module_name}.wy"), module_name, **options
    )


# --------------------------------------------------------------------------
# the Appendix A gate


def test_hello_compiles_to_the_appendix_bytes():
    image = compile_fixture("hello.wy")
    assert image.sections()[SECTION_IDS["code"]] == APPENDIX_A_CODE


GOLDEN_FIXTURES = fixture_names()


@pytest.mark.parametrize("name", GOLDEN_FIXTURES)
def test_fixture_matches_its_golden_listing(name):
    assert compile_fixture(name).to_wya() == fixture_source(name + "_a")


@pytest.mark.parametrize("name", GOLDEN_FIXTURES)
def test_golden_listing_assembles_to_the_same_binary(name):
    image = compile_fixture(name)
    assert assemble_wya(fixture_source(name + "_a")) == image.to_wyc()


def test_an_import_produces_no_table_entry_at_all():
    """`import` and `import_star` carry everything they need as operands: the
    path is a constant string, and a wildcard's except-list is a window of
    interned symbols the instruction reads the way `tuple` reads its items.

    Nothing about an import is a *reference to a name that gets bound*, which
    is what a table entry is for - so it does not get one. Section 7 holds
    message identities alone (doc/addendum.md)."""
    image = compile_fixture("wildcard/paint.wy")
    assert image.messages == [], "an import is not a message"
    listing = code_listing(image)
    assert any(line.startswith("import_star ") for line in listing), \
        "fixture must still exercise a wildcard"
    assert any(line.startswith("import ") for line in listing), \
        "fixture must still exercise a plain import"
    # The paths they carry are ordinary statics, interned like any constant.
    assert "palette" in image.statics


def test_a_wildcard_except_list_is_a_symbol_window():
    """`except (a, b)` lowers to two `lsym`s and a count, not to a table entry
    with an `x` field - which is what made a wildcard the one thing that could
    never be deduplicated."""
    image = compile_source("import a::* except (File, Reader)\nprintln(1)\n")
    listing = code_listing(image)
    star = next(i for i, line in enumerate(listing) if line.startswith("import_star"))
    assert listing[star - 2].startswith("lsym ")
    assert listing[star - 1].startswith("lsym ")
    assert listing[star].endswith("2 names"), listing[star]
    assert {"File", "Reader"} <= set(image.symbols)


def test_hello_image_tables():
    image = compile_fixture("hello.wy")
    assert image.statics == ["Hello ", "World"]
    # `println` is a name this module reads and does not define, so it is a
    # free global slot on the fill list - not a symbol, and not a relocation
    # (doc/addendum.md). Nothing external is left to intern.
    assert image.symbols == []
    assert [slot.name for slot in image.globals] == ["greet", "println"]
    assert image._free_doc() == {"println": 1}
    assert image._exports_doc() == {"greet": 0}
    assert image.messages == []

    (greet,) = image.functions
    assert (greet.name, greet.nlocals, greet.code_offset) == ("greet", 2, 11)
    assert [param.name for param in greet.params] == ["name"]


# --------------------------------------------------------------------------
# the fixtures stay interpreter-clean


@pytest.mark.parametrize("name", fixture_names())
def test_fixture_runs_under_the_interpreter(name, capsys):
    scope = {}
    populate_globals(scope)
    # wyrm's __STDOUT was bound to sys.stdout at import time, before pytest
    # swapped it out - _reset_std_handles exists for exactly this.
    wyrm_io._reset_std_handles()
    # A fixture in a subdirectory may import its neighbour, which resolves
    # through the script's own directory exactly as it would from the shell.
    directory = os.path.dirname(os.path.join(BYTECODE_DIR, name))
    previous = wyrm_modules.set_script_root(directory)
    try:
        eval_program(parse(fixture_source(name)), scope)
    finally:
        wyrm_modules.set_script_root(previous)
    if name == "hello.wy":
        assert capsys.readouterr().out == "Hello World\n"


# --------------------------------------------------------------------------
# lowering details


def test_a_call_argument_reuses_the_window_slot_it_lands_in():
    """A nested call's own window starts at the argument slot of the outer
    call, so no `move` is needed to place its result - this is why Appendix A
    has two `call`s and no copies between them."""
    image = compile_source('println(println("x"))\n')
    listing = image.to_wya()
    assert "move" not in listing
    assert listing.count("; call") == 2


def test_a_local_argument_is_copied_into_the_window():
    """A value that already lives in a register elsewhere has to be copied up
    into the call window, since arguments must be contiguous."""
    image = compile_source("fn f(a):\n    return println(a, a)\n")
    assert "; move" in image.to_wya()


def test_bare_return_returns_one_nil():
    image = compile_source("fn f():\n    return\n")
    listing = image.to_wya()
    assert "; lnil L0" in listing
    assert "; return L0 count=1" in listing


def test_a_body_that_falls_off_the_end_still_returns():
    # The body's value is its last statement's, so the call result is moved
    # into the reserved result register R and returned from there.
    image = compile_source('fn f():\n    println("x")\n')
    tail = body_listing(image, "f")[-2:]
    assert tail == ["move L0 <- L1", "return L0 count=1"]


def test_pass_compiles_to_nothing():
    """D6: `pass` emits no instruction at all; `noop` is for patching."""
    with_pass = compile_source("fn f():\n    pass\n").code
    without = compile_source("fn f():\n    return\n").code
    assert with_pass == without


def test_module_init_ends_with_a_zero_return():
    """Init used to open with `resolve`, which bound the whole referenced-name
    set before the first use of an external name. There is nothing to batch
    now (doc/addendum.md), so init starts straight into its own code."""
    listing = code_listing(compile_source('println("x")\n'))
    assert "resolve" not in listing
    assert listing[0].startswith("gget ")
    assert listing[-1] == "return count=0"


def test_integers_use_the_compact_form_when_they_fit():
    small = compile_source("fn f():\n    return 5\n")
    big = compile_source("fn f():\n    return 100000\n")
    assert "; i8 L0 <- 5" in small.to_wya()
    assert "; i32 L0 <- 100000" in big.to_wya()


def test_a_module_global_is_read_through_gget_not_a_relocation():
    """Everything is a `gget` now - a name the module defines and a name it
    only reads differ in which slot they land in and who fills it, not in how
    they are read (doc/addendum.md)."""
    image = compile_source("fn f():\n    return 1\n\nf()\n")
    assert "; gget" in image.to_wya()
    assert image.messages == []


def test_a_function_may_call_one_defined_later():
    image = compile_source("fn a():\n    return b()\n\nfn b():\n    return 1\n")
    assert [slot.name for slot in image.globals] == ["a", "b"]
    assert image.messages == []


# --------------------------------------------------------------------------
# fail loud


@pytest.mark.parametrize(
    "source, message",
    [
        ("fn f():\n    yield 1\n", "`yield` is only meaningful"),
        ("with pi: float = 3.14\n", "removed from the language"),
        ("class C:\n    signal changed()\n", "`signal`"),
        ("class C(A, B):\n    slot x: int\n", "records one superclass"),
        ("class C:\n    slot x: int = f()\n", "must be a constant"),
        ("fn f():\n    return super()\n", "`super` is only meaningful"),
        ("fn f():\n    return this\n", "`this` is only meaningful"),
        ("fn f():\n    return 2147483648\n", "does not fit in an i32"),
        ("fn f(a=1 + 1):\n    return a\n", "must be a constant"),
        ("fn f(*a, b):\n    return a\n", "follows a collecting parameter"),
        ("println(nope)\n", "undefined name 'nope'"),
        ("nope = 1\n", "not declared in this module"),
        ("fn f():\n    break\n", "`break` outside a loop"),
        ("fn f():\n    continue\n", "`continue` outside a loop"),
        ("a, b, c := 1, 2\n", "3 target(s) but 2 value(s)"),
    ],
)
def test_unsupported_constructs_fail_loud(source, message):
    with pytest.raises(CompileError) as excinfo:
        compile_source(source)
    assert message in str(excinfo.value)


def test_a_compile_error_names_its_source_position():
    with pytest.raises(CompileError) as excinfo:
        compile_source('println("ok")\nx := nope\n')
    assert excinfo.value.pos is not None
    assert "line 2" in str(excinfo.value)


def test_every_sample_either_compiles_or_says_why():
    """No sample may crash the compiler: the whole corpus either compiles or
    raises a CompileError naming the construct, never a traceback."""
    samples_dir = os.path.join(REPO_ROOT, "wypoc", "samples")
    for name in sorted(os.listdir(samples_dir)):
        if not name.endswith(".wy"):
            continue
        with open(os.path.join(samples_dir, name)) as f:
            source = f.read()
        try:
            tree = parse(source, filename=name)
        except SyntaxError:  # a sample that only the tokenizer tests care about
            continue
        try:
            compile_module(tree, os.path.splitext(name)[0], name)
        except CompileError:
            pass


# --------------------------------------------------------------------------
# the CLI


_VENV_WYRM = os.path.join(REPO_ROOT, ".venv", "bin", "wyrm")
WYRM = _VENV_WYRM if os.path.isfile(_VENV_WYRM) else shutil.which("wyrm")


@pytest.mark.skipif(
    not WYRM or not os.path.isfile(WYRM), reason="wyrm console script not installed"
)
def test_build_bc_writes_all_three_containers(tmp_path):
    source = tmp_path / "hello.wy"
    source.write_text(fixture_source("hello.wy"))
    out = tmp_path / "build"
    result = subprocess.run(
        [WYRM, "--build-bc", "-o", str(out), str(source)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (out / "hello.wy_a").read_text() == fixture_source("hello.wy_a")
    assert (out / "hello.wyc").read_bytes() == assemble_wya(fixture_source("hello.wy_a"))
    assert (out / "hello.c").exists()


@pytest.mark.skipif(
    not WYRM or not os.path.isfile(WYRM), reason="wyrm console script not installed"
)
def test_build_bc_emit_selects_containers(tmp_path):
    source = tmp_path / "hello.wy"
    source.write_text(fixture_source("hello.wy"))
    result = subprocess.run(
        [WYRM, "--build-bc", "--emit", "wya", str(source)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "hello.wy_a").exists()
    assert not (tmp_path / "hello.wyc").exists()


@pytest.mark.skipif(
    not WYRM or not os.path.isfile(WYRM), reason="wyrm console script not installed"
)
def test_build_bc_reports_an_unsupported_construct_without_a_traceback(tmp_path):
    source = tmp_path / "bad.wy"
    source.write_text("class C:\n    signal changed()\n")
    result = subprocess.run(
        [WYRM, "--build-bc", str(source)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "`signal`" in result.stderr
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------
# stage-0 lowering (M2)


def code_listing(image):
    """The disassembly of an image's code section, in order.

    The listing's parenthesised pool hints ("(println)", '("World")') are
    stripped: they are a reading aid, not part of the instruction.
    """
    section = image.to_wya().split("SECTION code")[1].split("\nSECTION ")[0]
    return [
        re.sub(r"\s{3}\(.*\)$", "", line.split("; ", 1)[1])
        for line in section.splitlines()
        if ": " in line and "; " in line
    ]


def body_listing(image, name):
    """Just the named function's instructions, without module init's."""
    listing = image.to_wya().split(f"; fn {name} ")[1]
    listing = listing.split("; fn ")[0].split("\nSECTION ")[0]
    return [
        re.sub(r"\s{3}\(.*\)$", "", line.split("; ", 1)[1])
        for line in listing.splitlines()
        if ": " in line and "; " in line
    ]


def test_a_functions_value_is_its_last_statement():
    """No `return` needed: the body's result register holds the last
    statement's value, and the epilogue returns it (spec 7.2)."""
    image = compile_source("fn f(a):\n    a * 2\n")
    assert code_listing(image)[-2:] == ["mul L0 <- P0 * L1", "return L0 count=1"]


def test_an_if_chain_writes_every_branch_into_one_register():
    image = compile_source(
        'fn f(n):\n    if n < 0:\n        "neg"\n    else:\n        "pos"\n'
    )
    listing = code_listing(image)
    assert listing.count("lconst L0 <- static#0") == 1
    assert listing.count("lconst L0 <- static#1") == 1
    assert listing[-1] == "return L0 count=1"


def test_an_if_with_no_else_starts_from_nil():
    """A chain nothing matches has to leave a value behind anyway."""
    image = compile_source('fn f(n):\n    if n < 0:\n        "neg"\n')
    assert body_listing(image, "f")[0] == "lnil L0"


def test_a_loop_has_no_value_of_its_own():
    """The POC evaluates every loop to nil (see doc/llm-bytecode.md 7.2 on
    `break v`), so the loop's value register is just initialized."""
    image = compile_source("fn f(n):\n    while n > 0:\n        n = n - 1\n")
    listing = body_listing(image, "f")
    assert listing[0] == "lnil L0"
    assert listing[-1] == "return L0 count=1"


def test_short_circuit_skips_the_right_hand_side():
    listing = body_listing(compile_source("fn f(a, b):\n    return a and b\n"), "f")
    assert listing[:3] == ["move L0 <- P0", "jf L0, +1", "move L0 <- P1"]
    listing = body_listing(compile_source("fn f(a, b):\n    return a or b\n"), "f")
    assert listing[1] == "jt L0, +1"


def test_for_else_and_break_land_on_different_targets():
    """Exhaustion jumps *to* the else clause; `break` jumps *past* it."""
    image = compile_source(
        "fn f(items):\n"
        "    for x in items:\n"
        "        break\n"
        "    else:\n"
        "        x = 1\n"
    )
    listing = body_listing(image, "f")
    # `done +2` reaches the else clause; the break's `jmp +2` reaches past it.
    assert "itnext L0 <- L2, done +2" in listing
    assert "jmp +2" in listing


def test_a_module_level_loop_variable_is_a_global():
    """Top-level names are module globals, so the loop variable is written
    back through `gset` after each `itnext`."""
    image = compile_source("var src = 0\nfor x in src:\n    println(x)\n")
    listing = code_listing(image)
    index = next(i for i, line in enumerate(listing) if line.startswith("itnext"))
    assert listing[index + 1].startswith("gset g1")


def test_set_if_unset_reads_the_global_before_testing_it():
    image = compile_source('var x: str\nx ?= "fallback"\n')
    listing = code_listing(image)
    assert "gget L0 <- g0" in listing
    assert "jnerr L0, +2" in listing


def test_a_forward_declaration_starts_unset():
    assert "lunset L0" in code_listing(compile_source("fn f():\n    var x: int\n"))


def test_a_call_can_be_asked_for_several_results():
    """`a, b := f()` compiles to one call with nres = 2 whose results are read
    straight out of the window (spec 7.2).

    The POC interpreter cannot run this yet - it models `return a, b` as one
    tuple - so this is asserted on the listing rather than in a golden fixture
    (see doc/llm-bytecode.md 7.2).
    """
    image = compile_source("fn f():\n    return 1, 2\n\na, b := f()\n")
    listing = code_listing(image)
    assert "call base=L0 argc=0 nres=2" in listing
    assert listing[listing.index("call base=L0 argc=0 nres=2") + 1 :][:2] == [
        "gset g1 <- L0",
        "gset g2 <- L1",
    ]


def test_multi_value_return_uses_a_window():
    image = compile_source("fn f(a, b):\n    return a, b\n")
    listing = code_listing(image)
    assert listing[-1] == "return L0 count=2"
    (entry,) = image.functions
    assert entry.nresults == 2


def test_float_literals_are_single_precision():
    """f32, not f64: the machine's native float is single precision, so the
    literal is rounded at compile time rather than at load."""
    image = compile_source("fn f():\n    return 1.5\n")
    assert "f32 L0 <- 1.5" in code_listing(image)


def test_symbols_are_interned_not_pooled():
    image = compile_source("fn f():\n    return 'ready\n")
    assert image.symbols == ["ready"]
    assert image.statics == []


def test_a_char_literal_is_its_codepoint():
    assert "i8 L0 <- 65" in code_listing(compile_source("fn f():\n    return \\A\n"))


def test_a_primitive_type_is_loaded_by_name_not_resolved_as_a_class():
    """The interpreter answers `is int` from a name table, so the compiler
    hands `is` the name rather than the binding - the bindings disagree with
    that table (`error` is a Class, `tuple` a constructor, `str` a builtin),
    which is what used to make `error(...) is error` false compiled and true
    interpreted."""
    image = compile_source("fn f(v):\n    return v is error\n")
    assert image.statics == ["error"]
    assert "lconst L0 <- static#0" in body_listing(image, "f")
    assert not [m for m in image.messages if "error" in str(m.path)], \
        "no class relocation to bind: the name is the constant"


def test_a_primitive_the_interpreter_has_no_binding_for_still_compiles():
    """`list`, `dict` and `uint` name types but are bound to nothing, so
    resolving them as classes made `x is list` a compile error while the
    walker answered it fine."""
    for name in ("list", "dict", "uint"):
        image = compile_source(f"fn f(v):\n    return v is {name}\n")
        assert image.statics == [name]


def test_a_type_union_is_checked_against_a_tuple_of_classes():
    image = compile_source("fn f(v):\n    return v is int | str\n")
    listing = body_listing(image, "f")
    assert "tuple L2 <- L0, 2 items" in listing
    assert listing[-2] == "is L3 <- P0 is L2"


# --------------------------------------------------------------------------
# stage-1a data, closures and error flow (M3)


def test_collection_literals_build_from_a_window():
    image = compile_source(
        'fn f():\n    return [1, 2], (3, 4), {"k": 5}, $[6, 7]\n'
    )
    listing = body_listing(image, "f")
    assert "list L2 <- L0, 2 items" in listing
    assert "tuple L3 <- L1, 2 items" in listing
    assert "dict L4 <- L2, 1 pairs" in listing
    assert "plist L5 <- L3, 2 items" in listing


def test_attribute_and_index_access():
    image = compile_source(
        "fn f(obj, key):\n"
        "    var a = obj.field\n"
        "    var b = obj[key]\n"
        "    obj.field = 1\n"
        "    obj[key] = 2\n"
    )
    listing = body_listing(image, "f")
    assert "getattr L0 <- P0.sym#0" in listing
    assert "getidx L1 <- P0[P1]" in listing
    assert "setattr P0.sym#0 <- L3" in listing
    assert "setidx P0[P1] <- L3" in listing


def test_a_chained_attribute_target_walks_to_its_last_name():
    """`a.b.c = v` reads `a.b`, then stores into its `c`."""
    image = compile_source("fn f(a):\n    a.b.c = 1\n")
    listing = body_listing(image, "f")
    assert listing[1] == "getattr L1 <- P0.sym#0"
    assert listing[3] == "setattr L1.sym#1 <- L2"


def test_a_swap_stages_both_values_before_writing_either():
    """`a, b = b, a` is a swap, not two assignments in a row, so no target is
    written until every value has been evaluated."""
    listing = body_listing(compile_source("fn f(a, b):\n    a, b = b, a\n"), "f")
    assert listing[1:5] == [
        "move L1 <- P1",
        "move L2 <- P0",
        "move P0 <- L1",
        "move P1 <- L2",
    ]


def test_a_literal_tuple_right_hand_side_is_never_materialized():
    listing = body_listing(compile_source("fn f(a, b):\n    a, b = 1, 2\n"), "f")
    assert not any(line.startswith("tuple") for line in listing)


def test_multi_target_assignment_from_a_value_unpacks():
    """`a, b := point` pulls exactly two items out of one indexable.

    The POC interpreter rejects this shape (see doc/llm-bytecode.md 7.2), so
    it is asserted on the listing rather than in a golden fixture.
    """
    listing = body_listing(compile_source("fn f(point):\n    a, b := point\n"), "f")
    assert "unpack L3.. <- P0, 2 items" in listing


def test_a_plain_capture_is_copied_by_value():
    image = compile_source("fn outer(n):\n    return fn(x): x + n\n")
    listing = body_listing(image, "outer")
    assert listing == [
        "move L1 <- P0",
        "closure L0 <- fn#1, 1 caps",
        "return L0 count=1",
    ]
    # index 0 is `outer`, whose table slot is reserved before its body (and
    # so the lambda inside it) is compiled.
    assert image.functions[1].ncaptures == 1


def test_a_captured_and_assigned_variable_is_hoisted_into_a_cell():
    """The cell is visible in the listing: a one-element `plist` built at
    entry, with every read and write going through it (spec 8.3)."""
    image = compile_source(
        "fn counter(start):\n"
        "    var total = start\n"
        "    fn bump(step):\n"
        "        total = total + step\n"
        "        return total\n"
        "    return bump\n"
    )
    outer = body_listing(image, "counter")
    assert "plist L1 <- L3, 1 items" in outer  # the box
    assert "setidx L1[L0] <- L3" in outer  # `var total = start` writes into it
    inner = body_listing(image, "bump")
    assert "getidx L2 <- P1[L0]" in inner  # the capture *is* the box
    assert "setidx P1[L0] <- L1" in inner
    # ... and nothing rebuilds it: the enclosing frame owns the only plist.
    assert not any(line.startswith("plist") for line in inner)


def test_an_unassigned_capture_needs_no_cell():
    image = compile_source("fn outer(n):\n    return fn(x): x + n\n")
    assert not any(line.startswith("plist") for line in code_listing(image))


def test_a_do_block_is_inlined_not_called():
    image = compile_source("fn f(a):\n    var b = do:\n        a + 1\n    return b\n")
    listing = body_listing(image, "f")
    assert not any(line.startswith("closure") for line in listing)
    assert "add L0 <- P0 + L1" in listing


def test_try_returns_the_error_to_the_caller():
    listing = body_listing(compile_source("fn f(g):\n    return try g()\n"), "f")
    # A real `return`, not a jump to the epilogue - `return` is what runs the
    # frame's defers.
    assert listing[-3:] == ["jnerr L0, +1", "return L0 count=1", "return L0 count=1"]


def test_catch_supplies_a_value_in_place_of_the_error():
    listing = body_listing(compile_source("fn f(g):\n    return g() catch 0\n"), "f")
    assert "jnerr L0, +1" in listing
    assert "i8 L0 <- 0" in listing


def test_catch_return_leaves_the_function():
    listing = body_listing(
        compile_source('fn f(g):\n    v := g() catch return "gave up"\n    return v\n'),
        "f",
    )
    assert listing.count("return") == 2 or sum(
        line.startswith("return") for line in listing
    ) == 2


def test_defer_arms_a_closure_for_the_frame():
    image = compile_source('fn f():\n    var log = 0\n    defer:\n        log = 1\n')
    listing = body_listing(image, "f")
    assert "defer_reg L3 mode=0" in listing
    assert any(fn.name == "<defer>" for fn in image.functions)


def test_defer_on_error_uses_mode_1():
    image = compile_source(
        "fn f():\n    var log = 0\n    defer on error:\n        log = 1\n"
    )
    assert "defer_reg L3 mode=1" in body_listing(image, "f")


def test_a_return_window_never_starts_in_the_p_frame():
    """`return` reads consecutive L slots, so a value living in a parameter
    register is copied down first (spec 3.1)."""
    listing = body_listing(compile_source("fn f(a):\n    return a\n"), "f")
    assert listing == ["move L0 <- P0", "return L0 count=1"]


# --------------------------------------------------------------------------
# stage-1b objects and modules (M4)


def test_a_class_becomes_a_classes_entry():
    image = compile_fixture("classes.wy")
    shape, square = image.classes
    assert [slot.name for slot in shape.slots] == ["name", "sides"]
    assert [image.statics[slot.default] for slot in shape.slots] == ["shape", 0]
    assert shape.init is not None
    assert [image.symbols[symbol] for symbol, _fn in shape.messages] == [
        "describe",
        "corners",
    ]
    # The superclass is named, never resolved at compile time (spec 6.3) - as
    # a global slot, because a name is a name and this module defines `Shape`.
    assert shape.superclass is None
    assert image.globals[square.superclass].name == "Shape"


def test_a_class_is_realized_in_module_init():
    listing = code_listing(compile_fixture("classes.wy"))
    assert listing[0] == "class L0 <- class#0"
    assert listing[1] == "gset g0 <- L0"


def test_a_method_is_a_message_dispatching_on_its_own_class():
    image = compile_source("class C:\n    fn go():\n        return 1\n")
    method = next(fn for fn in image.functions if fn.name == "go!")
    assert method.flags & 0b10  # bit1: message
    (dispatch,) = method.dispatch
    assert image.globals[dispatch].name == "C"


def test_slot_access_inside_a_method_is_symbolic_by_default():
    """An external superclass makes absolute offsets unknowable, so the
    general path stays `getattr`/`setattr` on `this` (spec 7.1)."""
    image = compile_source(
        "class C:\n    slot x: int = 0\n    fn go():\n        this.x = 1\n"
        "        return this.x\n"
    )
    listing = body_listing(image, "go!")
    x = image.symbols.index("x")
    assert f"setattr P0.sym#{x} <- L0" in listing
    assert not any(line.startswith("setslot") for line in listing)


def test_the_slot_optimization_needs_a_module_local_chain():
    source = (
        "class Base:\n    slot a: int = 1\n"
        "class Local(Base):\n    slot x: int = 0\n"
        "    fn go():\n        this.x = 1\n        return this.a\n"
        "class Foreign(Outside):\n    slot z: int = 0\n"
        "    fn touch():\n        this.z = 1\n        return this.z\n"
    )
    image = compile_module(parse(source), "m", slot_optimization=True)
    # Base's slot is 0 and Local's own is 1: instances lay them out base-first.
    assert body_listing(image, "go!")[:3] == [
        "i8 L0 <- 1",
        "setslot P0#1 <- L0",
        "getslot L0 <- P0#0",
    ]
    # Foreign's base is not this module's, so its layout is not ours to know.
    assert not any(
        line.startswith(("getslot", "setslot"))
        for line in body_listing(image, "touch!")
    )


def test_a_dispatched_function_registers_itself_at_load():
    """`fn [T] name` is not a module global - it is reached by sending
    `name`, so init registers it against the message and its types."""
    image = compile_fixture("messages.wy")
    listing = code_listing(image)
    assert any(line.startswith("reg_msg") for line in listing)
    assert "doubled" not in [slot.name for slot in image.globals]


def test_a_message_send_puts_the_receiver_at_the_window_base():
    listing = body_listing(
        compile_source("fn f(obj):\n    return obj ! go(1)\n"), "f"
    )
    assert listing[:3] == [
        "move L0 <- P0",
        "i8 L1 <- 1",
        "msg base=L0 argc=1 msg#0 nres=1",
    ]


def test_a_message_without_a_call_binds_it():
    listing = body_listing(compile_source("fn f(obj):\n    return obj ! go\n"), "f")
    assert "getmsg L0 <- P0 ! msg#0" in listing


def test_a_tuple_of_receivers_is_built_before_dispatch():
    """`(a, b) ! name(...)` is multiple dispatch: the receivers are tupled and
    the tuple is what the VM dispatches on."""
    listing = body_listing(
        compile_source("fn f(a, b):\n    return (a, b) ! go()\n"), "f"
    )
    assert "tuple L0 <- L1, 2 items" in listing
    assert "msg base=L0 argc=0 msg#0 nres=1" in listing


def test_super_chains_without_a_receiver_slot():
    """`super` reuses the dispatch already in progress, so its window is the
    arguments alone - no callee or receiver register.

    The POC interpreter cannot evaluate `super()` (see doc/llm-bytecode.md
    7.2), so this is asserted on the listing.
    """
    image = compile_source(
        'class C:\n    fn go():\n        return super() + 1\n'
    )
    assert "super base=L0 argc=0 nres=1" in body_listing(image, "go!")


def test_a_class_static_is_a_module_global():
    image = compile_source(
        'class C:\n    static count: int = 0\n\nprintln(C::count)\n'
    )
    (entry,) = image.classes
    assert [name for name, _index in entry.statics] == ["count"]
    # `C::count` reads that global directly - it is this module's own.
    assert "gget L1 <- g1" in code_listing(image)


# --------------------------------------------------------------------------
# imports


def test_imports_are_hoisted_to_the_top_of_init():
    """They used to be hoisted ahead of `resolve`, which bound everything they
    made reachable. `resolve` is gone and each import fills its own slots as it
    runs (doc/addendum.md), but the hoist stays: a name has to be filled before
    the module's own code reads it, and its code starts after the imports.
    """
    listing = code_listing(compile_fixture("two_module/report.wy"))
    imports = [i for i, line in enumerate(listing) if line.startswith("import")]
    assert imports, "fixture must still exercise an import"
    # Nothing but the import sequence itself - `import`, the `gset` that binds
    # what it named, and the `gget`/`gset` pair for each `import a::(x)` item.
    assert all(
        line.startswith(("import", "gset", "gget"))
        for line in listing[: imports[-1] + 1]
    )


def test_an_import_binds_the_root_package_and_the_leaf():
    image = compile_source("import a::b::c\n")
    assert [slot.name for slot in image.globals] == ["a", "c"]
    # One `import` per prefix, each carrying its path as a constant string:
    # `a::b::c` stays ambiguous between a submodule and a member of `a::b`,
    # so every prefix has to be loaded and the last one may be either.
    assert image.statics == ["a", "a::b", "a::b::c"]
    assert image.messages == []


def test_an_alias_renames_only_the_binding():
    image = compile_source("import a::b as x\n")
    assert [slot.name for slot in image.globals] == ["a", "x"]


def test_names_imported_out_of_a_module_come_from_free_slots():
    """`import a::(x)` reads a name *from* the dependency: a free slot named
    by the whole path, filled by the `import a` above it, copied into the
    global the statement binds (doc/addendum.md)."""
    image = compile_source("import a::(x, y as z)\n")
    free = image._free_doc()
    assert set(free) == {"a::x", "a::y"}
    listing = code_listing(image)
    # Straight after the imports that make the names reachable - there is no
    # `resolve` between them any more (doc/addendum.md).
    start = listing.index(f"gget L0 <- g{free['a::x']}")
    assert listing[start : start + 4] == [
        f"gget L0 <- g{free['a::x']}",
        "gset g1 <- L0",
        f"gget L0 <- g{free['a::y']}",
        "gset g2 <- L0",
    ]


def test_a_cross_module_reference_is_never_resolved_at_compile_time():
    """The importer records a path; what it means is the dependency's business
    at run time (spec 6.3)."""
    report = compile_fixture("two_module/report.wy")
    geometry = compile_fixture("two_module/geometry.wy")
    # Nothing at all is tabled for the import: its path is a constant string.
    # Every actual *name* reaching into the dependency is a free global slot
    # (doc/addendum.md).
    assert report.messages == []
    assert "geometry" in report.statics
    reaching_in = {"geometry"} | {
        name for name in report._free_doc() if name.startswith("geometry")
    }
    assert reaching_in == {"geometry", "geometry::area", "geometry::label",
                           "geometry::UNITS"}
    # Every one of them names something the dependency really exports...
    exported = {slot.name for slot in geometry.globals}
    for path in reaching_in - {"geometry"}:
        assert path.split("::")[1] in exported
    # ... and none of it leaked into the importer's own tables.
    assert "UNITS" not in {slot.name for slot in report.globals}


def test_a_static_import_lowers_like_an_ordinary_one():
    """`static` says the dependency is wanted at compile time only, which is
    a statement about what the importer may do with it, not about the code
    the import statement itself becomes (doc/llm-bytecode.md 7.2)."""
    plain = compile_source("import a::b\n")
    static = compile_source("import static a::b\n")
    assert plain.code == static.code


def test_a_block_declaration_gets_a_slot_of_its_own():
    """Shadowing takes a fresh slot and leaves the outer binding alone - the
    frame therefore has two `a`s, and the read after the block is the outer
    one."""
    image = compile_source(
        "fn f():\n    var a = 1\n    do:\n        var a = 2\n    return a\n"
    )
    listing = body_listing(image, "f")
    assert "i8 L0 <- 1" in listing and "i8 L1 <- 2" in listing
    assert listing[-1] == "return L0 count=1", "the outer `a` survives the block"


def test_a_name_declared_in_a_block_is_gone_after_it():
    """Spec: an inner declaration shadows "for the duration of the inner
    scope", so reading it afterwards is a refusal, not a stale slot."""
    with pytest.raises(CompileError) as caught:
        compile_source("fn f():\n    do:\n        var a = 5\n    return a\n")
    assert "undefined name 'a'" in str(caught.value)


def test_a_for_variable_is_scoped_to_its_loop():
    with pytest.raises(CompileError) as caught:
        compile_source("fn f():\n    for i in [1]:\n        pass\n    return i\n")
    assert "undefined name 'i'" in str(caught.value)


def test_a_top_level_block_declaration_is_not_a_module_member():
    """It needs storage, but `mod::a` and the exports table must still answer
    with the module's own `a`, not the block's."""
    image = compile_source("var a = 1\ndo:\n    var a = 2\n")
    exports = image._exports_doc()
    assert exports == {"a": 0}, "the module's own `a` is what is exported"
    assert len(image.globals) == 2, "the block's `a` still got a slot of its own"
    assert not image.globals[1].exported


def test_a_shadowing_declaration_that_needs_a_cell_is_refused():
    """One cell per name per frame (spec 8.3), so a shadowing declaration
    that a closure also captures cannot have a slot of its own - and saying
    so beats emitting bytes that quietly share one box."""
    with pytest.raises(CompileError) as caught:
        compile_source(
            "fn f():\n"
            "    var a = 1\n"
            "    do:\n"
            "        var a = 2\n"
            "        g := fn():\n"
            "            return a\n"
            "        a = 3\n"
            "        return g\n"
        )
    assert "captured by a closure" in str(caught.value)


def test_a_bare_slot_name_inside_a_method_reads_through_this():
    """`radius` means `this.radius`: the interpreter seeds slots into a
    method's scope beneath its parameters, and so does the compiler."""
    image = compile_source(
        "class C:\n"
        "    slot radius: int = 2\n"
        "    fn area():\n"
        "        radius = radius + 1\n"
        "        return radius\n"
    )
    listing = body_listing(image, "area!")
    radius = image.symbols.index("radius")
    assert f"getattr L1 <- P0.sym#{radius}" in listing
    assert f"setattr P0.sym#{radius} <- L0" in listing


def test_a_parameter_shadows_a_slot_of_the_same_name():
    image = compile_source(
        "class C:\n    slot v: int = 0\n    fn set(v):\n        return v\n"
    )
    assert body_listing(image, "set!") == ["move L0 <- P1", "return L0 count=1"]


def test_a_class_statics_bare_name_is_its_module_global():
    image = compile_source(
        "class C:\n    static total: int = 0\n    fn bump():\n        total = total + 1\n"
    )
    listing = body_listing(image, "bump!")
    assert "gget L2 <- g1" in listing
    assert "gset g1 <- L1" in listing


def test_a_dispatched_function_sees_its_receivers_slots():
    """`fn [C] name` outside the class body still resolves bare slot names,
    since `this` is a C."""
    image = compile_source(
        "class C:\n    slot x: int = 0\n\nfn [C] doubled():\n    return x * 2\n"
    )
    x = image.symbols.index("x")
    assert f"getattr L0 <- P0.sym#{x}" in body_listing(image, "doubled!")


def test_an_unknown_name_in_a_method_of_a_foreign_subclass_is_a_slot():
    """A chain that leaves this module has slots the compiler cannot
    enumerate, so an otherwise-unresolvable name is one of them."""
    image = compile_source(
        "class Local:\n    slot a: int = 0\n    fn go():\n        return a\n"
        "class Foreign(Outside):\n    fn go2():\n        return nope\n"
    )
    assert body_listing(image, "go2!")[0].startswith("getattr L0 <- P0.sym#")
    # ... but a fully module-local class still catches the typo.
    with pytest.raises(CompileError, match="undefined name 'nope'"):
        compile_source(
            "class Local:\n    slot a: int = 0\n    fn go():\n        return nope\n"
        )


# --------------------------------------------------------------------------
# stage-2: the long tail (M5)


def test_a_co_carries_the_coroutine_flag():
    image = compile_source("co gen():\n    yield 1\n")
    (entry,) = image.functions
    assert entry.flags & 1


def test_yield_suspends_with_a_window_and_resumes_into_it():
    """`yield v` evaluates to whatever the resumer sent, written back at the
    window base (spec 3.3)."""
    listing = body_listing(compile_source("co gen():\n    x := yield 1\n    return x\n"), "gen")
    assert listing[:3] == ["i8 L1 <- 1", "yield base=L1 count=1", "move L0 <- L1"]


def test_yield_of_several_values_widens_the_window():
    listing = body_listing(compile_source("co gen():\n    yield 1, 2\n"), "gen")
    assert "yield base=L1 count=2" in listing


def test_yield_from_delegates():
    listing = body_listing(
        compile_source("co gen(sub):\n    return yield from sub\n"), "gen"
    )
    assert "yield_from L0 <- P0" in listing


def test_yield_outside_a_coroutine_is_refused():
    with pytest.raises(CompileError, match="only meaningful inside a `co`"):
        compile_source("fn f():\n    yield 1\n")


def test_collecting_parameters_set_their_flags():
    image = compile_source("fn f(a, *rest, **opts):\n    return rest\n")
    (entry,) = image.functions
    assert entry.flags & 0b0100  # *args
    assert entry.flags & 0b1000  # **kwargs
    # They are ordinary P slots that arrive pre-collected.
    assert [param.name for param in entry.params] == ["a", "rest", "opts"]


def test_a_constant_parameter_default_lands_in_the_static_pool():
    image = compile_source('fn f(a, b="x"):\n    return a\n')
    (entry,) = image.functions
    assert entry.params[0].default is None
    assert image.statics[entry.params[1].default] == "x"


def test_a_spread_call_builds_a_tuple_and_a_dict():
    """`f(*a, **k)` always reaches the VM the same way: callee, positional
    tuple, keyword dict (spec 7.1)."""
    listing = body_listing(
        compile_source("fn f(items, opts):\n    println(1, *items, key=2, **opts)\n"),
        "f",
    )
    assert any(line.startswith("tuple ") for line in listing)
    # Concatenation is `add`, dispatched through the __add__ family.
    assert any(line.startswith("add ") for line in listing)
    assert any(line.startswith("dict ") for line in listing)
    assert any(line.startswith("call_va ") for line in listing)


def test_a_spread_message_uses_msg_va():
    listing = body_listing(
        compile_source("fn f(obj, items):\n    obj ! go(*items)\n"), "f"
    )
    assert "msg_va base=L1 msg#0 nres=1" in listing


def test_a_plain_call_never_becomes_a_va_call():
    listing = body_listing(compile_source("fn f(a):\n    println(a)\n"), "f")
    assert not any("call_va" in line for line in listing)


def test_a_function_static_is_a_module_global_bound_once():
    """"Bound once where the owner is created": the initializer runs in module
    init, after the closure that publishes the function - not per call."""
    image = compile_source(
        "fn counter():\n    static seen: int = 0\n    seen = seen + 1\n    return seen\n"
    )
    assert [slot.name for slot in image.globals] == ["counter", "counter::seen"]
    init = code_listing(image)
    assert init[init.index("gset g0 <- L0") + 1 :][:2] == ["i8 L1 <- 0", "gset g1 <- L1"]
    body = body_listing(image, "counter")
    assert body[0] == "gget L1 <- g1"


def test_a_wildcard_import_registers_a_namespace():
    image = compile_fixture("wildcard/paint.wy")
    listing = code_listing(image)
    star = next(line for line in listing if line.startswith("import_star "))
    assert star == f"import_star static#{image.statics.index('palette')} except 0, 0 names"
    # Imports are still hoisted ahead of the module's own code: a wildcard has
    # to have filled its slots before anything reads one.
    assert listing.index(star) < listing.index("closure L0 <- fn#0, 0 caps")


def test_a_wildcard_carries_its_except_list_in_registers():
    """The except-list is a window of interned symbols, not a table field -
    which is why a wildcard no longer needs an entry of its own at all."""
    image = compile_source("import a::* except (x, y)\n")
    listing = code_listing(image)
    star = next(i for i, line in enumerate(listing) if line.startswith("import_star"))
    assert listing[star].endswith("2 names")
    assert [line.split("sym#")[1].split()[0] for line in listing[star - 2:star]] == [
        str(image.symbols.index("x")), str(image.symbols.index("y")),
    ]


def test_a_wildcard_turns_an_unknown_name_into_a_free_slot():
    """With a wildcard in scope the compiler cannot say a name is wrong -
    only fill time can (spec 6.3)."""
    with pytest.raises(CompileError, match="undefined name 'somewhere'"):
        compile_source("println(somewhere)\n")
    image = compile_source("import a::*\nprintln(somewhere)\n")
    assert "somewhere" in image._free_doc()


def test_a_wildcard_import_binds_no_leaf_name():
    image = compile_source("import a::b::*\n")
    assert [slot.name for slot in image.globals] == ["a"]


# --------------------------------------------------------------------------
# decorators and the debug section


def test_decorators_run_at_compile_time():
    """The lowered tree is what the decorator answered, so the `println`
    `@traced` injects compiles exactly as a handwritten one would."""
    image = compile_fixture("decorators/decorated.wy")
    assert "calling loud" in image.statics
    assert body_listing(image, "loud")[:3] == [
        f"gget L0 <- g{image._free_doc()['println']}",
        f"lconst L1 <- static#{image.statics.index('calling loud')}",
        "call base=L0 argc=1 nres=1",
    ]


def test_a_decorator_failure_is_a_compile_error_not_a_traceback():
    with pytest.raises(CompileError, match="decorator expansion failed"):
        compile_source("@nosuchthing()\nfn f():\n    return 1\n")


def test_the_debug_section_maps_code_offsets_to_source_lines():
    image = compile_source("fn f():\n    return 1\n\nprintln(f())\n", "m")
    assert image.debug["ln"]
    lines = {int(offset): line for offset, line in image.debug["ln"].items()}
    # The body of `f` is one line, and it is the line `return 1` sits on.
    body = next(fn for fn in image.functions if fn.name == "f")
    assert lines[body.code_offset] == 2


def test_the_debug_section_records_the_file_name_not_its_path():
    """An absolute build path would make the image differ between machines
    for no reason a debugger cares about."""
    image = compile_module(
        parse("println(1)\n"), "m", source_file="/somewhere/else/m.wy"
    )
    assert image.debug["f"] == "m.wy"


def test_strip_leaves_the_debug_section_out():
    stripped = compile_module(parse("println(1)\n"), "m", "m.wy", debug=False)
    assert stripped.debug is None
    assert "SECTION debug" not in stripped.to_wya()


# --------------------------------------------------------------------------
# hardening (M6)


SAMPLES_DIR = os.path.join(REPO_ROOT, "wypoc", "samples")

# Why each remaining sample refuses. The list is pinned rather than counted,
# so a regression that turns a compiling sample into a refusal names itself,
# and closing a gap here is a deliberate edit rather than a silent drift.
#
# Four categories, none of them a compiler gap:
#   - `with` and `signal` are deliberate exclusions (see doc/llm-bytecode.md 7.2)
#   - `...` is a decorator-template placeholder, not a value (see section 9)
#   - four samples are fragments whose tests seed the names they use
#   - one leans on an interpreter quirk: a class static read bare at module
#     scope, where its name is `Counter::total`
EXPECTED_SAMPLE_REFUSALS = {
    "basics.wy": "removed from the language",
    "classes.wy": "undefined name 'canvas'",
    "control_flow.wy": "undefined name 'condition'",
    "decolib.wy": "decorator-template placeholder",
    "decorators.wy": "decorator expansion failed",
    "eval_builtins.wy": "undefined name 'display'",
    "eval_classes.wy": "undefined name 'total'",
    "eval_defer_with_do.wy": "removed from the language",
    "eval_io.wy": "undefined name 'path'",
    "eval_signals.wy": "`signal`",
    "lexical.wy": "removed from the language",
}


def sample_results():
    previous = wyrm_modules.set_script_root(SAMPLES_DIR)
    compiled, refused = [], {}
    try:
        for name in sorted(os.listdir(SAMPLES_DIR)):
            if not name.endswith(".wy"):
                continue
            with open(os.path.join(SAMPLES_DIR, name)) as f:
                source = f.read()
            try:
                tree = parse(source, filename=name)
            except SyntaxError:
                continue
            try:
                compile_module(
                    tree, os.path.splitext(name)[0], name, stub_unlowered=False
                )
                compiled.append(name)
            except CompileError as error:
                refused[name] = str(error)
    finally:
        wyrm_modules.set_script_root(previous)
    return compiled, refused


def test_the_sample_corpus_refuses_only_what_it_should(capsys):
    """The whole corpus either compiles or refuses for a reason on the list.

    capsys because decorator expansion runs real code, and one sample's
    decorators print while they expand.
    """
    compiled, refused = sample_results()
    assert set(refused) == set(EXPECTED_SAMPLE_REFUSALS), (
        "the set of refusing samples changed"
    )
    for name, expected in EXPECTED_SAMPLE_REFUSALS.items():
        assert expected in refused[name], name
    assert compiled, "no sample compiles at all"


def test_undefined_slot_accessors_leave_the_key_absent():
    """`getter = undefined` says the slot has no accessor of that kind, and
    the format says that by leaving the key out (D7 - no sentinels)."""
    image = compile_source(
        'class C:\n'
        '    slot v: int = 0 with:\n'
        '        getter = undefined\n'
        '        setter = fn (value): 1\n'
    )
    (entry,) = image.classes
    (slot,) = entry.slots
    assert slot.getter is None
    assert slot.setter is not None


def test_a_removed_construct_does_not_read_as_a_missing_feature():
    with pytest.raises(CompileError) as excinfo:
        compile_source("with pi: float = 3.14\n")
    assert "yet" not in str(excinfo.value)


def test_host_provided_globals_resolve():
    """`__ARGS` is seeded by the host (cli.py, repl.py), not by
    populate_globals, so nothing running can report it - but a compiled module
    still reaches it through a free slot like any other builtin."""
    image = compile_source("println(__ARGS)\n")
    assert "__ARGS" in image._free_doc()


def test_the_listing_interleaves_source_lines():
    """spec 5.1: source line numbers appear as comment-only lines when debug
    info exists - and an assembler discards them like any other comment."""
    image = compile_fixture("hello.wy")
    listing = image.to_wya()
    assert ";   hello.wy:3" in listing
    assert assemble_wya(listing) == image.to_wyc()


def test_a_stripped_listing_has_no_source_lines():
    image = compile_module(parse("println(1)\n"), "m", "m.wy", debug=False)
    assert ";   m.wy:" not in image.to_wya()


def test_if_is_an_expression_as_well_as_a_statement():
    """wyrm.gram has an `if_expr` rule producing the same `If` node, so `if`
    reaches the compiler in expression position too."""
    listing = body_listing(
        compile_source('fn pick(c):\n    return if c: "yes"\n    else: "no"\n'), "pick"
    )
    assert listing[0] == "jf P0, +2"
    assert listing.count("lconst L0 <- static#0") == 1
    assert listing[-1] == "return L0 count=1"


def test_an_if_expression_nested_in_a_do_block():
    """How this first showed up: an `if` in value position inside `do:`."""
    image = compile_source(
        'fn f(c):\n    return do:\n        if c: 1\n        else: 2\n'
    )
    assert "jf P0" in " ".join(body_listing(image, "f"))


@pytest.mark.parametrize(
    "source",
    [
        "fn f(obj):\n    obj.seen = {}\n",
        "fn f():\n    var xs = []\n",
        "fn f():\n    var t = ()\n",
        "fn f():\n    var p = $[]\n",
    ],
    ids=["dict", "list", "tuple", "plist"],
)
def test_an_empty_collection_literal_verifies(source):
    """An empty window's base register is never read, and the compiler leaves
    it pointing one past the last live slot - which is in range for nothing.
    compile_module verifies, so this passing is the assertion."""
    compile_source(source)


# --------------------------------------------------------------------------
# `foo::$ast` and stubs for bodies that will not lower


def test_ast_ref_builds_the_definitions_sexpr():
    """A compiled module carries no ASTs (spec 7.2) - it carries the
    instructions that rebuild the one s-expression it was asked for."""
    image = compile_source("fn tmpl(x):\n    return x + 1\n\nt := tmpl::$ast\n")
    listing = code_listing(image)
    assert "lsym L1 <- sym#0" in listing  # the 'fn head of the node
    assert image.symbols[:2] == ["fn", "tmpl"]
    assert any(line.startswith("plist ") for line in listing)


def test_the_emitted_sexpr_holds_exactly_the_trees_leaves():
    """The value the code builds has to be the tree `sexpr(foo::$ast)` gives,
    or a DSL reading it at run time sees something else.  There is no VM to
    run the result against yet, so the check is that the pools hold exactly
    the tree's leaves - every symbol and string it needs, and nothing else."""
    from wypoc.wyrm_builtins import NIL, Pair, Symbol

    from wypoc import sexpr as sexpr_module

    source = 'fn tmpl(x):\n    return x + "a"\n'
    encoded = sexpr_module.encode(parse(source).body[0])

    symbols, strings = set(), set()

    def walk(value):
        if isinstance(value, Symbol):
            symbols.add(value.name)
        elif isinstance(value, str):
            strings.add(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, Pair):
            walk(value.car)
            walk(value.cdr)
        elif value is not NIL and value is not None:
            assert isinstance(value, (int, float)), value

    walk(encoded)
    # Nothing else in this module interns a name or pools a constant, so the
    # pools are the tree's leaves exactly.
    image = compile_source(source + "\nt := tmpl::$ast\n")
    assert set(image.symbols) == symbols
    assert set(image.statics) == strings


def test_ast_ref_reaches_a_definition_that_will_not_itself_compile():
    """The point of the pairing: a template's body need not lower for its
    tree to be readable."""
    image = compile_source(
        "fn $tmpl(k):\n    return this ! peek(k)\n\nt := $tmpl::$ast\n",
        stub_unlowered=True,
    )
    assert [name for name, _reason in image.unlowered] == ["$tmpl"]
    assert "peek" in image.symbols  # its tree is still built in full


def test_ast_ref_cannot_reach_across_a_module_boundary():
    with pytest.raises(CompileError, match="carries no trees to reach across"):
        compile_source("t := elsewhere::$ast\n")


def test_a_body_that_will_not_lower_becomes_a_trap():
    image = compile_source(
        "fn template():\n    return this\n", stub_unlowered=True
    )
    assert body_listing(image, "template") == ["trap 0"]
    (name, reason) = image.unlowered[0]
    assert name == "template"
    assert "`this` is only meaningful" in reason


def test_a_stub_keeps_its_table_entry_and_its_binding():
    image = compile_source(
        "fn template():\n    return this\n\nprintln(template)\n",
        stub_unlowered=True,
    )
    assert [fn.name for fn in image.functions] == ["template"]
    assert "template" in [slot.name for slot in image.globals]


def test_a_stub_carries_no_referenced_names():
    """Whatever the abandoned attempt reached for goes with it - otherwise the
    VM would resolve names the trapping body never reads, and the verifier
    says so."""
    image = compile_source(
        'fn template():\n    println("x")\n    return this\n',
        stub_unlowered=True,
    )
    (entry,) = image.functions
    assert entry.uses == []


def test_the_listing_marks_a_stub():
    image = compile_source(
        "fn template():\n    return this\n", stub_unlowered=True
    )
    assert "STUB: `this` is only meaningful" in image.to_wya()


def test_strict_mode_refuses_instead_of_stubbing():
    """The default is forgiving; the compiler is not obliged to be."""
    with pytest.raises(CompileError, match="`this` is only meaningful"):
        compile_source("fn template():\n    return this\n")


def test_only_a_function_body_may_stub():
    """A top-level statement that will not lower is still a refusal, stubs or
    not - there is no frame to trap in."""
    with pytest.raises(CompileError, match="removed from the language"):
        compile_source("with pi: float = 3.14\n", stub_unlowered=True)
