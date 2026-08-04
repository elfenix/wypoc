"""Tree-walking evaluator prototype over the wypoc AST (wypoc/ast_nodes.py).

Leans entirely on Python's own runtime for values (int/float/str/bool) and
scoping (a plain dict passed in by the caller, just like Python's own
eval()/exec()) rather than modeling wyrm's value representation. Types are
ignored completely - no checking, no coercion beyond what Python does
naturally. This is a proof-of-concept for statement/expression evaluation,
not a real interpreter.
"""
import operator
import threading

from wypoc import ast_nodes as ast
from wypoc import wyrm_builtins
from wypoc import wyrm_modules
from wypoc.parse import parse


class Variable:
    """A bound name in a wyrm scope: holds whatever value it currently has."""

    def __init__(self, value=None):
        self.value = value

    def __repr__(self):
        return f"Variable({self.value!r})"


class Scope(dict):
    """A lexical scope: this level's own `var`-declared bindings (name ->
    Variable), plus an optional parent scope to search when a name isn't
    declared here - see doc/language-spec.md's Variables section ("declaring
    a name already declared in the same scope is an error; declaring a name
    visible from an enclosing scope... shadows it for the duration of the
    inner scope").

    A Scope *is* a dict (its own level's bindings), so `ctx[name]`/
    `name in ctx`/`ctx.get(name)` elsewhere in this module keep working
    unchanged, but now transparently walk outward through `parent` for
    reads while `declare_new` only ever checks/writes this level - see
    `get_cell`/`declared_here` below. Plain item assignment (`ctx[name] =
    value`, e.g. bind_new()) still writes to this level only, unconditionally
    - that's the "always fresh, no declare-checking" escape hatch used for
    fn/class/import/builtin bindings, which aren't part of the var/:=
    declaration system."""

    def __init__(self, parent: "Scope | None" = None):
        super().__init__()
        self.parent = parent

    def child(self) -> "Scope":
        return Scope(self)

    def get_cell(self, name: str):
        """The Variable bound to `name`, searching this scope then each
        enclosing one in turn - None if `name` isn't declared anywhere in
        the chain."""
        s = self
        while s is not None:
            cell = dict.get(s, name)
            if cell is not None:
                return cell
            s = s.parent
        return None

    def declared_here(self, name: str) -> bool:
        """Whether `name` is declared in *this* scope specifically (not an
        enclosing one) - the redeclaration check `var`/`:=` need."""
        return dict.__contains__(self, name)

    def declare_new(self, name: str, value=None) -> "Variable":
        if self.declared_here(name):
            raise NameError(f"'{name}' is already declared in this scope")
        cell = Variable(value)
        dict.__setitem__(self, name, cell)
        return cell

    def __contains__(self, name):
        return self.get_cell(name) is not None

    def __getitem__(self, name):
        cell = self.get_cell(name)
        if cell is None:
            raise KeyError(name)
        return cell

    def get(self, name, default=None):
        cell = self.get_cell(name)
        return default if cell is None else cell


class Function:
    """A user-defined fn (or lambda), closing over the scope it was defined in."""

    def __init__(self, name, node: "ast.FnDef | ast.Lambda", closure: dict):
        self.name = name
        self.node = node
        self.closure = closure

    def __repr__(self):
        return f"Function({self.name!r})"


class Class:
    """A user-defined class/type: parent classes plus its own slots/methods,
    unevaluated (method bodies aren't run until message dispatch happens).

    There's no dedicated constructor node any more - `init` (if any) is
    just a regular entry in `self.methods`, dispatched at construction time
    the same way any other message is (see instantiate() below), which is
    also how an `init` inherited from a base class (no override in this
    class) falls out for free via the normal multi-dispatch machinery."""

    def __init__(self, name, node: ast.ClassDef, closure: dict, bases: list):
        self.name = name
        self.node = node
        self.closure = closure
        self.bases: list["Class"] = bases
        self.slots: dict = {}
        self.methods: dict = {}
        self.coroutines: dict = {}
        for member in node.body:
            if isinstance(member, ast.SlotDef):
                self.slots[member.name] = member
            elif isinstance(member, ast.FnDef):
                self.methods[member.name] = member
            elif isinstance(member, ast.CoDef):
                self.coroutines[member.name] = member
            elif isinstance(member, ast.StaticDecl):
                # A class-scoped `static`: evaluated once, eagerly, right
                # here (vs. a fn-local static's lazy first-call init - see
                # eval_stmt's StaticDecl case) and bound directly into the
                # class's closure so every method (whose own local scope is
                # seeded from this same closure - see call_overload) sees
                # and shares the one Variable. This does mean the name is
                # also visible as an ordinary binding in the enclosing
                # scope the class was defined in - a POC simplification,
                # not full encapsulation.
                value = eval_expr(member.default, self.closure) if member.default is not None else None
                bind_new(member.name, value, self.closure)

    def all_slots(self) -> dict:
        """(slot_def, owning_class) in MRO order - base classes first, so a
        subclass's own slot of the same name overrides its base's, and each
        default expression is later evaluated in the scope it was declared
        in rather than the leaf class's. A simple linearization that
        doesn't try to handle diamond inheritance properly."""
        result: dict = {}
        for base in self.bases:
            result.update(base.all_slots())
        for name, slot_def in self.slots.items():
            result[name] = (slot_def, self)
        return result

    def __repr__(self):
        bases = ", ".join(b.name for b in self.bases)
        return f"Class({self.name!r}{f'({bases})' if bases else ''})"


# The built-in `error` type, as a real Class so `class Foo(error) {}`
# (base-class subclassing) type-checks like any other inheritance - see
# eval_stmt's ClassDef case, which just requires isinstance(base, Class).
# Its own construction (`error("msg")`) is special-cased in instantiate()
# to build a WyrmError rather than a plain ClassInstance, preserving the
# existing WyrmError representation for the base case; subclasses (no
# override) construct as ordinary ClassInstances - is_error() (see
# wyrm_builtins.py) recognizes both via class-ancestry.
ERROR_CLASS = Class(
    "error",
    ast.ClassDef("error", [], [ast.SlotDef("what", None, None, None)]),
    {},
    [],
)


def _builtin_error_subtype(name: str) -> Class:
    """A trivial `class {name}(error) {{}}` - one of doc/language-spec.md's
    predefined error subtypes (OutOfMemory, RuntimeError, OSError,
    StopIteration). No extra slots/behavior of their own; is_error()
    recognizes them (and any further user subclass) via class ancestry."""
    return Class(name, ast.ClassDef(name, [], []), {}, [ERROR_CLASS])


OUT_OF_MEMORY_CLASS = _builtin_error_subtype("OutOfMemory")
RUNTIME_ERROR_CLASS = _builtin_error_subtype("RuntimeError")
OS_ERROR_CLASS = _builtin_error_subtype("OSError")
STOP_ITERATION_CLASS = _builtin_error_subtype("StopIteration")


class ClassInstance:
    """A constructed object (built by calling its class - see
    call_value/instantiate): its class plus its own slot storage."""

    def __init__(self, cls: Class):
        self.cls = cls
        self.attrs: dict = {}

    def __repr__(self):
        attrs = ", ".join(f"{k}={v.value!r}" for k, v in self.attrs.items())
        return f"<{self.cls.name} {attrs}>"


class Coroutine:
    """A `co` definition itself (like Function, but for coroutines) - not
    yet running. Calling it (see call_value) builds a CoroutineInstance,
    which is what next()/send() actually drive."""

    def __init__(self, name, node: ast.CoDef, closure: dict):
        self.name = name
        self.node = node
        self.closure = closure

    def __repr__(self):
        return f"Coroutine({self.name!r})"


_current_coroutine = threading.local()


class CoroutineInstance:
    """A running (or not-yet-started, or finished) `co` instance, driven by
    next()/send() - see doc/language-spec.md's Coroutines section.

    Implemented with a dedicated (daemon) OS thread that blocks on a
    threading.Event whenever it isn't the one actively running - Python's
    GIL means only one of {caller thread, this coroutine's thread} is ever
    truly executing at a time, so despite being real threads this behaves
    like ordinary cooperative coroutines: `yield` (see _yield_value) hands
    control back to whichever next()/send() call is waiting, and stays
    suspended until the coroutine is driven again."""

    def __init__(self, node: ast.CoDef, local_ctx: dict):
        self.node = node
        self.local_ctx = local_ctx
        self._to_co = threading.Event()
        self._to_caller = threading.Event()
        self._in_value = None
        self._out_value = None
        self._started = False
        self._finished = False
        self._result = None
        self._thread: "threading.Thread | None" = None

    def __repr__(self):
        state = "finished" if self._finished else ("running" if self._started else "not started")
        return f"CoroutineInstance({self.node.name!r}, {state})"

    def _run(self) -> None:
        _current_coroutine.instance = self
        try:
            eval_block(self.node.body, self.local_ctx)
        except ReturnSignal as ret:
            self._result = ret.value
        except BaseException as exc:  # noqa: BLE001
            # An uncaught crash inside the body (e.g. an operation that
            # doesn't tolerate the value sent in) ends the coroutine like
            # any other completion, with an error as its result - so it
            # surfaces the same way doc/language-spec.md's own example
            # shows (`send(adder, nil) # value is error of stop
            # iteration`), not as a raw Python traceback in the caller.
            self._result = wyrm_builtins.error(str(exc))
        finally:
            self._finished = True
            self._to_caller.set()

    def _suspend(self, value):
        """Called from inside the coroutine's own thread (via
        _yield_value) when it hits a `yield`: hands `value` back to
        whichever next()/send() call is waiting, then blocks until driven
        again, returning whatever was sent in."""
        self._out_value = value
        self._to_caller.set()
        self._to_co.wait()
        self._to_co.clear()
        return self._in_value

    def _advance_raw(self, send_value) -> tuple:
        """Resumes the coroutine (starting its thread on first call) and
        blocks until it either yields or finishes. Returns
        (finished, value): value is the yielded value if not finished, or
        the coroutine's `return` value if finished - calling this again
        after it's already finished just keeps returning (True, result),
        matching Python generators' "StopIteration forever after
        exhaustion" behavior. Used directly by 'yield from' (see
        _yield_from); next()/send() (see below) wrap this into the
        StopIteration-error convention doc/language-spec.md describes."""
        if self._finished:
            return True, self._result
        if not self._started:
            self._started = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            self._in_value = send_value
            self._to_co.set()
        self._to_caller.wait()
        self._to_caller.clear()
        if self._finished:
            return True, self._result
        return False, self._out_value


def _yield_value(value):
    """`yield value` from inside a running coroutine's body: suspends the
    current (coroutine) thread and hands `value` back to next()/send()."""
    co = getattr(_current_coroutine, "instance", None)
    if co is None:
        raise TypeError("'yield' used outside of a coroutine body")
    return co._suspend(value)


def _yield_from(sub) -> "object":
    """`yield from sub`: re-yields every value `sub` yields (forwarding
    whatever's sent back in) until `sub` finishes, then evaluates to
    `sub`'s own return value - see doc/language-spec.md's "Coroutines also
    support delegation via 'yield from'"."""
    if not isinstance(sub, CoroutineInstance):
        raise TypeError("'yield from' expects a coroutine")
    outer = getattr(_current_coroutine, "instance", None)
    if outer is None:
        raise TypeError("'yield from' used outside of a coroutine body")
    finished, value = sub._advance_raw(None)
    while not finished:
        sent = outer._suspend(value)
        finished, value = sub._advance_raw(sent)
    return value


def instantiate_coroutine(node: ast.CoDef, closure: dict, positional, kwargs, this_value) -> CoroutineInstance:
    """Builds a CoroutineInstance: binds params/this into a fresh local
    scope (same machinery ordinary calls use - see _bind_params) but
    doesn't run the body yet. `this_value` is None for a bare `co(...)`
    call, or the receiver(s) for a message-dispatched `co [Cls] ...`."""
    local_ctx = closure.child()
    if this_value is not None:
        bind_new("this", this_value, local_ctx)
        if isinstance(this_value, ClassInstance):
            local_ctx.update(this_value.attrs)
    _bind_params(node, local_ctx, positional, kwargs, node.name)
    return CoroutineInstance(node, local_ctx)


def next_(co) -> "object":
    """(next co) -> resumes `co` (starting it, the first time) and returns
    what it yields, or a StopIteration error if it's already finished."""
    if not isinstance(co, CoroutineInstance):
        raise TypeError(f"next() expects a coroutine (got {type(co).__name__})")
    finished, value = co._advance_raw(None)
    return stop_iteration() if finished else value


def send_(co, value) -> "object":
    """(send co, value) -> sends `value` into `co` (resuming it) and
    returns what it yields next, or a StopIteration error if it finishes.
    `co` must already have been started with next() - see
    doc/language-spec.md's Coroutines section."""
    if not isinstance(co, CoroutineInstance):
        raise TypeError(f"send() expects a coroutine (got {type(co).__name__})")
    if not co._started:
        raise TypeError("a coroutine must be started with next() before send()")
    finished, out = co._advance_raw(value)
    return stop_iteration() if finished else out


class NativeBody:
    """Sits in a MethodOverload's `node` slot to mark an overload
    implemented directly in Python rather than by an ast.FnDef body - used
    for builtin per-value methods (e.g. str's substr) that need `!`
    message-call syntax but don't have a ClassInstance/Class behind them to
    hang a real `fn` on. Called as fn(receiver_or_receivers, *positional,
    **kwargs); see call_overload's NativeBody branch."""

    def __init__(self, fn):
        self.fn = fn

    def __repr__(self):
        return f"NativeBody({self.fn!r})"


class MethodOverload:
    """One arm of a Method: a receiver-class signature (one entry per
    dispatch position; None means "empty type" / wildcard, matching any
    receiver there) plus the fn body and the scope it closes over."""

    def __init__(self, signature: tuple, node, closure: dict):
        self.signature = signature
        self.node = node
        self.closure = closure

    def __repr__(self):
        sig = ", ".join(c.name if c is not None else "*" for c in self.signature)
        return f"MethodOverload([{sig}])"


class Method:
    """A message name's generic function: every overload registered under
    that name, whether from a plain `fn name(...)` promoted into method
    form, an external `fn [Cls, ...] name(...)`, or a class-body method
    (which registers itself here too - see eval_stmt's ClassDef handling)."""

    def __init__(self, name: str):
        self.name = name
        self.overloads: list[MethodOverload] = []

    def add_overload(self, signature: tuple, node, closure: dict) -> None:
        for i, existing in enumerate(self.overloads):
            if existing.signature == signature:
                self.overloads[i] = MethodOverload(signature, node, closure)
                return
        self.overloads.append(MethodOverload(signature, node, closure))

    def __repr__(self):
        return f"Method({self.name!r}, {len(self.overloads)} overload(s))"


class BoundMessage:
    """The result of `recv ! name` with no call parens: a receiver-bound,
    already-dispatch-resolved callable (per doc/language-spec.md: "The `!`
    operator creates a closure... it may be stored"). Calling it invokes
    the already-chosen overload with `this` bound to the original
    receiver(s), same as calling `recv ! name(...)` directly would."""

    def __init__(self, receivers: list, overload: MethodOverload):
        self.receivers = receivers
        self.overload = overload

    def __repr__(self):
        return f"BoundMessage({self.overload!r})"


class Module:
    """A loaded wyrm source file: its own top-level namespace, plus whatever
    submodules have been imported off of it so far (`import std::io`
    registers `io` here on `std`, mirroring Python's own module.submodule
    attribute behavior)."""

    def __init__(self, name: str, path: str, ctx: dict, is_package: bool):
        self.name = name          # "::"-joined path, e.g. "std::io"
        self.path = path          # filesystem path to the .wy file loaded
        self.ctx = ctx             # the module's own top-level namespace
        self.is_package = is_package
        self.submodules: dict = {}

    def __repr__(self):
        return f"Module({self.name!r})"


_module_cache: dict = {}


def clear_module_cache() -> None:
    """Forget every module loaded so far - mainly for test isolation."""
    _module_cache.clear()


def populate_globals(ctx: dict) -> None:
    """Seeds a fresh scope with the globals every piece of wyrm code should
    see, regardless of whether it's the top-level script (see cli.py) or a
    module loaded via `import` (see import_module below) - currently just
    the __-prefixed low-level I/O primitives (__open/__read/__write/...
    and __STDIN/__STDOUT/__STDERR). A module gets its own fresh `ctx` (see
    import_module), so without calling this for it too, code like
    corelib/std/io.wy's `__write(__STDOUT, value)` would see `__write` as
    undefined even though the top-level script's scope has it."""
    from wypoc import wyrm_builtins, wyrm_io

    wyrm_io.install(ctx)
    wyrm_builtins.install(ctx)


def import_module(path_segments, roots=None) -> Module:
    """Loads (or returns the already-cached) module for a `mod::sub::leaf`
    path. Parent packages are loaded first (so `import std::io` runs
    std/__init__.wy before std/io.wy) and register the child on their
    `.submodules`, matching Python's own import order and behavior."""
    path_segments = tuple(path_segments)
    key = "::".join(path_segments)
    if key in _module_cache:
        return _module_cache[key]

    parent = None
    if len(path_segments) > 1:
        parent = import_module(path_segments[:-1], roots)

    resolved = wyrm_modules.resolve_module_file(path_segments, roots)
    if resolved is None:
        searched = roots if roots is not None else wyrm_modules.search_paths()
        raise ImportError(f"no module named {key!r} (searched: {', '.join(searched)})")
    file_path, is_package = resolved

    with open(file_path) as f:
        src = f.read()
    tree = parse(src)

    module_ctx = Scope()
    populate_globals(module_ctx)
    mod = Module(key, file_path, module_ctx, is_package)
    _module_cache[key] = mod  # cache before eval so circular imports don't infinite-loop
    eval_program(tree, module_ctx)

    if parent is not None:
        parent.submodules[path_segments[-1]] = mod
    return mod


def eval_using(stmt: ast.Using, ctx: dict) -> None:
    """`using mod` bulk-imports every top-level name from `mod` into the
    current scope; `using alias = mod::name` imports one name, aliased. The
    unaliased `using mod::name` form is ambiguous at the syntax level (see
    wyrm.gram's note on `Using`) - resolved here by trying the whole path as
    a module first, and falling back to path[:-1]::path[-1] if that fails."""
    path = stmt.path
    if stmt.alias is not None:
        if len(path) < 2:
            raise ImportError(f"using {stmt.alias} = {'::'.join(path)} needs a module::name path")
        mod = import_module(path[:-1])
        bind_new(stmt.alias, lookup(path[-1], mod.ctx), ctx)
        return
    try:
        mod = import_module(path)
    except ImportError:
        if len(path) < 2:
            raise
        mod = import_module(path[:-1])
        bind_new(path[-1], lookup(path[-1], mod.ctx), ctx)
        return
    for name, var in mod.ctx.items():
        bind_new(name, unwrap(var), ctx)


class ReturnSignal(Exception):
    """Unwinds a function body back to call_function on `return`."""

    def __init__(self, value):
        self.value = value


class BreakSignal(Exception):
    """Unwinds a loop body back to the nearest enclosing while/for on `break`."""


class ContinueSignal(Exception):
    """Unwinds a loop body back to the nearest enclosing while/for on `continue`."""


def _safe_div(a, b):
    """a / b, except division by zero is a WyrmError value (catchable via
    try/catch) rather than a raw Python exception - see
    doc/language-spec.md's "Errors / RAII" `this.result = try num / den`
    example, which relies on this producing a proper error value."""
    try:
        return operator.truediv(a, b)
    except ZeroDivisionError:
        return wyrm_builtins.error("division by zero")


def _safe_mod(a, b):
    try:
        return operator.mod(a, b)
    except ZeroDivisionError:
        return wyrm_builtins.error("modulo by zero")


BINOPS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": _safe_div,
    "%": _safe_mod,
    "**": operator.pow,
    "&": operator.and_,
    "|": operator.or_,
    "^": operator.xor,
    "<": operator.lt,
    ">": operator.gt,
    "<=": operator.le,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
    "<=>": lambda a, b: (a > b) - (a < b),
    "in": lambda a, b: (chr(a) in b) if isinstance(b, str) and isinstance(a, int) and not isinstance(a, bool) else (a in b),
    "and": lambda a, b: a and b,
    "or": lambda a, b: a or b,
}


def _unescape(body: str) -> str:
    out = []
    i = 0
    table = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0"}
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            out.append(table.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def eval_string_literal(text: str) -> str:
    """Strip a Str node's raw token text down to its Python value."""
    if text.startswith('"""'):
        return _unescape(text[3:-2])
    if text[:1] in ("R", "r") and '"' in text:
        # Raw string: R"(...)"  or  R"tag(...)tag"
        open_paren = text.index("(")
        close_paren = text.rindex(")")
        return text[open_paren + 1:close_paren]
    return _unescape(text[1:-1])


CHAR_NAMES = {
    "newline": "\n",
    "space": " ",
    "tab": "\t",
    "return": "\r",
    "backspace": "\b",
    "formfeed": "\f",
    "null": "\0",
}


def eval_char_literal(text: str) -> int:
    """A Char node's raw token text (`\\a`, `\\newline`, ...) down to its
    u32 codepoint - same representation string indexing already produces
    (see eval_expr's ast.Index case: `"7"[0]` -> 55, not "7"), so a char
    literal and an indexed string char are interchangeable values."""
    body = text[1:]
    if len(body) == 1:
        return ord(body)
    if body in CHAR_NAMES:
        return ord(CHAR_NAMES[body])
    raise SyntaxError(f"unknown character name {text!r}")


def eval_number_literal(text: str):
    if text.lower().startswith("0x"):
        return int(text, 16)
    if any(c in text for c in ".eE") and not text.lower().startswith("0x"):
        return float(text)
    return int(text)


def unwrap(value):
    return value.value if isinstance(value, Variable) else value


class _UnsetType:
    """The value of a `static foo: int` (declare, no initializer) binding
    until something actually assigns it - distinct from `nil`/None, which
    is a perfectly good real value. lookup() below refuses to hand this
    out, matching doc/language-spec.md's "Undefined variables will
    generate an error if attempted to be evaluated"."""

    def __repr__(self):
        return "<unset>"


UNSET = _UnsetType()


def lookup(name: str, ctx: dict):
    if name not in ctx:
        raise NameError(f"undefined variable {name!r}")
    value = unwrap(ctx[name])
    if value is UNSET:
        raise NameError(f"variable {name!r} is declared but has no value yet")
    return value


def is_defined(name: str, ctx: dict) -> bool:
    """`defined('foo)`/`?=`'s "already defined and not of error type" check
    (see doc/language-spec.md's Variables section)."""
    if name not in ctx:
        return False
    value = unwrap(ctx[name])
    if value is UNSET:
        return False
    return not wyrm_builtins.is_error(value)


def bind(name: str, value, ctx: dict) -> None:
    existing = ctx.get(name)
    if isinstance(existing, Variable):
        existing.value = value
    else:
        ctx[name] = Variable(value)


def bind_new(name: str, value, ctx: dict) -> None:
    """Bind a fresh Variable regardless of what (if anything) already
    exists under `name` - used for parameter binding, which always
    shadows rather than mutating an outer variable of the same name."""
    ctx[name] = Variable(value)


def expose(ctx: dict, name: str, value) -> None:
    """Hand a plain Python value to wyrm code under `name`, exactly as if
    wyrm code had written `name = value` itself. `value` doesn't need to be
    callable - call_value's fallback (see below) calls anything Python
    considers callable, so a plain function/method/lambda/callable object
    just works as a wyrm function once exposed; non-callables work as
    ordinary bound variables.

        display = lambda v: print(v)
        expose(ctx, "display", display)
        eval_program(parse('display("hi")'), ctx)   # prints hi
    """
    bind_new(name, value, ctx)


def expose_all(ctx: dict, **values) -> None:
    """expose() for several names at once, e.g.:
        expose_all(ctx, display=print, sqrt=math.sqrt, pi=math.pi)
    """
    for name, value in values.items():
        expose(ctx, name, value)


def builtin(ctx: dict, name: str = None):
    """Decorator form of expose(): registers the function under `name` (or
    its own __name__ if omitted) and returns it unchanged, so it stays a
    perfectly ordinary Python function/callable to the rest of your code.

        @builtin(ctx, "display")
        def _display(v):
            print(v)
    """
    def decorator(fn):
        expose(ctx, name or fn.__name__, fn)
        return fn
    return decorator


def eval_args(arg_nodes, ctx: dict):
    """Evaluate a Call's raw argument nodes into (positional, kwargs)."""
    positional = []
    kwargs = {}
    for a in arg_nodes:
        if isinstance(a, ast.Kwarg):
            kwargs[a.name] = eval_expr(a.value, ctx)
        elif isinstance(a, ast.SpreadPos):
            positional.extend(eval_expr(a.value, ctx))
        elif isinstance(a, ast.SpreadKw):
            kwargs.update(eval_expr(a.value, ctx))
        else:
            positional.append(eval_expr(a, ctx))
    return positional, kwargs


def _static_store_for(node) -> dict:
    """The persistent Variable-storage dict for a fn/co's `static` locals -
    tied to the AST node itself (i.e. "the symbol definition", per
    doc/language-spec.md's Variables section) rather than to any one call's
    local_ctx, which is rebuilt fresh every call. Lazily attached to the
    node on first use; every call after that shares the same dict."""
    store = getattr(node, "_static_store", None)
    if store is None:
        store = {}
        node._static_store = store
    return store


def _bind_params(node, local_ctx: dict, positional, kwargs, display_name: str) -> None:
    """Binds a fn/co's params/*args/**kwargs into local_ctx (already seeded
    with whatever closure/this/slots the caller wants visible). Shared by
    _bind_params_and_run (ordinary fn/message calls, which also run the
    body immediately) and instantiate_coroutine (which binds params up
    front but only runs the body lazily, on first next()/send())."""
    kwargs = dict(kwargs)
    positional = list(positional)
    local_ctx["__statics__"] = _static_store_for(node)

    plain_params = []
    var_positional_name = None
    var_keyword_name = None
    for p in node.params:
        if isinstance(p, ast.VarPositional):
            var_positional_name = p.name
        elif isinstance(p, ast.VarKeyword):
            var_keyword_name = p.name
        else:
            plain_params.append(p)

    pos_index = 0
    for p in plain_params:
        if pos_index < len(positional):
            value = positional[pos_index]
            pos_index += 1
        elif p.name in kwargs:
            value = kwargs.pop(p.name)
        elif p.default is not None:
            value = eval_expr(p.default, local_ctx)
        else:
            raise TypeError(f"{display_name}() missing required argument: {p.name!r}")
        bind_new(p.name, value, local_ctx)

    leftover_positional = positional[pos_index:]
    if var_positional_name is not None:
        bind_new(var_positional_name, tuple(leftover_positional), local_ctx)
    elif leftover_positional:
        raise TypeError(
            f"{display_name}() takes {len(plain_params)} positional argument(s) "
            f"but {len(positional)} were given"
        )

    if var_keyword_name is not None:
        bind_new(var_keyword_name, dict(kwargs), local_ctx)
    elif kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TypeError(f"{display_name}() got unexpected keyword argument(s): {unexpected}")


def _bind_params_and_run(node, local_ctx: dict, positional, kwargs, display_name: str):
    """Shared by call_function and call_overload: binds params (see
    _bind_params) then runs the body and unwinds ReturnSignal."""
    _bind_params(node, local_ctx, positional, kwargs, display_name)
    try:
        eval_block(node.body, local_ctx)
    except ReturnSignal as ret:
        return ret.value
    return None


def call_function(fn: Function, positional, kwargs):
    local_ctx = fn.closure.child()
    return _bind_params_and_run(fn.node, local_ctx, positional, kwargs, fn.name or "<lambda>")


def call_overload(overload: MethodOverload, receivers: list, positional, kwargs):
    """Calls one resolved Method overload with `this` bound to the
    receiver (or the receiver tuple, for multi-dispatch). For a single
    ClassInstance receiver, its slot Variables are also seeded directly
    into local scope, so the body can read/write them by bare name (e.g.
    `radius`) as well as via `this.radius` - see doc/language-spec.md's
    "slot name directly accesses the internal storage"."""
    this_value = receivers[0] if len(receivers) == 1 else tuple(receivers)
    if isinstance(overload.node, NativeBody):
        return overload.node.fn(this_value, *positional, **kwargs)
    if isinstance(overload.node, ast.CoDef):
        # A message-dispatched coroutine (`co [Cls] name(...)`, invoked via
        # `recv ! name(...)`) - construct the CoroutineInstance rather than
        # running a body, exactly like calling a bare `co` does (see
        # instantiate_coroutine/call_value's Coroutine branch).
        return instantiate_coroutine(overload.node, overload.closure, positional, kwargs, this_value)
    local_ctx = overload.closure.child()
    if len(receivers) == 1 and isinstance(receivers[0], ClassInstance):
        local_ctx.update(receivers[0].attrs)
    bind_new("this", this_value, local_ctx)
    return _bind_params_and_run(overload.node, local_ctx, positional, kwargs, overload.node.name)


def call_value(func, positional, kwargs):
    if isinstance(func, Class):
        return instantiate(func, positional, kwargs)
    if isinstance(func, Function):
        return call_function(func, positional, kwargs)
    if isinstance(func, Coroutine):
        return instantiate_coroutine(func.node, func.closure, positional, kwargs, None)
    if isinstance(func, BoundMessage):
        return call_overload(func.overload, func.receivers, positional, kwargs)
    if isinstance(func, wyrm_builtins.PrimitiveType):
        if kwargs or len(positional) != 1:
            raise TypeError(f"{func.name}() takes exactly one positional argument")
        return func.cast(positional[0])
    if callable(func):
        return func(*positional, **kwargs)
    raise TypeError(f"{func!r} is not callable")


def _class_distance(cls: Class, target: Class, seen: frozenset = frozenset()) -> "int | None":
    """Steps from `cls` up its (possibly multiple-)inheritance graph to
    `target`, or None if `target` isn't an ancestor. 0 = exact match."""
    if cls is target:
        return 0
    if cls in seen:
        return None
    seen = seen | {cls}
    best = None
    for base in cls.bases:
        d = _class_distance(base, target, seen)
        if d is not None and (best is None or d + 1 < best):
            best = d + 1
    return best


def resolve_overload(method: "Method", receivers: list) -> MethodOverload:
    """Picks the overload matching `receivers` by class (or a wildcard
    "empty type" position), most-specific-wins, left to right: candidates
    are ranked by (distance at position 0, distance at position 1, ...),
    with a wildcard position always losing to any real-class match there
    (see MethodOverload/Method docstrings) - Python's own tuple comparison
    already implements exactly that positional tie-breaking order."""
    n = len(receivers)
    ranked = []
    for ov in method.overloads:
        if len(ov.signature) != n:
            continue
        distances = []
        ok = True
        for constraint, recv in zip(ov.signature, receivers):
            if constraint is None:
                distances.append(_WILDCARD_DISTANCE)
                continue
            recv_cls = recv.cls if isinstance(recv, ClassInstance) else None
            d = _class_distance(recv_cls, constraint) if recv_cls is not None else None
            if d is None:
                ok = False
                break
            distances.append(d)
        if ok:
            ranked.append((tuple(distances), ov))
    if not ranked:
        shapes = ", ".join(f"[{len(ov.signature)}]" for ov in method.overloads)
        raise TypeError(
            f"no overload of {method.name!r} matches {n} receiver(s) "
            f"(known overload arities: {shapes or 'none'})"
        )
    ranked.sort(key=lambda pair: pair[0])
    best_distances, best = ranked[0]
    ties = [ov for distances, ov in ranked if distances == best_distances]
    if len(ties) > 1:
        raise TypeError(f"ambiguous overload for {method.name!r}: {len(ties)} equally-specific matches")
    return best


_WILDCARD_DISTANCE = float("inf")


def _try_resolve_overload(method: "Method", receivers: list) -> "MethodOverload | None":
    """Like resolve_overload, but None (rather than raising) when nothing
    matches - used by instantiate() for `init`, which is optional: a class
    with no applicable `init` overload anywhere in its ancestry just skips
    construction-time dispatch instead of erroring, unless the caller
    passed constructor arguments (see instantiate())."""
    try:
        return resolve_overload(method, receivers)
    except TypeError:
        return None


def dispatch_message(name: str, receivers: list, args_node, ctx: dict):
    """Shared by `recv ! name(...)`, `recv ! name`, and `(a, b) ! name(...)`:
    looks up the generic function, resolves the best-matching overload for
    the given receivers, and either calls it immediately (args_node is a
    list, even an empty one for `name()`) or returns a BoundMessage
    (args_node is None, i.e. `recv ! name` with no call parens)."""
    method = lookup(name, ctx)
    if not isinstance(method, Method):
        raise TypeError(f"{name!r} is not a message/method (got {method!r})")
    overload = resolve_overload(method, receivers)
    if args_node is None:
        return BoundMessage(receivers, overload)
    positional, kwargs = eval_args(args_node, ctx)
    return call_overload(overload, receivers, positional, kwargs)


def register_native_method(name: str, fn, ctx: dict, arity: int = 1) -> None:
    """Registers `fn` (a plain Python callable) as a wildcard message
    overload under `name`, so it's callable via `recv ! name(...)` even
    though `recv` isn't a ClassInstance - used for builtin per-value
    methods like str's substr, which have no Class to hang a real `fn [Cls]
    name(...)` overload on. `arity` is the number of dispatch positions
    (receivers) the wildcard should match; almost always 1."""
    existing = unwrap(ctx[name]) if name in ctx else None
    method = existing if isinstance(existing, Method) else Method(name)
    if method is not existing:
        bind(name, method, ctx)
    method.add_overload((None,) * arity, NativeBody(fn), {})


def _lookup_class(name: str, ctx: dict) -> Class:
    cls = lookup(name, ctx)
    if not isinstance(cls, Class):
        raise TypeError(f"{name!r} is not a class (used as a method dispatch type)")
    return cls


def register_overload(name: str, signature, node, closure: dict, target_ctx: dict) -> None:
    """Defines or extends the generic function bound to `name` in
    target_ctx. `signature=None` means a plain (non-method) `fn`: it stays
    an ordinary Function unless/until `name` is (or becomes) a Method, at
    which point per doc/language-spec.md it's "promoted" - the existing
    plain function becomes that Method's wildcard/"empty type" overload.
    A wildcard's dispatch arity (how many receivers it matches) isn't
    something a bare `fn name(...)` states explicitly - it's derived from
    whichever `[Cls, ...]` signature is triggering the promotion (or, if
    a bare `fn` is redefined after the Method already exists, from one of
    its existing overloads), since that's the arity actually in use."""
    existing = unwrap(target_ctx[name]) if name in target_ctx else None
    if signature is None and not isinstance(existing, Method):
        bind(name, Function(name, node, closure), target_ctx)
        return
    if isinstance(existing, Method):
        method = existing
    else:
        method = Method(name)
        if isinstance(existing, Function):
            wildcard_arity = len(signature) if signature is not None else 1
            method.add_overload((None,) * wildcard_arity, existing.node, existing.closure)
        bind(name, method, target_ctx)
    if signature is None:
        wildcard_arity = len(method.overloads[0].signature) if method.overloads else 1
        signature = (None,) * wildcard_arity
    method.add_overload(signature, node, closure)


def _zero_value(type_expr: "ast.TypeExpr | None"):
    """A slot's implicit default when it has none declared, per
    doc/language-spec.md's "Basic Classes": bool -> false, int/uint -> 0,
    everything else (float, GC types, or no type hint at all) -> nil
    (Python None throughout this POC - see e.g. instantiate's old
    no-default branch, or an unset variable's value elsewhere)."""
    if type_expr is None or not type_expr.parts:
        return None
    name = type_expr.parts[-1]
    if name == "bool":
        return False
    if name in ("int", "uint"):
        return 0
    return None


def stop_iteration() -> ClassInstance:
    """A fresh StopIteration instance - what next()/send() hand back once
    a coroutine has finished (see instantiate_coroutine/CoroutineInstance
    above)."""
    inst = ClassInstance(STOP_ITERATION_CLASS)
    inst.attrs["what"] = Variable("StopIteration")
    return inst


_PRIMITIVE_TYPE_CHECKS = {
    "nil": lambda v: v is None,
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "uint": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    "float": lambda v: isinstance(v, float),
    # No distinct symbol representation in this POC (see eval_expr's
    # ast.Symbol case) - `is sym` can't be told apart from `is str` here.
    "str": lambda v: isinstance(v, str),
    "sym": lambda v: isinstance(v, str),
    "error": lambda v: wyrm_builtins.is_error(v),
}


def _matches_type(value, type_expr: "ast.TypeExpr", ctx: dict) -> bool:
    """`value is <type_expr>` for one constraint of a (possibly unioned)
    type check - see eval_expr's ast.TypeCheck case. Primitive type names
    check the Python representation directly; anything else is looked up
    as a Class and checked by ancestry (so `x is Shape` also matches a
    Circle instance)."""
    name = type_expr.parts[-1] if type_expr.parts else None
    check = _PRIMITIVE_TYPE_CHECKS.get(name)
    if check is not None:
        return check(value)
    try:
        cls = _lookup_class(name, ctx)
    except (NameError, TypeError):
        return False
    return isinstance(value, ClassInstance) and _class_distance(value.cls, cls) is not None


def instantiate(cls: Class, positional, kwargs) -> "ClassInstance | WyrmError":
    """`Cls(...)`: builds an instance with every slot zero-valued (or its
    declared default), then dispatches to `init` (if the class or any
    ancestor defines one) with `this` bound to the new instance, exactly
    like any other message send - so an `init` inherited from a base class
    is picked up automatically by the same multi-dispatch resolution any
    other inherited method would use. If `init`'s result is an error, that
    error is returned in place of the instance (RAII - see
    doc/language-spec.md's "Errors / RAII" example).

    ERROR_CLASS is special-cased: it has no `init` (it's a builtin, not
    wyrm-defined), so `error("msg")` is handled directly here instead."""
    if cls is ERROR_CLASS:
        if kwargs or len(positional) != 1:
            raise TypeError("error(...) takes exactly one positional argument (the message)")
        return wyrm_builtins.error(positional[0])

    inst = ClassInstance(cls)
    for slot_name, (slot_def, owner) in cls.all_slots().items():
        if slot_def.default is not None:
            value = eval_expr(slot_def.default, owner.closure)
        else:
            value = _zero_value(slot_def.type)
        inst.attrs[slot_name] = Variable(value)

    method = _lookup_dunder(cls, "init")
    overload = _try_resolve_overload(method, [inst]) if method is not None else None
    if overload is not None:
        result = call_overload(overload, [inst], positional, kwargs)
        if wyrm_builtins.is_error(result):
            return result
        return inst
    if positional or kwargs:
        raise TypeError(f"{cls.name}(...) takes no arguments (no applicable 'init')")
    return inst


def _lookup_dunder(cls: Class, name: str) -> "Method | None":
    """A special method (e.g. `__iter__`) applicable to `cls`, if any class
    in scope has defined one - same lookup instantiate() uses for `init`:
    dunders register into the class's own defining scope (`cls.closure`)
    as ordinary Methods, so this is just a name lookup, not a new
    mechanism."""
    method = unwrap(cls.closure[name]) if name in cls.closure else None
    return method if isinstance(method, Method) else None


def _call_dunder(instance: ClassInstance, name: str, positional=()):
    """Dispatches `name` (e.g. `__iter__`) on `instance` the same way a
    message send would, or raises if no applicable overload exists."""
    method = _lookup_dunder(instance.cls, name)
    overload = _try_resolve_overload(method, [instance]) if method is not None else None
    if overload is None:
        raise TypeError(f"'{instance.cls.name}' object has no applicable {name}")
    return call_overload(overload, [instance], list(positional), {})


def _iter_values(value, ctx: dict):
    """Values produced by `for x in value:` - see doc/language-spec.md's
    "The 'in' statement must be an iterable. See special methods for the
    contract":

      - a ClassInstance dispatches `__iter__` first (its result - typically
        a coroutine - is what's actually iterated);
      - a CoroutineInstance is driven with next() until it finishes, so
        `for x in some_coroutine():` just drives the coroutine - the same
        idea `__iter__` hooks into, and why no separate __next__ protocol
        is needed here;
      - anything else (str/list/tuple/dict/Pair/nil/...) iterates however
        Python already knows how to iterate it.
    """
    if isinstance(value, ClassInstance):
        value = _call_dunder(value, "__iter__")
    if isinstance(value, CoroutineInstance):
        while True:
            finished, item = value._advance_raw(None)
            if finished:
                return
            yield item
        return
    try:
        iterator = iter(value)
    except TypeError:
        raise TypeError(f"'{type(value).__name__}' object is not iterable") from None
    yield from iterator


def _resolve_target_value(target, ctx: dict):
    """Reads the value a target currently denotes - used when a target is
    itself the *base* of an IndexTarget (`grid[i][j] = x`'s `grid[i]`, or
    `this.cells[i] = x`'s `this.cells`), so IndexTarget can nest on top of
    any other target shape. Mirrors the lookup rules assign_target uses
    for its own base, factored out rather than duplicated."""
    if isinstance(target, ast.NameTarget):
        return lookup(target.name, ctx)
    if isinstance(target, ast.AttrTarget):
        obj = lookup("this", ctx) if isinstance(target.base, ast.ThisRef) else lookup(target.base, ctx)
        for name in target.attrs:
            if not isinstance(obj, ClassInstance):
                raise TypeError(f"'.' access is only supported on class instances right now (got {type(obj).__name__})")
            obj = lookup(name, obj.attrs)
        return obj
    if isinstance(target, ast.IndexTarget):
        obj = _resolve_target_value(target.base, ctx)
        idx = eval_expr(target.index, ctx)
        return obj[idx]
    raise NotImplementedError(f"unsupported target base: {target}")


def assign_target(target, value, ctx: dict) -> None:
    if isinstance(target, ast.NameTarget):
        # Plain `=` (and `?=`, via eval_stmt/eval_expr) only ever assigns to
        # an already-declared name - see doc/language-spec.md's Variables
        # section ("Assigning to an undeclared name is a compile-time
        # error"). Declaring a fresh name is `var`/`:=`'s job (VarDecl - see
        # eval_stmt), not this function's.
        cell = ctx.get_cell(target.name)
        if cell is None:
            raise NameError(
                f"cannot assign to undeclared variable {target.name!r} "
                f"(declare it first with 'var' or ':=')"
            )
        cell.value = value
        return
    if isinstance(target, ast.AttrTarget):
        obj = lookup("this", ctx) if isinstance(target.base, ast.ThisRef) else lookup(target.base, ctx)
        for name in target.attrs[:-1]:
            if not isinstance(obj, ClassInstance):
                raise TypeError(f"'.' assignment is only supported on class instances right now (got {type(obj).__name__})")
            obj = lookup(name, obj.attrs)
        if not isinstance(obj, ClassInstance):
            raise TypeError(f"'.' assignment is only supported on class instances right now (got {type(obj).__name__})")
        bind(target.attrs[-1], value, obj.attrs)
        return
    if isinstance(target, ast.IndexTarget):
        # `base[index] = value` - base is itself a target (Name/Attr/
        # another Index), resolved to the actual container the same way a
        # read would be, then mutated in place via Python's own
        # __setitem__ (arrays are plain lists, dicts are plain dicts in
        # this evaluator, so this needs no new value representation).
        obj = _resolve_target_value(target.base, ctx)
        idx = eval_expr(target.index, ctx)
        if isinstance(obj, (str, tuple)):
            raise TypeError(f"'{type(obj).__name__}' object does not support item assignment (immutable)")
        try:
            obj[idx] = value
        except TypeError:
            raise TypeError(f"'{type(obj).__name__}' object does not support item assignment") from None
        return
    raise NotImplementedError(f"unsupported assignment target: {target}")


def eval_expr(node, ctx: dict):
    if isinstance(node, ast.Num):
        return eval_number_literal(node.value)
    if isinstance(node, ast.Str):
        return eval_string_literal(node.value)
    if isinstance(node, ast.Char):
        return eval_char_literal(node.value)
    if isinstance(node, ast.Bool):
        return node.value
    if isinstance(node, ast.Symbol):
        return node.name
    if isinstance(node, ast.Name):
        return lookup(node.id, ctx)
    if isinstance(node, ast.Tuple):
        return tuple(eval_expr(item, ctx) for item in node.items)
    if isinstance(node, ast.Array):
        return [eval_expr(item, ctx) for item in node.items]
    if isinstance(node, ast.Dict):
        return {eval_expr(e.key, ctx): eval_expr(e.value, ctx) for e in node.entries}
    if isinstance(node, ast.Pair):
        # $[1, 2, 3] -> Pair(1, Pair(2, Pair(3, NIL))) - always a proper
        # list; an improper one is built with the `pair` constructor
        # instead (see doc/language-spec.md's "Pair List" section). Built
        # right to left so each element becomes the car of a fresh cons
        # cell around whatever's already been built (see
        # wyrm_builtins.Pair/cons).
        result = wyrm_builtins.NIL
        for item in reversed(node.elements):
            result = wyrm_builtins.Pair(eval_expr(item, ctx), result)
        return result
    if isinstance(node, ast.UnaryOp):
        val = eval_expr(node.operand, ctx)
        if node.op == "neg":
            return -val
        if node.op == "not":
            return not val
        raise NotImplementedError(f"unsupported unary op: {node.op}")
    if isinstance(node, ast.BinOp):
        left = eval_expr(node.left, ctx)
        right = eval_expr(node.right, ctx)
        try:
            return BINOPS[node.op](left, right)
        except KeyError:
            raise NotImplementedError(f"unsupported binary op: {node.op}")
    if isinstance(node, ast.Lambda):
        return Function(None, node, ctx)
    if isinstance(node, ast.Call):
        func = eval_expr(node.func, ctx)
        positional, kwargs = eval_args(node.args, ctx)
        return call_value(func, positional, kwargs)
    if isinstance(node, ast.Index):
        obj = eval_expr(node.obj, ctx)
        idx = eval_expr(node.index, ctx)
        if isinstance(obj, str):
            # No unicode support yet - just the ascii code point (a u32) of
            # the single indexed char, per doc/language-spec.md's Lookup
            # Operator ("7"[0] -> the u32 for the char '7', not the char).
            return ord(obj[idx])
        return obj[idx]
    if isinstance(node, ast.Scope):
        obj = eval_expr(node.obj, ctx)
        if isinstance(obj, Module):
            if node.name in obj.submodules:
                return obj.submodules[node.name]
            return lookup(node.name, obj.ctx)
        raise NotImplementedError(f"'::' is only supported on modules right now (got {type(obj).__name__})")
    if isinstance(node, ast.Attr):
        obj = eval_expr(node.obj, ctx)
        if isinstance(obj, CoroutineInstance) and node.name == "value":
            # "The return statement from a coroutine is stored in the
            # 'value' attribute. An active coroutine will return an error
            # when accessing [it]" - doc/language-spec.md's Coroutines.
            if not obj._finished:
                return wyrm_builtins.error(f"coroutine {obj.node.name!r} has not finished")
            return obj._result
        if isinstance(obj, ClassInstance):
            return lookup(node.name, obj.attrs)
        raise NotImplementedError(f"'.' is only supported on class instances right now (got {type(obj).__name__})")
    if isinstance(node, ast.Message):
        receiver = eval_expr(node.obj, ctx)
        return dispatch_message(node.name, [receiver], node.args, ctx)
    if isinstance(node, ast.MessageTupleExpr):
        receivers = [eval_expr(e, ctx) for e in node.items]
        return dispatch_message(node.name, receivers, node.args, ctx)
    if isinstance(node, ast.ThisRef):
        return lookup("this", ctx)
    if isinstance(node, ast.Defined):
        return is_defined(node.symbol.name, ctx)
    if isinstance(node, ast.SetIfUnset):
        if isinstance(node.target, ast.Name):
            # `?=` operates on an already-declared variable (typically a
            # forward `var`, or a `static`) - it never implicitly declares
            # one; see doc/language-spec.md's Variables section.
            cell = ctx.get_cell(node.target.id)
            if cell is None:
                raise NameError(
                    f"'?=' target {node.target.id!r} is not declared "
                    f"(declare it first with 'var' or 'static')"
                )
            if cell.value is not UNSET and not wyrm_builtins.is_error(cell.value):
                return cell.value
            value = eval_expr(node.value, ctx)
            cell.value = value
            return value
        # Best-effort for a non-plain-name target (e.g. `this.x ?= 5`):
        # short-circuit if already a real, non-error value; otherwise just
        # evaluate the RHS. Writing back through an arbitrary lvalue
        # expression here isn't modeled by this POC evaluator.
        try:
            current = eval_expr(node.target, ctx)
        except NameError:
            current = UNSET
        if current is not UNSET and not wyrm_builtins.is_error(current):
            return current
        return eval_expr(node.value, ctx)
    if isinstance(node, ast.TypeCheck):
        value = eval_expr(node.value, ctx)
        return any(_matches_type(value, t, ctx) for t in node.types)
    if isinstance(node, ast.Yield):
        if node.from_:
            sub = eval_expr(node.value, ctx)
            return _yield_from(sub)
        value = eval_expr(node.value, ctx) if node.value is not None else None
        return _yield_value(value)
    if isinstance(node, ast.Try):
        value = eval_expr(node.value, ctx)
        if wyrm_builtins.is_error(value):
            raise ReturnSignal(value)
        return value
    if isinstance(node, ast.Catch):
        value = eval_expr(node.value, ctx)
        if not wyrm_builtins.is_error(value):
            return value
        if isinstance(node.handler, ast.Return):
            handler_value = eval_expr(node.handler.value, ctx) if node.handler.value is not None else None
            raise ReturnSignal(handler_value)
        return eval_expr(node.handler, ctx)
    raise NotImplementedError(f"cannot evaluate {type(node).__name__}")


def eval_stmt(stmt, ctx: dict) -> None:
    if isinstance(stmt, ast.Import):
        import_module(stmt.path)  # loads the whole chain, caching every prefix along the way
        bind_new(stmt.path[0], _module_cache[stmt.path[0]], ctx)
        return
    if isinstance(stmt, ast.FromImport):
        mod = import_module(stmt.path)
        for name in stmt.names:
            bind_new(name, lookup(name, mod.ctx), ctx)
        return
    if isinstance(stmt, ast.Using):
        eval_using(stmt, ctx)
        return
    if isinstance(stmt, ast.VarDecl):
        # `var`, and the `:=` shorthand (see actions.make_assignment_stmt) -
        # always declares fresh name(s) in the *current* scope; error if any
        # target is already declared there (shadowing an enclosing scope's
        # name of the same name is fine - see doc/language-spec.md's
        # Variables section). No initializer -> each target is Unset.
        if stmt.values is None:
            for t in stmt.targets:
                ctx.declare_new(t.name, UNSET)
            return
        values = [eval_expr(v, ctx) for v in stmt.values]
        if len(stmt.targets) == 1 and len(values) == 1:
            ctx.declare_new(stmt.targets[0].name, values[0])
        elif len(stmt.targets) == len(values):
            for t, v in zip(stmt.targets, values):
                ctx.declare_new(t.name, v)
        else:
            raise ValueError(
                f"declaration target/value count mismatch: "
                f"{len(stmt.targets)} targets, {len(values)} values"
            )
        return
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
        if stmt.op == "?=":
            # Each target/value pair short-circuits independently: a
            # NameTarget whose current value isn't an error keeps it
            # without evaluating that value expression at all - see
            # doc/language-spec.md's "set if unset" operator. The target
            # must already be declared (a forward `var`, or `static`).
            if len(targets) != len(stmt.values):
                raise ValueError(
                    f"assignment target/value count mismatch: "
                    f"{len(targets)} targets, {len(stmt.values)} values"
                )
            for t, v_node in zip(targets, stmt.values):
                if isinstance(t, ast.NameTarget):
                    cell = ctx.get_cell(t.name)
                    if cell is None:
                        raise NameError(
                            f"'?=' target {t.name!r} is not declared "
                            f"(declare it first with 'var' or 'static')"
                        )
                    if cell.value is not UNSET and not wyrm_builtins.is_error(cell.value):
                        continue
                    cell.value = eval_expr(v_node, ctx)
                else:
                    assign_target(t, eval_expr(v_node, ctx), ctx)
            return
        values = [eval_expr(v, ctx) for v in stmt.values]
        if len(targets) == 1 and len(values) == 1:
            assign_target(targets[0], values[0], ctx)
        elif len(targets) == len(values):
            for t, v in zip(targets, values):
                assign_target(t, v, ctx)
        else:
            raise ValueError(
                f"assignment target/value count mismatch: "
                f"{len(targets)} targets, {len(values)} values"
            )
        return
    if isinstance(stmt, ast.StaticDecl):
        store = ctx.get("__statics__")
        if store is None:
            raise TypeError("'static' is only valid inside a fn/co body (or a class body)")
        if stmt.name not in store:
            value = eval_expr(stmt.default, ctx) if stmt.default is not None else UNSET
            store[stmt.name] = Variable(value)
        # Alias, don't copy: later plain assignment (`foo = foo + 1`) or
        # `?=` mutates this same Variable in place (see bind()), so the
        # write is visible through `store` on the next call too.
        ctx[stmt.name] = store[stmt.name]
        return
    if isinstance(stmt, ast.ExprStmt):
        eval_expr(stmt.value, ctx)
        return
    if isinstance(stmt, ast.Yield):
        # A bare `yield 1` / `yield from sub()` line (yield_stmt, not
        # wrapped in ExprStmt - see wyrm.gram). Same suspension as the
        # expression form; the yielded-back-in value is just discarded.
        if stmt.from_:
            _yield_from(eval_expr(stmt.value, ctx))
        else:
            _yield_value(eval_expr(stmt.value, ctx) if stmt.value is not None else None)
        return
    if isinstance(stmt, ast.Pass):
        return
    if isinstance(stmt, ast.Return):
        raise ReturnSignal(eval_expr(stmt.value, ctx) if stmt.value is not None else None)
    if isinstance(stmt, ast.If):
        # Each branch body is its own child scope, so a `var`/`:=` inside
        # one branch never leaks into (or collides with a redeclare error
        # against) another branch or the enclosing scope - see
        # doc/language-spec.md's Variables section.
        if eval_expr(stmt.cond, ctx):
            eval_block(stmt.body, ctx.child())
            return
        for clause in stmt.elifs:
            if eval_expr(clause.cond, ctx):
                eval_block(clause.body, ctx.child())
                return
        if stmt.orelse is not None:
            eval_block(stmt.orelse, ctx.child())
        return
    if isinstance(stmt, ast.Continue):
        raise ContinueSignal()
    if isinstance(stmt, ast.Break):
        raise BreakSignal()
    if isinstance(stmt, ast.While):
        # A fresh child scope per iteration, matching `for`'s per-iteration
        # scoping below - a `var`/`:=` local declared in the body doesn't
        # collide with the same declaration on the next iteration.
        while eval_expr(stmt.cond, ctx):
            try:
                eval_block(stmt.body, ctx.child())
            except ContinueSignal:
                continue
            except BreakSignal:
                break
        return
    if isinstance(stmt, ast.For):
        # The loop variable is itself a declaration, fresh per iteration and
        # scoped to that iteration's body - a closure created in one
        # iteration captures that iteration's own binding, and the name is
        # entirely out of scope once the loop ends (an outer variable of the
        # same name is shadowed for the loop's duration, unaffected by it) -
        # see doc/language-spec.md's "for" section.
        broke = False
        last_iter_scope = None
        for item in _iter_values(eval_expr(stmt.iter, ctx), ctx):
            iter_scope = ctx.child()
            iter_scope.declare_new(stmt.var, item)
            last_iter_scope = iter_scope
            try:
                eval_block(stmt.body, iter_scope)
            except ContinueSignal:
                continue
            except BreakSignal:
                broke = True
                break
        if not broke and stmt.orelse is not None:
            # The loop variable stays visible (bound to the final
            # iteration's value, or Unset if the body never ran at all) for
            # the `else` clause specifically - see doc/language-spec.md.
            else_scope = last_iter_scope.child() if last_iter_scope is not None else ctx.child()
            if last_iter_scope is None:
                else_scope.declare_new(stmt.var, UNSET)
            eval_block(stmt.orelse, else_scope)
        return
    if isinstance(stmt, ast.FnDef):
        signature = None if stmt.class_target is None else tuple(
            _lookup_class(n, ctx) for n in stmt.class_target
        )
        register_overload(stmt.name, signature, stmt, ctx, ctx)
        return
    if isinstance(stmt, ast.CoDef):
        # A tagged `co [Cls, ...] name(...)` is a message like a tagged
        # `fn` (dispatched via `recv ! name(...)`, see call_overload's
        # CoDef branch); a bare `co name(...)` is a plain callable
        # coroutine factory instead (see call_value's Coroutine branch).
        if stmt.class_target is None:
            bind(stmt.name, Coroutine(stmt.name, stmt, ctx), ctx)
        else:
            signature = tuple(_lookup_class(n, ctx) for n in stmt.class_target)
            register_overload(stmt.name, signature, stmt, ctx, ctx)
        return
    if isinstance(stmt, ast.ClassDef):
        bases = [eval_expr(b, ctx) for b in stmt.bases]
        for base, base_expr in zip(bases, stmt.bases):
            if not isinstance(base, Class):
                raise TypeError(f"base class {base_expr!r} does not name a class (got {base!r})")
        cls = Class(stmt.name, stmt, ctx, bases)
        bind(stmt.name, cls, ctx)
        # A class-body method/coroutine is equivalent to `fn`/`co`
        # `[ThisClass] name(...)` defined externally - see
        # doc/language-spec.md's Messages section.
        for method_name, method_node in cls.methods.items():
            register_overload(method_name, (cls,), method_node, cls.closure, ctx)
        for co_name, co_node in cls.coroutines.items():
            register_overload(co_name, (cls,), co_node, cls.closure, ctx)
        return
    raise NotImplementedError(f"cannot evaluate statement {type(stmt).__name__}")


def eval_block(stmts, ctx: dict) -> None:
    """Runs a list of statements directly in the given scope - `ctx` is
    already whatever scope this block belongs to (a fresh child Scope for
    an if/while/for/fn body - see eval_stmt/call_function/etc - or the
    caller's own scope for a straight-through sequence like a module or
    function's top-level statements)."""
    for stmt in stmts:
        eval_stmt(stmt, ctx)


def eval_program(program: ast.Program, ctx: dict) -> dict:
    """Runs `program` in scope `ctx`, mutating it in place (returning the
    same object) - `ctx` may be an ordinary dict (e.g. a fresh `{}` from a
    caller that hasn't been introduced to Scope, as several tests and
    cli.py do): wrapped in a real root Scope for the run, then copied back
    so the caller's own object still reflects the resulting top-level
    bindings afterward."""
    scope = ctx if isinstance(ctx, Scope) else Scope()
    if scope is not ctx:
        scope.update(ctx)
    eval_block(program.body, scope)
    if scope is not ctx:
        ctx.update(scope)
    return ctx
