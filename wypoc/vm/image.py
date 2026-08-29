"""Reading a `.wyc` image into the shape a VM runs from.

This is load step 1-5 of doc/wyc-format.md §7.1: parse the container, decode
the sections, and turn each table into records with its indices already
checked. Nothing here executes anything, and nothing here is lazy - an image
that will fail is refused now rather than part-way through a program.

The strictness is deliberate. Every index in every table is bounds-checked
once, at load, so the interpreter loop never has to; and every rejection
§2 calls for happens here, so a malformed image cannot reach the loop at all.
"""

import struct
from dataclasses import dataclass, field

from wypoc.compiler_bc import bsonlite
from wypoc.compiler_bc.image import (
    FN_COROUTINE,
    FN_KWARGS,
    FN_MESSAGE,
    FN_VARARGS,
    SECTION_IDS,
    SECTION_NAMES,
)

from .errors import ImageError

MAGIC = b"WYC\x00"
CONTAINER_VERSION = 1
DEBUG_SECTION = SECTION_IDS["debug"]


@dataclass
class Param:
    name: str
    default: object = None  # static pool index, or None


@dataclass
class Function:
    name: str
    params: list
    nlocals: int
    code_offset: int
    flags: int
    dispatch: list = field(default_factory=list)
    ncaptures: int = 0
    nresults: int = 1

    @property
    def is_coroutine(self):
        return bool(self.flags & FN_COROUTINE)

    @property
    def is_message(self):
        return bool(self.flags & FN_MESSAGE)

    @property
    def has_varargs(self):
        return bool(self.flags & FN_VARARGS)

    @property
    def has_kwargs(self):
        return bool(self.flags & FN_KWARGS)

    @property
    def pframe(self):
        """this values, then parameters, then captures (spec 1.1)."""
        return len(self.dispatch) + len(self.params) + self.ncaptures


@dataclass
class Slot:
    name: str
    default: object = None  # static index, or None
    getter: object = None  # function index, or None
    setter: object = None


@dataclass
class Class:
    name: str
    superclass: object = None  # global slot index, or None
    slots: list = field(default_factory=list)
    init: object = None  # function index, or None
    messages: list = field(default_factory=list)  # (symbol index, fn index)
    statics: list = field(default_factory=list)  # (name, global index)


@dataclass
class Message:
    """One message identity, by path. The last table with a runtime binding
    behind it: a message resolves in its own namespace, and an unqualified one
    creates the identity when nothing has it yet (doc/wyc-format.md §7.3)."""

    path: tuple  # symbol indices

    def spell(self, symbols):
        return "::".join(symbols[i] for i in self.path)


class LoadedImage:
    """One decoded `.wyc`, checked and ready to link."""

    def __init__(self, blob: bytes):
        self.sections = _split(blob)
        header = self._document("header", required=True)
        self.name = _require(header, "n", str, "header")
        version = _require(header, "v", int, "header")
        if version != 1:
            raise ImageError(f"header format version {version}, expected 1")
        self.nglobals = _require(header, "g", int, "header")
        self.init_nlocals = _require(header, "l", int, "header")

        self.statics = self._array("statics")
        self.symbols = self._array("symbols")
        self.code = self._code()
        self.messages = [_message(e, i) for i, e in enumerate(self._array("messages"))]
        self.functions = [_function(e, i) for i, e in enumerate(self._array("functions"))]
        self.classes = [_class(e, i) for i, e in enumerate(self._array("classes"))]
        self.slot_defaults = {
            int(key): value
            for key, value in self._document("slot_defaults").items()
        }
        self.exports = dict(self._document("exports"))
        # name -> global slot, for every name this module references but does
        # not define (§8.11). The fill list: the loader walks it against the
        # builtins, and each import walks it again for what that import
        # supplies (doc/addendum.md).
        self.free = dict(self._document("free"))
        self.debug = self._document("debug")

        self._check_bounds()

    # -- section access ---------------------------------------------------

    def _document(self, name, required=False) -> dict:
        payload = self.sections.get(SECTION_IDS[name])
        if payload is None:
            if required:
                raise ImageError(f"image has no {name} section")
            return {}
        try:
            return bsonlite.decode_document(payload)
        except Exception as error:
            raise ImageError(f"{name} section: {error}") from error

    def _array(self, name) -> list:
        payload = self.sections.get(SECTION_IDS[name])
        if payload is None:
            return []
        try:
            return bsonlite.decode_array(payload)
        except Exception as error:
            raise ImageError(f"{name} section: {error}") from error

    def _code(self) -> list:
        payload = self.sections.get(SECTION_IDS["code"])
        if payload is None:
            raise ImageError("image has no code section")
        if len(payload) % 4:
            raise ImageError(f"code section is {len(payload)} bytes, not a whole number of words")
        return list(struct.unpack(f"<{len(payload) // 4}I", payload))

    # -- validation --------------------------------------------------------

    def _check_bounds(self):
        """Every index in every table, checked once so the loop need not.

        §3: "A loader MUST reject any index that is out of range for its
        table." Doing it here is what lets the interpreter treat a decoded
        operand as trustworthy.
        """
        for index, message in enumerate(self.messages):
            for symbol in message.path:
                self._bound(symbol, self.symbols, f"message {index} path")
        for index, fn in enumerate(self.functions):
            where = f"function {index} ({fn.name})"
            self._bound(fn.code_offset, self.code, f"{where} code offset")
            for param in fn.params:
                if param.default is not None:
                    self._bound(param.default, self.statics, f"{where} default")
            for slot in fn.dispatch:
                self._bound(slot, range(self.nglobals), f"{where} dispatch type")
        for index, cls in enumerate(self.classes):
            where = f"class {index} ({cls.name})"
            if cls.superclass is not None:
                self._bound(cls.superclass, range(self.nglobals), f"{where} superclass")
            if cls.init is not None:
                self._bound(cls.init, self.functions, f"{where} init")
            for slot in cls.slots:
                if slot.default is not None:
                    self._bound(slot.default, self.statics, f"{where} slot default")
                for accessor in (slot.getter, slot.setter):
                    if accessor is not None:
                        self._bound(accessor, self.functions, f"{where} accessor")
            for symbol, function in cls.messages:
                self._bound(symbol, self.symbols, f"{where} message name")
                self._bound(function, self.functions, f"{where} message body")
            for _name, glob in cls.statics:
                self._bound(glob, range(self.nglobals), f"{where} static")
        for slot in self.slot_defaults:
            self._bound(slot, range(self.nglobals), "slot_defaults")
        for name, slot in self.exports.items():
            self._bound(slot, range(self.nglobals), f"export {name!r}")
        for name, slot in self.free.items():
            self._bound(slot, range(self.nglobals), f"free name {name!r}")

    def _bound(self, index, table, what):
        if not isinstance(index, int) or not 0 <= index < len(table):
            raise ImageError(
                f"{what}: index {index} is out of range ({len(table)} entries)"
            )

    # -- convenience -------------------------------------------------------

    def source_line(self, offset):
        """The source line for a code offset, or None.

        The table has one entry per *run* of instructions on a line, so the
        answer is the greatest key not exceeding `offset` (§8.9).
        """
        lines = self.debug.get("ln") or {}
        best = None
        for key, line in lines.items():
            at = int(key)
            if at <= offset and (best is None or at > best[0]):
                best = (at, line)
        return best[1] if best else None

    @property
    def source_file(self):
        return self.debug.get("f")

    def source_location(self, offset):
        """`file:line` for a code offset, or None when the image was stripped.

        The debug section is advisory and a VM must run identically without it
        (§8.9) - so nothing depends on this, and everything that reports a
        fault uses it when it is there. A trap that can say `errors.wy:14`
        instead of `word 63` is the difference between an answer and a
        mystery.
        """
        line = self.source_line(offset)
        if line is None:
            return None
        return f"{self.source_file or self.name}:{line}"

    def __repr__(self):
        return f"LoadedImage({self.name!r}, {len(self.code)} words)"


def load(blob: bytes) -> LoadedImage:
    return LoadedImage(blob)


def load_file(path: str) -> LoadedImage:
    with open(path, "rb") as handle:
        return LoadedImage(handle.read())


# --------------------------------------------------------------------------
# the container (spec 2)


def _split(blob: bytes) -> dict:
    if len(blob) < 8:
        raise ImageError("image is shorter than its own container header")
    if blob[:4] != MAGIC:
        raise ImageError(f"bad magic {blob[:4]!r}, expected {MAGIC!r}")
    if blob[4] != CONTAINER_VERSION:
        raise ImageError(
            f"container version {blob[4]}, expected {CONTAINER_VERSION}; this "
            "format is deliberately not forward-compatible"
        )
    count = blob[5]
    directory_end = 8 + 12 * count
    if directory_end > len(blob):
        raise ImageError(f"directory of {count} entries runs past the end of the image")

    sections = {}
    previous = None
    for i in range(count):
        section_id, _r1, _r2, offset, length = struct.unpack_from("<BBHII", blob, 8 + 12 * i)
        if section_id not in SECTION_NAMES:
            # Not skipped: an id this loader does not know may carry meaning
            # it would then silently ignore (§2 rule 5).
            raise ImageError(f"unknown section id {section_id}")
        if previous is not None and section_id <= previous:
            raise ImageError(
                f"directory is not in ascending id order: {section_id} after {previous}"
            )
        previous = section_id
        end = offset + length
        if offset < directory_end or end > len(blob):
            raise ImageError(
                f"section {SECTION_NAMES[section_id]} spans {offset}..{end}, "
                f"outside the {len(blob)}-byte image"
            )
        sections[section_id] = blob[offset:end]
    return sections


# --------------------------------------------------------------------------
# section records


def _require(document, key, kind, where):
    value = document.get(key)
    if not isinstance(value, kind):
        raise ImageError(f"{where}: key {key!r} must be {kind.__name__}, got {value!r}")
    return value


def _message(entry, index) -> Message:
    if not isinstance(entry, dict):
        raise ImageError(f"message {index} is not a document")
    return Message(path=tuple(entry.get("p", ())))


def _function(entry, index) -> Function:
    where = f"function {index}"
    if not isinstance(entry, dict):
        raise ImageError(f"{where} is not a document")
    params = [
        Param(_require(p, "n", str, f"{where} parameter"), p.get("d"))
        for p in entry.get("p", ())
    ]
    return Function(
        name=_require(entry, "n", str, where),
        params=params,
        nlocals=_require(entry, "l", int, where),
        code_offset=_require(entry, "c", int, where),
        flags=_require(entry, "f", int, where),
        dispatch=list(entry.get("t", ())),
        ncaptures=entry.get("k", 0),
        nresults=entry.get("r", 1),
    )


def _class(entry, index) -> Class:
    where = f"class {index}"
    if not isinstance(entry, dict):
        raise ImageError(f"{where} is not a document")
    slots = [
        Slot(
            _require(sl, "n", str, f"{where} slot"),
            sl.get("d"),
            sl.get("g"),
            sl.get("s"),
        )
        for sl in entry.get("sl", ())
    ]
    return Class(
        name=_require(entry, "n", str, where),
        superclass=entry.get("s"),
        slots=slots,
        init=entry.get("i"),
        messages=[(m["y"], m["f"]) for m in entry.get("m", ())],
        statics=[(st["n"], st["g"]) for st in entry.get("st", ())],
    )
