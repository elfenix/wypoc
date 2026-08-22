"""Runtime support library imported by --compile-py's generated code
(`from wypoc.compiler_py import engine` / `from wypoc.compiler_py.engine
import Context, Cursor, Machine, ...`). Every generated `output_dir/wyrm/
__init__.py` re-exports Machine/Context/Cursor/dispatch from here - this
module's content is defined once, in wypoc itself; an output tree never
vendors its source.

Deliberately independent of wyrm_eval_parse_tree.py's runtime
representations (Scope/Class/Coroutine/...) - see the compiler_py package
docstring. The one thing this module has in common with the interpreter is
the *behavior* it reproduces (errors-as-values, multi-dispatch messages,
cooperative coroutine suspension), not any shared code or state.
"""
import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


# ---------------------------------------------------------------------
# Errors as values
# ---------------------------------------------------------------------

@dataclass
class WyrmError:
    what: str


def is_error(value: Any) -> bool:
    return isinstance(value, WyrmError)


def error(what: str) -> WyrmError:
    return WyrmError(what)


# ---------------------------------------------------------------------
# print / str() and friends - independently mirrors wyrm_builtins.py's
# _format/_to_str/_to_int/_to_float/_to_bool/_to_sym exactly (bare mode
# for str(value)/print, repr mode for values nested inside a container -
# `str("hi")` is `hi` but `str(["hi"])` is `['hi']`).
# ---------------------------------------------------------------------

def _wy_format(value: Any, repr_mode: bool) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Symbol):
        return f"'{value.name}" if repr_mode else value.name
    if isinstance(value, str):
        return f"'{value}'" if repr_mode else value
    if isinstance(value, Pair):
        parts = []
        node = value
        while isinstance(node, Pair):
            parts.append(_wy_format(node.car, True))
            node = node.cdr
        tail = "" if node is None else f" . {_wy_format(node, True)}"
        return f"$[{', '.join(parts)}{tail}]"
    if isinstance(value, list):
        return "[" + ", ".join(_wy_format(v, True) for v in value) + "]"
    if isinstance(value, tuple):
        inner = ", ".join(_wy_format(v, True) for v in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{_wy_format(k, True)}: {_wy_format(v, True)}" for k, v in value.items()
        ) + "}"
    if isinstance(value, WyrmError):
        return f"error({value.what})"
    return str(value)


def _wy_str(value: Any) -> str:
    return _wy_format(value, repr_mode=False)


def wy_print(*args: Any) -> None:
    print(" ".join(_wy_str(a) for a in args), end="")


def wy_str(value: Any) -> str:
    return _wy_format(value, repr_mode=False)


def wy_int(value: Any) -> int:
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def wy_float(value: Any) -> float:
    return float(value)


def wy_bool(value: Any) -> bool:
    return bool(value)


def wy_sym(value: Any) -> "Symbol":
    if isinstance(value, Symbol):
        return value
    if isinstance(value, str):
        return Symbol(value)
    raise TypeError(f"sym: cannot make a symbol from {type(value).__name__}")


@dataclass
class Symbol:
    """A `'name` symbol literal. Only needs to support equality/being a
    distinct value from a plain string for the fixtures this backend
    currently compiles - not a full parity implementation of wyrm_builtins.
    Symbol (interning, etc.)."""
    name: str


# ---------------------------------------------------------------------
# Pair / cons-list ($[...] literal, car/cdr/cons/reverse/len). `nil` still
# maps straight to Python None (see expressions.NAME_LITERALS) rather than
# a dedicated sentinel - a Pair chain's terminator is plain None, so an
# empty `$[]` list *is* None, matching every existing "x == nil"/"is nil"
# use already compiled against None. Mirrors wyrm_builtins.Pair/car/cdr/
# cons/reverse/length, independently (no shared code with the interpreter).
# ---------------------------------------------------------------------

class Pair:
    def __init__(self, car: Any, cdr: Any):
        self.car = car
        self.cdr = cdr

    def __repr__(self):
        return f"Pair({self.car!r}, {self.cdr!r})"

    def __eq__(self, other):
        return isinstance(other, Pair) and self.car == other.car and self.cdr == other.cdr

    def __contains__(self, item):
        node = self
        while isinstance(node, Pair):
            if node.car == item:
                return True
            node = node.cdr
        return False

    def __iter__(self):
        node = self
        while isinstance(node, Pair):
            yield node.car
            node = node.cdr

    def __getitem__(self, index):
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


def wy_cons(a: Any, b: Any) -> Pair:
    return Pair(a, b)


def wy_car(x: Any) -> Any:
    if isinstance(x, Pair):
        return x.car
    if x is None:
        return WyrmError("car: cannot take car of '() (the empty list)")
    if isinstance(x, (str, list, tuple)):
        if not x:
            return WyrmError("car: empty list/array/string has no first element")
        return x[0]
    return WyrmError(f"car: not a pair/list/array/string (got {type(x).__name__})")


def wy_cdr(x: Any) -> Any:
    if isinstance(x, Pair):
        return x.cdr
    if x is None:
        return WyrmError("cdr: cannot take cdr of '() (the empty list)")
    if isinstance(x, (str, list, tuple)):
        if not x:
            return WyrmError("cdr: empty list/array/string has no rest")
        return x[1:]
    return WyrmError(f"cdr: not a pair/list/array/string (got {type(x).__name__})")


def wy_reverse(node: Any) -> Any:
    out = None
    p = node
    while isinstance(p, Pair):
        out = Pair(p.car, out)
        p = p.cdr
    return out


def wy_len(x: Any) -> int:
    if isinstance(x, (str, list, tuple, dict)):
        return len(x)
    n = 0
    p = x
    while isinstance(p, Pair):
        n += 1
        p = p.cdr
    return n


# ---------------------------------------------------------------------
# Low-level POSIX-style I/O primitives (__open/__read/__write/__lseek/
# __dup2/__close/__flush, __STDIN/__STDOUT/__STDERR) - mirrors
# wyrm_io.py's wyrm_open/wyrm_read/... exactly (an independent copy: this
# module never imports wyrm_io, matching the package's "own animal" rule).
# std::io.wy and friends call these directly by name at wyrm top level.
# ---------------------------------------------------------------------

WY_STDIN = 0
WY_STDOUT = 1
WY_STDERR = 2

_io_handles: dict = {WY_STDIN: sys.stdin, WY_STDOUT: sys.stdout, WY_STDERR: sys.stderr}
_io_next_handle = 3


def _io_get(handle: int):
    try:
        return _io_handles[handle]
    except KeyError:
        raise OSError(f"bad file handle: {handle}")


def wy_open(path: str, mode: str = "r") -> int:
    global _io_next_handle
    f = open(path, mode)
    handle = _io_next_handle
    _io_next_handle += 1
    _io_handles[handle] = f
    return handle


def wy_read(handle: int, size: int = -1):
    return _io_get(handle).read(size)


def wy_write(handle: int, data) -> int:
    f = _io_get(handle)
    n = f.write(data)
    f.flush()
    return n


def wy_lseek(handle: int, offset: int, whence: int = 0) -> int:
    f = _io_get(handle)
    f.seek(offset, whence)
    return f.tell()


def wy_dup2(old_handle: int, new_handle: int) -> int:
    target = _io_get(old_handle)
    existing = _io_handles.get(new_handle)
    if existing is not None and existing is not target and new_handle not in (WY_STDIN, WY_STDOUT, WY_STDERR):
        existing.close()
    _io_handles[new_handle] = target
    return new_handle


def wy_close(handle: int) -> int:
    f = _io_get(handle)
    f.close()
    del _io_handles[handle]
    return 0


def wy_flush(handle: int) -> int:
    _io_get(handle).flush()
    return 0


# ---------------------------------------------------------------------
# Arithmetic that answers an error value instead of raising, matching
# wyrm_eval_parse_tree.py's _safe_div/_safe_mod exactly - `try`/`catch`
# need division-by-zero to be a catchable value, not a Python exception.
# ---------------------------------------------------------------------

def wy_div(a: Any, b: Any) -> Any:
    try:
        return a / b
    except ZeroDivisionError:
        return WyrmError("division by zero")


def wy_mod(a: Any, b: Any) -> Any:
    try:
        return a % b
    except ZeroDivisionError:
        return WyrmError("modulo by zero")


def wy_index(obj: Any, index: Any) -> Any:
    """`obj[index]` - an out-of-range/missing index is a catchable error
    value here too (matches the interpreter: "An out-of-range index
    becomes a catchable error value instead of crashing"). Indexing a
    string answers the indexed char's ascii code point (an int), not a
    1-character substring - matches the interpreter's own ast.Index case
    exactly (no unicode support yet; see its docstring - `"7"[0]` is the
    u32 for '7', not the char)."""
    try:
        if isinstance(obj, str):
            return ord(obj[index])
        return obj[index]
    except (IndexError, KeyError, TypeError) as exc:
        return WyrmError(str(exc))


# ---------------------------------------------------------------------
# Context / Machine
# ---------------------------------------------------------------------

class Context:
    """A per-module namespace, mirroring the interpreter's Scope. Ordinary
    function locals are plain Python locals in generated code (not routed
    through Context) - this mainly threads `machine`/`this` through
    message dispatch and satisfies do_import(ctx)'s signature."""

    def __init__(self, machine: "Machine", parent: "Optional[Context]" = None):
        self.machine = machine
        self.parent = parent
        self.vars: dict = {}
        self.this: Any = None

    def child(self) -> "Context":
        return Context(self.machine, parent=self)

    def with_this(self, receiver: Any) -> "Context":
        ctx = self.child()
        ctx.this = receiver
        return ctx

    def get(self, name: str) -> Any:
        ctx = self
        while ctx is not None:
            if name in ctx.vars:
                return ctx.vars[name]
            ctx = ctx.parent
        raise KeyError(name)

    def set_local(self, name: str, value: Any) -> None:
        self.vars[name] = value

    def set(self, name: str, value: Any) -> None:
        ctx = self
        while ctx is not None:
            if name in ctx.vars:
                ctx.vars[name] = value
                return
            ctx = ctx.parent
        raise KeyError(name)


class Machine:
    """The dispatch-table registry + coroutine bookkeeping root, one
    instance per running program, constructed by the entry script's
    __main__ block and threaded through every Context."""

    def __init__(self):
        self.message_tables: "dict[str, MessageTable]" = {}
        self._loaded_modules: "set[str]" = set()

    def table_for(self, qualified_name: str) -> "MessageTable":
        return self.message_tables.setdefault(qualified_name, MessageTable(qualified_name))

    def mark_loaded(self, dotted_module: str) -> bool:
        """True the first time a given module name is seen; False on any
        later call - so a diamond-imported module's do_import runs exactly
        once, mirroring the interpreter's _module_cache."""
        if dotted_module in self._loaded_modules:
            return False
        self._loaded_modules.add(dotted_module)
        return True

    def root_context(self) -> Context:
        return Context(self)


# ---------------------------------------------------------------------
# Message dispatch (multi-dispatch generic functions)
# ---------------------------------------------------------------------

class WyrmDispatchError(Exception):
    pass


@dataclass
class Overload:
    signature: tuple  # tuple[type | None, ...]; None = wildcard slot
    fn: Callable[..., Awaitable[Any]]


class MessageTable:
    def __init__(self, name: str):
        self.name = name
        self.methods: "dict[str, list[Overload]]" = {}

    def register(self, message_name: str, signature: tuple, fn) -> None:
        overloads = self.methods.setdefault(message_name, [])
        for i, ov in enumerate(overloads):
            if ov.signature == signature:
                overloads[i] = Overload(signature, fn)
                return
        overloads.append(Overload(signature, fn))

    def merge(self, other: "MessageTable") -> None:
        for name, overloads in other.methods.items():
            for ov in overloads:
                self.register(name, ov.signature, ov.fn)


_WILDCARD = float("inf")


def _class_distance(cls: type, target: type) -> "Optional[int]":
    """BFS up __wy_bases__ (a class-decorator-set tuple of base fields
    classes, kept separate from Python's own dataclass-subclassing MRO -
    see classes.py) - None if `target` isn't an ancestor of `cls`."""
    seen = set()
    frontier = [(cls, 0)]
    while frontier:
        current, dist = frontier.pop(0)
        if current is target:
            return dist
        if current in seen:
            continue
        seen.add(current)
        for base in getattr(current, "__wy_bases__", ()):
            frontier.append((base, dist + 1))
    return None


def _rank_overloads(overloads, receivers: list):
    ranked = []
    for ov in overloads:
        if len(ov.signature) != len(receivers):
            continue
        distances = []
        ok = True
        for constraint, recv in zip(ov.signature, receivers):
            if constraint is None:
                distances.append(_WILDCARD)
                continue
            d = _class_distance(type(recv), constraint)
            if d is None:
                ok = False
                break
            distances.append(d)
        if ok:
            ranked.append((tuple(distances), ov))
    ranked.sort(key=lambda pair: pair[0])
    return ranked


def try_resolve_overload(table: MessageTable, name: str, receivers: list) -> "Optional[Overload]":
    """Like resolve_overload, but returns None instead of raising when
    `name` has no overloads at all, or none match `receivers` - used by a
    class's generated constructor wrapper to ask "does this class define
    an init?" without treating "no custom constructor" as an error. A tie
    between two real (non-wildcard) matches is still an error."""
    overloads = table.methods.get(name)
    if not overloads:
        return None
    ranked = _rank_overloads(overloads, receivers)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        raise WyrmDispatchError(f"ambiguous overload for {name!r}")
    return ranked[0][1]


def resolve_overload(table: MessageTable, name: str, receivers: list) -> Overload:
    overloads = table.methods.get(name)
    if not overloads:
        raise WyrmDispatchError(f"no message named {name!r}")
    ranked = _rank_overloads(overloads, receivers)
    if not ranked:
        raise WyrmDispatchError(
            f"no overload of {name!r} matches {len(receivers)} receiver(s)")
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        raise WyrmDispatchError(f"ambiguous overload for {name!r}")
    return ranked[0][1]


# ---------------------------------------------------------------------
# Native `!`-callable methods on primitive values (str/list/dict) -
# mirrors wyrm_builtins.py's substr/resize/expand/append/remove exactly
# (registered there via register_native_method, not a real Class).
# ---------------------------------------------------------------------

_UNSET = WyrmError("Unset")

# A `static x = default` local's "not yet initialized" marker (see
# statements.py's `_static_decl`) - deliberately not `_UNSET` above, since
# that's a real wyrm value (Unset) a static local's *default* could
# legitimately evaluate to; this one is a Python-only sentinel nothing
# wyrm-level can ever produce.
_STATIC_UNSET = object()


def wy_substr(s: str, start: int, count: int) -> str:
    return s[start:start + count]


def wy_resize(lst: list, count: int) -> list:
    if count < len(lst):
        del lst[count:]
    elif count > len(lst):
        lst.extend(_UNSET for _ in range(count - len(lst)))
    return lst


def wy_expand(lst: list, value: Any, count: int) -> list:
    lst.extend(value for _ in range(count))
    return lst


def wy_append(lst: list, value: Any) -> list:
    lst.append(value)
    return lst


def wy_remove(d: dict, key: Any) -> Any:
    return d.pop(key)


_NATIVE_METHODS = {
    "substr": wy_substr, "resize": wy_resize, "expand": wy_expand,
    "append": wy_append, "remove": wy_remove,
}
_NATIVE_METHOD_TYPES = {
    "substr": str, "resize": list, "expand": list,
    "append": list, "remove": dict,
}


class Receivers(list):
    """Wraps a `MessageTupleExpr`'s (`foo,bar!baz`) receiver list so
    dispatch() can tell "these are multiple receivers" apart from an
    ordinary single receiver that just happens to be a wyrm list/tuple
    value itself (e.g. `some_list ! append(x)`) - only this wrapper type
    triggers the multi-receiver unpacking below, never a plain list/tuple."""


def dispatch(receivers, name: str, table: MessageTable):
    """`foo!bar` (bare) compiles to `dispatch(foo, "bar", _TABLE)`, a sync
    call returning an awaitable-producing closure; `foo!bar(x, y)` compiles
    to `await dispatch(foo, "bar", _TABLE)(x, y)` - calling the closure is
    always awaited, since every wyrm fn is async.

    Every registered overload function has the uniform signature
    `(this, *declared_params)` - `this` is the single receiver, or a tuple
    of all receivers for a multi-dispatch message, mirroring the
    interpreter's call_overload (receivers become `this`, not extra
    leading positional parameters).

    A single receiver that's itself a str/list/dict answers one of the
    handful of builtin native methods (append/resize/expand/remove/
    substr) directly, ahead of the message table - matches the
    interpreter's register_native_method, which makes these callable via
    `!` without a real Class behind them (see wyrm_builtins.py)."""
    receiver_list = list(receivers) if isinstance(receivers, Receivers) else [receivers]
    if len(receiver_list) == 1 and name in _NATIVE_METHODS:
        fn = _NATIVE_METHODS[name]
        if isinstance(receiver_list[0], _NATIVE_METHOD_TYPES[name]):
            this_value = receiver_list[0]

            async def _bound_native(*args, **kwargs):
                return fn(this_value, *args, **kwargs)

            return _bound_native
    overload = resolve_overload(table, name, receiver_list)
    this_value = receiver_list[0] if len(receiver_list) == 1 else tuple(receiver_list)

    async def _bound(*args, **kwargs):
        return await overload.fn(this_value, *args, **kwargs)

    return _bound


# ---------------------------------------------------------------------
# Coroutines (`co`) - asyncio.Event handoff, not threads/native generators
# ---------------------------------------------------------------------

class Cursor:
    """One running/suspended/finished `co` instance."""

    def __init__(self, body: "Callable[[Cursor], Awaitable[Any]]"):
        self._body = body
        self._to_co = asyncio.Event()
        self._to_caller = asyncio.Event()
        self._in_value: Any = None
        self._out_value: Any = None
        self._started = False
        self._finished = False
        self._result: Any = None
        self._task: "Optional[asyncio.Task]" = None

    async def _runner(self):
        try:
            self._result = await self._body(self)
        except Exception as exc:
            self._result = WyrmError(str(exc))
        self._finished = True
        self._to_caller.set()

    async def _advance(self, send_value: Any) -> Any:
        """Raw resume, returning the *real* value regardless of whether
        this call causes the coroutine to finish: the yielded value if
        still suspended afterward, or the coroutine's actual `return`
        value (or internal-exception-turned-WyrmError) if it just
        finished. Used internally by yield-from forwarding, which needs
        the delegate's real completion value - see expressions.py's
        _compile_yield_from. The wyrm-level next()/send() builtins below
        wrap this and additionally discard that real value in favor of a
        StopIteration error whenever the call ends with the coroutine
        finished (see their own docstrings for why)."""
        if self._finished:
            return self._result
        self._in_value = send_value
        if not self._started:
            self._started = True
            self._task = asyncio.ensure_future(self._runner())
        else:
            self._to_co.set()
        await self._to_caller.wait()
        self._to_caller.clear()
        return self._result if self._finished else self._out_value

    async def yield_(self, value: Any) -> Any:
        self._out_value = value
        self._to_caller.set()
        self._to_co.clear()
        await self._to_co.wait()
        return self._in_value

    async def next(self) -> Any:
        """`next(co)`: resumes `co` and returns what it yields - or a
        StopIteration error if this call causes it to finish (even just
        now), discarding its actual return value; only `.value` ever
        exposes that (matches wyrm_eval_parse_tree.next_ exactly, right
        down to the perhaps-surprising "the return value is never seen by
        next()/send(), only by .value" behavior)."""
        value = await self._advance(None)
        return WyrmError("StopIteration") if self._finished else value

    async def send(self, value: Any) -> Any:
        """`send(co, value)`: like next(), but resumes with a value sent
        in rather than None - see next()'s docstring for the StopIteration
        conversion this applies too (matches wyrm_eval_parse_tree.send_)."""
        result = await self._advance(value)
        return WyrmError("StopIteration") if self._finished else result

    @property
    def value(self) -> Any:
        if not self._finished:
            return WyrmError("...has not finished")
        return self._result


def wy_attr_value(obj: Any) -> Any:
    """`.value` - special-cased by expressions.py's Attr codegen exactly
    where the interpreter special-cases it (an isinstance check at the
    Attr access site, not a compile-time decision, since a compiled
    expression's runtime type isn't known statically): a Cursor's `.value`
    is its own property; anything else's "value" slot is an ordinary
    attribute named `wy_value`."""
    if isinstance(obj, Cursor):
        return obj.value
    return obj.wy_value


async def _wy_aiter(value, table: "Optional[MessageTable]" = None):
    """`for x in expr:` iteration helper: direct iteration for plain
    Python containers, cursor-driving for a `co`, or - for anything else -
    dispatching `__iter__` (which must answer a Cursor, exactly like a
    message-dispatched `co [Cls] __iter__()` does) through `table`, the
    calling module's own `_TABLE` (passed in by statements.py's For
    codegen - this helper has no message table of its own to look in)."""
    if isinstance(value, Cursor):
        while True:
            item = await value.next()
            if value._finished:
                return
            yield item
    elif isinstance(value, (list, tuple, dict, str, Pair)) or value is None:
        for item in (value or ()):
            yield item
    elif table is not None:
        cursor = await dispatch(value, "__iter__", table)()
        async for item in _wy_aiter(cursor, table):
            yield item
    else:
        for item in value:
            yield item
