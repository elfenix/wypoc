"""The module image (spec 4) and its three containers (spec 5).

`ModuleImage` is the compiler's output object: pools, tables, and a code
array, built up through typed `add_*` calls that deduplicate and enforce the
spec 1.1 limits.  From one image come three interchangeable serializations,
which by construction carry identical section payloads:

* `to_wya()`  - the ASCII listing: human-readable ground truth, diffable in
                review, and assemblable again by `assemble_wya()`
* `to_wyc()`  - the binary container
* `to_c()`    - C arrays, for linking a module straight into a firmware build

The listing is the one a person reads, so it carries the disassembly and the
pool contents as comments; the assembler discards every one of them, which is
what keeps the round-trip honest.
"""

import struct
from dataclasses import dataclass, field

from . import bsonlite, opcodes
from .errors import CompileError

FORMAT_VERSION = 1  # header "v"
CONTAINER_VERSION = 1  # .wyc byte 4
WYC_MAGIC = b"WYC\x00"
WYA_MAGIC = "WYA 1"

# Section ids (spec 4).  Order here is the canonical directory order.
SECTION_IDS = {
    "header": 1,
    "statics": 2,
    "slot_defaults": 3,
    "symbols": 4,
    "functions": 5,
    "classes": 6,
    "messages": 7,
    "code": 8,
    "debug": 9,
    "exports": 10,
    "free": 11,
}
SECTION_NAMES = {value: name for name, value in SECTION_IDS.items()}

# The relocation kinds are retired. Every name is a global slot now, and the
# import instructions carry their own path and except-list as operands, so
# section 7 holds message identities alone - one kind, so no kind field
# (doc/addendum.md).

# Function flag bits (spec 4.6, key "f").
FN_COROUTINE = 1 << 0
FN_MESSAGE = 1 << 1
FN_VARARGS = 1 << 2
FN_KWARGS = 1 << 3

TABLE_LIMIT = 65535  # statics, symbols, relocs, functions, classes, globals

# A global with no constant default starts Unset and is absent from
# slot_defaults - there are no -1 sentinels anywhere in the format (D7).
NO_DEFAULT = object()

_BYTES_PER_LISTING_LINE = 16
_COMMENT_COLUMN = 54  # ';' lands here, per the appendix listing


@dataclass
class GlobalSlot:
    name: str  # for listings only; the image records just the count
    default: object = NO_DEFAULT
    # A slot holding a top-level block's own declaration is storage, not a
    # module member: it is not what `mod::name` should find, so it stays out
    # of the exports table (see add_shadow_global).
    exported: bool = True
    # A name this module *references* but does not define - `println`, or
    # `palette::mix`. It is a global slot like any other, so reading it is an
    # ordinary `gget`, but nothing in this module's own code ever writes it:
    # it is filled by whichever layer supplies the name (doc/addendum.md).
    # Not exported either - the module does not define it, so `mod::name` must
    # not answer with it.
    free: bool = False


@dataclass
class Param:
    name: str
    default: object = None  # static pool index, or None


@dataclass
class Function:
    name: str
    params: list = field(default_factory=list)
    nlocals: int = 0
    code_offset: int = 0  # in u32 words
    flags: int = 0
    dispatch: list = field(default_factory=list)  # reloc idxs, messages only
    ncaptures: int = 0
    nresults: int = 1
    uses: list = field(default_factory=list)  # reloc idxs resolved at first call


@dataclass
class Slot:
    name: str
    default: object = None  # static idx, or None
    getter: object = None  # fn idx, or None
    setter: object = None  # fn idx, or None


@dataclass
class Class:
    name: str
    superclass: object = None  # reloc idx, or None (= object)
    slots: list = field(default_factory=list)
    init: object = None  # fn idx, or None
    messages: list = field(default_factory=list)  # (symbol idx, fn idx)
    statics: list = field(default_factory=list)  # (name, global idx)


class ModuleImage:
    """One compiled module: pools, tables, code, and the three serializers."""

    def __init__(self, module_name: str):
        self.module_name = module_name
        self.statics = []
        self.symbols = []
        self.messages = []
        self.globals = []
        self.functions = []
        self.classes = []
        self.code = []  # u32 words
        self.init_nlocals = 0  # header "l": the init routine's L-frame size
        # The messages init sends. Compiler-side only - the verifier checks a
        # scope against what it reads - and never serialized: binding is not
        # batched, so an image has no use for the set (doc/addendum.md).
        self.module_uses = []
        self.debug = None  # {"f": file, "ln": {...}} or None
        # (function name, reason) for every body that would not lower and
        # became a trapping stub.  Not part of the image format - it is what
        # the compiler tells its caller about what it could not do.
        self.unlowered = []
        self._static_index = {}
        self._symbol_index = {}
        self._message_index = {}
        self._global_index = {}
        self._free_index = {}  # free name -> its slot (see add_free_global)

    # -- pools -----------------------------------------------------------

    def add_static(self, value) -> int:
        """Intern a constant into the static pool, returning its index.

        Dedup is type-aware: `1`, `1.0` and `True` are three different
        constants even though Python compares them equal.
        """
        key = (_static_kind(value), value)
        existing = self._static_index.get(key)
        if existing is not None:
            return existing
        index = _next_index(self.statics, "static pool entries")
        self.statics.append(value)
        self._static_index[key] = index
        return index

    def add_free_global(self, name: str) -> int:
        """Reserve a slot for a name this module references but never defines.

        Keyed by the name as written, so `palette::mix` and a bare `mix` are
        different slots - they are different names, and the second is only
        reachable through a wildcard while the first is not.

        Deliberately a separate index from `add_global`: a free name must not
        collide with a module member of the same name, and `_declare_module_names`
        has already claimed a slot for every name this module actually defines
        before any body is lowered. A free slot therefore means the compiler
        looked and found nothing, which is exactly when the name has to come
        from somewhere else.
        """
        existing = self._free_index.get(name)
        if existing is not None:
            return existing
        index = _next_index(self.globals, "module globals")
        self.globals.append(GlobalSlot(name, NO_DEFAULT, exported=False, free=True))
        self._free_index[name] = index
        return index

    def add_shadow_global(self, name: str) -> int:
        """Reserve a global slot that is *not* interned under `name`.

        A `var` inside a top-level `do:`/`if`/loop body needs storage, but it
        is not a module member: an outer name it shadows keeps the exported
        slot, and the block's own binding goes out of scope with the block.
        """
        index = _next_index(self.globals, "module globals")
        self.globals.append(GlobalSlot(name, NO_DEFAULT, exported=False))
        return index

    def add_symbol(self, name: str) -> int:
        existing = self._symbol_index.get(name)
        if existing is not None:
            return existing
        if len(name.encode("utf-8")) > 255:
            raise CompileError(f"symbol {name!r} exceeds the 255-byte symbol limit")
        index = _next_index(self.symbols, "symbols per module")
        self.symbols.append(name)
        self._symbol_index[name] = index
        return index

    def add_message(self, path) -> int:
        """Add (or reuse) a `messages` entry, returning its index.

        `path` is a list of name components (or a "::"-joined string); the
        entry stores them as symbol indices. Identical paths share one index -
        one message name is one identity, which is the whole point of the
        table.

        The last table with a runtime binding behind it. A message resolves in
        its own namespace, and an unqualified one *creates* the identity when
        nothing has it yet, so it is not a global slot and never was.
        """
        if isinstance(path, str):
            path = path.split("::")
        path = tuple(path)
        if not path:
            raise CompileError("a message needs at least one path component")
        existing = self._message_index.get(path)
        if existing is not None:
            return existing
        index = _next_index(self.messages, "messages per module")
        entry = {"p": [self.add_symbol(part) for part in path]}
        entry["_path"] = path  # listing only; stripped before encoding
        self.messages.append(entry)
        self._message_index[path] = index
        return index

    def add_global(self, name: str, default=NO_DEFAULT) -> int:
        """Reserve a module global slot.  Names are for listings; the image
        records only the slot count and the constant defaults."""
        existing = self._global_index.get(name)
        if existing is not None:
            if default is not NO_DEFAULT:
                self.globals[existing].default = default
            return existing
        index = _next_index(self.globals, "module globals")
        self.globals.append(GlobalSlot(name, default))
        self._global_index[name] = index
        return index

    def global_index(self, name: str) -> int:
        index = self._global_index.get(name)
        if index is None:
            raise CompileError(f"no module global named {name!r}")
        return index

    # -- tables ----------------------------------------------------------

    def add_function(
        self,
        name,
        params=(),
        nlocals=0,
        code_offset=0,
        flags=0,
        dispatch=(),
        ncaptures=0,
        nresults=1,
        uses=(),
    ) -> int:
        index = _next_index(self.functions, "functions per module")
        self.functions.append(
            Function(
                name=name,
                params=[_as_param(p) for p in params],
                nlocals=nlocals,
                code_offset=code_offset,
                flags=flags,
                dispatch=list(dispatch),
                ncaptures=ncaptures,
                nresults=nresults,
                uses=list(uses),
            )
        )
        return index

    def add_class(
        self, name, superclass=None, slots=(), init=None, messages=(), statics=()
    ) -> int:
        index = _next_index(self.classes, "classes per module")
        messages = list(messages)
        if len(messages) > 16:
            raise CompileError(
                f"class {name} has {len(messages)} messages, over the 16-entry message map limit"
            )
        self.classes.append(
            Class(
                name=name,
                superclass=superclass,
                slots=list(slots),
                init=init,
                messages=messages,
                statics=list(statics),
            )
        )
        return index

    # -- code ------------------------------------------------------------

    def here(self) -> int:
        """The word offset the next emitted instruction will land at."""
        return len(self.code)

    def emit(self, words) -> int:
        """Append encoded instruction words; returns the offset they start at."""
        offset = len(self.code)
        for word in words:
            if not 0 <= word <= 0xFFFFFFFF:
                raise CompileError(f"code word {word} is not a u32")
            self.code.append(word)
        return offset

    # -- section payloads -------------------------------------------------

    def sections(self) -> dict:
        """The emitted sections as `{id: payload bytes}`, in directory order.

        Empty optional sections are left out entirely rather than emitted as
        empty containers - a missing section reads as "nothing here" on the
        loader side just as a missing BSON key does.
        """
        out = {}
        out[SECTION_IDS["header"]] = bsonlite.encode_document(self._header_doc())
        if self.statics:
            out[SECTION_IDS["statics"]] = bsonlite.encode_array(self.statics)
        defaults = self._slot_defaults_doc()
        if defaults:
            out[SECTION_IDS["slot_defaults"]] = bsonlite.encode_document(defaults)
        if self.symbols:
            out[SECTION_IDS["symbols"]] = bsonlite.encode_array(self.symbols)
        if self.functions:
            out[SECTION_IDS["functions"]] = bsonlite.encode_array(self._function_docs())
        if self.classes:
            out[SECTION_IDS["classes"]] = bsonlite.encode_array(self._class_docs())
        if self.messages:
            out[SECTION_IDS["messages"]] = bsonlite.encode_array(self._message_docs())
        out[SECTION_IDS["code"]] = self.code_bytes()
        if self.debug:
            out[SECTION_IDS["debug"]] = bsonlite.encode_document(self.debug)
        exports = self._exports_doc()
        if exports:
            out[SECTION_IDS["exports"]] = bsonlite.encode_document(exports)
        free = self._free_doc()
        if free:
            out[SECTION_IDS["free"]] = bsonlite.encode_document(free)
        return dict(sorted(out.items()))

    def code_bytes(self) -> bytes:
        return struct.pack(f"<{len(self.code)}I", *self.code)

    def _header_doc(self) -> dict:
        return {
            "n": self.module_name,
            "v": FORMAT_VERSION,
            "g": len(self.globals),
            "l": self.init_nlocals,
        }

    def _exports_doc(self) -> dict:
        """`{ name: global slot }` for every module global.

        Without it a compiled module cannot answer a `::` lookup: resolving
        `geometry::UNITS` walks to the module object and then asks it for
        `UNITS` by name (spec 7.3), and the header records only how many
        global slots there are, not what they are called.
        """
        return {slot.name: index for index, slot in enumerate(self.globals)
                if slot.exported}

    def _free_doc(self) -> dict:
        """`{ name: global slot }` for every free name (§8.11).

        This is the fill list: at load the VM walks it against the builtins,
        and each import walks it again for whatever that import supplies. The
        name is the spelling as the source wrote it, `::` and all, because
        that is what has to be resolved.
        """
        return {slot.name: index for index, slot in enumerate(self.globals)
                if slot.free}

    def _slot_defaults_doc(self) -> dict:
        return {
            str(index): slot.default
            for index, slot in enumerate(self.globals)
            if slot.default is not NO_DEFAULT
        }

    def _function_docs(self) -> list:
        docs = []
        for fn in self.functions:
            doc = {"n": fn.name}
            params = []
            for param in fn.params:
                entry = {"n": param.name}
                if param.default is not None:
                    entry["d"] = param.default
                params.append(entry)
            doc["p"] = params
            doc["l"] = fn.nlocals
            doc["c"] = fn.code_offset
            doc["f"] = fn.flags
            if fn.dispatch:
                doc["t"] = list(fn.dispatch)
            if fn.ncaptures:
                doc["k"] = fn.ncaptures
            if fn.nresults != 1:
                doc["r"] = fn.nresults
            # No `u`: see ModuleImage.module_uses. `fn.uses` stays for the
            # verifier and stops at the image boundary.
            docs.append(doc)
        return docs

    def _class_docs(self) -> list:
        docs = []
        for cls in self.classes:
            doc = {"n": cls.name}
            if cls.superclass is not None:
                doc["s"] = cls.superclass
            slots = []
            for slot in cls.slots:
                entry = {"n": slot.name}
                if slot.default is not None:
                    entry["d"] = slot.default
                if slot.getter is not None:
                    entry["g"] = slot.getter
                if slot.setter is not None:
                    entry["s"] = slot.setter
                slots.append(entry)
            doc["sl"] = slots
            if cls.init is not None:
                doc["i"] = cls.init
            doc["m"] = [{"y": sym, "f": fn} for sym, fn in cls.messages]
            doc["st"] = [{"n": name, "g": index} for name, index in cls.statics]
            docs.append(doc)
        return docs

    def _message_docs(self) -> list:
        return [
            {key: value for key, value in entry.items() if not key.startswith("_")}
            for entry in self.messages
        ]

    # -- listing annotations ----------------------------------------------

    def describe(self, kind: str, index: int):
        """A short note on what a pool index holds, for listing comments."""
        try:
            if kind == "static":
                return _static_repr(self.statics[index])
            if kind == "symbol":
                return f'"{self.symbols[index]}"'
            if kind == "message":
                return "::".join(self.messages[index]["_path"])
            if kind == "global":
                return self.globals[index].name
        except (IndexError, KeyError):
            return None
        return None

    # -- containers --------------------------------------------------------

    def to_wyc(self) -> bytes:
        """The binary container (spec 5.3)."""
        sections = self.sections()
        header = bytearray(WYC_MAGIC)
        header.append(CONTAINER_VERSION)
        header.append(len(sections))
        header += struct.pack("<H", 0)
        directory_end = len(header) + 12 * len(sections)
        payloads = bytearray()
        directory = bytearray()
        for section_id, payload in sections.items():
            offset = directory_end + len(payloads)
            directory += struct.pack("<BBHII", section_id, 0, 0, offset, len(payload))
            payloads += payload
            while len(payloads) % 4:
                payloads.append(0)
        return bytes(header + directory + payloads)

    def to_wya(self) -> str:
        """The ASCII listing (spec 5.1)."""
        lines = [f"{WYA_MAGIC} {self.module_name}", ""]
        for section_id, payload in self.sections().items():
            name = SECTION_NAMES[section_id]
            lines.append(f"SECTION {name}")
            if name == "code":
                lines.extend(self._code_listing())
            else:
                lines.extend(_hex_listing(payload, self._section_annotations(name)))
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"

    def to_c(self, header_include="wyrm/image.h") -> str:
        """The C-array container (spec 5.2)."""
        symbol = self.module_name.replace("::", "__")
        if not symbol.isidentifier():
            raise CompileError(
                f"module name {self.module_name!r} does not encode to a valid C identifier"
            )
        out = [
            f"/* Generated from {self.module_name} by wypoc's bytecode compiler. */",
            "",
            "#include <stdint.h>",
            f'#include "{header_include}"',
            "",
        ]
        sections = self.sections()
        for section_id, payload in sections.items():
            name = SECTION_NAMES[section_id]
            if name == "code":
                continue
            out.append(f"static const uint8_t {symbol}_{name}[] = {{")
            for start in range(0, len(payload), 12):
                chunk = payload[start : start + 12]
                out.append("    " + " ".join(f"0x{byte:02X}," for byte in chunk))
            out.append("};")
            out.append("")
        out.append(f"static const uint32_t {symbol}_code[] = {{")
        for offset, text, nwords in opcodes.disassemble(self.code, self):
            words = self.code[offset : offset + nwords]
            body = " ".join(f"0x{word:08X}," for word in words)
            out.append(f"    {body:<26} /* {offset:04X}  {text} */")
        out.append("};")
        out.append("")
        out.append(f"const wy_module_image {symbol}_image = {{")
        out.append(f'    .name = "{self.module_name}",')
        out.append("    .sections = {")
        for section_id in sections:
            name = SECTION_NAMES[section_id]
            macro = f"WY_SEC_{name.upper()}"
            if name == "code":
                out.append(
                    f"        [{macro}] = {{ (const uint8_t*) {symbol}_code, sizeof {symbol}_code }},"
                )
            else:
                out.append(
                    f"        [{macro}] = {{ {symbol}_{name}, sizeof {symbol}_{name} }},"
                )
        out.append("    },")
        out.append("};")
        out.append("")
        return "\n".join(out)

    # -- listing helpers ---------------------------------------------------

    def _code_listing(self):
        """One instruction per line, with the disassembly as its comment, a
        comment-only banner where each function body starts, and the source
        line interleaved as a comment wherever the debug table records one
        (spec 5.1).  An assembler discards all of it."""
        starts = {0: "module init"}
        stubbed = dict(self.unlowered)
        for fn in self.functions:
            banner = f"fn {fn.name} (word offset {fn.code_offset})"
            if fn.name in stubbed:
                banner += f" - STUB: {stubbed[fn.name]}"
            starts[fn.code_offset] = banner
        source_lines = {
            int(offset): line
            for offset, line in (self.debug or {}).get("ln", {}).items()
        }
        lines = []
        for offset, text, nwords in opcodes.disassemble(self.code, self):
            banner = starts.get(offset)
            if banner:
                lines.append(f"; {banner}")
            source_line = source_lines.get(offset)
            if source_line is not None:
                name = self.debug.get("f")
                where = f"{name}:{source_line}" if name else f"line {source_line}"
                lines.append(f";   {where}")
            payload = struct.pack(f"<{nwords}I", *self.code[offset : offset + nwords])
            data = f"{offset * 4:04X}: " + " ".join(f"{byte:02X}" for byte in payload)
            lines.append(f"{data:<{_COMMENT_COLUMN}}; {text}")
        return lines

    def _section_annotations(self, name):
        """`{byte offset: comment}` for the entry beginning at that offset."""
        if name == "statics":
            return _array_annotations(
                self.statics,
                lambda i, v: f"[{i}] {_static_kind(v)} {_static_repr(v)}",
            )
        if name == "symbols":
            return _array_annotations(self.symbols, lambda i, v: f'[{i}] "{v}"')
        if name == "functions":
            return _array_annotations(self._function_docs(), _function_note)
        if name == "classes":
            return _array_annotations(
                self._class_docs(), lambda i, v: f"[{i}] class {v['n']}"
            )
        if name == "messages":
            return _array_annotations(self._message_docs(), self._message_note)
        if name == "header":
            return _document_annotations(self._header_doc())
        if name == "slot_defaults":
            return _document_annotations(self._slot_defaults_doc())
        if name == "exports":
            return _document_annotations(self._exports_doc())
        if name == "free":
            return _document_annotations(self._free_doc())
        if name == "debug":
            return _document_annotations(self.debug or {})
        return {}

    def _message_note(self, index, _doc):
        return f'[{index}] {"::".join(self.messages[index]["_path"])}' 


# --------------------------------------------------------------------------
# the .wy_a assembler (spec 5.1)


def assemble_wya(text: str) -> bytes:
    """Assemble a `.wy_a` listing back into `.wyc` bytes.

    The whole job: check the magic, collect hex bytes per section, verify each
    data line's address against how many bytes that section has collected so
    far (the guard against a hand-edit slip), and wrap the result.
    """
    lines = text.splitlines()
    cursor = 0
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines):
        raise CompileError("empty .wy_a input")
    magic = lines[cursor].strip()
    if not magic.startswith(WYA_MAGIC):
        raise CompileError(f"bad .wy_a magic line: {magic!r}")
    module_name = magic[len(WYA_MAGIC) :].strip()

    payloads = {}
    current = None
    for number, raw in enumerate(lines[cursor + 1 :], start=cursor + 2):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("SECTION "):
            name = line[len("SECTION ") :].strip()
            if name not in SECTION_IDS:
                raise CompileError(f"line {number}: unknown section {name!r}")
            current = payloads.setdefault(SECTION_IDS[name], bytearray())
            continue
        if current is None:
            raise CompileError(f"line {number}: data before any SECTION line")
        address, _, rest = line.partition(":")
        if not rest:
            raise CompileError(f"line {number}: expected 'addr: bytes'")
        try:
            expected = int(address, 16)
        except ValueError:
            raise CompileError(f"line {number}: bad address {address!r}") from None
        if expected != len(current):
            raise CompileError(
                f"line {number}: address {expected:04X} does not match the "
                f"{len(current):04X} bytes collected so far in this section"
            )
        data, _, _comment = rest.partition(";")
        for token in data.split():
            if len(token) != 2:
                raise CompileError(f"line {number}: {token!r} is not a hex byte")
            try:
                current.append(int(token, 16))
            except ValueError:
                raise CompileError(f"line {number}: {token!r} is not a hex byte") from None

    return _wrap_wyc({key: bytes(value) for key, value in sorted(payloads.items())})


def _wrap_wyc(sections: dict) -> bytes:
    header = bytearray(WYC_MAGIC)
    header.append(CONTAINER_VERSION)
    header.append(len(sections))
    header += struct.pack("<H", 0)
    directory_end = len(header) + 12 * len(sections)
    directory = bytearray()
    payloads = bytearray()
    for section_id, payload in sections.items():
        offset = directory_end + len(payloads)
        directory += struct.pack("<BBHII", section_id, 0, 0, offset, len(payload))
        payloads += payload
        while len(payloads) % 4:
            payloads.append(0)
    return bytes(header + directory + payloads)


def read_wyc(data: bytes) -> dict:
    """Split a `.wyc` image back into `{section id: payload}` (tests, tools)."""
    if data[:4] != WYC_MAGIC:
        raise CompileError("not a .wyc image")
    if data[4] != CONTAINER_VERSION:
        raise CompileError(f"unsupported .wyc container version {data[4]}")
    count = data[5]
    sections = {}
    for i in range(count):
        section_id, _r1, _r2, offset, length = struct.unpack_from("<BBHII", data, 8 + 12 * i)
        sections[section_id] = data[offset : offset + length]
    return sections


# --------------------------------------------------------------------------
# small helpers


def _next_index(pool, limit_name):
    if len(pool) >= TABLE_LIMIT:
        raise CompileError(f"module exceeds the limit of {TABLE_LIMIT} {limit_name}")
    return len(pool)


def _as_param(param):
    if isinstance(param, Param):
        return param
    if isinstance(param, str):
        return Param(param)
    name, default = param
    return Param(name, default)


def _static_kind(value):
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "f64"
    if isinstance(value, str):
        return "str"
    if isinstance(value, (bytes, bytearray)):
        return "bin"
    raise CompileError(f"{type(value).__name__} is not a static pool constant")


def _static_repr(value):
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return '"' + escaped + '"'
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray)):
        return f"{len(value)} bytes"
    return str(value)


def _function_note(index, doc):
    params = ", ".join(p["n"] for p in doc["p"])
    return f"[{index}] {doc['n']}  p:[{params}]  l:{doc['l']}  c:{doc['c']}  f:{doc['f']}"


def _array_annotations(items, note):
    """Byte offset of each array element within its encoded BSON array."""
    annotations = {}
    offset = 4  # past the int32 total length
    for index, item in enumerate(items):
        annotations[offset] = note(index, item)
        offset += len(bsonlite._element(str(index), item))
    return annotations


def _document_annotations(document):
    annotations = {}
    offset = 4
    for key, value in document.items():
        annotations[offset] = f"{key}: {_doc_value_repr(value)}"
        offset += len(bsonlite._element(str(key), value))
    return annotations


def _doc_value_repr(value):
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return _static_repr(value)


def _hex_listing(payload, annotations):
    lines = []
    for start in range(0, len(payload), _BYTES_PER_LISTING_LINE):
        chunk = payload[start : start + _BYTES_PER_LISTING_LINE]
        data = f"{start:04X}: " + " ".join(f"{byte:02X}" for byte in chunk)
        # Every entry that begins on this line gets its comment shown - two
        # short BSON elements (e.g. header's `v` and `g`) can easily share a
        # 16-byte row, and a silently dropped one is a real field a reader
        # would never see.
        comment = "; ".join(
            annotations[offset]
            for offset in sorted(annotations)
            if start <= offset < start + _BYTES_PER_LISTING_LINE
        )
        lines.append(f"{data:<{_COMMENT_COLUMN}}; {comment}" if comment else data)
    return lines
