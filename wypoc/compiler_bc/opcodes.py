"""The v1 opcode set (spec 3) as data, plus the encoder, decoder and disassembler.

This module is the **single source of truth** for the instruction set: every
other part of the toolchain - the emitters, the verifier, the .wy_a listing,
and eventually the generated C enum header for the VM - reads the `OPS` table
here.  Nothing else may hardcode an opcode number.

Encoding (spec 2), little-endian:

    word 0:  [ a0 : u16 ][ f : u8 ][ op : u8 ]
    word 1:  [ a1 : u16 ][ a2 : u16 ]        (a1 in bits 16-31)

Bit 7 of the opcode is the length: `op < 0x80` is one word, `op >= 0x80` is
two.  Ranges: 0x00-0x3F core one-word, 0x40-0x7F compact forms of pairable
ops, 0x80-0xBF two-word-only ops, 0xC0-0xFF the wide twins of the pairable
ops (`wide = compact | 0x80`).
"""

import struct
from dataclasses import dataclass, field

from .errors import CompileError

LONG_START = 0x80  # WYRM_OP_LONG_START: bit 7 of the opcode selects two words

# Form of an entry in the table below.
CORE = "core"  # 0x00-0x3F, one word, no wide twin
PAIRABLE = "pairable"  # 0x40-0x7F compact, | 0x80 wide
LONG = "long"  # 0x80-0xBF, two words, no compact twin

# Shape of a pairable op: which operand is the one that gets demoted into `f`
# in the compact form, and therefore what "does it fit?" means for it.
SHAPE_INV = "inv"  # a0 = table index (full u16 always), reg in f / a1
SHAPE_REG = "reg"  # a0 = dst, source reg in f / a1
SHAPE_IMM = "imm"  # a0 = dst, immediate in f (i8) / word1 (i32)
SHAPE_JCOND = "jcond"  # cond reg in f / a0, offset in a0 (i16) / word1 (i32)
SHAPE_JMP = "jmp"  # offset in a0 (i16) / word1 (i32)


@dataclass(frozen=True)
class Op:
    """One row of the spec 3 tables.

    `operands` maps a label to `(slot, kind)`, where slot is one of
    "a0"/"f"/"a1"/"a2"/"w1" and kind drives both rendering and (later)
    verification.  `fmt` is a format string over those labels, or a callable
    taking the decoded label dict - used by the handful of ops whose listing
    text varies with an operand value.
    """

    value: int
    name: str
    form: str
    fmt: object
    operands: dict = field(default_factory=dict)
    shape: str = ""  # pairable only
    wide_name: str = ""  # pairable only, when the wide twin is spelled differently
    wide_operands: dict = field(default_factory=dict)  # pairable only
    wide_fmt: object = None  # pairable only, when it differs

    @property
    def wide_value(self) -> int:
        return self.value | LONG_START

    @property
    def words(self) -> int:
        return 2 if self.value >= LONG_START else 1


def _core(value, name, fmt, **operands):
    return Op(value=value, name=name, form=CORE, fmt=fmt, operands=operands)


def _long(value, name, fmt, **operands):
    return Op(value=value, name=name, form=LONG, fmt=fmt, operands=operands)


def _pair(value, name, shape, fmt, compact, wide, wide_name="", wide_fmt=None):
    return Op(
        value=value,
        name=name,
        form=PAIRABLE,
        shape=shape,
        fmt=fmt,
        operands=compact,
        wide_operands=wide,
        wide_name=wide_name or name,
        wide_fmt=wide_fmt,
    )


def _binop(value, name, symbol):
    """A three-address arithmetic/comparison op: a0 = dst, a1 = lhs, a2 = rhs."""
    return _long(
        value,
        name,
        "%s {dst} <- {lhs} %s {rhs}" % (name, symbol),
        dst=("a0", "reg"),
        lhs=("a1", "reg"),
        rhs=("a2", "reg"),
    )


def _render_return(values):
    # The appendix spells a zero-value return without a base register, since
    # the base is meaningless when nothing is returned.
    if values["count"] == 0:
        return "return count=0"
    return "return {base} count={count}".format(**values)


OPS = [
    # -- 3.1 core one-word ops -------------------------------------------
    _core(0x00, "noop", "noop"),
    _core(0x01, "trap", "trap {code}", code=("f", "imm")),
    _core(
        0x02,
        "return",
        _render_return,
        base=("a0", "reg"),
        count=("f", "count"),
    ),
    _core(0x03, "lnil", "lnil {dst}", dst=("a0", "reg")),
    _core(0x04, "lbool", "lbool {dst} <- {flag}", dst=("a0", "reg"), flag=("f", "flag")),
    _core(0x05, "lunset", "lunset {dst}", dst=("a0", "reg")),
    # `import_star` moved to 0x82 when it grew an except-window; 0x06 is
    # retired and reserved.
    # 0x07 (`resolve`) is retired and reserved. It bound the module's
    # referenced-name set in one batch; names are global slots filled by
    # whatever supplies them now, so there was nothing left to batch.
    # -- 3.2 pairable ops -------------------------------------------------
    _pair(
        0x40,
        "i8",
        SHAPE_IMM,
        "i8 {dst} <- {imm}",
        {"dst": ("a0", "reg"), "imm": ("f", "i8")},
        {"dst": ("a0", "reg"), "imm": ("w1", "i32")},
        wide_name="i32",
        wide_fmt="i32 {dst} <- {imm}",
    ),
    _pair(
        0x41,
        "move",
        SHAPE_REG,
        "move {dst} <- {src}",
        {"dst": ("a0", "reg"), "src": ("f", "reg8")},
        {"dst": ("a0", "reg"), "src": ("a1", "reg")},
    ),
    _pair(
        0x42,
        "gget",
        SHAPE_INV,
        "gget {dst} <- {glob}",
        {"glob": ("a0", "global"), "dst": ("f", "reg8")},
        {"glob": ("a0", "global"), "dst": ("a1", "reg")},
    ),
    _pair(
        0x43,
        "gset",
        SHAPE_INV,
        "gset {glob} <- {src}",
        {"glob": ("a0", "global"), "src": ("f", "reg8")},
        {"glob": ("a0", "global"), "src": ("a1", "reg")},
    ),
    # 0x44 (`rget`) and 0x50 (`rset`) are retired and reserved. Every name a
    # module references is a global slot now, read and written by `gget` and
    # `gset` (doc/addendum.md), so there is nothing left for a relocation-
    # addressed load or store to do.
    _pair(
        0x45,
        "lsym",
        SHAPE_INV,
        "lsym {dst} <- {sym}",
        {"sym": ("a0", "symbol"), "dst": ("f", "reg8")},
        {"sym": ("a0", "symbol"), "dst": ("a1", "reg")},
    ),
    _pair(
        0x46,
        "lconst",
        SHAPE_INV,
        "lconst {dst} <- {stat}",
        {"stat": ("a0", "static"), "dst": ("f", "reg8")},
        {"stat": ("a0", "static"), "dst": ("a1", "reg")},
    ),
    _pair(
        0x47,
        "import",
        SHAPE_INV,
        "import {dst} <- {path}",
        {"path": ("a0", "static"), "dst": ("f", "reg8")},
        {"path": ("a0", "static"), "dst": ("a1", "reg")},
    ),
    _pair(
        0x48,
        "neg",
        SHAPE_REG,
        "neg {dst} <- {src}",
        {"dst": ("a0", "reg"), "src": ("f", "reg8")},
        {"dst": ("a0", "reg"), "src": ("a1", "reg")},
    ),
    _pair(
        0x49,
        "inv",
        SHAPE_REG,
        "inv {dst} <- {src}",
        {"dst": ("a0", "reg"), "src": ("f", "reg8")},
        {"dst": ("a0", "reg"), "src": ("a1", "reg")},
    ),
    _pair(
        0x4A,
        "not",
        SHAPE_REG,
        "not {dst} <- {src}",
        {"dst": ("a0", "reg"), "src": ("f", "reg8")},
        {"dst": ("a0", "reg"), "src": ("a1", "reg")},
    ),
    _pair(
        0x4B,
        "jf",
        SHAPE_JCOND,
        "jf {cond}, {rel}",
        {"cond": ("f", "reg8"), "rel": ("a0", "rel")},
        {"cond": ("a0", "reg"), "rel": ("w1", "rel")},
    ),
    _pair(
        0x4C,
        "jt",
        SHAPE_JCOND,
        "jt {cond}, {rel}",
        {"cond": ("f", "reg8"), "rel": ("a0", "rel")},
        {"cond": ("a0", "reg"), "rel": ("w1", "rel")},
    ),
    _pair(
        0x4D,
        "jerr",
        SHAPE_JCOND,
        "jerr {cond}, {rel}",
        {"cond": ("f", "reg8"), "rel": ("a0", "rel")},
        {"cond": ("a0", "reg"), "rel": ("w1", "rel")},
    ),
    _pair(
        0x4E,
        "jnerr",
        SHAPE_JCOND,
        "jnerr {cond}, {rel}",
        {"cond": ("f", "reg8"), "rel": ("a0", "rel")},
        {"cond": ("a0", "reg"), "rel": ("w1", "rel")},
    ),
    _pair(
        0x4F,
        "jmp",
        SHAPE_JMP,
        "jmp {rel}",
        {"rel": ("a0", "rel")},
        {"rel": ("w1", "rel")},
    ),
    # -- 3.3 two-word-only ops: loads and data ---------------------------
    _long(0x80, "f32", "f32 {dst} <- {imm}", dst=("a0", "reg"), imm=("w1", "f32")),
    _long(
        0x81,
        "tuple",
        "tuple {dst} <- {base}, {count} items",
        dst=("a0", "reg"),
        base=("a1", "reg"),
        count=("f", "count"),
    ),
    _long(
        0x82,
        "list",
        "list {dst} <- {base}, {count} items",
        dst=("a0", "reg"),
        base=("a1", "reg"),
        count=("f", "count"),
    ),
    _long(
        0x83,
        "dict",
        "dict {dst} <- {base}, {count} pairs",
        dst=("a0", "reg"),
        base=("a1", "reg"),
        count=("f", "count"),
    ),
    _long(
        0x84,
        "plist",
        "plist {dst} <- {base}, {count} items",
        dst=("a0", "reg"),
        base=("a1", "reg"),
        count=("f", "count"),
    ),
    # arithmetic / comparison
    _binop(0x85, "add", "+"),
    _binop(0x86, "sub", "-"),
    _binop(0x87, "mul", "*"),
    _binop(0x88, "div", "/"),
    _binop(0x89, "mod", "%"),
    _binop(0x8A, "pow", "**"),
    _binop(0x8B, "band", "&"),
    _binop(0x8C, "bor", "|"),
    _binop(0x8D, "shl", "<<"),
    _binop(0x8E, "shr", ">>"),
    _binop(0x8F, "eq", "=="),
    _binop(0x90, "ne", "!="),
    _binop(0x91, "lt", "<"),
    _binop(0x92, "le", "<="),
    _binop(0x93, "gt", ">"),
    _binop(0x94, "ge", ">="),
    _binop(0x95, "bxor", "^"),
    _long(
        0x96,
        "in",
        "in {dst} <- {item} in {container}",
        dst=("a0", "reg"),
        item=("a1", "reg"),
        container=("a2", "reg"),
    ),
    _long(
        0x97,
        "is",
        "is {dst} <- {val} is {type}",
        dst=("a0", "reg"),
        val=("a1", "reg"),
        type=("a2", "reg"),
    ),
    # object access
    _long(
        0x98,
        "getidx",
        "getidx {dst} <- {obj}[{index}]",
        dst=("a0", "reg"),
        obj=("a1", "reg"),
        index=("a2", "reg"),
    ),
    _long(
        0x99,
        "setidx",
        "setidx {obj}[{index}] <- {src}",
        obj=("a0", "reg"),
        index=("a1", "reg"),
        src=("a2", "reg"),
    ),
    _long(
        0x9A,
        "getattr",
        "getattr {dst} <- {obj}.{sym}",
        dst=("a0", "reg"),
        obj=("a1", "reg"),
        sym=("a2", "symbol"),
    ),
    _long(
        0x9B,
        "setattr",
        "setattr {obj}.{sym} <- {src}",
        obj=("a0", "reg"),
        sym=("a1", "symbol"),
        src=("a2", "reg"),
    ),
    _long(
        0x9C,
        "getslot",
        "getslot {dst} <- {obj}#{slot}",
        dst=("a0", "reg"),
        obj=("a1", "reg"),
        slot=("a2", "slot"),
    ),
    _long(
        0x9D,
        "setslot",
        "setslot {obj}#{slot} <- {src}",
        obj=("a0", "reg"),
        slot=("a1", "slot"),
        src=("a2", "reg"),
    ),
    # calls and returns
    _long(
        0xA0,
        "call",
        "call base={base} argc={argc} nres={nres}",
        base=("a0", "reg"),
        argc=("f", "count"),
        nres=("a1", "count"),
    ),
    _long(
        0xA1,
        "call_va",
        "call_va base={base} nres={nres}",
        base=("a0", "reg"),
        nres=("a1", "count"),
    ),
    _long(
        0xA2,
        "msg",
        "msg base={base} argc={argc} {message} nres={nres}",
        base=("a0", "reg"),
        argc=("f", "count"),
        message=("a1", "message"),
        nres=("a2", "count"),
    ),
    _long(
        0xA3,
        "msg_va",
        "msg_va base={base} {message} nres={nres}",
        base=("a0", "reg"),
        message=("a1", "message"),
        nres=("a2", "count"),
    ),
    _long(
        0xA4,
        "getmsg",
        "getmsg {dst} <- {recv} ! {message}",
        dst=("a0", "reg"),
        recv=("a1", "reg"),
        message=("a2", "message"),
    ),
    _long(
        0xA5,
        "super",
        "super base={base} argc={argc} nres={nres}",
        base=("a0", "reg"),
        argc=("f", "count"),
        nres=("a1", "count"),
    ),
    _long(
        0xA6,
        "return_cps",
        "return_cps base={base} argc={argc} cont={cont}",
        base=("a0", "reg"),
        argc=("f", "count"),
        cont=("a1", "reg"),
    ),
    _long(
        0xA7,
        "yield",
        "yield base={base} count={count}",
        base=("a0", "reg"),
        count=("f", "count"),
    ),
    # construction and registration
    _long(
        0xA8,
        "closure",
        "closure {dst} <- {fn}, {ncaps} caps",
        dst=("a0", "reg"),
        fn=("a1", "function"),
        caps=("a2", "reg"),
        ncaps=("f", "count"),
    ),
    _long(0xA9, "class", "class {dst} <- {cls}", dst=("a0", "reg"), cls=("a1", "class")),
    _long(
        0xAA,
        "new_instance",
        "new_instance {dst} <- {cls}",
        dst=("a0", "reg"),
        cls=("a1", "reg"),
    ),
    _long(
        0xAB,
        "new_primitive",
        "new_primitive {dst} <- type {tag}",
        dst=("a0", "reg"),
        tag=("f", "imm"),
    ),
    _long(
        0xAC,
        "reg_msg",
        "reg_msg {message} <- {fn}, types {types}",
        message=("a0", "message"),
        fn=("a1", "reg"),
        types=("a2", "reg"),
    ),
    _long(
        0xAD,
        "defer_reg",
        "defer_reg {closure} mode={mode}",
        closure=("a0", "reg"),
        mode=("f", "imm"),
    ),
    # iteration
    _long(0xAE, "iter", "iter {dst} <- {src}", dst=("a0", "reg"), src=("a1", "reg")),
    _long(
        0xAF,
        "itnext",
        "itnext {dst} <- {iter}, done {rel}",
        dst=("a0", "reg"),
        iter=("a1", "reg"),
        rel=("a2", "rel"),
    ),
    # delegation, unpacking, scope access
    _long(
        0xB0,
        "yield_from",
        "yield_from {dst} <- {sub}",
        dst=("a0", "reg"),
        sub=("a1", "reg"),
    ),
    _long(
        0xB1,
        "unpack",
        "unpack {dst}.. <- {src}, {count} items",
        dst=("a0", "reg"),
        src=("a1", "reg"),
        count=("f", "count"),
    ),
    _long(
        0xB2,
        "cmp3",
        "cmp3 {dst} <- {lhs} <=> {rhs}",
        dst=("a0", "reg"),
        lhs=("a1", "reg"),
        rhs=("a2", "reg"),
    ),
    _long(
        0xB3,
        "getscope",
        "getscope {dst} <- {obj}::{sym}",
        dst=("a0", "reg"),
        obj=("a1", "reg"),
        sym=("a2", "symbol"),
    ),
    _long(
        0xB4,
        "setscope",
        "setscope {obj}::{sym} <- {src}",
        obj=("a0", "reg"),
        sym=("a1", "symbol"),
        src=("a2", "reg"),
    ),
    # Two words because it carries a window: the path is a constant string and
    # the except-list is `count` interned symbols starting at `base`, read the
    # same way `tuple`/`list`/`dict` read theirs. Neither needs a table.
    _long(
        0xB5,
        "import_star",
        "import_star {path} except {base}, {count} names",
        path=("a0", "static"),
        base=("a1", "base"),
        count=("f", "count"),
    ),
]

BY_NAME = {}
BY_VALUE = {}
for _op in OPS:
    BY_NAME[_op.name] = _op
    BY_VALUE[_op.value] = _op
    if _op.form == PAIRABLE:
        BY_VALUE[_op.wide_value] = _op
        if _op.wide_name != _op.name:
            BY_NAME[_op.wide_name] = _op
del _op


def lookup(op) -> Op:
    """Resolve a mnemonic or an opcode value to its table entry."""
    entry = BY_NAME.get(op) if isinstance(op, str) else BY_VALUE.get(op)
    if entry is None:
        raise CompileError(f"unknown opcode {op!r}")
    return entry


# --------------------------------------------------------------------------
# register references (spec 2.1)

P_BIT = 0x8000


def L(slot: int) -> int:
    """A u16 register reference naming L`slot` (a local or temp)."""
    if not 0 <= slot <= 0x7FFF:
        raise CompileError(f"L{slot} is outside the 32767-local frame limit")
    return slot


def P(slot: int) -> int:
    """A u16 register reference naming P`slot` (a this value, param or capture)."""
    if not 0 <= slot <= 0x7FFF:
        raise CompileError(f"P{slot} is outside the 32767-slot P frame limit")
    return P_BIT | slot


def is_p(reg: int) -> bool:
    return bool(reg & P_BIT)


def reg_index(reg: int) -> int:
    return reg & 0x7FFF


def reg_name(reg: int) -> str:
    return f"P{reg_index(reg)}" if is_p(reg) else f"L{reg}"


def to_reg8(reg: int):
    """The 8-bit form of a register reference, or None when it does not fit.

    A reg8 addresses L0-L127 (bit 7 clear) or P0-P127 (bit 7 set); anything
    above that forces the wide form of the instruction.
    """
    index = reg_index(reg)
    if index > 127:
        return None
    return (0x80 | index) if is_p(reg) else index


def from_reg8(byte: int) -> int:
    return P(byte & 0x7F) if byte & 0x80 else L(byte)


def fits_i8(value: int) -> bool:
    return -128 <= value <= 127


def fits_i16(value: int) -> bool:
    return -32768 <= value <= 32767


# --------------------------------------------------------------------------
# encoding


def pack(op, f=0, a0=0, a1=0, a2=0, w1=None):
    """Encode one instruction into a list of 1 or 2 u32 words.

    `op` is a mnemonic or an opcode value; for a pairable op the mnemonic
    selects the compact form, so callers that want the wide form pass its
    value (`compact | 0x80`) or - for i8/i32 - the wide mnemonic.  `w1` sets
    the whole second word (a 32-bit payload) and is mutually exclusive with
    a1/a2.
    """
    entry = lookup(op)
    value = op if isinstance(op, int) else _value_for_name(entry, op)
    _check_field("op", value, 0, 0xFF)
    _check_field("f", f, 0, 0xFF)
    _check_field("a0", a0, 0, 0xFFFF)
    word0 = (a0 & 0xFFFF) << 16 | (f & 0xFF) << 8 | value
    if value < LONG_START:
        if a1 or a2 or w1 is not None:
            raise CompileError(f"{entry.name}: one-word form has no second word")
        return [word0]
    if w1 is not None:
        if a1 or a2:
            raise CompileError(f"{entry.name}: w1 and a1/a2 are alternatives")
        word1 = w1 & 0xFFFFFFFF
    else:
        _check_field("a1", a1, 0, 0xFFFF)
        _check_field("a2", a2, 0, 0xFFFF)
        word1 = (a1 & 0xFFFF) << 16 | (a2 & 0xFFFF)
    return [word0, word1]


def _value_for_name(entry, name):
    if entry.form == PAIRABLE and name == entry.wide_name != entry.name:
        return entry.wide_value
    return entry.value


def _check_field(name, value, low, high):
    if not isinstance(value, int) or isinstance(value, bool):
        raise CompileError(f"instruction field {name} must be an int, got {value!r}")
    if not low <= value <= high:
        raise CompileError(f"instruction field {name}={value} is outside {low}..{high}")


def pack_pairable(name, primary=0, secondary=0):
    """Encode a pairable op, choosing the compact form when the operands fit.

    What `primary`/`secondary` mean follows the op's shape (spec 3.2):

    * `inv`   - primary = table index, secondary = register reference
    * `reg`   - primary = destination register, secondary = source register
    * `imm`   - primary = destination register, secondary = integer immediate
    * `jcond` - primary = condition register, secondary = jump offset
    * `jmp`   - secondary = jump offset (primary unused)

    This is the only place the compact/wide fallback rule lives; emitters call
    it rather than deciding for themselves.
    """
    entry = lookup(name)
    if entry.form != PAIRABLE:
        raise CompileError(f"{entry.name} is not a pairable op")
    wide = entry.wide_value
    if entry.shape == SHAPE_INV:
        byte = to_reg8(secondary)
        if byte is None:
            return pack(wide, a0=primary, a1=secondary)
        return pack(entry.value, a0=primary, f=byte)
    if entry.shape == SHAPE_REG:
        byte = to_reg8(secondary)
        if byte is None:
            return pack(wide, a0=primary, a1=secondary)
        return pack(entry.value, a0=primary, f=byte)
    if entry.shape == SHAPE_IMM:
        if fits_i8(secondary):
            return pack(entry.value, a0=primary, f=secondary & 0xFF)
        return pack(wide, a0=primary, w1=secondary & 0xFFFFFFFF)
    if entry.shape == SHAPE_JCOND:
        byte = to_reg8(primary)
        if byte is None or not fits_i16(secondary):
            return pack(wide, a0=primary, w1=secondary & 0xFFFFFFFF)
        return pack(entry.value, a0=secondary & 0xFFFF, f=byte)
    if entry.shape == SHAPE_JMP:
        if fits_i16(secondary):
            return pack(entry.value, a0=secondary & 0xFFFF)
        return pack(wide, w1=secondary & 0xFFFFFFFF)
    raise CompileError(f"{entry.name}: unhandled pairable shape {entry.shape!r}")


# --------------------------------------------------------------------------
# decoding and disassembly


def unpack(words, offset=0):
    """Decode the instruction at `words[offset]`.

    Returns `(entry, is_wide, fields, nwords)` where `fields` carries the raw
    slot values `op/f/a0/a1/a2/w1`.
    """
    word0 = words[offset]
    value = word0 & 0xFF
    entry = BY_VALUE.get(value)
    if entry is None:
        raise CompileError(f"unknown opcode 0x{value:02X} at word {offset}")
    fields = {
        "op": value,
        "f": (word0 >> 8) & 0xFF,
        "a0": (word0 >> 16) & 0xFFFF,
        "a1": 0,
        "a2": 0,
        "w1": 0,
    }
    if value < LONG_START:
        return entry, False, fields, 1
    if offset + 1 >= len(words):
        raise CompileError(f"truncated two-word instruction at word {offset}")
    word1 = words[offset + 1]
    fields["w1"] = word1
    fields["a1"] = (word1 >> 16) & 0xFFFF
    fields["a2"] = word1 & 0xFFFF
    is_wide = entry.form == PAIRABLE
    return entry, is_wide, fields, 2


def disassemble_one(words, offset=0, image=None):
    """Render the instruction at `words[offset]` as `(text, nwords)`."""
    entry, is_wide, fields, nwords = unpack(words, offset)
    operands = entry.wide_operands if is_wide else entry.operands
    fmt = (entry.wide_fmt or entry.fmt) if is_wide else entry.fmt
    values = {}
    for label, (slot, kind) in operands.items():
        values[label] = _decode_operand(fields[slot], kind)
    text = fmt(values) if callable(fmt) else fmt.format(**values)
    annotation = _annotate(entry, operands, fields, image)
    if annotation:
        text = f"{text}   {annotation}"
    return text, nwords


def disassemble(words, image=None, start_word=0):
    """Render a whole code array as `(word_offset, text, nwords)` triples."""
    offset = 0
    out = []
    while offset < len(words):
        text, nwords = disassemble_one(words, offset, image)
        out.append((start_word + offset, text, nwords))
        offset += nwords
    return out


class _RenderedInt(int):
    """An int that prints as its operand rendering (so `{count}` == 0 tests work)."""

    def __new__(cls, value, text):
        self = super().__new__(cls, value)
        self.text = text
        return self

    def __str__(self):
        return self.text

    def __format__(self, spec):
        return format(self.text, spec)


def _decode_operand(raw, kind):
    if kind == "reg":
        return _RenderedInt(raw, reg_name(raw))
    if kind == "reg8":
        reg = from_reg8(raw)
        return _RenderedInt(reg, reg_name(reg))
    if kind == "global":
        return _RenderedInt(raw, f"g{raw}")
    if kind == "static":
        return _RenderedInt(raw, f"static#{raw}")
    if kind == "symbol":
        return _RenderedInt(raw, f"sym#{raw}")
    if kind == "message":
        return _RenderedInt(raw, f"msg#{raw}")
    if kind == "function":
        return _RenderedInt(raw, f"fn#{raw}")
    if kind == "class":
        return _RenderedInt(raw, f"class#{raw}")
    if kind == "rel":
        signed = _signed(raw, 16 if raw <= 0xFFFF else 32)
        return _RenderedInt(signed, f"{signed:+d}")
    if kind == "i8":
        return _RenderedInt(_signed(raw, 8), str(_signed(raw, 8)))
    if kind == "i32":
        return _RenderedInt(_signed(raw, 32), str(_signed(raw, 32)))
    if kind == "f32":
        (value,) = struct.unpack("<f", struct.pack("<I", raw & 0xFFFFFFFF))
        return value
    # imm / count / flag / slot: plain numbers
    return _RenderedInt(raw, str(raw))


def _signed(raw, bits):
    sign = 1 << (bits - 1)
    return (raw & (sign - 1)) - (raw & sign)


def _annotate(entry, operands, fields, image):
    """The parenthesised hint a listing puts after an instruction, when the
    image can tell us what a pool index actually holds."""
    if image is None:
        return ""
    notes = []
    for label, (slot, kind) in operands.items():
        note = image.describe(kind, fields[slot]) if hasattr(image, "describe") else None
        if note:
            notes.append(note)
    return f"({', '.join(notes)})" if notes else ""


# --------------------------------------------------------------------------
# the C side of the table
#
# The VM decodes the same instructions this module encodes, so its enum and
# accessors are generated from the table above rather than written a second
# time and kept in step by hand.  tools/generate_opcode_header.py writes the
# result; a test asserts the checked-in header still matches, so the two
# cannot drift.


C_HEADER_PATH = "include/wyrm/opcode.h"


def c_header() -> str:
    """The `wyrm/opcode.h` this table implies."""
    out = [
        "/* wyrm bytecode opcodes - GENERATED from"
        " wypoc/compiler_bc/opcodes.py.",
        " *",
        " * Do not hand-edit: run tools/generate_opcode_header.py after"
        " changing the",
        " * opcode table. doc/llm-bytecode.md section 3 is the prose"
        " alongside it.",
        " *",
        " * Instruction encoding (section 2), little-endian:",
        " *",
        " *   word 0:  [ a0 : u16 ][ f : u8 ][ op : u8 ]",
        " *   word 1:  [ a1 : u16 ][ a2 : u16 ]",
        " *",
        " * The accessors below place `f` at bits 8-15, which is the fix"
        " Appendix B",
        " * D1 records: the reference opcode.h packed it at 24-31, colliding"
        " with a0. */",
        "#ifndef WYRM_OPCODE_H",
        "#define WYRM_OPCODE_H",
        "",
        "#include <stdint.h>",
        "",
        f"/* Bit 7 of the opcode selects the instruction length. */",
        f"#define WYRM_OP_LONG_START 0x{LONG_START:02X}",
        "#define WYRM_OP_WORDS(op) (((op) & WYRM_OP_LONG_START) ? 2u : 1u)",
        "",
        "#define WYRM_OP(code)  ((uint8_t)((code)[0] & 0xff))",
        "#define WYRM_F(code)   ((uint8_t)(((code)[0] >> 8) & 0xff))",
        "#define WYRM_A0(code)  ((uint16_t)(((code)[0] >> 16) & 0xffff))",
        "#define WYRM_A1(code)  ((uint16_t)(((code)[1] >> 16) & 0xffff))",
        "#define WYRM_A2(code)  ((uint16_t)((code)[1] & 0xffff))",
        "",
        "/* Register references (section 2.1): bit 15 of a u16 ref, or bit 7",
        " * of the 8-bit `f` form, selects the P stack. */",
        "#define WYRM_REG_P_BIT   0x8000u",
        "#define WYRM_REG_IS_P(r) (((r) & WYRM_REG_P_BIT) != 0)",
        "#define WYRM_REG_INDEX(r) ((r) & 0x7fffu)",
        "#define WYRM_REG8_IS_P(r) (((r) & 0x80u) != 0)",
        "#define WYRM_REG8_INDEX(r) ((r) & 0x7fu)",
        "",
        "typedef enum wy_opcode {",
    ]
    rows = []
    for op in sorted(OPS, key=lambda entry: entry.value):
        rows.append((f"WY_OP_{op.name.upper()}", op.value, op.form))
        if op.form == PAIRABLE:
            rows.append(
                (f"WY_OP_{op.wide_name.upper()}_WIDE", op.wide_value, "wide")
            )
    width = max(len(name) for name, _value, _form in rows)
    for name, value, form in rows:
        out.append(f"    {name:<{width}} = 0x{value:02X},  /* {form} */")
    out.append("} wy_opcode;")
    out.append("")
    out.append(f"#define WYRM_OP_COUNT {len(rows)}")
    out.append("")
    out.append("#endif /* WYRM_OPCODE_H */")
    return "\n".join(out) + "\n"
