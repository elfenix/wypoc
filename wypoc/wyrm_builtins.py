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


class Symbol:
    """`'name` - a symbol: an interned name that is its own value type,
    distinct from the string of the same characters (`'a != "a"`).

    Symbols used to evaluate to plain Python strings here, which made the
    canonical s-expression format (doc/sexpr-spec.md) unrepresentable: a
    node's head kind, an operator, and a `'str` node's text would all have
    been the same kind of value, so `$['str, "int"]` and `$['str, 'int]`
    could not be told apart on the way back in. Symbols are interned, so
    identity comparison works too, but `__eq__`/`__hash__` are defined on
    the name so a hand-built Symbol compares equal to the interned one."""

    __slots__ = ("name",)
    _interned: dict = {}

    def __new__(cls, name: str):
        existing = cls._interned.get(name)
        if existing is not None:
            return existing
        self = super().__new__(cls)
        object.__setattr__(self, "name", name)
        cls._interned[name] = self
        return self

    def __repr__(self):
        return f"'{self.name}"

    def __eq__(self, other):
        return isinstance(other, Symbol) and self.name == other.name

    def __hash__(self):
        return hash((Symbol, self.name))


class _EllipsisType:
    """`...` - the placeholder value. Ordinary enough to store and pass
    around, which is what lets a template's body carry one (see the
    Decorators section of doc/language-spec.md): a template is a real
    function, so its hole has to be real syntax."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "..."


ELLIPSIS = _EllipsisType()


class PrimitiveType:
    """A wyrm primitive type, usable as a value. Calling it (`str(4)`) casts
    its single argument to this type via `cast`."""

    def __init__(self, name: str, cast):
        self.name = name
        self.cast = cast

    def __repr__(self):
        return f"PrimitiveType({self.name!r})"


def _to_str(value, ctx: dict | None = None) -> str:
    """`str(value)` - the "bare" rendering, where a str/symbol contributes
    its own characters and nothing else. Containers render their elements
    in *repr* mode instead (see _repr_str), so `str("hi")` is `hi` while
    `str(["hi"])` is `['hi']`, matching the reference implementation's
    single `append_value(sb, v, repr)` with its one repr flag.

    `ctx` is the calling scope's message table, threaded down so a class
    instance nested inside a container can still answer its own `__str__`
    (see _format) - the same hook str_value applies at the top level, just
    passed along instead of asked for again."""
    return _format(value, repr_mode=False, ctx=ctx)


def _repr_str(value, ctx: dict | None = None) -> str:
    """The rendering a value gets when it appears *inside* a container:
    strings quoted, symbols quote-prefixed, everything else as in _to_str."""
    return _format(value, repr_mode=True, ctx=ctx)


def display(value, ctx: dict | None = None) -> str:
    """How a value is echoed back when it's the value of something the user
    asked for directly - the REPL's answer line (see wypoc/repl.py). Same
    rendering a value gets inside a container, so `"hi"` echoes as `'hi'`
    and is told apart from the symbol `'hi` and the bare characters `hi`."""
    return _repr_str(value, ctx=ctx)


def _format(value, repr_mode: bool, ctx: dict | None = None) -> str:
    if value is None or value is NIL:
        # One `nil`, spelled the way the language spells it. wypoc's NIL is
        # also the empty pair list (`$[]`), and a fn that falls off its end
        # answers Python's None; both are wyrm's single nil value, so both
        # print as `nil` rather than as two spellings of the same thing.
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Symbol):
        return f"'{value.name}" if repr_mode else value.name
    if isinstance(value, str):
        return f"'{value}'" if repr_mode else value
    if value is ELLIPSIS:
        return "..."
    if isinstance(value, Pair):
        # Printed in the literal syntax that builds it - `$[1, 2, 3]`, and
        # `$[1, 2 . 3]` when the chain ends in something other than nil.
        parts = []
        node = value
        while isinstance(node, Pair):
            parts.append(_format(node.car, True, ctx))
            node = node.cdr
        tail = "" if node is NIL or node is None else f" . {_format(node, True, ctx)}"
        return f"$[{', '.join(parts)}{tail}]"
    if isinstance(value, list):
        return "[" + ", ".join(_format(v, True, ctx) for v in value) + "]"
    if isinstance(value, tuple):
        inner = ", ".join(_format(v, True, ctx) for v in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{_format(k, True, ctx)}: {_format(v, True, ctx)}" for k, v in value.items()
        ) + "}"
    if isinstance(value, WyrmError):
        return f"error({value.what})"
    if ctx is not None:
        # A class instance that answers `__str__` controls its own
        # rendering here too, not just at the top level (str_value) - a
        # Token nested inside a parsed `$[...]` tree should print the same
        # way whether it's handed to str() directly or found inside a
        # container being printed.
        from wypoc.wyrm_eval_parse_tree import ClassInstance, has_message_for, send_message
        if isinstance(value, ClassInstance) and has_message_for("__str__", [value], ctx):
            return send_message("__str__", [value], [], {}, ctx)
    return str(value)


def _to_int(value) -> int:
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def _to_float(value) -> float:
    return float(value)


def _to_bool(value) -> bool:
    return bool(value)


def _to_sym(value) -> "Symbol":
    """`sym("name")` / `sym('name)` - the cast into the symbol type, so a
    name computed as a string can become the symbol an s-expression node
    needs for its head kind."""
    if isinstance(value, Symbol):
        return value
    if isinstance(value, str):
        return Symbol(value)
    raise TypeError(f"sym: cannot make a symbol from {type(value).__name__}")


STR = PrimitiveType("str", _to_str)
INT = PrimitiveType("int", _to_int)
FLOAT = PrimitiveType("float", _to_float)
BOOL = PrimitiveType("bool", _to_bool)
SYM = PrimitiveType("sym", _to_sym)

PRIMITIVE_TYPES = {t.name: t for t in (STR, INT, FLOAT, BOOL, SYM)}


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
    """(error "message") -> a new WyrmError carrying that message. This
    backs the *base* `error` type's construction specifically - see
    wyrm_eval_parse_tree.instantiate()'s special case for ERROR_CLASS.
    User error subtypes (`class Foo(error) {}`) construct as ordinary
    ClassInstances instead; is_error() below recognizes both."""
    return WyrmError(what)


def is_error(value) -> bool:
    """True for the base error type's WyrmError representation, for an
    instance of any user class that (transitively) subclasses `error`
    (see doc/language-spec.md's "Error may be inherited to create new
    error types"), and for UNSET (doc/language-spec.md lists `Unset` as a
    predefined `error` subtype) - the single predicate `try`/`catch`/`?=`/
    `defined()` all check (see wyrm_eval_parse_tree.py)."""
    from wypoc.wyrm_eval_parse_tree import UNSET

    if value is UNSET or isinstance(value, WyrmError):
        return True
    from wypoc.wyrm_eval_parse_tree import ClassInstance, ERROR_CLASS, _class_distance

    if isinstance(value, ClassInstance):
        return _class_distance(value.cls, ERROR_CLASS) is not None
    return False


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

    def __iter__(self):
        # So `for x in some_pair_chain:` (see wyrm_eval_parse_tree.py's
        # _iter_values) walks car-by-car out to the terminating NIL, same
        # as len()/__contains__ above.
        node = self
        while isinstance(node, Pair):
            yield node.car
            node = node.cdr
        if node is not NIL:
            raise TypeError("iteration: improper list has no well-defined end")

    def __getitem__(self, index):
        # `pair_list[i]` treats the chain like an array: `$[1, 2, 3][0] ==
        # 1`. eval_expr's ast.Index case catches IndexError/TypeError and
        # turns it into a catchable WyrmError, same as an out-of-range list
        # index, so an improper-list walk-off or a non-int index lands there
        # rather than crashing.
        if not isinstance(index, int) or isinstance(index, bool):
            raise TypeError(f"pair index must be an int (got {type(index).__name__})")
        if index < 0:
            raise IndexError("pair index out of range")
        node = self
        for _ in range(index):
            if not isinstance(node, Pair):
                raise IndexError("pair index out of range")
            node = node.cdr
        if not isinstance(node, Pair):
            raise IndexError("pair index out of range")
        return node.car


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

    def __iter__(self):
        return iter(())

    def __eq__(self, other):
        # wyrm has one nil. This evaluator reaches it two ways - the `nil`
        # literal/empty pair list (this singleton) and Python's None, which
        # is what a fn falling off its end answers and what a slot with no
        # zero value holds - so the two compare equal rather than being a
        # distinction wyrm code can see. Python tries the reflected
        # operand when None's own __eq__ answers NotImplemented, so this
        # covers `nil == x` and `x == nil` alike.
        return other is self or other is None

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(None)


NIL = _Nil()


def cons(a, b):
    """(cons a b) -> a new Pair whose car is `a` and cdr is `b`."""
    return Pair(a, b)


def car(x):
    """car, generalized past Scheme's cons-only version to anything
    list-adjacent that wypoc already has lying around: a Pair, or a Python
    str/list/tuple standing in for wyrm's string/array/tuple values. A bad
    input (empty list/array/string, nil, or an unrelated type) is a
    catchable WyrmError rather than a raised exception - see _safe_div's
    docstring for why: wyrm code should be able to `try`/`catch` this
    instead of the interpreter crashing."""
    if isinstance(x, Pair):
        return x.car
    if x is NIL:
        return error("car: cannot take car of '() (the empty list)")
    if isinstance(x, (str, list, tuple)):
        if not x:
            return error("car: empty list/array/string has no first element")
        return x[0]
    return error(f"car: not a pair/list/array/string (got {type(x).__name__})")


def cdr(x):
    """cdr, generalized the same way as car (see above)."""
    if isinstance(x, Pair):
        return x.cdr
    if x is NIL:
        return error("cdr: cannot take cdr of '() (the empty list)")
    if isinstance(x, (str, list, tuple)):
        if not x:
            return error("cdr: empty list/array/string has no rest")
        return x[1:]
    return error(f"cdr: not a pair/list/array/string (got {type(x).__name__})")


def reverse(node):
    """(reverse node) -> a new pair list with node's elements in reverse
    order. `node` must be a proper pair list (NIL-terminated, or NIL
    itself) - walks it car-by-car, consing each element onto a fresh chain
    built from the tail backwards, same as the reference implementation's
    std::pairs::reverse."""
    out = NIL
    p = node
    while isinstance(p, Pair):
        out = Pair(p.car, out)
        p = p.cdr
    return out


def nreverse(container):
    """(nreverse container) -> reverses `container` in place and returns it.
    A pair chain is reversed by relinking cdrs in place (walking node-by-node,
    swapping each one's cdr to point at the previously-seen node); a list is
    reversed with Python's own in-place list.reverse(). Unlike reverse()
    above, which always builds a fresh chain, this mutates its argument and
    hands the same (now-reordered) object back - the wyrm counterpart to
    Scheme's own nreverse/list.reverse! distinction from a plain reverse."""
    if isinstance(container, list):
        container.reverse()
        return container
    if container is NIL:
        return NIL
    if isinstance(container, Pair):
        prev = NIL
        node = container
        while isinstance(node, Pair):
            nxt = node.cdr
            node.cdr = prev
            prev = node
            node = nxt
        return prev
    raise TypeError(f"nreverse: unsupported value type ({type(container).__name__})")


def set_car(pair, new_car):
    """($set_car(p, x)) -> sets p's car to x in place and returns p, the
    mutating counterpart to car()/cons() - Scheme's set-car!."""
    if not isinstance(pair, Pair):
        raise TypeError(f"$set_car: not a pair (got {type(pair).__name__})")
    pair.car = new_car
    return pair


def set_cdr(pair, new_cdr):
    """($set_cdr(p, x)) -> sets p's cdr to x in place and returns p, the
    mutating counterpart to cdr()/cons() - Scheme's set-cdr!."""
    if not isinstance(pair, Pair):
        raise TypeError(f"$set_cdr: not a pair (got {type(pair).__name__})")
    pair.cdr = new_cdr
    return pair


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


def resize(lst, count: int):
    """(lst ! resize(count)) -> expands or prunes list `lst` in place to
    exactly `count` items: items beyond `count` are dropped, and new items
    (when growing) are the Unset error value - consistent with a
    forward-declared variable or a slot with no default (see
    doc/language-spec.md's Variables section). Note that a list's elements
    are plain values, not Variable cells, so reading a resized-in Unset
    item back out (`lst[i]`) just hands back the Unset value itself rather
    than raising the way an unassigned variable's lookup would. A message
    (`!`), not a plain call, since it's a method on list values - see
    register_native_method (wyrm_eval_parse_tree.py). Returns `lst`."""
    from wypoc.wyrm_eval_parse_tree import UNSET

    if not isinstance(lst, list):
        raise TypeError(f"resize: not a list (got {type(lst).__name__})")
    if count < 0:
        raise ValueError(f"resize: count must be >= 0 (got {count})")
    if count < len(lst):
        del lst[count:]
    elif count > len(lst):
        lst.extend(UNSET for _ in range(count - len(lst)))
    return lst


def expand(lst, value, count: int):
    """(lst ! expand(value, count)) -> grows list `lst` in place by `count`
    more items, each a copy of `value` - the sized-allocation counterpart
    to resize, which can only fill with Unset. `count` is relative (items
    added), not a new total length, so `[1] ! expand(0, 3)` is
    `[1, 0, 0, 0]`; `count == 0` is a no-op. `value` is stored by
    reference, exactly as an element assignment would: expanding with a
    list or dict gives `count` references to that same object, not copies
    of it. A message (`!`), not a plain call, since it's a method on list
    values - see register_native_method (wyrm_eval_parse_tree.py).
    Returns `lst`."""
    if not isinstance(lst, list):
        raise TypeError(f"expand: not a list (got {type(lst).__name__})")
    if count < 0:
        raise ValueError(f"expand: count must be >= 0 (got {count})")
    lst.extend(value for _ in range(count))
    return lst


def append(lst, value):
    """(lst ! append(value)) -> adds `value` to the end of list `lst` in
    place, growing it by one. A message (`!`), not a plain call, since it's
    a method on list values - see register_native_method
    (wyrm_eval_parse_tree.py). Returns `lst`, so appends chain
    (`l ! append(1) ! append(2)`)."""
    if not isinstance(lst, list):
        raise TypeError(f"append: not a list (got {type(lst).__name__})")
    lst.append(value)
    return lst


def remove(d, key):
    """(d ! remove(key)) -> removes `key` from dict `d` in place and
    returns the value that was removed. A message (`!`), not a plain call,
    since it's a method on dict values - see register_native_method
    (wyrm_eval_parse_tree.py) for how a builtin like this becomes
    dispatchable that way without a real Class behind it. A missing key
    raises, same as plain indexing (`d[key]`) already does for dicts."""
    if not isinstance(d, dict):
        raise TypeError(f"remove: not a dict (got {type(d).__name__})")
    return d.pop(key)


def signal_connect(sig, callback):
    """(sig ! connect(callback)) -> subscribes `callback` to signal `sig`
    (a SignalValue - see wyrm_eval_parse_tree.py), so a later `emit` calls
    it along with every other subscriber. A message on a value with no real
    Class behind it, same as substr/append/remove above. Returns `sig`, so
    a connect chains the same way append does."""
    from wypoc.wyrm_eval_parse_tree import SignalValue

    if not isinstance(sig, SignalValue):
        raise TypeError(f"connect: not a signal (got {type(sig).__name__})")
    sig.subscribers.append(callback)
    return sig


def signal_disconnect(sig, callback):
    """(sig ! disconnect(callback)) -> undoes a previous connect(callback);
    disconnecting a callback that isn't (or is no longer) subscribed is a
    no-op, not an error - symmetrical with how a signal starts out with no
    subscribers at all rather than needing one removed to be "empty"."""
    from wypoc.wyrm_eval_parse_tree import SignalValue

    if not isinstance(sig, SignalValue):
        raise TypeError(f"disconnect: not a signal (got {type(sig).__name__})")
    if callback in sig.subscribers:
        sig.subscribers.remove(callback)
    return sig


def tuple_(*rest) -> tuple:
    """(tuple a, b, c) -> a new tuple holding the given arguments, the same
    way `fn tuple(*rest) { return rest; }` would: *rest already collects a
    native fn's overflow positional args into a Python tuple (see
    _bind_params), so this just hands that straight back."""
    return rest


def print_(ctx: dict, *args) -> None:
    """(print a, b, c) -> writes each argument, space-separated, to stdout,
    stringified the same way str() would (e.g. bools as true/false, and a
    class instance that answers `__str__` controlling its own rendering -
    see str_value) - with no trailing newline. See println_ below for the
    newline-terminated form. Goes through wyrm_io's own write (not a raw
    Python print()) so it's still subject to the same __STDOUT handle - e.g.
    stdout-capturing tests that call wyrm_io._reset_std_handles() see
    print's output too."""
    from wypoc import wyrm_io
    from wypoc.wyrm_eval_parse_tree import str_value

    wyrm_io.wyrm_write(wyrm_io.STDOUT, " ".join(str_value(ctx, a) for a in args))


def println_(ctx: dict, *args) -> None:
    """(println a, b, c) -> like print(), but terminates the line with `\\n`.
    A builtin so it needs no `import std::io`, unlike corelib/std/io.wy's
    own println (kept for parity with the reference implementation's module
    layout - either spelling reaches the same wyrm_io.STDOUT write)."""
    from wypoc import wyrm_io
    from wypoc.wyrm_eval_parse_tree import str_value

    wyrm_io.wyrm_write(wyrm_io.STDOUT, " ".join(str_value(ctx, a) for a in args) + "\n")


def end_() -> None:
    """`end()` - stops the current thread/process's own top-level run,
    cleanly, without the implicit `exit()` that falling off the end of
    main's top level would otherwise trigger (see EndSignal's docstring,
    wyrm_eval_parse_tree.py, and cli.py's script-running path). A thread
    spawned via `thread <module-path>` calling `end()` itself, once that
    lands, stops just that one thread/process - not the whole program."""
    from wypoc.wyrm_eval_parse_tree import EndSignal

    raise EndSignal()


def exit_(code: int = 0) -> None:
    """`exit()`/`exit(code)` - stops the whole program immediately with the
    given process exit code (0 if omitted), same as falling off the end of
    main's top level normally does. See ExitSignal's docstring."""
    from wypoc.wyrm_eval_parse_tree import ExitSignal

    raise ExitSignal(code)


def resolve(fut) -> "object":
    """`resolve(fut)` - blocks until `fut` (a `task expr`'s Future - see
    ast.TaskSpawn) is resolved, then returns its value, or re-raises
    whatever the resolving thread failed with. Called on anything but a
    Future, that's a usage error, not a silent pass-through."""
    from wypoc.wyrm_eval_parse_tree import Future

    if not isinstance(fut, Future):
        raise TypeError(f"resolve: not a future (got {type(fut).__name__})")
    return fut.wait()


def _help_signature(node, keyword: str) -> str:
    """`fn foo(a, b) -> int` / `class Foo(Base)` - the one-line header
    help() leads with, for a def node (`ast.FnDef`/`ast.CoDef`/
    `ast.ClassDef`) or a bodiless `ast.Lambda` (which has no name, params
    aside)."""
    from wypoc import ast_nodes as ast
    from wypoc.symbols import _render_signature

    if isinstance(node, ast.Lambda):
        params = ", ".join(getattr(p, "name", "?") for p in node.params)
        return f"fn ({params})"
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(getattr(b, "name", None) or str(b) for b in node.bases)
        return f"class {node.name}" + (f"({bases})" if bases else "")
    return _render_signature(node, keyword)


def _help_summary(doc) -> str:
    """The first line of a doc comment - what a member listing shows
    instead of the whole block."""
    return (doc or "").splitlines()[0] if doc else "(no documentation)"


def _help_lines(value) -> list:
    """The lines `help(value)` writes - see help_ below for what each
    branch covers."""
    from wypoc import ast_nodes as ast
    from wypoc.wyrm_eval_parse_tree import Class, ClassInstance, Coroutine, Function, Module

    if isinstance(value, ClassInstance):
        return [f"(instance of {value.cls.name})", ""] + _help_lines(value.cls)

    if isinstance(value, (Function, Coroutine)):
        keyword = "co" if isinstance(value, Coroutine) else "fn"
        lines = [_help_signature(value.node, keyword)]
        doc = getattr(value.node, "doc", None)
        lines += ["", doc] if doc else ["", "(no documentation)"]
        return lines

    if isinstance(value, Class):
        node = value.node
        lines = [_help_signature(node, "class")]
        lines += ["", node.doc] if node.doc else ["", "(no documentation)"]
        members = sorted(list(value.methods.items()) + list(value.coroutines.items()), key=lambda kv: kv[0])
        if members:
            lines += ["", "Methods:"]
            for name, member_node in members:
                keyword = "co" if isinstance(member_node, ast.CoDef) else "fn"
                lines.append(f"  {_help_signature(member_node, keyword)} - {_help_summary(member_node.doc)}")
        return lines

    if isinstance(value, Module):
        tree = value.tree
        lines = [f"<module {value.name}>"]
        lines += ["", tree.doc] if tree is not None and tree.doc else ["", "(no documentation)"]
        members = []
        for stmt in (tree.body if tree is not None else []):
            while isinstance(stmt, ast.Decorated):
                stmt = stmt.inner
            if isinstance(stmt, (ast.FnDef, ast.CoDef, ast.ClassDef)):
                members.append(stmt)
        if members:
            lines += ["", "Members:"]
            for node in sorted(members, key=lambda n: n.name):
                keyword = ("co" if isinstance(node, ast.CoDef) else
                           "class" if isinstance(node, ast.ClassDef) else "fn")
                lines.append(f"  {_help_signature(node, keyword)} - {_help_summary(node.doc)}")
        return lines

    return [f"(no documentation for {type(value).__name__} value)"]


def help_(value=None) -> None:
    """(help) -> writes a short usage reminder.
    (help x) -> writes x's documentation to stdout: for a `fn`/`co`, its
    signature and doc comment; for a class, its signature/doc plus each
    method's signature and doc summary; for a `Module` (see import_module,
    wyrm_eval_parse_tree.py), the module's own doc comment plus every
    top-level fn/co/class it defines, each with a one-line summary; for an
    instance, its class's documentation. Doc comments come from the
    sphinx/doxygen-style `#`/`#:` comment block written directly above a
    `fn`/`class` (or the file's own leading block, for a module) - see
    parse.py's _attach_doc_comments, the only place `.doc` gets populated
    on an AST node."""
    from wypoc import wyrm_io

    if value is None:
        wyrm_io.wyrm_write(wyrm_io.STDOUT, "help(x) - show documentation for a fn, co, class, module, or instance\n")
        return
    wyrm_io.wyrm_write(wyrm_io.STDOUT, "\n".join(_help_lines(value)) + "\n")


def install(ctx: dict) -> None:
    """Expose str/int/float/bool, cons/pair/car/cdr/reverse/nreverse, tuple,
    nil, copy, len, print, println, end, exit, resolve, and help as
    wyrm-visible builtins, plus str's substr, list's resize/expand/append,
    and dict's remove as `!`-callable messages. `pair` is `cons` under
    another name - doc/language-spec.md's
    spelling for building an improper pair-list cell (`pair('a, 'b)`),
    since the `$[...]` pair-list literal only ever builds proper lists.
    `$set_car`/`$set_cdr` are Scheme's set-car!/set-cdr!: they mutate a
    Pair's car/cdr in place and return the pair, unlike cons/reverse which
    always build something new - nreverse is the same in-place-mutation
    idea applied to a whole chain (or a list) at once.

    `error` is bound to ERROR_CLASS (a real Class), not the `error()`
    Python function directly - calling it (`error("msg")`) goes through
    the ordinary Class-is-callable construction path (see call_value),
    which special-cases ERROR_CLASS to build a WyrmError; that's also
    what makes `class Foo(error) {}` legal (base must be a Class).

    `next`/`send` drive a coroutine (see CoroutineInstance); the four
    predefined error subtypes (OutOfMemory, RuntimeError, OSError,
    StopIteration - see doc/language-spec.md's Fundamental Types) are
    exposed the same way `error` is, as real subclassable Classes."""
    from wypoc.wyrm_eval_parse_tree import (
        ContextualBuiltin, ERROR_CLASS, OS_ERROR_CLASS, OUT_OF_MEMORY_CLASS,
        RUNTIME_ERROR_CLASS, STOP_ITERATION_CLASS, TREE_BASE_CLASS, expose_all,
        install_native_decorators, macroexpand_value, next_, parse_source,
        register_native_method, send_, sexpr_value, str_value,
    )

    # str is a PrimitiveType like int/float/bool/sym (PRIMITIVE_TYPES), but
    # its cast is overridden with a ContextualBuiltin: a class instance that
    # answers `__str__` controls its own rendering (see str_value), which
    # needs the calling scope's message table the way `sexpr` does.
    primitive_types = {**PRIMITIVE_TYPES, "str": ContextualBuiltin(str_value, "str")}

    expose_all(
        ctx, **primitive_types, cons=cons, pair=cons, car=car, cdr=cdr, nil=NIL,
        copy=copy, len=length,
        print=ContextualBuiltin(print_, "print"),
        println=ContextualBuiltin(println_, "println"),
        error=ERROR_CLASS,
        reverse=reverse, nreverse=nreverse,
        **{"$set_car": set_car, "$set_cdr": set_cdr},
        tuple=tuple_, end=end_, exit=exit_, resolve=resolve,
        OutOfMemory=OUT_OF_MEMORY_CLASS, RuntimeError=RUNTIME_ERROR_CLASS,
        OSError=OS_ERROR_CLASS, StopIteration=STOP_ITERATION_CLASS,
        next=next_, send=send_, TreeBase=TREE_BASE_CLASS, help=help_,
        # `sexpr` is a builtin function and unqualified, deliberately not a
        # module path: that is what makes one decorator's source run against
        # either representation of a tree without even differing by an
        # import. It needs the calling scope to ask whether a class answers
        # `__sexpr`, hence the ContextualBuiltin wrapper.
        sexpr=ContextualBuiltin(sexpr_value, "sexpr"),
        # `macroexpand(tree)` - forces an unexpanded (outside-in) nested
        # decorator application to run, answering the fully-expanded tree.
        # See wyrm_eval_parse_tree.expand_decorated's docstring.
        macroexpand=ContextualBuiltin(macroexpand_value, "macroexpand"),
        # `parse(source)` - a string's own tree(s), boxed the same way
        # `foo::$ast` is, so `sexpr(parse(source))` works exactly like
        # `sexpr(foo::$ast)` does. Plain (not contextual): unlike sexpr, it
        # never needs the calling scope.
        parse=parse_source,
    )
    install_native_decorators(ctx)
    register_native_method("substr", substr, ctx)
    register_native_method("remove", remove, ctx)
    register_native_method("resize", resize, ctx)
    register_native_method("expand", expand, ctx)
    register_native_method("append", append, ctx)
    register_native_method("connect", signal_connect, ctx)
    register_native_method("disconnect", signal_disconnect, ctx)
