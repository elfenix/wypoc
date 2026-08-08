"""Multi-line renderings of a value, for the REPL's answer line.

`wyrm_builtins.display` renders a value the way wyrm code spells it, on one
line, however long that line turns out to be - which is what you want inside
a container, in an error message, or from `str()`. It's not what you want
looking at a 40-slot object or a nested dict at a prompt, so this module
renders the three shapes that get unreadable when they're long:

    pair chain      lisp: `(1 2 3)`, parens rather than `$[1, 2, 3]`, with
                     an improper tail spelled `(1 2 . 3)`
    dict / array    JSON layout: one member per line, indented, closing
                     delimiter back at the opening line's indentation
    class instance  the shape of the class definition that would declare it:
                     `Point:` and then one `name: value` per slot

Everything else falls back to `display`.

"Smart" indentation means a value is only broken across lines when its
one-line form doesn't fit `width` at the column it starts at - so short
results still read as one line, and only what's genuinely big spreads out. A
class instance is the exception: it's always a block, because the point of
its rendering is to look like the definition.

The REPL uses this unless `:set compact` is on (see repl.py's OPTIONS), in
which case results go back to `display`'s one-liners.
"""
from wypoc import wyrm_builtins
from wypoc.wyrm_builtins import NIL, Pair
from wypoc.wyrm_eval_parse_tree import ClassInstance

# One level of nesting, matching the indentation wyrm's own sources use.
INDENT = "    "

DEFAULT_WIDTH = 80

# The characters a front end highlights in a rendered value (see repl.py's
# readline output and repl_tui.py's SessionLog): the structure, picked out
# from the data inside it. Kept here so both front ends agree on it.
DELIMITERS = r"[()\[\]{}]"


def pretty(value, width: int = DEFAULT_WIDTH) -> str:
    """`value` rendered across as many lines as it needs, and no more."""
    return _render(value, column=0, indent=0, width=width)


def _render(value, column: int, indent: int, width: int) -> str:
    """`value` starting at `column`, with any lines after the first indented
    from `indent` (the indentation of the line it starts on, which is not
    the same as where on that line it starts - a dict that begins after
    `key: ` still closes its brace under `key`)."""
    flat = _flat(value)
    if flat is not None and column + len(flat) <= width:
        return flat
    return _broken(value, indent, width)


def _flat(value) -> "str | None":
    """The one-line rendering, or None for a value that is always a block
    (a class instance, or anything containing one)."""
    if isinstance(value, ClassInstance):
        return None
    if isinstance(value, Pair):
        items, tail = _chain(value)
        parts = [_flat(item) for item in items]
        if tail is not None:
            parts += [".", _flat(tail)]
        if any(part is None for part in parts):
            return None
        return "(" + " ".join(parts) + ")"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            rendered = _flat(item)
            if rendered is None:
                return None
            parts.append(f"{wyrm_builtins.display(key)}: {rendered}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        parts = [_flat(item) for item in value]
        if any(part is None for part in parts):
            return None
        return "[" + ", ".join(parts) + "]"
    return wyrm_builtins.display(value)


def _broken(value, indent: int, width: int) -> str:
    if isinstance(value, ClassInstance):
        return _broken_instance(value, indent, width)
    if isinstance(value, Pair):
        return _broken_pair(value, indent, width)
    if isinstance(value, dict):
        return _broken_members(
            "{", "}", indent, width,
            [(f"{wyrm_builtins.display(key)}: ", item) for key, item in value.items()])
    if isinstance(value, list):
        return _broken_members("[", "]", indent, width,
                               [("", item) for item in value])
    # Nothing else has a broken form; a long string stays a long string.
    return wyrm_builtins.display(value)


def _broken_pair(value, indent: int, width: int) -> str:
    """Lisp layout: elements align under the first one, just inside the open
    paren, and the closing paren rides the last element's line.

        (1
         2
         3)
    """
    items, tail = _chain(value)
    column = indent + 1
    lines = [_render(item, column, column, width) for item in items]
    if tail is not None:
        lines.append(". " + _render(tail, column + 2, column, width))
    body = ("\n" + " " * column).join(lines)
    return f"({body})"


def _broken_members(opener: str, closer: str, indent: int, width: int,
                    members: list) -> str:
    """JSON layout: opener, one `prefix + value` per line indented a level
    in, closer back at the opening line's indentation."""
    if not members:
        return opener + closer
    inner = indent + len(INDENT)
    lines = []
    for prefix, item in members:
        rendered = _render(item, inner + len(prefix), inner, width)
        lines.append(" " * inner + prefix + rendered)
    return opener + "\n" + ",\n".join(lines) + "\n" + " " * indent + closer


def _broken_instance(value, indent: int, width: int) -> str:
    """The class-definition shape:

        Point:
            x: 1.0
            y: 2.0

    A slot holding another instance keeps its header on the slot's own line
    (`start: Point:`) and indents that one's slots a level further, which is
    how the same nesting would be written as source."""
    inner = indent + len(INDENT)
    lines = [f"{value.cls.name}:"]
    for name, cell in value.attrs.items():
        prefix = f"{name}: "
        rendered = _render(cell.value, inner + len(prefix), inner, width)
        lines.append(" " * inner + prefix + rendered)
    return "\n".join(lines)


def _chain(value: Pair) -> tuple:
    """A cons chain as (elements, improper tail) - the tail is None for a
    proper, nil-terminated list."""
    items = []
    node = value
    while isinstance(node, Pair):
        items.append(node.car)
        node = node.cdr
    tail = None if node is NIL or node is None else node
    return items, tail
