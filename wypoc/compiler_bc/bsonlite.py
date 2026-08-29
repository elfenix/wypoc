"""The pinned BSON subset used by the structured module sections (spec 4.1).

Standard BSON framing - int32 total length, elements, trailing 0x00 - with
exactly eight element types permitted:

    0x01 double | 0x02 string | 0x03 document | 0x04 array
    0x05 binary (subtype 0) | 0x08 bool | 0x0A null | 0x10 int32

Anything else is refused in both directions, so a loader on the C side can
be a ~200-line reader with no schema negotiation.  This module is the whole
dependency: the compiler ships its own encoder rather than pulling in a BSON
library, because it must control exactly which types can appear.

Python values map as: dict -> document, list/tuple -> array (keys "0", "1",
...), str -> string, bool -> bool, int -> int32, float -> double,
bytes -> binary, None -> null.  `bool` is checked before `int` because it is
an `int` subclass in Python.
"""

import struct

from .errors import CompileError

TAG_DOUBLE = 0x01
TAG_STRING = 0x02
TAG_DOCUMENT = 0x03
TAG_ARRAY = 0x04
TAG_BINARY = 0x05
TAG_BOOL = 0x08
TAG_NULL = 0x0A
TAG_INT32 = 0x10

PERMITTED_TAGS = frozenset(
    {
        TAG_DOUBLE,
        TAG_STRING,
        TAG_DOCUMENT,
        TAG_ARRAY,
        TAG_BINARY,
        TAG_BOOL,
        TAG_NULL,
        TAG_INT32,
    }
)

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1


# --------------------------------------------------------------------------
# encoding


def encode_document(mapping) -> bytes:
    """Encode a dict as a BSON document.  Key order is the dict's own order."""
    return _frame((_element(str(key), value) for key, value in mapping.items()))


def encode_array(items) -> bytes:
    """Encode a sequence as a BSON array (keys are the decimal indices)."""
    return _frame(_element(str(i), value) for i, value in enumerate(items))


def encode(value) -> bytes:
    """Encode a dict as a document or a list/tuple as an array."""
    if isinstance(value, dict):
        return encode_document(value)
    if isinstance(value, (list, tuple)):
        return encode_array(value)
    raise CompileError(
        f"bsonlite: a section payload must be a document or an array, not {type(value).__name__}"
    )


def _frame(elements) -> bytes:
    body = b"".join(elements)
    return struct.pack("<i", len(body) + 5) + body + b"\x00"


def _element(key: str, value) -> bytes:
    tag, payload = _value(value)
    return bytes([tag]) + _cstring(key) + payload


def _value(value):
    # bool before int: True is an int in Python, and a bool must not silently
    # widen into an int32 element.
    if value is None:
        return TAG_NULL, b""
    if isinstance(value, bool):
        return TAG_BOOL, b"\x01" if value else b"\x00"
    if isinstance(value, int):
        if not INT32_MIN <= value <= INT32_MAX:
            raise CompileError(
                f"bsonlite: integer {value} does not fit in an int32 element"
            )
        return TAG_INT32, struct.pack("<i", value)
    if isinstance(value, float):
        return TAG_DOUBLE, struct.pack("<d", value)
    if isinstance(value, str):
        encoded = value.encode("utf-8") + b"\x00"
        return TAG_STRING, struct.pack("<i", len(encoded)) + encoded
    if isinstance(value, (bytes, bytearray)):
        return TAG_BINARY, struct.pack("<i", len(value)) + b"\x00" + bytes(value)
    if isinstance(value, dict):
        return TAG_DOCUMENT, encode_document(value)
    if isinstance(value, (list, tuple)):
        return TAG_ARRAY, encode_array(value)
    raise CompileError(
        f"bsonlite: {type(value).__name__} has no representation in the pinned BSON subset"
    )


def _cstring(text: str) -> bytes:
    encoded = text.encode("utf-8")
    if b"\x00" in encoded:
        raise CompileError("bsonlite: a key may not contain a NUL byte")
    return encoded + b"\x00"


# --------------------------------------------------------------------------
# decoding
#
# Decoding exists for the tests, the .wy_a disassembler, and anything that
# reads an image back; it is as strict as the encoder.


def decode_document(data: bytes) -> dict:
    value, end = _read_document(data, 0)
    if end != len(data):
        raise CompileError(
            f"bsonlite: {len(data) - end} trailing byte(s) after the document"
        )
    return value


def decode_array(data: bytes) -> list:
    """Decode a document whose keys must be "0", "1", ... into a list."""
    document = decode_document(data)
    for position, key in enumerate(document):
        if key != str(position):
            raise CompileError(
                f"bsonlite: array element {position} has key {key!r}, expected {str(position)!r}"
            )
    return list(document.values())


def decode(data: bytes):
    """Decode as an array when the keys are dense decimal indices, else as a document."""
    document = decode_document(data)
    if all(key == str(i) for i, key in enumerate(document)):
        return list(document.values())
    return document


def _read_document(data: bytes, offset: int):
    if offset + 4 > len(data):
        raise CompileError("bsonlite: truncated document length")
    (total,) = struct.unpack_from("<i", data, offset)
    end = offset + total
    if total < 5 or end > len(data):
        raise CompileError(f"bsonlite: bad document length {total}")
    if data[end - 1] != 0x00:
        raise CompileError("bsonlite: document is not NUL-terminated")
    result = {}
    cursor = offset + 4
    while cursor < end - 1:
        tag = data[cursor]
        cursor += 1
        key, cursor = _read_cstring(data, cursor)
        value, cursor = _read_value(data, cursor, tag)
        result[key] = value
    if cursor != end - 1:
        raise CompileError("bsonlite: element list overruns the document")
    return result, end


def _read_value(data: bytes, offset: int, tag: int):
    if tag not in PERMITTED_TAGS:
        raise CompileError(f"bsonlite: element type 0x{tag:02X} is outside the subset")
    if tag == TAG_NULL:
        return None, offset
    if tag == TAG_BOOL:
        byte = data[offset]
        if byte not in (0, 1):
            raise CompileError(f"bsonlite: bool byte 0x{byte:02X} is neither 0 nor 1")
        return byte == 1, offset + 1
    if tag == TAG_INT32:
        (value,) = struct.unpack_from("<i", data, offset)
        return value, offset + 4
    if tag == TAG_DOUBLE:
        (value,) = struct.unpack_from("<d", data, offset)
        return value, offset + 8
    if tag == TAG_STRING:
        (length,) = struct.unpack_from("<i", data, offset)
        offset += 4
        if length < 1 or offset + length > len(data):
            raise CompileError(f"bsonlite: bad string length {length}")
        if data[offset + length - 1] != 0x00:
            raise CompileError("bsonlite: string is not NUL-terminated")
        return data[offset : offset + length - 1].decode("utf-8"), offset + length
    if tag == TAG_BINARY:
        (length,) = struct.unpack_from("<i", data, offset)
        subtype = data[offset + 4]
        if subtype != 0x00:
            raise CompileError(f"bsonlite: binary subtype 0x{subtype:02X} is not 0")
        start = offset + 5
        if length < 0 or start + length > len(data):
            raise CompileError(f"bsonlite: bad binary length {length}")
        return data[start : start + length], start + length
    # document / array: both decode as documents; an array is recognized by
    # its keys, exactly as decode() does at the top level.
    value, end = _read_document(data, offset)
    if tag == TAG_ARRAY:
        return list(value.values()), end
    return value, end


def _read_cstring(data: bytes, offset: int):
    end = data.find(b"\x00", offset)
    if end < 0:
        raise CompileError("bsonlite: unterminated key")
    return data[offset:end].decode("utf-8"), end + 1
