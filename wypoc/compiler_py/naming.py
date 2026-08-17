"""Identifier translation from wyrm names to Python names.

wyrm identifiers (see wyrm_tokenizer.py) start with `isalpha()`/`_` and
continue with `isalnum()`/`_`/`$` - `$` is the *only* wyrm-legal character
that isn't legal in a Python identifier, so it is the only character this
module needs an escape for.

Every wyrm-level user identifier - function/coroutine/class names, params,
locals, slot names - is translated with `py_ident`, which does two things
at once: a blanket `wy_` prefix (so a generated name can never collide with
a Python keyword - `class`, `return`, `yield`, `is`, `with`, ... are all
valid wyrm identifiers - or with one of this compiler's own helper names
like `ctx`/`machine`/`dispatch`), and a reversible escape of `$`.

The `$` escape uses the literal substring `DWY` as a marker. Since `DWY`
could itself appear in a wyrm name, an original `DWY` is first escaped to
`DWY_0_` - this must happen *before* `$` is turned into `DWY`, so that the
only bare `DWY` substrings in the final string are ones this pass itself
inserted for a `$`.

    reg$0   -> wy_regDWY0        (one $)
    foo     -> wy_foo            (blanket prefix, nothing to escape)
    xDWYy   -> wy_xDWY_0_y       (literal DWY escaped first)
    a$DWYb  -> wy_aDWYDWY_0_b    (both rules applied, still unambiguous)
"""

WY_PREFIX = "wy_"
_DWY_MARKER = "DWY"
_DWY_ESCAPED = "DWY_0_"


def escape(name: str) -> str:
    """Just the reversible $/DWY escaping, with no `wy_` prefix - used to
    build compiler-internal names (message-dispatch function names,
    dataclass "fields" type names) out of one or more wyrm identifiers
    without every part carrying its own redundant `wy_`."""
    escaped = name.replace(_DWY_MARKER, _DWY_ESCAPED)
    return escaped.replace("$", _DWY_MARKER)


def py_ident(name: str) -> str:
    return f"{WY_PREFIX}{escape(name)}"


def fields_class_name(class_name: str) -> str:
    """The internal @dataclass type name for a compiled wyrm class - never
    referenced by user code (which calls the constructor function bound to
    py_ident(class_name) instead), only used for isinstance/TypeCheck and
    dispatch-signature purposes."""
    return f"_wy_{escape(class_name)}_fields"


def wildcard_wrapper_name(msg_name: str) -> str:
    """A promoted plain `fn name(...)`'s registered overload can't be the
    plain function itself - dispatch() always calls every registered
    overload as `(this, *args, **kwargs)`, but a plain fn's own Python
    signature has no `this` parameter (the interpreter just binds `this`
    into scope regardless of whether a plain fn's body references it,
    which Python's static parameter lists can't do) - so promotion
    registers this small `this`-discarding adapter instead."""
    return f"_wy_wildcard_{escape(msg_name)}"


def message_fn_name(msg_name: str, class_targets) -> str:
    """The standalone Python function implementing one message overload -
    `fn [Circle] describe()` -> `_wy_msg_describe__on_Circle`, `fn [Circle,
    Square] collide()` -> `_wy_msg_collide__on_Circle_Square`."""
    suffix = "_".join(escape(c) for c in class_targets)
    return f"_wy_msg_{escape(msg_name)}__on_{suffix}"


def module_dotted(path_segments) -> str:
    """`["std", "io"]` -> `"wyrm.std.io"` - the dotted Python import path
    for a wyrm module mirrored into the output tree's `wyrm` namespace
    package. Path segments are used as-is: wyrm_modules.resolve_module_file
    already guarantees they're filesystem-safe, and none of the reserved
    Python-keyword concerns that motivate py_ident's blanket prefixing
    apply to package/module *file* names the way they do to identifiers
    referenced from inside a module body."""
    return ".".join(("wyrm", *path_segments))


def module_path_tuple(path_segments) -> tuple:
    """`["std", "io"]` -> `("wyrm", "std", "io")` - the output tree's
    on-disk directory/file path for a mirrored module (join with os.sep and
    append ".py" to the last segment to get the real file path)."""
    return ("wyrm", *path_segments)
