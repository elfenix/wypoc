"""Primitive-type values and core builtins exposed to wyrm code.

Primitive types (str, int, float, bool) are each a PrimitiveType: a
first-class value that names the type (so it can be referenced, compared,
passed around) and also acts as a cast when called, e.g. `str(4)` -> "4".
Casting is handled explicitly by call_value (see wyrm_eval_parse_tree.py)
rather than by making PrimitiveType a plain Python callable, since a cast is
conceptually different from invoking a user-defined function or message - it
always takes exactly one value and never involves param binding, defaults,
*args/**kwargs, etc.

cons/car/cdr are plain Python callables (ordinary wyrm functions, no special
interpreter support needed) implementing Scheme's cons-cell trio - see Pair
below.
"""


class PrimitiveType:
    """A wyrm primitive type, usable as a value. Calling it (`str(4)`) casts
    its single argument to this type via `cast`."""

    def __init__(self, name: str, cast):
        self.name = name
        self.cast = cast

    def __repr__(self):
        return f"PrimitiveType({self.name!r})"


def _to_str(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _to_int(value) -> int:
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def _to_float(value) -> float:
    return float(value)


def _to_bool(value) -> bool:
    return bool(value)


STR = PrimitiveType("str", _to_str)
INT = PrimitiveType("int", _to_int)
FLOAT = PrimitiveType("float", _to_float)
BOOL = PrimitiveType("bool", _to_bool)

PRIMITIVE_TYPES = {t.name: t for t in (STR, INT, FLOAT, BOOL)}


class WyrmError:
    """The internal tracked "error" type `error(what)` builds and `try`/
    `catch` (see wyrm.gram's Try/Catch nodes, handled in eval_expr) check
    for: `try EXPR` returns out of the enclosing function immediately if
    EXPR evaluates to one of these, and `EXPR catch HANDLER` substitutes
    HANDLER's value instead. Nothing but that error-ness check inspects
    it right now - `what` is carried along only for display/comparison."""

    def __init__(self, what: str):
        self.what = what

    def __repr__(self):
        return f"WyrmError({self.what!r})"

    def __eq__(self, other):
        return isinstance(other, WyrmError) and self.what == other.what

    def __hash__(self):
        return hash((WyrmError, self.what))


def error(what: str) -> WyrmError:
    """(error "message") -> a new WyrmError carrying that message - the
    only way wyrm code constructs one."""
    return WyrmError(what)


class Pair:
    """A Scheme-style mutable cons cell: `(car . cdr)`. Chaining pairs
    through `cdr` (NIL-terminated for a proper list, any other value for an
    improper one) is how Scheme builds lists out of cons - see
    doc/language-spec.md's "Pair / List" section and wyrm.gram's
    pair_literal/pair_body rules, which the `'(...)` syntax builds these
    from at eval time (see eval_expr's ast.Pair case)."""

    def __init__(self, car, cdr):
        self.car = car
        self.cdr = cdr

    def __repr__(self):
        return f"Pair({self.car!r}, {self.cdr!r})"

    def __eq__(self, other):
        return isinstance(other, Pair) and self.car == other.car and self.cdr == other.cdr

    def __contains__(self, item):
        # So `x in some_pair_chain` (wyrm.gram's comp_op 'in') walks the
        # list the same way len()/car/cdr already do, rather than raising -
        # Python has no default __contains__ for an arbitrary object.
        node = self
        while isinstance(node, Pair):
            if node.car == item:
                return True
            node = node.cdr
        return False


class _Nil:
    """The empty list, Scheme's `'()` - a singleton distinct from Pair, None,
    or any other value, so `x == nil` unambiguously means "end of (proper)
    list". Also what a bare `'()` literal and a cons-chain's implicit tail
    (no explicit `'.` improper-list tail) both evaluate to."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "'()"

    def __bool__(self):
        # Matches Scheme, not Lisp: the empty list is truthy: only #f/false is
        # falsy. Without this Python's default falls back to len(), and NIL
        # has neither, so it'd already be truthy - stated explicitly anyway
        # since it's easy to get backwards when porting Lisp intuitions.
        return True

    def __contains__(self, item):
        return False


NIL = _Nil()


def cons(a, b):
    """(cons a b) -> a new Pair whose car is `a` and cdr is `b`."""
    return Pair(a, b)


def car(x):
    """car, generalized past Scheme's cons-only version to anything
    list-adjacent that wypoc already has lying around: a Pair, or a Python
    str/list/tuple standing in for wyrm's string/array/tuple values."""
    if isinstance(x, Pair):
        return x.car
    if x is NIL:
        raise TypeError("car: cannot take car of '() (the empty list)")
    if isinstance(x, (str, list, tuple)):
        if not x:
            raise TypeError("car: empty list/array/string has no first element")
        return x[0]
    raise TypeError(f"car: not a pair/list/array/string (got {type(x).__name__})")


def cdr(x):
    """cdr, generalized the same way as car (see above)."""
    if isinstance(x, Pair):
        return x.cdr
    if x is NIL:
        raise TypeError("cdr: cannot take cdr of '() (the empty list)")
    if isinstance(x, (str, list, tuple)):
        if not x:
            raise TypeError("cdr: empty list/array/string has no rest")
        return x[1:]
    raise TypeError(f"cdr: not a pair/list/array/string (got {type(x).__name__})")


def copy(x):
    """(copy x) -> a new shallow copy of x: the immediate container/instance
    is new, but anything it holds (slot values, elements, entries) is shared
    with the original rather than itself copied. Immutable/singleton values
    (str/int/float/bool/nil) have no distinct "copy" to make, so they're
    returned as-is."""
    from wypoc.wyrm_eval_parse_tree import ClassInstance, Variable

    if isinstance(x, ClassInstance):
        new = ClassInstance(x.cls)
        new.attrs = {name: Variable(var.value) for name, var in x.attrs.items()}
        return new
    if isinstance(x, Pair):
        return Pair(x.car, x.cdr)
    if isinstance(x, list):
        return list(x)
    if isinstance(x, dict):
        return dict(x)
    if isinstance(x, tuple):
        return tuple(x)
    if isinstance(x, (str, int, float, bool)) or x is NIL:
        return x
    raise TypeError(f"copy: unsupported value type ({type(x).__name__})")


def length(x) -> int:
    """(len x) -> the number of elements in x, for every collection type
    wypoc currently has: str (chars), list (wyrm arrays), tuple, dict
    (entries), and Pair chains (walked car-by-car out to the terminating
    NIL - '() itself has length 0). An improper list (one whose final cdr
    isn't NIL) has no well-defined length, same as Scheme's own `length`."""
    if isinstance(x, (str, list, tuple, dict)):
        return len(x)
    if x is NIL:
        return 0
    if isinstance(x, Pair):
        n = 0
        node = x
        while isinstance(node, Pair):
            n += 1
            node = node.cdr
        if node is not NIL:
            raise TypeError("len: improper list has no well-defined length")
        return n
    raise TypeError(f"len: unsupported value type ({type(x).__name__})")


def substr(s, start: int, count: int) -> str:
    """(s ! substr(start, count)) -> the `count` characters of `s` starting
    at 0-based index `start`, e.g. "asdf" ! substr(1, 2) -> "sd". A
    message (`!`), not a plain call, since it's a method on string values -
    see register_native_method (wyrm_eval_parse_tree.py) for how a builtin
    like this becomes dispatchable that way without a real Class behind it.
    Uses plain Python slice semantics: a start/count that runs past the end
    just clips rather than raising."""
    if not isinstance(s, str):
        raise TypeError(f"substr: not a string (got {type(s).__name__})")
    return s[start:start + count]


def print_(*args) -> None:
    """(print a, b, c) -> writes each argument, space-separated, to stdout,
    stringified the same way str() would (e.g. bools as true/false) - with
    no trailing newline. See println (corelib/std/io.wy) for the
    newline-terminated, import-required form this complements; unlike
    println, print needs no `import std::io` since it's a builtin. Goes
    through wyrm_io's own write (not a raw Python print()) so it's still
    subject to the same __STDOUT handle - e.g. stdout-capturing tests that
    call wyrm_io._reset_std_handles() see print's output too."""
    from wypoc import wyrm_io

    wyrm_io.wyrm_write(wyrm_io.STDOUT, " ".join(_to_str(a) for a in args))


def install(ctx: dict) -> None:
    """Expose str/int/float/bool, cons/car/cdr, nil, copy, len, and print
    as wyrm-visible builtins, plus str's substr as a `!`-callable message."""
    from wypoc.wyrm_eval_parse_tree import expose_all, register_native_method

    expose_all(
        ctx, **PRIMITIVE_TYPES, cons=cons, car=car, cdr=cdr, nil=NIL,
        copy=copy, len=length, print=print_, error=error,
    )
    register_native_method("substr", substr, ctx)
