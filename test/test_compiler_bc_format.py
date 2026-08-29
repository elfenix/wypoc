"""Format-layer tests for the bytecode compiler (doc/llm-bytecode.md).

The anchor is the spec's Appendix A worked example: the hand-assembled
`hello.wy` image is frozen ground truth.  Here it is built from `ModuleImage`
builder calls with no AST in sight, and its section bytes are asserted
against the appendix listing verbatim.  When the lowering pipeline lands, it
has to reproduce these same bytes from source - so anything that changes them
is a spec change first and a code change second.
"""

import os
import re
import shutil
import subprocess

import pytest
from conftest import REPO_ROOT

from wypoc.compiler_bc import bsonlite, opcodes
from wypoc.compiler_bc.errors import CompileError
from wypoc.compiler_bc.image import (
    SECTION_IDS,
    ModuleImage,
    assemble_wya,
    read_wyc,
)

INCLUDE_DIR = os.path.join(REPO_ROOT, "wypoc", "compiler_bc", "include")


def unhex(text: str) -> bytes:
    """Bytes from a hexdump, ignoring layout - lets a test quote the spec."""
    return bytes.fromhex(re.sub(r"\s+", "", text))


# The three sections the appendix writes out in full.
APPENDIX_STATICS = unhex(
    """
    20 00 00 00 02 30 00 07 00 00 00 48 65 6C 6C 6F
    20 00 02 31 00 06 00 00 00 57 6F 72 6C 64 00 00
    """
)
APPENDIX_SYMBOLS = unhex(
    """
    14 00 00 00 02 30 00 08 00 00 00 70 72 69 6E 74
    6C 6E 00 00
    """
)
APPENDIX_CODE = unhex(
    """
    A8 00 00 00 00 00 00 00
    43 00 00 00
    42 00 01 00
    42 01 00 00
    46 02 01 00
    A0 01 01 00 00 00 01 00
    A0 01 00 00 00 00 01 00
    02 00 00 00
    46 00 00 00
    85 00 01 00 00 80 00 00
    02 01 01 00
    """
)

L = opcodes.L
P = opcodes.P


@pytest.fixture
def hello_image():
    """Appendix A's `hello.wy` image, hand-built from builder calls.

        fn greet(name):
            return "Hello " + name

        println(greet("World"))

    Pool order follows the compiler's own rule - first encounter in a
    left-to-right, top-to-bottom walk - so "Hello " (inside greet) precedes
    "World" (in the top-level call).
    """
    image = ModuleImage("hello")
    hello_static = image.add_static("Hello ")
    world_static = image.add_static("World")
    greet_global = image.add_global("greet")
    # `println` is a name this module reads and does not define: a free global
    # slot on the fill list, read by an ordinary `gget` (doc/addendum.md).
    println = image.add_free_global("println")

    # module init
    image.emit(opcodes.pack("closure", a0=L(0), a1=0, a2=0, f=0))
    image.emit(opcodes.pack_pairable("gset", greet_global, L(0)))
    image.emit(opcodes.pack_pairable("gget", println, L(0)))
    image.emit(opcodes.pack_pairable("gget", greet_global, L(1)))
    image.emit(opcodes.pack_pairable("lconst", world_static, L(2)))
    image.emit(opcodes.pack("call", a0=L(1), f=1, a1=1))
    image.emit(opcodes.pack("call", a0=L(0), f=1, a1=1))
    image.emit(opcodes.pack("return", a0=L(0), f=0))

    # fn greet
    greet_offset = image.here()
    image.emit(opcodes.pack_pairable("lconst", hello_static, L(0)))
    image.emit(opcodes.pack("add", a0=L(1), a1=L(0), a2=P(0)))
    image.emit(opcodes.pack("return", a0=L(1), f=1))
    image.add_function("greet", params=["name"], nlocals=2, code_offset=greet_offset)
    return image


# --------------------------------------------------------------------------
# 1. Appendix A, reproduced from builder calls


def test_appendix_a_section_bytes(hello_image):
    sections = hello_image.sections()
    assert sections[SECTION_IDS["statics"]] == APPENDIX_STATICS
    assert sections[SECTION_IDS["code"]] == APPENDIX_CODE
    # No symbols section at all: the only name hello interns used to be the
    # `println` relocation path, and that is a free global slot now
    # (doc/addendum.md). An absent section and an empty one are the same
    # thing to a loader (§8), and a writer omits rather than emits empty.
    assert SECTION_IDS["symbols"] not in sections


def test_appendix_a_tables(hello_image):
    sections = hello_image.sections()
    header = bsonlite.decode_document(sections[SECTION_IDS["header"]])
    # Two globals: `greet`, which the module defines, and `println`, which it
    # only reads. `u` is empty and the relocation table is gone - there is
    # nothing left for either to hold (doc/addendum.md).
    assert header == {"n": "hello", "v": 1, "g": 2, "l": 0}

    (greet,) = bsonlite.decode_array(sections[SECTION_IDS["functions"]])
    assert greet == {"n": "greet", "p": [{"n": "name"}], "l": 2, "c": 11, "f": 0}

    assert SECTION_IDS["messages"] not in sections
    assert bsonlite.decode_document(sections[SECTION_IDS["exports"]]) == {"greet": 0}
    assert bsonlite.decode_document(sections[SECTION_IDS["free"]]) == {"println": 1}


def test_greet_body_disassembles_as_the_appendix_reads_it(hello_image):
    listing = [text for _offset, text, _n in opcodes.disassemble(hello_image.code[11:])]
    assert listing == [
        "lconst L0 <- static#0",
        "add L1 <- L0 + P0",
        "return L1 count=1",
    ]


# --------------------------------------------------------------------------
# 2. round-trip: the listing assembles back to the same binary


def test_wya_round_trips_to_wyc(hello_image):
    assert assemble_wya(hello_image.to_wya()) == hello_image.to_wyc()


def test_wya_listing_shape(hello_image):
    lines = hello_image.to_wya().splitlines()
    assert lines[0] == "WYA 1 hello"
    assert "SECTION statics" in lines
    assert any(line.endswith('; [1] str "World"') for line in lines)
    assert any(line.endswith("; gset g0 <- L0   (greet)") for line in lines)
    assert "; fn greet (word offset 11)" in lines
    assert all(len(line) <= 120 for line in lines)


def test_assembler_rejects_an_address_that_does_not_match(hello_image):
    listing = hello_image.to_wya().replace("0010:", "0020:", 1)
    with pytest.raises(CompileError, match="does not match"):
        assemble_wya(listing)


def test_assembler_ignores_comments_and_blank_lines(hello_image):
    listing = hello_image.to_wya()
    noisy = "\n".join(
        line for line in listing.splitlines() for line in (line, "; a note", "")
    )
    assert assemble_wya(noisy) == assemble_wya(listing)


def test_wyc_directory_matches_the_sections(hello_image):
    sections = read_wyc(hello_image.to_wyc())
    assert sections == hello_image.sections()


# --------------------------------------------------------------------------
# 3. the .c container carries the same payloads, and compiles


def c_section_payloads(source: str, symbol: str) -> dict:
    """Read the byte/word arrays back out of a generated .c file."""
    stripped = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    payloads = {}
    for kind, name, body in re.findall(
        rf"static const (uint8_t|uint32_t) {symbol}_(\w+)\[\] = \{{(.*?)\}};",
        stripped,
        flags=re.S,
    ):
        values = [int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]+)", body)]
        width = 1 if kind == "uint8_t" else 4
        payloads[name] = b"".join(v.to_bytes(width, "little") for v in values)
    return payloads


def test_c_payloads_are_byte_identical_to_the_binary(hello_image):
    payloads = c_section_payloads(hello_image.to_c(), "hello")
    for section_id, expected in hello_image.sections().items():
        name = [n for n, i in SECTION_IDS.items() if i == section_id][0]
        assert payloads[name] == expected, name


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler on PATH")
def test_generated_c_compiles_clean(hello_image, tmp_path):
    source = tmp_path / "hello.c"
    source.write_text(hello_image.to_c())
    result = subprocess.run(
        ["cc", "-std=c99", "-Wall", "-Werror", "-I", INCLUDE_DIR, "-c",
         str(source), "-o", str(tmp_path / "hello.o")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_c_identifier_is_required(hello_image):
    hello_image.module_name = "not-an-identifier"
    with pytest.raises(CompileError, match="valid C identifier"):
        hello_image.to_c()


# --------------------------------------------------------------------------
# 4. bsonlite


@pytest.mark.parametrize(
    "value, encoded",
    [
        ({}, "05 00 00 00 00"),
        ({"v": 1}, "0C 00 00 00 10 76 00 01 00 00 00 00"),
        ({"b": True}, "09 00 00 00 08 62 00 01 00"),
        ({"z": None}, "08 00 00 00 0A 7A 00 00"),
        ({"s": "hi"}, "0F 00 00 00 02 73 00 03 00 00 00 68 69 00 00"),
        ({"d": 1.5}, "10 00 00 00 01 64 00 00 00 00 00 00 00 F8 3F 00"),
        ({"n": {"v": 2}}, "14 00 00 00 03 6E 00 0C 00 00 00 10 76 00 02 00 00 00 00 00"),
        ({"x": b"\x01\x02"}, "0F 00 00 00 05 78 00 02 00 00 00 00 01 02 00"),
    ],
)
def test_bsonlite_fixed_vectors(value, encoded):
    blob = unhex(encoded)
    assert bsonlite.encode_document(value) == blob
    assert bsonlite.decode_document(blob) == value


def test_bsonlite_array_keys_are_the_indices():
    assert bsonlite.encode_array(["a", "b"]) == bsonlite.encode_document(
        {"0": "a", "1": "b"}
    )
    assert bsonlite.decode_array(bsonlite.encode_array([1, 2, 3])) == [1, 2, 3]


def test_bsonlite_matches_a_known_good_implementation():
    bson = pytest.importorskip("bson")
    document = {"n": "hello", "v": 1, "g": 1, "u": [0], "f": 2.5, "b": True, "z": None}
    assert bsonlite.encode_document(document) == bson.encode(document)


def test_bsonlite_refuses_types_outside_the_subset():
    with pytest.raises(CompileError, match="pinned BSON subset"):
        bsonlite.encode_document({"s": {1, 2}})
    with pytest.raises(CompileError, match="int32"):
        bsonlite.encode_document({"big": 2**31})


def test_bsonlite_decode_refuses_element_types_outside_the_subset():
    # 0x07 is BSON's ObjectId - valid BSON, not in the pinned eight.
    blob = unhex("14 00 00 00 07 6F 00" + " 00" * 12 + " 00")
    with pytest.raises(CompileError, match="outside the subset"):
        bsonlite.decode_document(blob)


def test_bool_and_int_are_distinct_statics():
    image = ModuleImage("m")
    assert image.add_static(1) != image.add_static(True)
    assert image.add_static(1) != image.add_static(1.0)
    assert image.add_static("x") == image.add_static("x")


# --------------------------------------------------------------------------
# 5. opcode table invariants (spec 2, 3)


def test_no_duplicate_opcode_values():
    values = [op.value for op in opcodes.OPS]
    assert len(values) == len(set(values))
    names = [op.name for op in opcodes.OPS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("op", opcodes.OPS, ids=lambda op: op.name)
def test_opcode_ranges(op):
    if op.form == opcodes.CORE:
        assert op.value < 0x40
    elif op.form == opcodes.PAIRABLE:
        assert 0x40 <= op.value < opcodes.LONG_START
        assert op.wide_value == op.value | 0x80
        assert 0xC0 <= op.wide_value <= 0xFF
    else:
        assert opcodes.LONG_START <= op.value < 0xC0


def test_length_is_the_opcode_high_bit():
    for value, op in opcodes.BY_VALUE.items():
        words = opcodes.pack(value, a0=0)
        assert len(words) == (2 if value >= opcodes.LONG_START else 1), op.name


def test_register_reference_encoding():
    assert opcodes.P(3) == 0x8003
    assert opcodes.reg_name(opcodes.P(3)) == "P3"
    assert opcodes.reg_name(opcodes.L(3)) == "L3"
    assert opcodes.to_reg8(opcodes.P(3)) == 0x83
    assert opcodes.to_reg8(opcodes.L(127)) == 127
    assert opcodes.to_reg8(opcodes.L(128)) is None
    assert opcodes.from_reg8(0x83) == opcodes.P(3)


@pytest.mark.parametrize(
    "name, primary, secondary, wide",
    [
        ("lconst", 1, L(2), False),
        ("lconst", 1, L(200), True),  # register does not fit in reg8
        ("lconst", 900, L(2), False),  # a big table index does not force wide
        ("move", L(0), P(127), False),
        ("move", L(0), P(128), True),
        ("i8", L(0), 127, False),
        ("i8", L(0), 128, True),
        ("jf", L(1), -4, False),
        ("jf", L(1), 40000, True),
        ("jmp", 0, 4, False),
        ("jmp", 0, -40000, True),
    ],
)
def test_compact_form_is_used_whenever_operands_fit(name, primary, secondary, wide):
    words = opcodes.pack_pairable(name, primary, secondary)
    assert (len(words) == 2) is wide
    assert (words[0] & 0xFF) == (
        opcodes.lookup(name).wide_value if wide else opcodes.lookup(name).value
    )


@pytest.mark.parametrize(
    "words",
    [
        opcodes.pack("lnil", a0=L(0)),
        opcodes.pack("call", a0=L(4), f=3, a1=2),
        opcodes.pack_pairable("i8", L(0), -7),
        opcodes.pack_pairable("i8", L(0), 100000),
        opcodes.pack_pairable("jf", L(300), -12),
        opcodes.pack("closure", a0=L(2), a1=1, a2=L(3), f=2),
    ],
)
def test_pack_unpack_round_trip(words):
    entry, is_wide, fields, nwords = opcodes.unpack(words)
    assert nwords == len(words)
    assert opcodes.pack(
        fields["op"],
        f=fields["f"],
        a0=fields["a0"],
        **({"w1": fields["w1"]} if nwords == 2 else {}),
    )[0] == words[0]
    text, _ = opcodes.disassemble_one(words)
    assert text.startswith(entry.wide_name if is_wide else entry.name)


def test_pack_rejects_out_of_range_fields():
    with pytest.raises(CompileError, match="outside"):
        opcodes.pack("lnil", a0=0x10000)
    with pytest.raises(CompileError, match="no second word"):
        opcodes.pack("lnil", a0=0, a1=1)


def test_limits_are_enforced():
    image = ModuleImage("m")
    image.symbols = ["s"] * 65535
    with pytest.raises(CompileError, match="65535 symbols"):
        image.add_symbol("one-too-many")


# --------------------------------------------------------------------------
# 6. the generated C header
#
# The VM decodes what this compiler encodes, so its enum and accessors come
# from the same table rather than being written twice and kept in step by
# hand.


HEADER_PATH = os.path.join(INCLUDE_DIR, "wyrm", "opcode.h")


def test_the_checked_in_opcode_header_matches_the_table():
    """A change to the opcode table that skips tools/generate_opcode_header.py
    fails here rather than shipping a stale header."""
    with open(HEADER_PATH) as f:
        assert f.read() == opcodes.c_header(), (
            "wyrm/opcode.h is out of date - run "
            "`python tools/generate_opcode_header.py`"
        )


def test_the_header_names_every_opcode_exactly_once():
    header = opcodes.c_header()
    for op in opcodes.OPS:
        assert f"WY_OP_{op.name.upper()} " in header
        if op.form == opcodes.PAIRABLE:
            assert f"WY_OP_{op.wide_name.upper()}_WIDE " in header
    assert header.count("= 0x") == len(opcodes.BY_VALUE)


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler on PATH")
def test_the_generated_header_compiles_and_decodes_the_way_python_does(tmp_path):
    """The header's accessors are the D1 fix in C; this checks they pull the
    same fields out of a real instruction that `unpack` does."""
    words = opcodes.pack("call", a0=opcodes.L(0x1234), f=7, a1=0x2345, a2=0x6789)
    source = tmp_path / "check.c"
    source.write_text(
        "#include <stdio.h>\n"
        '#include "wyrm/opcode.h"\n'
        "int main(void) {\n"
        f"    const uint32_t code[2] = {{ 0x{words[0]:08X}u, 0x{words[1]:08X}u }};\n"
        '    printf("%u %u %u %u %u %u\\n", WYRM_OP(code), WYRM_F(code),\n'
        "           WYRM_A0(code), WYRM_A1(code), WYRM_A2(code),\n"
        "           WYRM_OP_WORDS(WYRM_OP(code)));\n"
        "    return WY_OP_CALL == 0xA0 ? 0 : 1;\n"
        "}\n"
    )
    binary = tmp_path / "check"
    build = subprocess.run(
        ["cc", "-std=c99", "-Wall", "-Werror", "-I", INCLUDE_DIR,
         str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0
    _entry, _wide, fields, nwords = opcodes.unpack(words)
    assert run.stdout.split() == [
        str(fields["op"]), str(fields["f"]), str(fields["a0"]),
        str(fields["a1"]), str(fields["a2"]), str(nwords),
    ]
