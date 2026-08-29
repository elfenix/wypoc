"""Structural verification of a module image.

The verifier is the safety net for a register machine: it re-reads a finished
image the way the VM's loader will and checks the things a lowering bug
quietly gets wrong - a jump into the middle of an instruction, a register
operand past the end of its frame, a call window that runs off the frame, a
table index out of range, a `u` set that does not match the names the body
actually references.

It is deliberately structural, not semantic: it says nothing about whether the
program is *right*, only that the image is well formed.  `compile_module` runs
it on every image it produces, so a lowering bug surfaces as a `CompileError`
at compile time rather than as a crash in a VM that does not exist yet.
"""

from . import opcodes
from .errors import CompileError

# The ops whose operands address a run of consecutive frame slots.  Each entry
# maps an opcode name to a function of the decoded fields returning
# `(base slot ref, count)` pairs that must fit inside the frame.


class Region:
    """One function's slice of the code array, with the frame it runs in."""

    def __init__(self, name, start, end, nlocals, pframe, uses):
        self.name = name
        self.start = start
        self.end = end
        self.nlocals = nlocals
        self.pframe = pframe
        self.uses = set(uses)


def verify(image) -> None:
    """Check `image`; raise `CompileError` on the first problem found."""
    for region in _regions(image):
        _verify_region(image, region)


def _regions(image):
    """Split the code array into the init routine and each function body.

    Bodies are laid out end to end in code-offset order, so each one runs to
    the start of the next.
    """
    entries = sorted(
        ((fn.code_offset, fn) for fn in image.functions), key=lambda pair: pair[0]
    )
    bounds = [offset for offset, _fn in entries] + [len(image.code)]
    regions = [
        Region(
            "<init>",
            0,
            bounds[0],
            image.init_nlocals,
            0,
            image.module_uses,
        )
    ]
    for index, (offset, fn) in enumerate(entries):
        pframe = len(fn.params) + fn.ncaptures + len(fn.dispatch)
        regions.append(
            Region(fn.name, offset, bounds[index + 1], fn.nlocals, pframe, fn.uses)
        )
    return regions


def _verify_region(image, region):
    boundaries = set()
    instructions = []
    offset = region.start
    while offset < region.end:
        entry, is_wide, fields, nwords = opcodes.unpack(image.code, offset)
        if offset + nwords > region.end:
            _fail(region, offset, "instruction runs past the end of the body")
        boundaries.add(offset)
        instructions.append((offset, entry, is_wide, fields, nwords))
        offset += nwords
    boundaries.add(region.end)

    referenced = set()

    for offset, entry, is_wide, fields, nwords in instructions:
        operands = entry.wide_operands if is_wide else entry.operands
        unread = _unread_operands(entry, fields)
        for label, (slot, kind) in operands.items():
            if label in unread:
                continue
            raw = fields[slot]
            if kind in ("reg", "reg8"):
                reg = opcodes.from_reg8(raw) if kind == "reg8" else raw
                _check_register(region, offset, entry, label, reg)
            elif kind == "rel":
                _check_jump(region, offset, entry, raw, nwords, boundaries, slot)
            elif kind == "message":
                _check_index(region, offset, entry, label, raw, image.messages)
                referenced.add(raw)
            elif kind == "static":
                _check_index(region, offset, entry, label, raw, image.statics)
            elif kind == "symbol":
                _check_index(region, offset, entry, label, raw, image.symbols)
            elif kind == "global":
                _check_index(region, offset, entry, label, raw, image.globals)
            elif kind == "function":
                _check_index(region, offset, entry, label, raw, image.functions)
            elif kind == "class":
                _check_index(region, offset, entry, label, raw, image.classes)
        for base, count in _windows(entry, fields):
            _check_window(region, offset, entry, base, count)

    _check_uses(image, region, referenced)


# --------------------------------------------------------------------------
# individual checks


def _check_register(region, offset, entry, label, reg):
    index = opcodes.reg_index(reg)
    if opcodes.is_p(reg):
        if index >= region.pframe:
            _fail(
                region,
                offset,
                f"{entry.name} {label} is P{index}, but the P frame holds "
                f"{region.pframe} slot(s)",
            )
    elif index >= region.nlocals:
        _fail(
            region,
            offset,
            f"{entry.name} {label} is L{index}, but the frame holds "
            f"{region.nlocals} slot(s)",
        )


def _check_window(region, offset, entry, base, count):
    if opcodes.is_p(base):
        _fail(
            region,
            offset,
            f"{entry.name} window base is P{opcodes.reg_index(base)}; windows "
            "live in the L frame",
        )
    if base + count > region.nlocals:
        _fail(
            region,
            offset,
            f"{entry.name} window L{base}..L{base + count - 1} runs past the "
            f"{region.nlocals}-slot frame",
        )


def _check_jump(region, offset, entry, raw, nwords, boundaries, slot):
    bits = 32 if slot == "w1" else 16
    delta = (raw & ((1 << (bits - 1)) - 1)) - (raw & (1 << (bits - 1)))
    target = offset + nwords + delta
    if not region.start <= target <= region.end:
        _fail(
            region,
            offset,
            f"{entry.name} jumps to word {target}, outside the body "
            f"[{region.start}, {region.end})",
        )
    if target not in boundaries:
        _fail(
            region,
            offset,
            f"{entry.name} jumps to word {target}, which is not an "
            "instruction boundary",
        )


def _check_index(region, offset, entry, label, raw, table):
    if raw >= len(table):
        _fail(
            region,
            offset,
            f"{entry.name} {label} is index {raw}, but that table has "
            f"{len(table)} entr(ies)",
        )


def _check_uses(image, region, referenced):
    """The scope's `u` set is exactly the `messages` entries its code reads.

    A class's superclass and a method's dispatch types used to belong here too
    - they are operands of the `classes` and `functions` tables rather than of
    any opcode, and they had to be bound before the `class` that realizes them
    ran. They are global slots now (doc/addendum.md), so they are not part of
    this set and need no separate accounting.
    """
    missing = referenced - region.uses
    if missing:
        names = ", ".join(_message_name(image, index) for index in sorted(missing))
        _fail(
            region,
            region.start,
            f"references {names} without listing them in its referenced-name set",
        )
    extra = region.uses - referenced
    if extra:
        names = ", ".join(_message_name(image, index) for index in sorted(extra))
        _fail(
            region,
            region.start,
            f"lists {names} in its referenced-name set but never reads them",
        )


def _message_name(image, index):
    try:
        entry = image.messages[index]
    except IndexError:
        return f"msg#{index}"
    return f'msg#{index} ({"::".join(entry.get("_path", ()))})' 


# --------------------------------------------------------------------------
# register windows


def _unread_operands(entry, fields) -> frozenset:
    """Operand labels this instruction does not actually read.

    An empty window has a base register the VM never touches - `return
    count=0` and `dict dst, base, 0` are both meaningful with any base at all,
    and the compiler naturally leaves it pointing one past the last live slot.
    Checking it would reject correct code, so it is skipped here for the same
    reason the disassembler leaves it out of `return count=0`.
    """
    if fields["f"]:
        return frozenset()
    name = entry.name
    if name in ("return", "yield"):
        return frozenset({"base"})
    if name in ("tuple", "list", "plist", "dict"):
        return frozenset({"base"})
    if name == "closure":
        return frozenset({"caps"})
    if name == "unpack":
        return frozenset({"dst"})
    return frozenset()


def _windows(entry, fields):
    """The `(base, count)` runs of frame slots an instruction touches."""
    name = entry.name
    a0, a1, a2, f = fields["a0"], fields["a1"], fields["a2"], fields["f"]
    if name == "return":
        return [(a0, f)] if f else []
    if name == "call":
        # callee plus arguments going in, results coming back
        return [(a0, f + 1), (a0, max(a1, 1))]
    if name == "super":
        # no callee slot: the dispatch is already in progress, so the window
        # is the arguments alone (spec 3.3)
        return [(a0, max(f, 1)), (a0, max(a1, 1))]
    if name == "call_va":
        return [(a0, 3), (a0, max(a1, 1))]
    if name == "msg":
        return [(a0, f + 1), (a0, max(a2, 1))]
    if name == "msg_va":
        return [(a0, 3), (a0, max(a2, 1))]
    if name == "yield":
        return [(a0, max(f, 1))]
    if name in ("tuple", "list", "plist"):
        return [(a1, f)]
    if name == "dict":
        return [(a1, 2 * f)]
    if name == "unpack":
        return [(a0, f)]
    if name == "closure":
        return [(a2, f)] if f else []
    return []


def _fail(region, offset, message):
    raise CompileError(f"verify: {region.name} at word {offset}: {message}")
