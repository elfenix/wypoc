"""Literal-token decoding, independent of the interpreter.

wyrm_eval_parse_tree.py has its own eval_string_literal/eval_number_literal/
eval_char_literal doing the identical job at eval time; this is a
deliberately separate, small copy rather than an import, since compiler_py
is meant to depend on the interpreter only through decorators_pass.py (which
reuses it as a sub-evaluator, not as a shared literal-decoding utility) -
see wypoc/compiler_py/__init__.py's module docstring.
"""

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0"}

CHAR_NAMES = {
    "newline": "\n",
    "space": " ",
    "tab": "\t",
    "return": "\r",
    "backspace": "\b",
    "formfeed": "\f",
    "null": "\0",
}


def _unescape(body: str) -> str:
    out = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            out.append(_ESCAPES.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def str_value(text: str) -> str:
    """A Str node's raw token text -> its decoded string value."""
    if text.startswith('"""'):
        return _unescape(text[3:-2])
    if text[:1] in ("R", "r") and '"' in text:
        open_paren = text.index("(")
        close_paren = text.rindex(")")
        return text[open_paren + 1:close_paren]
    return _unescape(text[1:-1])


def char_value(text: str) -> int:
    """A Char node's raw token text (`\\a`, `\\newline`, ...) -> its u32
    codepoint - same representation string-indexing produces in wyrm."""
    body = text[1:]
    if len(body) == 1:
        return ord(body)
    if body in CHAR_NAMES:
        return ord(CHAR_NAMES[body])
    raise ValueError(f"unknown character name {text!r}")


def num_value(text: str):
    lower = text.lower()
    if lower.startswith("0x"):
        return int(text, 16)
    if lower.startswith("0b"):
        return int(text, 2)
    if any(c in text for c in ".eE"):
        return float(text)
    return int(text)
