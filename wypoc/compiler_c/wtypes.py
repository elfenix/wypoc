"""Wyrm primitive-type and operator tables shared by every compiler_c
submodule that needs to know what `--compile` actually supports.

A wyrm value crossing the interpreter boundary is a `wyrm_value` - a type tag
plus a register-sized payload. Inside a compiled function body it is an
ordinary C scalar instead, so arithmetic compiles to arithmetic; the table
below is the mapping between the two, and boxing happens only at the
boundaries (parameters in, arguments out, the result).
"""
from .errors import err


class WType:
    """One supported wyrm primitive type: how it is spelled as a C scalar,
    which `wyrm_value` tag and payload field carry it, the constructor that
    boxes it, and its zero value."""

    __slots__ = ("name", "ctype", "tag", "field", "box", "zero")

    def __init__(self, name, ctype, tag, field, box, zero):
        self.name = name
        self.ctype = ctype
        self.tag = tag
        self.field = field
        self.box = box
        self.zero = zero

    def boxed(self, expr: str) -> str:
        """`expr` (a C expression of this type's C scalar type) as a
        `wyrm_value`."""
        return f"{self.box}(({self.ctype})({expr}))"

    def unboxed(self, value_expr: str) -> str:
        """A `wyrm_value` C expression read back as this type's C scalar."""
        return f"(({self.ctype})({value_expr}).data.{self.field})"


TYPES = {
    t.name: t for t in (
        WType("int", "wyrm_word", "WYRM_TYPE_TAG_WORD", "word", "lang_value_int", "0"),
        WType("uint", "wyrm_uword", "WYRM_TYPE_TAG_WORD", "uword", "lang_value_int", "0"),
        WType("bool", "bool", "WYRM_TYPE_TAG_BOOL", "flag", "lang_value_bool", "false"),
        # `float` is representable now: the interpreter this targets has a
        # dedicated float tag and payload field, which is what the previous
        # backend was missing rather than anything about the compiler.
        WType("float", "wyrm_float", "WYRM_TYPE_TAG_FLOAT", "fp", "lang_value_float", "0.0"),
    )
}

# wyrm operator -> C operator. `and`/`or` are here for the ordinary case;
# expressions.py lowers them to a temporary when the right operand needs
# statements emitted before it, since C's `&&`/`||` short-circuit and hoisted
# statements would not.
BINOPS = {
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
    "&": "&", "|": "|", "^": "^",
    "<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "==", "!=": "!=",
    "and": "&&", "or": "||",
}

# Operators that make no sense on a float in C (`%` and the bitwise set are
# integer-only), checked so the generated C can't fail to compile.
INTEGER_ONLY_BINOPS = frozenset({"%", "&", "|", "^"})


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


def wtype(type_expr, what) -> WType:
    """The WType a declared type annotation names. `--compile` does no type
    inference, so an annotation is the only thing that can give a local a
    concrete representation - hence the demand for one, and the specific
    error naming what was left untyped."""
    if type_expr is None:
        err(f"{what} needs an explicit type for --compile")
    if len(type_expr.parts) != 1 or type_expr.parts[0] not in TYPES:
        name = "::".join(type_expr.parts)
        err(
            f"{what} has unsupported type {name!r}; --compile supports "
            f"{', '.join(repr(n) for n in TYPES)}"
        )
    return TYPES[type_expr.parts[0]]
