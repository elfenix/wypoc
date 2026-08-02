"""Wyrm primitive-type and operator tables shared by every compiler_c
submodule that needs to know what --compile v1 actually supports."""
from .errors import err

# wyrm primitive type name -> (C local type, wyrm_type_tag, wyrm_primitive field).
# float is deliberately absent: include/wyrm/types.h's wyrm_type_tag enum has no
# dedicated FLOAT tag yet, so there's no real encoding to target.
TYPES = {
    "int": ("wyrm_word", "WYRM_TYPE_TAG_WORD", "word"),
    "bool": ("bool", "WYRM_TYPE_TAG_WORD", "word"),
}

BINOPS = {
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
    "&": "&", "|": "|", "^": "^",
    "<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "==", "!=": "!=",
    "and": "&&", "or": "||",
}


def c_ident(name: str) -> str:
    """Sanitize an arbitrary module name into a valid C identifier fragment."""
    ident = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    if not ident or ident[0].isdigit():
        ident = f"m_{ident}"
    return ident


def is_float_literal(text: str) -> bool:
    if text.lower().startswith("0x"):
        return False
    return "." in text or "e" in text.lower()


def ctype(type_expr, what) -> str:
    if type_expr is None or len(type_expr.parts) != 1 or type_expr.parts[0] not in TYPES:
        name = "::".join(type_expr.parts) if type_expr else "<untyped>"
        err(f"{what} has unsupported type {name!r}; --compile v1 only supports 'int' and 'bool'")
    return type_expr.parts[0]
