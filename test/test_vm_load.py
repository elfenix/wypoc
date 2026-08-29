"""Loading a `.wyc` image (wypoc/vm/image.py, wypoc/vm/module.py).

M0 of gen/bytecode-vm-plan.md: an image becomes a linked module, and nothing
runs. Two things are being proved. First, that every image the compiler emits
loads and its tables come back the way they went in - the compiler and the VM
agree about the format, which is the whole reason for having a second
consumer. Second, that a malformed image is refused *at load*, by the specific
rule doc/wyc-format.md §2 states, rather than surfacing later as a crash in
the interpreter loop.
"""

import struct

import pytest
from conftest import bytecode_fixture_names as fixture_names
from conftest import compile_bytecode_fixture as compile_fixture
from conftest import compile_bytecode_source as compile_source

from wypoc import wyrm_eval_parse_tree as ev
from wypoc.compiler_bc.image import SECTION_IDS
from wypoc.vm import ImageError, LinkError, LoadedModule, load
from wypoc.vm import disasm
from wypoc.vm.module import UNBOUND

# --------------------------------------------------------------------------
# every image the compiler emits loads, and round-trips


@pytest.mark.parametrize("name", fixture_names())
def test_every_fixture_image_loads(name):
    built = compile_fixture(name)
    loaded = load(built.to_wyc())

    assert loaded.name == built.module_name
    assert loaded.nglobals == len(built.globals)
    assert loaded.init_nlocals == built.init_nlocals
    assert loaded.statics == built.statics
    assert loaded.symbols == built.symbols
    assert loaded.code == built.code
    # Only the module's own members are exported. A free name is a slot this
    # module reads but does not define, and a block-local shadow slot is
    # storage rather than a member - neither is what `mod::name` should find.
    assert loaded.exports == {
        slot.name: i for i, slot in enumerate(built.globals) if slot.exported
    }
    assert loaded.free == {
        slot.name: i for i, slot in enumerate(built.globals) if slot.free
    }

    assert len(loaded.functions) == len(built.functions)
    for got, want in zip(loaded.functions, built.functions):
        assert got.name == want.name
        assert [p.name for p in got.params] == [p.name for p in want.params]
        assert (got.nlocals, got.code_offset, got.flags) == (
            want.nlocals, want.code_offset, want.flags,
        )
        assert got.dispatch == want.dispatch
        assert got.ncaptures == want.ncaptures

    assert len(loaded.classes) == len(built.classes)
    for got, want in zip(loaded.classes, built.classes):
        assert got.name == want.name
        assert got.superclass == want.superclass
        assert [s.name for s in got.slots] == [s.name for s in want.slots]
        assert got.init == want.init
        assert got.messages == [tuple(m) for m in want.messages]

    assert len(loaded.messages) == len(built.messages)
    for got, want in zip(loaded.messages, built.messages):
        assert list(got.path) == want["p"]


@pytest.mark.parametrize("name", fixture_names())
def test_every_fixture_links(name):
    module = LoadedModule(load(compile_fixture(name).to_wyc()))
    assert len(module.globals) == module.image.nglobals
    assert len(module.bindings) == len(module.image.messages)
    assert all(b is UNBOUND for b in module.bindings)


def test_a_stripped_image_loads_identically():
    """`debug` is advisory: an image without it must load the same way."""
    source = 'fn f(a):\n    return a + 1\n\nprintln(f(1))\n'
    full = load(compile_source(source).to_wyc())
    stripped = load(compile_source(source, debug=False).to_wyc())
    assert full.code == stripped.code
    assert full.exports == stripped.exports
    assert full.debug and not stripped.debug
    assert stripped.source_line(0) is None


# --------------------------------------------------------------------------
# globals and the bind table (spec 7.1 steps 1-4)


def test_globals_start_unset():
    module = LoadedModule(load(compile_source("var a = 1\nvar b = 2\n").to_wyc()))
    # `var a = 1` is a *statement* in init, not a constant default, so both
    # slots are Unset until init runs.
    assert module.globals == [ev.UNSET, ev.UNSET]


def test_constant_slot_defaults_are_applied_at_load():
    built = compile_source("var a = 1\n")
    built.globals[0].default = 7  # what a constant initializer would record
    module = LoadedModule(load(built.to_wyc()))
    assert module.globals[0] == 7


def test_a_referenced_builtin_is_a_free_slot_not_a_message():
    """`println` is a name this module reads and does not define, so it is a
    global slot on the fill list. Section 7 holds message identities alone
    now, and a plain script has none."""
    image = load(compile_source("println(1)\n").to_wyc())
    assert image.free == {"println": 0}
    assert image.exports == {}
    assert image.messages == []


def test_an_unfilled_free_slot_reads_as_unset():
    """Nothing fills it here - `LoadedModule` alone does not run the load-time
    builtin pass - so the slot holds what any declared-but-unassigned global
    holds. An unresolved name is Unset now, not a bespoke error value."""
    module = LoadedModule(load(compile_source("println(1)\n").to_wyc()))
    assert module.globals[module.image.free["println"]] is ev.UNSET


def test_a_message_identity_binds_on_first_read():
    """The relocation table holds nothing but message identities and import
    paths now, and a message has no import to wait on: the first read resolves
    it, and the second gets the same object back."""
    module = LoadedModule(load(compile_source(
        "class C:\n    slot v: int = 0\n\nfn [C] greet():\n    return 1\n"
    ).to_wyc()))
    assert all(b is UNBOUND for b in module.bindings)
    first = module.bound(0)
    assert isinstance(first, ev.Method)
    assert module.bound(0) is first


# --------------------------------------------------------------------------
# the interop façade: exports (spec 8.10)


def test_exports_map_every_global_by_name():
    image = load(compile_source("var top = 1\n\nfn helper():\n    return 2\n").to_wyc())
    assert image.exports == {"top": 0, "helper": 1}


def test_a_global_reads_and_writes_through_its_cell():
    """The seam: a compiled module's globals are slots, an interpreted
    module's are `Variable` cells. A GlobalCell is one slot wearing the other
    shape, so both sides share storage instead of copying."""
    module = LoadedModule(load(compile_source("var counter = 0\n").to_wyc()))
    cell = module.cell("counter")
    module.globals[0] = 41
    assert cell.value == 41           # compiled write, interpreted read
    cell.value = 42
    assert module.globals[0] == 42    # interpreted write, compiled read
    assert ev.unwrap(cell) == 42      # and it unwraps like a Variable


def test_the_module_facade_answers_a_scope_lookup():
    """What `geometry::UNITS` needs: walk to the module object, ask it for a
    name (spec 7.3)."""
    module = LoadedModule(load(compile_fixture("two_module/geometry.wy").to_wyc()))
    assert module.export_names() == ["UNITS", "area", "label"]
    assert "UNITS" in module
    assert module.get("UNITS") is module.cell("UNITS")
    assert module.get("nope") is None

    as_module = module.as_module()
    assert isinstance(as_module, ev.Module)
    assert "UNITS" in as_module.ctx
    module.globals[0] = "cm"
    assert ev.unwrap(as_module.ctx["UNITS"]) == "cm"


def test_a_module_with_no_globals_has_no_exports_section():
    image = load(compile_source("println(1)\n").to_wyc())
    assert image.exports == {}


# --------------------------------------------------------------------------
# rejection: every rule of spec 2


def wyc(source="println(1)\n"):
    return bytearray(compile_source(source).to_wyc())


def test_rejects_a_short_file():
    with pytest.raises(ImageError, match="shorter than"):
        load(b"WYC")


def test_rejects_bad_magic():
    blob = wyc()
    blob[0:4] = b"ELF\x00"
    with pytest.raises(ImageError, match="bad magic"):
        load(bytes(blob))


def test_rejects_a_future_container_version():
    blob = wyc()
    blob[4] = 2
    with pytest.raises(ImageError, match="not forward-compatible"):
        load(bytes(blob))


def test_rejects_an_unknown_section_id():
    """Not skipped: an id this loader does not know may carry meaning it
    would then silently ignore (§2 rule 5)."""
    blob = wyc()
    blob[8] = 99
    with pytest.raises(ImageError, match="unknown section id 99"):
        load(bytes(blob))


def test_rejects_an_unsorted_directory():
    blob = wyc()
    first = bytes(blob[8:20])
    second = bytes(blob[20:32])
    blob[8:20] = second
    blob[20:32] = first
    with pytest.raises(ImageError, match="not in ascending id order"):
        load(bytes(blob))


def test_rejects_a_duplicate_section_id():
    blob = wyc()
    blob[20] = blob[8]  # second entry claims the first's id
    with pytest.raises(ImageError, match="ascending id order"):
        load(bytes(blob))


def test_rejects_a_section_running_past_the_end():
    blob = wyc()
    struct.pack_into("<I", blob, 16, 1 << 20)  # first entry's length
    with pytest.raises(ImageError, match="outside the"):
        load(bytes(blob))


def test_rejects_a_section_overlapping_the_directory():
    blob = wyc()
    struct.pack_into("<I", blob, 12, 4)  # first entry's offset
    with pytest.raises(ImageError, match="outside the"):
        load(bytes(blob))


def test_rejects_a_missing_code_section():
    built = compile_source()
    sections = built.sections()
    del sections[SECTION_IDS["code"]]
    from wypoc.compiler_bc.image import _wrap_wyc

    with pytest.raises(ImageError, match="no code section"):
        load(_wrap_wyc(sections))


def test_rejects_a_header_that_is_not_bson():
    blob = wyc()
    _id, _r1, _r2, offset, _length = struct.unpack_from("<BBHII", blob, 8)
    blob[offset] = 0xFF  # a nonsense document length
    with pytest.raises(ImageError, match="header section"):
        load(bytes(blob))


def test_rejects_an_out_of_range_table_index():
    """Bounds are checked once, at load, so the interpreter loop can trust a
    decoded operand (§3).

    A class's superclass is a global slot index (§8.6), and out-of-range is
    out-of-range whether the table it indexes is a name table or the globals.
    """
    built = compile_source("class C:\n    slot v: int = 0\n")
    built.classes[0].superclass = 99
    with pytest.raises(ImageError, match="superclass"):
        load(built.to_wyc())


def test_rejects_an_export_pointing_at_no_slot():
    built = compile_source("var a = 1\n")
    built.globals[0].name = "a"
    image = built.sections()
    from wypoc.compiler_bc import bsonlite
    from wypoc.compiler_bc.image import _wrap_wyc

    image[SECTION_IDS["exports"]] = bsonlite.encode_document({"a": 5})
    with pytest.raises(ImageError, match="export 'a'"):
        load(_wrap_wyc(image))


# --------------------------------------------------------------------------
# tracing


def test_disassembly_comes_from_the_compilers_own_table():
    """One source of truth for the instruction set: the VM renders through
    `compiler_bc.opcodes` rather than describing instructions its own way."""
    module = LoadedModule(load(compile_fixture("hello.wy").to_wyc()))
    lines = disasm.body(module, module.image.functions[0])
    assert [line.split(None, 1)[1] for line in lines] == [
        "lconst L0 <- static#0   ('Hello ')",
        "add L1 <- L0 + P0",
        "return L1 count=1",
    ]
    assert "hello.wy:3" in disasm.one(module, 12)
