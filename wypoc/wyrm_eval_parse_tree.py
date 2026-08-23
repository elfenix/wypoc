"""Tree-walking evaluator prototype over the wypoc AST (wypoc/ast_nodes.py).

Leans entirely on Python's own runtime for values (int/float/str/bool) and
scoping (a plain dict passed in by the caller, just like Python's own
eval()/exec()) rather than modeling wyrm's value representation. Types are
ignored completely - no checking, no coercion beyond what Python does
naturally. This is a proof-of-concept for statement/expression evaluation,
not a real interpreter.

eval_expr/eval_stmt (native, plain recursive functions) are still what
`eval_program` and every other pre-existing entry point call directly, and
are still what actually executes a Wyrm program - but neither is where a
Wyrm-level *call* (a `fn` call, a `recv ! name(...)` message send, or a
`Cls(...)` construction) recurses any more. call_function/call_overload/
instantiate all run through _run_driver, an explicit, heap-bounded stack of
suspended generators - _eval_expr_gen/_eval_stmt_impl_gen/
_run_scoped_block_gen and friends, full "_gen" twins of eval_expr/
_eval_stmt_impl/run_scoped_block that recurse into each other via
`yield from` (safe - that nesting mirrors the expression/block's own
already-bounded syntax structure, not Wyrm-level call-chain depth) but
`yield` a _TailCall (see its docstring) at any Call/Message/
MessageTupleExpr whose target is a plain Function, an ordinary
FnDef-backed message overload, or a Class, instead of recursing into
call_value. _run_driver turns each of those into a new stack entry rather
than a new Python frame - so a call *anywhere* a Wyrm program can write one
(a bare `return f(n - 1)`, buried in `f(n - 1) + 1`, as a `while` condition,
a collection literal, an argument to another call, ...) is what stays
heap-bounded rather than C-stack-bounded, not just the bare-tail-call shape
an earlier version of this module supported. eval_expr/eval_stmt's own
native recursion is only ever reached once _eval_expr_gen/_eval_stmt_impl_gen
have already handled everything that node type needs generator-aware
treatment - see their own docstrings for the (short) list of exceptions,
e.g. `ClassDef`'s base-class expressions, deemed not worth the added
surface. See _run_driver's own docstring for the full mechanism, and
_MAX_DRIVER_DEPTH for the resulting depth's own generous ceiling.
"""
import operator
import sys
import threading
from dataclasses import fields as _dc_fields

from wypoc import ast_nodes as ast
from wypoc import cache as cache_mod
from wypoc import sexpr
from wypoc import wyrm_builtins
from wypoc import wyrm_modules
from wypoc.parse import parse

# Debugger hooks (wypoc/dap/debugger.py) - both None, and both checked
# before use, so a plain `wyrm`/REPL run pays nothing and behaves exactly
# as before. `_call_stack` is a debugger's own list of Frame objects that
# this module pushes to/pops from around every call - _run_driver's own
# push/pop (see its docstring) for a trampolined call, or a plain
# try/finally at the few remaining native call sites (CoroutineInstance._run,
# eval_program) - so it reflects the true Wyrm-level call chain either way,
# not just whatever's on Python's own stack. `_stmt_hook` is called at the
# top of every `eval_stmt`/`_eval_stmt_gen`, the one place every statement
# everywhere (module top level, fn/co bodies, if/while/for bodies - loops
# re-enter it once per iteration via run_scoped_block/_run_scoped_block_gen)
# passes through.
_call_stack: "list | None" = None
_stmt_hook = None
_exception_hook = None
_last_captured_exc = None

# _run_driver's explicit call stack (see call_function/call_overload/
# instantiate) is heap-bounded, not C-stack-bounded, so a genuinely
# recursive Wyrm program no longer risks a native RecursionError/segfault -
# but an *infinitely* recursive one (a bug, not deep-but-finite recursion)
# would otherwise just grow this list forever until the process runs out of
# memory. This is a generous infinite-recursion guard, not a realistic
# ceiling on legitimate recursion depth - _run_driver raises a plain,
# catchable-by-Python-except RecursionError once the explicit stack reaches
# it, same exception type (and similar message) a native stack overflow
# would have raised before this module gained a trampoline.
_MAX_DRIVER_DEPTH = 1_000_000


class Frame:
    """One entry in a debugger's explicit call stack - a wyrm-level analog
    of a Python stack frame. `_run_driver`'s own explicit stack (see its
    docstring) tracks a trampolined call's suspended generator and nothing
    more; Frame is the separate, debugger-facing record of the same
    call - name, scope, and (once a statement inside it has run) source
    position - kept distinct so this module never has to import the
    debugger package (wypoc/dap/debugger.py) to use it - only the
    reverse."""

    __slots__ = ("name", "scope", "current_pos")

    def __init__(self, name: str, scope: "Scope"):
        self.name = name
        self.scope = scope
        self.current_pos = None

    def __repr__(self):
        return f"Frame({self.name!r})"


class Variable:
    """A bound name in a wyrm scope: holds whatever value it currently has."""

    def __init__(self, value=None, immutable=False):
        self.value = value
        self.immutable = immutable

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
        self.defers: list = []  # (on_error, body) pairs, in declaration order - see run_scoped_block

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

    def __str__(self):
        # What `str(f)` shows wyrm code, as against the debugging repr above:
        # the language's own spelling of the thing. wyrm_builtins._format
        # falls back to str() for values it has no case of its own for.
        return f"<fn {self.name}>" if self.name else "<fn>"


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
        self.signals: dict = {}
        self.methods: dict = {}
        self.coroutines: dict = {}
        for member in node.body:
            if isinstance(member, ast.SlotDef):
                self.slots[member.name] = member
            elif isinstance(member, ast.SignalDef):
                self.signals[member.name] = member
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

    def all_signals(self) -> dict:
        """(name -> SignalDef) in MRO order, base classes first - the
        signal counterpart to all_slots(), used the same way: to seed every
        signal a class inherits or declares with its own fresh SignalValue
        at construction time (see instantiate/_instantiate_gen)."""
        result: dict = {}
        for base in self.bases:
            result.update(base.all_signals())
        result.update(self.signals)
        return result

    def __repr__(self):
        bases = ", ".join(b.name for b in self.bases)
        return f"Class({self.name!r}{f'({bases})' if bases else ''})"

    def __str__(self):
        return f"<class {self.name}>"


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


# The root class of every syntax tree that crosses into wyrm code. A
# decorator is `fn [TreeBase] name(...)`, so the tree it receives has to be
# an instance of a real Class for ordinary dispatch (resolve_overload) to
# find it - which is what makes a decorator's source identical whether the
# tree is this interpreter's AST or a self-hosted parser's own node objects.
# The AST node itself lives in the instance's `__tree` slot; `sexpr()`
# unwraps it (see sexpr_value below), so the box is an indirection in
# practice and the identity in effect.
TREE_BASE_CLASS = Class(
    "TreeBase",
    ast.ClassDef("TreeBase", [], [ast.SlotDef("__tree", None, None, None)]),
    {},
    [],
)

_TREE_SLOT = "__tree"


def tree_box(node) -> "ClassInstance":
    """`node` boxed as a TreeBase instance - what a decorator is handed and
    what `foo::$ast` evaluates to."""
    inst = ClassInstance(TREE_BASE_CLASS)
    inst.attrs[_TREE_SLOT] = Variable(node)
    return inst


def is_tree_box(value) -> bool:
    return isinstance(value, ClassInstance) and value.cls is TREE_BASE_CLASS


def parse_source(source: str):
    """`parse(source)` - `source`'s own tree(s), boxed exactly like `$ast`
    (see tree_box) so `sexpr()` unwraps them the same way:
    `sexpr(parse("v := 5"))` reads the `vardecl` node directly, the same
    shape a decorator receives. A syntax error raises (a SyntaxError, same
    as any other malformed wyrm), letting a wyrm-level `try` catch it like
    any other builtin misuse.

    `source` may hold more than one statement; a single statement unboxes
    to its own tree (the common case - inspecting one snippet), while more
    than one answers a list of boxed trees, one per statement. Blank source
    answers nil, there being no tree to show."""
    tree = parse(source, filename="<parse>")
    boxes = [tree_box(stmt) for stmt in tree.body]
    if not boxes:
        return wyrm_builtins.NIL
    if len(boxes) == 1:
        return boxes[0]
    return boxes


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


class SignalValue:
    """One instance's own subscriber list for a `signal name(...)` member
    (see Class.all_signals/_instantiate_gen, which seed one of these per
    signal into every new ClassInstance's attrs, exactly where a slot's
    value would live). Reached from wyrm code as an ordinary attribute
    (`obj.name`, or bare `name`/`this.name` inside a method - the same
    lookup a slot gets), and connected to via `obj.name ! connect(cb)` (see
    register_native_method's "connect" registration in populate_globals):
    a message on a value with no real Class behind it, the same trick
    str's substr/list's append use."""

    def __init__(self, name: str):
        self.name = name
        self.subscribers: list = []

    def __repr__(self):
        return f"SignalValue({self.name!r}, {len(self.subscribers)} subscriber(s))"


class Future:
    """What `task expr` (ast.TaskSpawn) evaluates to - a placeholder for a
    result some other thread will eventually deliver, via `resolve_with`/
    `fail_with`. `resolve(fut)` (wyrm_builtins.py) is the wyrm-visible way
    to block on `.wait()`.

    Only `_dispatch_remote_message`'s async branch (see its docstring)
    ever actually resolves one via a background thread - this is not a
    general-purpose promise type, just enough machinery for that one case."""

    def __init__(self):
        self._event = threading.Event()
        self._value = None
        self._error: "Exception | None" = None

    def resolve_with(self, value) -> None:
        self._value = value
        self._event.set()

    def fail_with(self, exc: Exception) -> None:
        self._error = exc
        self._event.set()

    def wait(self):
        """Blocks until resolved, then returns the value - or re-raises
        whatever the resolving thread failed with."""
        self._event.wait()
        if self._error is not None:
            raise self._error
        return self._value

    def __repr__(self):
        state = "pending" if not self._event.is_set() else (
            "failed" if self._error is not None else "resolved")
        return f"Future({state})"


# `task expr`'s push/pop stack of in-flight Futures (see ast.TaskSpawn's
# eval case) - thread-local like _current_coroutine above, since a `task`
# block's push and pop both happen on whichever thread is evaluating it,
# and _dispatch_remote_message reads its top from that same thread, never
# across threads (the background thread it may spin only ever holds a
# Future reference captured *before* starting, not the stack itself).
_task_stack = threading.local()


def _current_task_future() -> "Future | None":
    """The Future a `remote ! name(...)` reached right now should resolve
    asynchronously into, or None if it's not inside any `task expr` on
    this thread - see _dispatch_remote_message."""
    stack = getattr(_task_stack, "frames", None)
    return stack[-1] if stack else None


def _push_task_future(future: "Future") -> None:
    stack = getattr(_task_stack, "frames", None)
    if stack is None:
        stack = []
        _task_stack.frames = stack
    stack.append(future)


def _pop_task_future() -> None:
    _task_stack.frames.pop()


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

    def __str__(self):
        return f"<co {self.name}>"


_current_coroutine = threading.local()

# A coroutine body runs on its own dedicated thread (see CoroutineInstance
# below), but unlike wypoc/parse.py's own dedicated parsing thread, it was
# started with whatever threading.stack_size() defaults to - so a deeply
# recursive coroutine body had *less* headroom than top-level code, not
# more. Pairs a bigger C stack with a bigger recursion-limit ceiling exactly
# like parse.py's _run_with_big_stack, for the same reason: raising the
# ceiling without the matching stack size trades a catchable RecursionError
# for an uncatchable segfault. Serialized with a lock since
# threading.stack_size() is a process-global knob affecting whatever thread
# is created next, not just this one.
_CO_STACK_SIZE = 64 * 1024 * 1024  # 64 MiB, matching parse.py
_CO_RECURSION_LIMIT = 20000
_co_thread_lock = threading.Lock()


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
        # See _CO_RECURSION_LIMIT's docstring: paired with this thread's
        # bigger C stack (set by _advance_raw before starting it), not
        # touched here. Restored in `finally` so the ceiling stays raised
        # only for the lifetime of this thread's own execution.
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, _CO_RECURSION_LIMIT))
        # Runs on this instance's own daemon thread, but push/pop stay
        # correct without extra locking: the GIL plus the _to_co/_to_caller
        # handoff mean only one of {caller thread, this thread} is ever
        # truly executing (see the class docstring), and that handoff
        # already brackets this call, so _call_stack is never touched by
        # two threads "at once" even though it's one shared list.
        if _call_stack is not None:
            _call_stack.append(Frame(self.node.name, self.local_ctx))
        try:
            run_scoped_block(self.node.body, self.local_ctx)
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
            sys.setrecursionlimit(old_limit)
            if _call_stack is not None:
                _call_stack.pop()
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
            with _co_thread_lock:
                previous_stack_size = threading.stack_size()
                threading.stack_size(_CO_STACK_SIZE)
                try:
                    self._thread = threading.Thread(target=self._run, daemon=True)
                    self._thread.start()
                finally:
                    threading.stack_size(previous_stack_size)
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

    def __init__(self, name: str, owner: "str | None" = None):
        self.name = name
        self.overloads: list[MethodOverload] = []
        # The module that first defined this message - purely diagnostic
        # (an _AmbiguousMessage collision names its two candidates with it),
        # not consulted for dispatch or extension.
        self.owner = owner

    def add_overload(self, signature: tuple, node, closure: dict) -> None:
        for i, existing in enumerate(self.overloads):
            if existing.signature == signature:
                self.overloads[i] = MethodOverload(signature, node, closure)
                return
        self.overloads.append(MethodOverload(signature, node, closure))

    def __repr__(self):
        return f"Method({self.name!r}, {len(self.overloads)} overload(s))"


class _AmbiguousMessage:
    """Placeholder `message_table` entry for a name that two distinct,
    independently-owned messages both landed on unqualified in the same
    scope (e.g. two `import ...::*`s that happen to collide) - see
    doc/addendum.md's "Message identity across modules". Unqualified use of
    the name is an error pointing at `mod::name`; `mod::name` itself
    resolves straight against the naming module's own message_table and
    never sees this placeholder at all."""

    def __init__(self, first: str, second: str):
        self.first = first
        self.second = second

    def __repr__(self):
        return f"_AmbiguousMessage({self.first!r}, {self.second!r})"


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

    def __init__(self, name: str, path: str, ctx: dict, is_package: bool, tree: "ast.Program | None" = None):
        self.name = name          # "::"-joined path, e.g. "std::io"
        self.path = path          # filesystem path to the .wy file loaded
        self.ctx = ctx             # the module's own top-level namespace
        self.is_package = is_package
        self.submodules: dict = {}
        # The parsed source, kept around (not just its side effects in
        # `ctx`) so help() (wyrm_builtins.py) can read the module's own doc
        # comment and walk its top-level defs - `ctx` alone can't do that,
        # since populate_globals binds builtins/prelude names into the same
        # dict level as the module's own top-level names.
        self.tree = tree

    def __repr__(self):
        return f"Module({self.name!r})"

    def __str__(self):
        return f"<module {self.name}>"


_module_cache: dict = {}


def clear_module_cache() -> None:
    """Forget every module loaded so far - mainly for test isolation."""
    _module_cache.clear()


_PRELUDE_TREE: "ast.Program | None" = None


def _prelude_tree() -> "ast.Program":
    """corelib/prelude.wy (real wyrm source - e.g. `co range(...)`),
    parsed once and reused - see populate_globals below."""
    global _PRELUDE_TREE
    if _PRELUDE_TREE is None:
        path = wyrm_modules.prelude_path()
        with open(path) as f:
            _PRELUDE_TREE = parse(f.read())
    return _PRELUDE_TREE


def populate_globals(ctx: dict, name: str = "__main__") -> None:
    """Seeds a fresh scope with the globals every piece of wyrm code should
    see, regardless of whether it's the top-level script (see cli.py) or a
    module loaded via `import` (see import_module below): the __-prefixed
    low-level I/O primitives (__open/__read/__write/... and __STDIN/
    __STDOUT/__STDERR), the Python-level builtins (car/cdr/substr/...), and
    corelib/prelude.wy's real-wyrm-source globals (e.g. `co range(...)`).
    A module gets its own fresh `ctx` (see import_module), so without
    calling this for it too, code like corelib/std/io.wy's
    `__write(__STDOUT, value)` would see `__write` as undefined even
    though the top-level script's scope has it. Also seeds `__dynamic__`
    (see import_module) to `true`, so code that checks it doesn't hit a
    NameError when run somewhere other than a `dynamic`/`static`-tagged
    import - the top-level script itself, or a module loaded directly by
    something other than eval_import.

    Also seeds `__name__`, mirroring Python: the entry script sees
    `__name__ == "__main__"` (the default here - every caller other than
    import_module is populating a top-level-ish scope, e.g. the REPL, the
    debugger, or a bare `import`less script), while a module reached via
    `import`/`import static` sees its own fully-qualified `"::"`-joined
    name (e.g. `"std::io"`), passed in explicitly by import_module below.
    Unlike Python, `__name__` is always fully qualified - there's no
    equivalent of a script run directly by relative filename ending up
    with a bare, unqualified `__name__`."""
    from wypoc import wyrm_builtins, wyrm_dbus, wyrm_io, wyrm_nng, wyrm_socket, wyrm_sys, wyrm_term

    wyrm_io.install(ctx)
    wyrm_builtins.install(ctx)
    wyrm_dbus.install(ctx)
    wyrm_term.install(ctx)
    wyrm_socket.install(ctx)
    wyrm_nng.install(ctx)
    wyrm_sys.install(ctx)
    eval_program(_prelude_tree(), ctx)
    expose(ctx, "__dynamic__", True)
    expose(ctx, "__name__", name)


def import_module(path_segments, roots=None, dynamic: bool = True) -> Module:
    """Loads (or returns the already-cached) module for a `mod::sub::leaf`
    path. Parent packages are loaded first (so `import std::io` runs
    std/__init__.wy before std/io.wy) and register the child on their
    `.submodules`, matching Python's own import order and behavior.

    `dynamic` is exposed to the module's own top level as `__dynamic__` -
    `false` for `import static a::b`, `true` for a plain `import a::b`
    (see eval_import). This interpreter has no real way to run only a
    module's declarations and defer its side-effecting statements (see
    _adopt_messages' docstring) - `import static` still runs the whole
    body, immediately, same as a plain import - so `__dynamic__` is a
    stopgap: a module that wants to behave differently when it's reached
    through `static` (skip a side effect a decorator-only load shouldn't
    trigger, say) can guard that code with `if __dynamic__:` itself, by
    hand, until a real load/run split exists.

    Because the module only actually runs once - the first import of it,
    whichever form that was, per `_module_cache` - `__dynamic__` reflects
    *that* import, not necessarily the one on screen: `import static a; ...;
    import a` leaves `a.__dynamic__` false throughout, since the static
    import got there first and the second is just a cache hit. Each import
    (cached or not) still refreshes `__dynamic__` to its own `dynamic`, so
    module code that reads it lazily (inside a fn, rather than only at
    top level) sees whichever import happened most recently - known
    incomplete, acceptable for the POC."""
    path_segments = tuple(path_segments)
    key = "::".join(path_segments)
    if key in _module_cache:
        mod = _module_cache[key]
        bind("__dynamic__", dynamic, mod.ctx)
        return mod

    parent = None
    if len(path_segments) > 1:
        parent = import_module(path_segments[:-1], roots, dynamic)

    resolved = wyrm_modules.resolve_module_file(path_segments, roots)
    if resolved is None:
        searched = roots if roots is not None else wyrm_modules.search_paths()
        raise ImportError(f"no module named {key!r} (searched: {', '.join(searched)})")
    file_path, is_package = resolved

    # Same AST cache cli.py uses for the top-level script (wypoc/cache.py) -
    # a hit (source unchanged since last run, by mtime) skips tokenizing and
    # PEG-parsing entirely. Imported modules are exactly where this pays
    # off most: corelib files like wyrm::parser::parser (600+ lines) get
    # re-parsed on every single `import`, in every process, regardless of
    # how many times their *content* has actually changed - measured at
    # ~10-20% of total runtime for a script that imports the self-hosted
    # parser. A miss just parses as usual and populates the cache for next
    # time; any cache failure (unwritable directory, corrupt entry, ...) is
    # invisible here since cache.load/save never raise.
    tree = cache_mod.load(file_path)
    if tree is None:
        with open(file_path) as f:
            src = f.read()
        tree = parse(src)
        cache_mod.save(file_path, tree)

    module_ctx = Scope()
    populate_globals(module_ctx, name=key)
    bind("__dynamic__", dynamic, module_ctx)
    mod = Module(key, file_path, module_ctx, is_package, tree=tree)
    _module_cache[key] = mod  # cache before eval so circular imports don't infinite-loop
    eval_program(tree, module_ctx)

    if parent is not None:
        parent.submodules[path_segments[-1]] = mod
    return mod


def eval_import(stmt: ast.Import, ctx: dict) -> None:
    """Implements every `import` form doc/language-spec.md's "Modules and
    Imports" section describes - the old `using` keyword's bulk, aliased,
    and listed imports are all `import` forms now (see ast_nodes.Import):

      import a::b::c            - binds root `a` (for `::`-chain
                                   navigation, e.g. `a::b::c` still works
                                   afterward) *and* leaf `c` (bare access)
      import a::b::c as x       - like above, but the leaf is bound as
                                   `x` instead of bare `c`
      import a::b::(x, y as z)  - pulls x/y (as x/z) out of a::b, checking
                                   the variable namespace then the message
                                   one for each (see _import_one)
      import a::b::*            - bulk-imports every name (variable and
                                   message) out of a::b, like `using a::b`
                                   used to
      import a::b::* except x   - same, skipping the given name(s)

    A path may continue from an existing binding instead of a fresh
    top-level module search: after `import std as _std`, `import _std::io`
    resolves by continuing from what `_std` already names, not by
    searching for a real top-level module called `_std`.

    The bare `import a::b::c` form's leaf is ambiguous at the syntax level
    (see wyrm.gram's note on `import_stmt`/`module_path`) - resolved here
    by trying the whole path as a module first, falling back to
    path[:-1]::path[-1] (module::symbol) if that fails."""
    prior = unwrap(lookup(stmt.path[0], ctx)) if stmt.path[0] in ctx else None
    if isinstance(prior, Module):
        real_path = prior.name.split("::") + list(stmt.path[1:])
        bind_root = False  # stmt.path[0] already names something; nothing fresh to bind
    else:
        real_path = list(stmt.path)
        bind_root = True

    dynamic = not stmt.static
    if stmt.wildcard or stmt.items is not None:
        mod = import_module(real_path, dynamic=dynamic)
        if stmt.wildcard:
            excluded = set(stmt.except_names or ())
            for name, var in mod.ctx.items():
                # str: skips mod.ctx's message_table sentinel entry. The
                # `__`-prefixed names (__name__, __dynamic__, __ARGS, the
                # low-level __open/__STDIN/... primitives populate_globals
                # installs into every scope) are per-scope implementation
                # details, not module-declared exports - copying them would
                # clobber the importing scope's own __name__/__dynamic__
                # with the imported module's, which is exactly the bug a
                # transitive chain of wildcard imports (a::* importing
                # b::* importing c::*, say) used to produce: the deepest
                # module's __name__ would win everywhere up the chain.
                if (isinstance(name, str) and not name.startswith("__")
                        and name not in excluded):
                    bind_new(name, unwrap(var), ctx)
            dest_messages = message_table(ctx)
            local_owner = None
            try:
                local_owner = lookup("__name__", ctx)
            except NameError:
                pass
            for name, method in message_table(mod.ctx).items():
                if name not in excluded:
                    _merge_message(dest_messages, name, method, local_owner=local_owner)
        else:
            for item in stmt.items:
                _import_one(item.name, item.alias or item.name, mod, ctx)
        return

    try:
        target = import_module(real_path, dynamic=dynamic)
    except ImportError:
        if len(real_path) < 2:
            raise
        parent = import_module(real_path[:-1], dynamic=dynamic)
        _import_one(stmt.path[-1], stmt.alias or stmt.path[-1], parent, ctx)
    else:
        bind_new(stmt.alias or stmt.path[-1], target, ctx)
        if stmt.static:
            _adopt_messages(target, ctx)

    if bind_root and len(stmt.path) > 1:
        bind_new(stmt.path[0], _module_cache[stmt.path[0]], ctx)


def _merge_message(destination: dict, name: str, method, dest_name: "str | None" = None,
                    local_owner: "str | None" = None) -> None:
    """Brings `method` (an imported module's canonical `Method` object - by
    reference, never copied) into `destination`'s (an importer's
    message_table) namespace under `dest_name` (or `name` if the import
    isn't renaming it) - the shared step behind `import a::b::*`,
    `import static`, and a single named `import a::b::(msg)`.

    A second import of the *same* canonical object (re-importing, or two
    wildcard imports that both reach it) is a no-op. A name this importing
    module (`local_owner`) already defines itself always wins - imports
    never shadow local definitions, matching register_overload's "a local
    fn always wins over an ambiguous import" rule for the symmetric case.
    Otherwise, two *different* canonical objects landing on the same
    destination name is the collision doc/addendum.md's "Message identity
    across modules" makes a hard error to use unqualified: the entry
    becomes an _AmbiguousMessage, resolved only by `mod::name`."""
    dest_name = dest_name or name
    existing = destination.get(dest_name)
    if (isinstance(method, Method) and method.owner is None
            and isinstance(existing, Method) and existing.owner is None):
        # Both sides are native-registered (owner is only ever set for a
        # user-code `fn` - see register_overload), e.g. `append`: every
        # module's own populate_globals() already installed an equivalent,
        # separately-built Method for it, so there's nothing to import and
        # no real collision - just two identical builtins, not two distinct
        # protocols that happen to share a name.
        return
    if existing is None:
        destination[dest_name] = method
    elif existing is method:
        pass
    elif isinstance(existing, Method) and local_owner is not None and existing.owner == local_owner:
        pass  # the importing module's own definition wins
    elif isinstance(existing, _AmbiguousMessage):
        pass  # already ambiguous; a third collision doesn't change the diagnosis
    else:
        first = existing.owner if isinstance(existing, Method) else None
        second = method.owner if isinstance(method, Method) else None
        destination[dest_name] = _AmbiguousMessage(first or "?", second or "?")


def _adopt_messages(mod: "Module", ctx: dict) -> None:
    """Pulls `mod`'s messages into `ctx`'s message namespace - what
    `import static a::b` adds over a plain `import a::b`.

    A decorator is reached as a *selector* rather than through the module
    binding (`@traced`, never `@decolib::traced`: a selector is never a
    path), so a plain import leaves it unreachable no matter that the module
    is loaded. Adopting the table is what closes that, and it is the same
    thing `import a::b::*` already does for the wildcard case.

    A name this module already defines wins: a static import brings in
    behaviour, and quietly replacing a local definition with an imported one
    would be the wrong way round.

    Note what this does *not* do: the `static` constraint itself - no
    closures over a live environment, no coroutines, no reaching into the
    importing module's state - is recorded, not enforced. This interpreter
    runs a module's top level when it is imported either way, so the
    constraint buys nothing here that it buys in a compiled implementation.
    `import_module`'s `__dynamic__` is the stopgap for that: a module can
    check it and skip a side effect itself, by hand, since the interpreter
    won't skip it for them."""
    destination = message_table(ctx)
    local_owner = None
    try:
        local_owner = lookup("__name__", ctx)
    except NameError:
        pass
    for name, method in message_table(mod.ctx).items():
        _merge_message(destination, name, method, local_owner=local_owner)


def _import_one(name: str, dest_name: str, mod: "Module", ctx: dict) -> None:
    """Imports a single name from `mod` into `ctx` as `dest_name`, checking
    the variable namespace first and the message namespace second - see
    message_table's docstring on why they're separate tables now."""
    if name in mod.ctx:
        bind_new(dest_name, lookup(name, mod.ctx), ctx)
        return
    method = message_table(mod.ctx).get(name)
    if method is None:
        raise NameError(f"undefined variable {name!r}")
    local_owner = None
    try:
        local_owner = lookup("__name__", ctx)
    except NameError:
        pass
    _merge_message(message_table(ctx), name, method, dest_name, local_owner=local_owner)


class ReturnSignal(Exception):
    """Unwinds a function body back to call_function on `return`."""

    def __init__(self, value):
        self.value = value


class BreakSignal(Exception):
    """Unwinds a loop body back to the nearest enclosing while/for on `break`."""


class ContinueSignal(Exception):
    """Unwinds a loop body back to the nearest enclosing while/for on `continue`."""


class EndSignal(Exception):
    """Raised by the `end()` builtin (wyrm_builtins.py) - unwinds all the
    way out of eval_program, back to whichever entry point is running the
    current thread/process's own top level (cli.py's main() for the main
    process; wyrm_remote.py's `_remote_main` for a `thread`-spawned one -
    see doc/language-spec.md's forthcoming "wyrm routines" section for the
    lifecycle rule this is part of: main falling off the end of its top
    level implicitly `exit()`s, unless it called `end()` first, in which
    case it just stops - same as any other thread/process finishing its own
    work does)."""


class ExitSignal(Exception):
    """Raised by the `exit()` builtin (wyrm_builtins.py) - unwinds out of
    eval_program the same way EndSignal does, but the entry point that
    catches it terminates the whole process (`sys.exit(code)`) rather than
    just stopping cleanly."""

    def __init__(self, code: int = 0):
        self.code = code


class WyrmLocatedError:
    """Mixed into a *shadow* of whatever Python exception class the
    evaluator (or a builtin) raised deep inside a statement - `_locate_exc`
    below builds and caches one such shadow per original exception type on
    first use (`_LOCATED_EXC_CLASSES`), e.g. `TypeError` gets shadowed by a
    dynamically-built `type("LocatedTypeError", (WyrmLocatedError,
    TypeError), {})`, rather than this module hand-maintaining a parallel
    type per TypeError/NameError/ValueError/IndexError/... A `TypeError`
    escaping evaluation is still, literally, a TypeError once shadowed -
    `except TypeError` and `pytest.raises(TypeError, ...)` elsewhere in
    this codebase (and in wyrm code's own `try`/`catch`-adjacent Python
    interop, however unlikely) keep working exactly as if this feature
    didn't exist - while `.loc` (a Span, see ast_nodes.py) and `.original`
    (the plain, unshadowed exception `raise ... from` chains onto) ride
    along too, and `__str__` renders both.

    A parse failure already gets a location for free
    (SyntaxError.lineno/offset - see wyrm_tokenizer.TokenizeError); nothing
    played that role for the vast majority of exceptions the evaluator
    raises once parsing is done, so eval_stmt/_eval_stmt_gen - the one
    place every statement everywhere passes through (see eval_stmt's own
    docstring) - shadow whatever escaped like this before it continues up
    to cli.py/repl.py/a debugger.

    Only the *first* (innermost) eval_stmt/_eval_stmt_gen frame to see a
    raw exception shadows it - same "first sighting" rule
    `_last_captured_exc` already enforced for the debugger's exception
    hook, reused here so an exception crossing several nested statements
    (an inner `if`'s body, a call's own body, ...) keeps the innermost,
    most precise location rather than the outermost one it happens to
    unwind through last."""

    def __init__(self, loc, original: Exception):
        self.loc = loc
        self.original = original
        self.args = original.args

    def __str__(self):
        base = f"{type(self.original).__name__}: {self.original}"
        if not self.loc:
            return base
        line, col = self.loc[0], self.loc[1]
        return f"{base} (line {line}, col {col + 1})"


_LOCATED_EXC_CLASSES: dict = {}


def _located_class(original_cls: type) -> type:
    """The `WyrmLocatedError`-shadow of `original_cls`, building (and
    caching) it on first request - see `WyrmLocatedError`'s docstring."""
    shadow = _LOCATED_EXC_CLASSES.get(original_cls)
    if shadow is None:
        shadow = type(f"Located{original_cls.__name__}",
                       (WyrmLocatedError, original_cls), {})
        _LOCATED_EXC_CLASSES[original_cls] = shadow
    return shadow


# The two most common non-parse mistakes - a typo'd name (NameError) and a
# value of the wrong shape (TypeError) - pre-built and importable by name,
# so e.g. `except WyrmNameError` is spellable without reaching for
# `_located_class`. Every other exception type still gets shadowed too
# (see _locate_exc/_located_class); these are just its two busiest tenants.
WyrmNameError = _located_class(NameError)
WyrmTypeError = _located_class(TypeError)


# Exceptions eval_stmt/_eval_stmt_gen never shadow: wyrm's own control-flow
# unwinding (return/break/continue/end/exit) - a WyrmLocatedError instance
# is excluded by its own isinstance check in _locate_exc below, not listed
# here, since (unlike these) it isn't one fixed type.
_UNWRAPPED_EXC_TYPES = (ReturnSignal, BreakSignal, ContinueSignal, EndSignal, ExitSignal)


def _locate_exc(stmt, exc: BaseException) -> BaseException:
    """The exception eval_stmt/_eval_stmt_gen should actually propagate for
    `exc`, having just escaped evaluating `stmt`: `exc` itself, unchanged,
    for wyrm's own control-flow signals, an already-shadowed
    WyrmLocatedError, or anything that isn't an Exception at all
    (KeyboardInterrupt, SystemExit, GeneratorExit - not the evaluator's
    concern to annotate); a new instance of `exc`'s own type shadowed with
    a `.loc`/`.original`, tagged with `stmt.pos`, otherwise."""
    if (not isinstance(exc, Exception) or isinstance(exc, _UNWRAPPED_EXC_TYPES)
            or isinstance(exc, WyrmLocatedError)):
        return exc
    return _located_class(type(exc))(stmt.pos, exc)


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


def _safe_shift(shift):
    """`<<` / `>>`, with a negative shift count answering an error value
    rather than raising - the same treatment `/` gives division by zero, and
    for the same reason: it's a runtime condition wyrm code should be able
    to `catch`, not an interpreter fault."""

    def apply(a, b):
        try:
            return shift(a, b)
        except ValueError:
            return wyrm_builtins.error("negative shift count")

    return apply


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
    "<<": _safe_shift(operator.lshift),
    ">>": _safe_shift(operator.rshift),
    "<": operator.lt,
    ">": operator.gt,
    "<=": operator.le,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
    "<=>": lambda a, b: (a > b) - (a < b),
    "in": lambda a, b: (chr(a) in b) if isinstance(b, str) and isinstance(a, int) and not isinstance(a, bool) else (a in b),
    # "and"/"or" are short-circuited directly in eval_expr's ast.BinOp case
    # (they must not evaluate their right operand eagerly), so they have no
    # entry here.
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
    lower = text.lower()
    if lower.startswith("0x"):
        return int(text, 16)
    if lower.startswith("0b"):
        return int(text, 2)
    if any(c in text for c in ".eE"):
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


_MESSAGES_KEY = object()  # sentinel: never equal to any wyrm name (always a str)


def message_table(ctx: dict) -> dict:
    """The message namespace belonging to `ctx`'s module: a `name -> Method`
    table held entirely apart from the variable namespace `ctx` itself
    indexes, per doc/language-spec.md's "Properties and messages occupy
    separate namespaces" (and the reference implementation's parallel
    module-globals/message tables) - so a variable and a message may share
    a name without contending for a cell, and `!` (dispatch_message) never
    resolves a plain binding by accident.

    Stored under a non-string sentinel key so it's invisible to every
    string-keyed wyrm lookup (lookup/bind/declare_new/`in`/iteration all
    take/see only str names) while still surviving plain dict copies -
    Scope wrapping in eval_program, eval_import's bind_new loop, etc. - the
    same way an ordinary binding would. Walks up Scope.parent (module code
    always runs in a scope chain rooted at its module_ctx) so every nested
    scope shares its module's one table; a plain, unparented dict (as several
    tests and wyrm_builtins.install() use directly) just holds its own."""
    root = ctx
    if isinstance(ctx, Scope):
        while root.parent is not None:
            root = root.parent
    table = dict.get(root, _MESSAGES_KEY)
    if table is None:
        table = {}
        dict.__setitem__(root, _MESSAGES_KEY, table)
    return table


def lookup(name: str, ctx: dict):
    # A single ctx.get() - not the `name not in ctx` + `ctx[name]` pair this
    # used to be - since both walk the same Scope.parent chain via get_cell
    # (see Scope's docstring): the old form paid for that walk twice on
    # every lookup, the single hottest call in the interpreter.
    cell = ctx.get(name)
    if cell is None:
        raise NameError(f"undefined variable {name!r}")
    value = unwrap(cell)
    if value is UNSET:
        raise NameError(f"variable {name!r} is declared but has no value yet")
    return value


def is_defined(name: str, ctx: dict) -> bool:
    """`defined('foo)`/`?=`'s "already defined and not of error type" check
    (see doc/language-spec.md's Variables section)."""
    cell = ctx.get(name)
    if cell is None:
        return False
    value = unwrap(cell)
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


def _param_shape(node):
    """`node.params` (a fn/co/lambda's declared parameter list) sorted once
    into (plain_params, var_positional_name, var_keyword_name) and cached on
    the node - same identity-caching pattern as _static_store_for above.
    This shape is fixed by the node's own syntax, so re-deriving it on every
    single call (as _bind_params used to) was pure repeated work: a rule
    like a packrat parser's `peek`/`mark`/`advance` gets called thousands of
    times over one parse, and its param list never changes between them."""
    shape = getattr(node, "_param_shape", None)
    if shape is not None:
        return shape
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
    shape = (plain_params, var_positional_name, var_keyword_name)
    node._param_shape = shape
    return shape


def _bind_params(node, local_ctx: dict, positional, kwargs, display_name: str) -> None:
    """Binds a fn/co's params/*args/**kwargs into local_ctx (already seeded
    with whatever closure/this/slots the caller wants visible). Shared by
    _build_call_activation (ordinary fn/message calls, driven by
    _run_driver - see call_function/call_overload) and
    instantiate_coroutine (which binds params up front but only runs the
    body lazily, on first next()/send())."""
    kwargs = dict(kwargs)
    local_ctx["__statics__"] = _static_store_for(node)

    plain_params, var_positional_name, var_keyword_name = _param_shape(node)

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


class _TailCall:
    """Yielded by _eval_expr_gen at any `f(...)` / `recv ! name(...)` /
    `Cls(...)` call site whose target is a plain user Function, an ordinary
    FnDef-backed message overload, or a Class - wherever it appears in an
    expression, not just in statement-tail position (`return f(...)`,
    `f(n - 1) + 1`, `while f(n):`, an argument to another call, ... all
    reach this the same way). The driver (_run_driver) turns this into a
    new activation pushed onto its own explicit stack instead of a native
    recursive call, so a chain of such calls (however deep - self-recursion,
    mutual recursion, a decorator invoking the body it decorates, ...) grows
    a Python list, not the C stack. `build` is a zero-argument callable
    answering the _CallActivation to push - _make_call_activation for a
    plain Call, _make_overload_activation for a message send,
    _make_instantiate_activation for a construction - deferred rather than
    built eagerly so a failure while binding params (e.g. wrong argument
    count) is raised at the point _run_driver actually calls it, where the
    driver can catch it and deliver it back to the caller like any other
    exception a call raises (see _run_driver)."""

    __slots__ = ("build",)

    def __init__(self, build):
        self.build = build


class _CallActivation:
    """One entry on _run_driver's explicit stack: a suspended generator -
    _run_scoped_block_gen for a Function/overload call's body, or
    _instantiate_gen for a `Cls(...)` construction - plus whether it pushed
    its own debugger Frame (a call body always does; _instantiate_gen never
    does, matching instantiate()'s current behaviour where only the `init`
    call it may make shows up in the stack, not instantiation itself), so
    _run_driver knows whether popping this activation should pop
    _call_stack too."""

    __slots__ = ("gen", "pushed_frame")

    def __init__(self, gen, pushed_frame: bool):
        self.gen = gen
        self.pushed_frame = pushed_frame


def _eval_args_gen(arg_nodes, ctx: dict):
    """The generator-driven twin of eval_args - each argument expression is
    evaluated via `yield from _eval_expr_gen(...)` instead of a native
    `eval_expr(...)` call, so a call nested inside an argument expression
    (`f(g(x))`) is trampolined too, not just `f`'s own call."""
    positional = []
    kwargs = {}
    for a in arg_nodes:
        if isinstance(a, ast.Kwarg):
            kwargs[a.name] = yield from _eval_expr_gen(a.value, ctx)
        elif isinstance(a, ast.SpreadPos):
            spread = yield from _eval_expr_gen(a.value, ctx)
            positional.extend(spread)
        elif isinstance(a, ast.SpreadKw):
            spread = yield from _eval_expr_gen(a.value, ctx)
            kwargs.update(spread)
        else:
            value = yield from _eval_expr_gen(a, ctx)
            positional.append(value)
    return positional, kwargs


def _eval_tail_message_gen(name: str, receivers: list, args_node, ctx: dict, module: "str | None" = None):
    """Shared by _eval_expr_gen's `ast.Message`/`ast.MessageTupleExpr`
    cases (called only when there's an actual call, i.e. `args_node is not
    None`): resolves the overload and either yields a _TailCall (an
    ordinary FnDef-backed overload - the message-send analogue of a Call to
    a plain Function) or calls it immediately (NativeBody/CoDef, neither of
    which recurse into further Wyrm-level call depth the way a body of Wyrm
    statements can - see call_overload)."""
    overload = _resolve_message(name, receivers, ctx, module)
    positional, kwargs = yield from _eval_args_gen(args_node, ctx)
    if isinstance(overload.node, NativeBody):
        this_value = receivers[0] if len(receivers) == 1 else tuple(receivers)
        return overload.node.fn(this_value, *positional, **kwargs)
    if isinstance(overload.node, ast.CoDef):
        this_value = receivers[0] if len(receivers) == 1 else tuple(receivers)
        return instantiate_coroutine(overload.node, overload.closure, positional, kwargs, this_value)
    value = yield _TailCall(lambda: _make_overload_activation(overload, receivers, positional, kwargs))
    return value


def _eval_if_gen(node: "ast.If", ctx: dict):
    """The generator-driven twin of _eval_if - shared, like _eval_if
    itself, by `if` as a statement and `if` as an expression."""
    cond = yield from _eval_expr_gen(node.cond, ctx)
    if cond:
        value = yield from _run_block_gen(node.body, ctx)
        return value
    for clause in node.elifs:
        cond = yield from _eval_expr_gen(clause.cond, ctx)
        if cond:
            value = yield from _run_block_gen(clause.body, ctx)
            return value
    if node.orelse is not None:
        value = yield from _run_block_gen(node.orelse, ctx)
        return value
    return None


# ---------------------------------------------------------------------
# _eval_expr_gen's dispatch tables
# ---------------------------------------------------------------------
#
# Two tables, both indexed by node.TAG (see ast_nodes.Node.__init_subclass__)
# instead of the isinstance chain _eval_expr_gen used to open with:
#
#   _EXPR_SIMPLE_DISPATCH  a node kind that never recurses into a
#                          sub-expression or a call - Name, a literal,
#                          `this`, ... - so its handler is a *plain*
#                          function, called directly (no `yield from`, no
#                          generator object created for it).
#   _EXPR_GEN_DISPATCH     everything else - a generator function, entered
#                          via `yield from` exactly as its code did when it
#                          was inlined in _eval_expr_gen directly.
#
# The split matters because _eval_expr_gen is itself a generator function
# (it contains `yield`), so every call to it already pays one generator
# object's creation - unavoidable as long as callers write
# `yield from _eval_expr_gen(...)`. Routing a *simple* case through a
# second `yield from` (into its own generator handler) would double that
# cost for no reason; calling its handler as a plain function keeps it to
# the one unavoidable generator plus an O(1) array lookup. A *gen* case was
# already going to need its own suspension points, so no extra generator is
# introduced versus the old inlined code - dispatch just moves from an
# isinstance chain to an array index.


def _expr_name(node, ctx):
    return lookup(node.id, ctx)


def _expr_thisref(node, ctx):
    return lookup("this", ctx)


def _expr_symbol(node, ctx):
    return wyrm_builtins.Symbol(node.name)


def _expr_num(node, ctx):
    return eval_number_literal(node.value)


def _expr_str(node, ctx):
    return eval_string_literal(node.value)


def _expr_char(node, ctx):
    return eval_char_literal(node.value)


def _expr_bool(node, ctx):
    return node.value


def _expr_ellipsis(node, ctx):
    return wyrm_builtins.ELLIPSIS


def _expr_lambda(node, ctx):
    return Function(None, node, ctx)


def _expr_threadspawn(node, ctx):
    from wypoc.wyrm_remote import spawn_module_process
    return spawn_module_process(node.path)


def _expr_defined(node, ctx):
    if not isinstance(node.symbol, ast.Symbol):
        raise TypeError("defined() takes a symbol literal, e.g. defined('foo)")
    return is_defined(node.symbol.name, ctx)


def _expr_binop(node, ctx):
    if node.op == "and":
        left = yield from _eval_expr_gen(node.left, ctx)
        if not left:
            return left
        value = yield from _eval_expr_gen(node.right, ctx)
        return value
    if node.op == "or":
        left = yield from _eval_expr_gen(node.left, ctx)
        if left:
            return left
        value = yield from _eval_expr_gen(node.right, ctx)
        return value
    left = yield from _eval_expr_gen(node.left, ctx)
    right = yield from _eval_expr_gen(node.right, ctx)
    try:
        return BINOPS[node.op](left, right)
    except KeyError:
        raise NotImplementedError(f"unsupported binary op: {node.op}")


def _expr_do(node, ctx):
    value = yield from _run_block_gen(node.body, ctx)
    return value


def _expr_attr(node, ctx):
    obj = yield from _eval_expr_gen(node.obj, ctx)
    if isinstance(obj, CoroutineInstance) and node.name == "value":
        if not obj._finished:
            return wyrm_builtins.error(f"coroutine {obj.node.name!r} has not finished")
        return obj._result
    if isinstance(obj, ClassInstance):
        return lookup(node.name, obj.attrs)
    from wypoc.wyrm_remote import RemoteModule
    if isinstance(obj, RemoteModule):
        return obj.signal(node.name)
    raise NotImplementedError(f"'.' is only supported on class instances right now (got {type(obj).__name__})")


def _expr_call(node, ctx):
    func = yield from _eval_expr_gen(node.func, ctx)
    positional, kwargs = yield from _eval_args_gen(node.args, ctx)
    if isinstance(func, ContextualBuiltin):
        return func.fn(ctx, *positional, **kwargs)
    if isinstance(func, Function):
        value = yield _TailCall(lambda: _make_call_activation(func, positional, kwargs))
        return value
    if isinstance(func, Class):
        value = yield _TailCall(lambda: _make_instantiate_activation(func, positional, kwargs))
        return value
    return call_value(func, positional, kwargs)


def _expr_if(node, ctx):
    value = yield from _eval_if_gen(node, ctx)
    return value


def _expr_message(node, ctx):
    receiver = yield from _eval_expr_gen(node.obj, ctx)
    from wypoc.wyrm_remote import RemoteModule
    if isinstance(receiver, RemoteModule):
        if node.module is not None:
            raise NotImplementedError("a module-qualified message (mod::name) isn't supported on a `thread` receiver")
        return _dispatch_remote_message(receiver, node.name, node.args, ctx)
    if node.args is None:
        return BoundMessage([receiver], _resolve_message(node.name, [receiver], ctx, node.module))
    value = yield from _eval_tail_message_gen(node.name, [receiver], node.args, ctx, node.module)
    return value


def _expr_typecheck(node, ctx):
    value = yield from _eval_expr_gen(node.value, ctx)
    return any(_matches_type(value, t, ctx) for t in node.types)


def _expr_index(node, ctx):
    obj = yield from _eval_expr_gen(node.obj, ctx)
    idx = yield from _eval_expr_gen(node.index, ctx)
    if isinstance(obj, str):
        try:
            return ord(obj[idx])
        except IndexError as exc:
            return wyrm_builtins.error(str(exc))
    if isinstance(obj, dict):
        return obj.get(idx, UNSET)
    try:
        return obj[idx]
    except (IndexError, TypeError, KeyError) as exc:
        return wyrm_builtins.error(str(exc))


def _expr_decorated(node, ctx):
    expanded = expand_decorated(node, ctx, as_statement=False)
    value = yield from _eval_expr_gen(expanded, ctx)
    return value


def _expr_catch(node, ctx):
    value = yield from _eval_expr_gen(node.value, ctx)
    if not wyrm_builtins.is_error(value):
        return value
    if isinstance(node.handler, ast.Return):
        handler_value = None
        if node.handler.value is not None:
            handler_value = yield from _eval_expr_gen(node.handler.value, ctx)
        raise ReturnSignal(handler_value)
    value = yield from _eval_expr_gen(node.handler, ctx)
    return value


def _expr_tuple(node, ctx):
    items = []
    for item in node.items:
        value = yield from _eval_expr_gen(item, ctx)
        items.append(value)
    return tuple(items)


def _expr_array(node, ctx):
    items = []
    for item in node.items:
        value = yield from _eval_expr_gen(item, ctx)
        items.append(value)
    return items


def _expr_dict(node, ctx):
    result = {}
    for e in node.entries:
        key = yield from _eval_expr_gen(e.key, ctx)
        value = yield from _eval_expr_gen(e.value, ctx)
        result[key] = value
    return result


def _expr_pair(node, ctx):
    elements = []
    for item in node.elements:
        value = yield from _eval_expr_gen(item, ctx)
        elements.append(value)
    result = wyrm_builtins.NIL
    for value in reversed(elements):
        result = wyrm_builtins.Pair(value, result)
    return result


def _expr_unaryop(node, ctx):
    val = yield from _eval_expr_gen(node.operand, ctx)
    if node.op == "neg":
        return -val
    if node.op == "pos":
        return +val
    if node.op == "inv":
        return ~val
    if node.op == "not":
        return not val
    raise NotImplementedError(f"unsupported unary op: {node.op}")


def _expr_astref(node, ctx):
    target = yield from _eval_expr_gen(node.obj, ctx)
    definition = getattr(target, "node", None)
    if definition is None:
        raise TypeError(
            f"'::${node.field}' needs a fn, co or class definition "
            f"(got {type(target).__name__})"
        )
    return tree_box(definition)


def _expr_scope(node, ctx):
    obj = yield from _eval_expr_gen(node.obj, ctx)
    if isinstance(obj, Module):
        if node.name in obj.submodules:
            return obj.submodules[node.name]
        return lookup(node.name, obj.ctx)
    raise NotImplementedError(f"'::' is only supported on modules right now (got {type(obj).__name__})")


def _expr_messagetupleexpr(node, ctx):
    receivers = []
    for e in node.items:
        value = yield from _eval_expr_gen(e, ctx)
        receivers.append(value)
    if node.args is None:
        return BoundMessage(receivers, _resolve_message(node.name, receivers, ctx))
    value = yield from _eval_tail_message_gen(node.name, receivers, node.args, ctx)
    return value


def _expr_taskspawn(node, ctx):
    future = Future()
    _push_task_future(future)
    try:
        yield from _eval_expr_gen(node.expr, ctx)
    finally:
        _pop_task_future()
    return future


def _expr_setifunset(node, ctx):
    if isinstance(node.target, ast.Name):
        cell = ctx.get_cell(node.target.id)
        if cell is None:
            raise NameError(
                f"'?=' target {node.target.id!r} is not declared "
                f"(declare it first with 'var' or 'static')"
            )
        if cell.value is not UNSET and not wyrm_builtins.is_error(cell.value):
            return cell.value
        value = yield from _eval_expr_gen(node.value, ctx)
        cell.value = value
        return value
    try:
        current = yield from _eval_expr_gen(node.target, ctx)
    except NameError:
        current = UNSET
    if current is not UNSET and not wyrm_builtins.is_error(current):
        return current
    value = yield from _eval_expr_gen(node.value, ctx)
    return value


def _expr_yield(node, ctx):
    if node.from_:
        sub = yield from _eval_expr_gen(node.value, ctx)
        return _yield_from(sub)
    value = (yield from _eval_expr_gen(node.value, ctx)) if node.value is not None else None
    return _yield_value(value)


def _expr_try(node, ctx):
    value = yield from _eval_expr_gen(node.value, ctx)
    if wyrm_builtins.is_error(value):
        raise ReturnSignal(value)
    return value


_EXPR_SIMPLE_HANDLERS = {
    ast.Name: _expr_name,
    ast.ThisRef: _expr_thisref,
    ast.Symbol: _expr_symbol,
    ast.Num: _expr_num,
    ast.Str: _expr_str,
    ast.Char: _expr_char,
    ast.Bool: _expr_bool,
    ast.EllipsisExpr: _expr_ellipsis,
    ast.Lambda: _expr_lambda,
    ast.ThreadSpawn: _expr_threadspawn,
    ast.Defined: _expr_defined,
}

_EXPR_GEN_HANDLERS = {
    ast.BinOp: _expr_binop,
    ast.Do: _expr_do,
    ast.Attr: _expr_attr,
    ast.Call: _expr_call,
    ast.If: _expr_if,
    ast.Message: _expr_message,
    ast.TypeCheck: _expr_typecheck,
    ast.Index: _expr_index,
    ast.Decorated: _expr_decorated,
    ast.Catch: _expr_catch,
    ast.Tuple: _expr_tuple,
    ast.Array: _expr_array,
    ast.Dict: _expr_dict,
    ast.Pair: _expr_pair,
    ast.UnaryOp: _expr_unaryop,
    ast.AstRef: _expr_astref,
    ast.Scope: _expr_scope,
    ast.MessageTupleExpr: _expr_messagetupleexpr,
    ast.TaskSpawn: _expr_taskspawn,
    ast.SetIfUnset: _expr_setifunset,
    ast.Yield: _expr_yield,
    ast.Try: _expr_try,
}

_EXPR_SIMPLE_DISPATCH = [None] * ast.Node._next_tag
_EXPR_GEN_DISPATCH = [None] * ast.Node._next_tag
for _cls, _fn in _EXPR_SIMPLE_HANDLERS.items():
    _EXPR_SIMPLE_DISPATCH[_cls.TAG] = _fn
for _cls, _fn in _EXPR_GEN_HANDLERS.items():
    _EXPR_GEN_DISPATCH[_cls.TAG] = _fn
del _cls, _fn


def _eval_expr_gen(node, ctx: dict):
    """The generator-driven twin of eval_expr - a full mirror, case for
    case: every case that recurses into a sub-expression does so via
    `yield from _eval_expr_gen(...)` instead of a native `eval_expr(...)`
    call, and `ast.Call`/`ast.Message`/`ast.MessageTupleExpr` yield a
    _TailCall (see _TailCall's docstring) instead of recursing into
    call_value/call_function/call_overload when the resolved callable is a
    plain user Function, a Class, or an ordinary FnDef-backed message
    overload - so a call *anywhere* inside an expression tree (not just in
    bare statement-tail position - `f(n - 1) + 1`, not only
    `return f(n - 1)`) is trampolined by _run_driver.

    Safe to recurse into itself via `yield from` for expression-internal
    structure (an operand, an argument, a collection element, ...): that
    nesting is bounded by the expression's own (already-parsed, therefore
    already-bounded - see wypoc/parse.py's own big-stack workaround for the
    *parser's* version of this same shape of problem) syntax depth, not by
    Wyrm-level call-chain depth - the same reasoning that already lets
    _eval_stmt_impl_gen/_eval_if_gen recurse into nested if/while/do blocks
    via `yield from` rather than pushing a new _run_driver activation for
    them.

    Dispatches on `node.TAG` against the two tables above rather than an
    isinstance chain - see their docstring for why simple/gen are split."""
    tag = node.TAG
    simple = _EXPR_SIMPLE_DISPATCH[tag]
    if simple is not None:
        return simple(node, ctx)
    handler = _EXPR_GEN_DISPATCH[tag]
    if handler is None:
        raise NotImplementedError(f"cannot evaluate {type(node).__name__}")
    value = yield from handler(node, ctx)
    return value


def _eval_stmt_impl_gen(stmt, ctx: dict):
    """The generator-driven twin of _eval_stmt_impl - a full mirror (via
    _eval_expr_gen/_run_scoped_block_gen/_eval_if_gen in place of
    eval_expr/run_scoped_block/_eval_if), so a Wyrm-level call reachable
    from *any* expression in a trampolined call's body - not just a bare
    tail-position call - is trampolined by _run_driver. Only the handful of
    statement kinds with no expression-evaluation/nested-block involvement
    at all (Import, FromImport, Pass, Continue, Break, a `defer`'s own
    registration) or whose expressions are vanishingly unlikely to contain
    deep recursion and not worth the added surface (FnDef/CoDef's
    class-target lookups, ClassDef's base-class expressions) still fall
    through to the native _eval_stmt_impl."""
    # Ordered by measured call frequency (see _eval_expr_gen's reordering
    # note above) - ExprStmt/VarDecl/If/Return/Assign dominate, so they lead.
    if isinstance(stmt, ast.ExprStmt):
        value = yield from _eval_expr_gen(stmt.value, ctx)
        return value
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
        values = []
        for v in stmt.values:
            value = yield from _eval_expr_gen(v, ctx)
            values.append(value)
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
                    cell.value = yield from _eval_expr_gen(v_node, ctx)
                else:
                    value = yield from _eval_expr_gen(v_node, ctx)
                    assign_target(t, value, ctx)
            return
        values = []
        for v in stmt.values:
            value = yield from _eval_expr_gen(v, ctx)
            values.append(value)
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
            value = UNSET
            if stmt.default is not None:
                value = yield from _eval_expr_gen(stmt.default, ctx)
            store[stmt.name] = Variable(value)
        # Alias, don't copy: later plain assignment (`foo = foo + 1`) or
        # `?=` mutates this same Variable in place (see bind()), so the
        # write is visible through `store` on the next call too.
        ctx[stmt.name] = store[stmt.name]
        return
    if isinstance(stmt, ast.If):
        value = yield from _eval_if_gen(stmt, ctx)
        return value
    if isinstance(stmt, ast.Return):
        value = None
        if stmt.value is not None:
            value = yield from _eval_expr_gen(stmt.value, ctx)
        raise ReturnSignal(value)
    if isinstance(stmt, ast.Yield):
        # A bare `yield 1` / `yield from sub()` line (yield_stmt, not
        # wrapped in ExprStmt - see wyrm.gram). Same suspension as the
        # expression form; the yielded-back-in value is just discarded.
        if stmt.from_:
            value = yield from _eval_expr_gen(stmt.value, ctx)
            _yield_from(value)
        else:
            value = None
            if stmt.value is not None:
                value = yield from _eval_expr_gen(stmt.value, ctx)
            _yield_value(value)
        return
    if isinstance(stmt, ast.While):
        # A fresh child scope per iteration, matching `for`'s per-iteration
        # scoping below - a `var`/`:=` local declared in the body doesn't
        # collide with the same declaration on the next iteration, and a
        # `defer` inside the body fires at the end of *that* iteration
        # (see run_scoped_block).
        while (yield from _eval_expr_gen(stmt.cond, ctx)):
            try:
                yield from _run_block_gen(stmt.body, ctx)
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
        # see doc/language-spec.md's "for" section. A `defer` inside the
        # body fires at the end of that iteration (see run_scoped_block).
        iterable = yield from _eval_expr_gen(stmt.iter, ctx)
        broke = False
        last_iter_scope = None
        for item in _iter_values(iterable, ctx):
            iter_scope = ctx.child()
            iter_scope.declare_new(stmt.var, item)
            last_iter_scope = iter_scope
            try:
                yield from _run_scoped_block_gen(stmt.body, iter_scope)
            except ContinueSignal:
                continue
            except BreakSignal:
                broke = True
                break
        if not broke and stmt.orelse is not None:
            else_scope = last_iter_scope.child() if last_iter_scope is not None else ctx.child()
            if last_iter_scope is None:
                else_scope.declare_new(stmt.var, UNSET)
            yield from _run_scoped_block_gen(stmt.orelse, else_scope)
        return
    if isinstance(stmt, (ast.WithSimple, ast.WithBlock)):
        # `with` declares an immutable binding in the current scope - see
        # doc/language-spec.md's "immutable bindings". `with_block` is
        # sugar for several `with_stmt_simple`s in a row.
        bindings = [stmt] if isinstance(stmt, ast.WithSimple) else stmt.bindings
        for binding in bindings:
            value = yield from _eval_expr_gen(binding.value, ctx)
            cell = ctx.declare_new(binding.name, value)
            cell.immutable = True
        return
    if isinstance(stmt, ast.Decorated):
        expanded = expand_decorated(stmt, ctx, as_statement=True)
        value = yield from _eval_stmt_gen(expanded, ctx)
        return value
    return _eval_stmt_impl(stmt, ctx)


def _eval_stmt_gen(stmt, ctx: dict):
    """The generator-driven twin of eval_stmt - identical debugger-hook and
    location-tagging wrapper (_stmt_hook up front, _exception_hook's
    once-per-exception/innermost-sighting dedup, _locate_exc's location
    tag - see eval_stmt's own docstring) around _eval_stmt_impl_gen instead
    of _eval_stmt_impl."""
    if _stmt_hook is not None:
        _stmt_hook(stmt, ctx)
    global _last_captured_exc
    try:
        value = yield from _eval_stmt_impl_gen(stmt, ctx)
        return value
    except (ReturnSignal, BreakSignal, ContinueSignal):
        raise
    except BaseException as exc:
        if exc is _last_captured_exc:
            raise
        if _exception_hook is not None and _call_stack is not None:
            _exception_hook(exc, list(_call_stack), stmt.pos)
        located = _locate_exc(stmt, exc)
        _last_captured_exc = located
        if located is exc:
            raise
        raise located from exc


# A body containing any of these, at its own top level (not inside a
# further-nested block - those get their own scope decision independently),
# writes directly into whichever scope it runs in: a `var`/`:=` or `with`
# declares a name there, `static` aliases one, `defer` registers a
# teardown callback on it. A body with none of these can safely run in
# whatever scope its caller already has - see _body_needs_scope. A
# `Decorated` statement is treated as "maybe" (conservatively requires a
# scope) since what it expands to isn't known without running the
# decorator.
_SCOPE_REQUIRING_STMT_TYPES = (
    ast.VarDecl, ast.WithSimple, ast.WithBlock, ast.StaticDecl, ast.Defer,
    ast.Decorated,
)

# Keyed by id(body) - safe because every `body` this is ever called with is
# a list embedded in the (persistent, never-freed-while-running) parsed
# AST, so its id can't be reused out from under this cache mid-run.
_body_needs_scope_cache: dict = {}


def _body_needs_scope(body: list) -> bool:
    """Whether running `body` needs a fresh child Scope of its own, or can
    just run directly in whatever scope its caller already has - see
    _SCOPE_REQUIRING_STMT_TYPES. Measured on a self-hosted-parser workload,
    ~60% of `do:`/`if`/`while` block executions declare nothing at all: the
    fresh Scope (and the extra parent-chain hop it adds to every lookup
    inside) was pure overhead for those. Cached per body list, since the
    scan only needs to happen once per distinct body in the tree."""
    key = id(body)
    cached = _body_needs_scope_cache.get(key)
    if cached is None:
        cached = any(isinstance(s, _SCOPE_REQUIRING_STMT_TYPES) for s in body)
        _body_needs_scope_cache[key] = cached
    return cached


def _eval_block_gen(stmts, ctx: dict):
    """The generator-driven twin of eval_block."""
    value = None
    for stmt in stmts:
        value = yield from _eval_stmt_gen(stmt, ctx)
    return value


def _run_block_gen(body, ctx: dict):
    """Runs `body` against `ctx`, creating a fresh child scope via
    _run_scoped_block_gen only if _body_needs_scope says it's actually
    needed - otherwise just `_eval_block_gen`s it directly in `ctx`,
    skipping both the Scope allocation and the extra parent-chain hop it
    would otherwise add to every name lookup inside. Used wherever a block
    used to get an unconditional `ctx.child()` for a body that, in the
    common case, never declares anything: `do:`, each `if`/`elif`/`else`
    branch, and a `while` body. NOT used for a `for` body (its iter_scope
    is needed regardless, to hold the loop variable) or a call's own frame
    (a different, always-needed scope)."""
    if _body_needs_scope(body):
        value = yield from _run_scoped_block_gen(body, ctx.child())
    else:
        value = yield from _eval_block_gen(body, ctx)
    return value


def _run_scoped_block_gen(body, scope: "Scope"):
    """The generator-driven twin of run_scoped_block - same defer/
    exit-reason logic (see run_scoped_block's own docstring), just around a
    `yield from _eval_block_gen(...)` instead of a native `eval_block(...)`
    call, so a call anywhere inside `body` (including inside a nested
    if/while/for/do - see _eval_stmt_impl_gen) can still suspend up to
    _run_driver instead of recursing."""
    try:
        value = yield from _eval_block_gen(body, scope)
    except ReturnSignal as ret:
        if scope.defers:
            _run_defers(scope, wyrm_builtins.is_error(ret.value))
        raise
    except (BreakSignal, ContinueSignal):
        if scope.defers:
            _run_defers(scope, False)
        raise
    except BaseException:
        if scope.defers:
            _run_defers(scope, True)
        raise
    else:
        if scope.defers:
            _run_defers(scope, False)
        return value


def _build_call_activation(node, local_ctx: dict, positional, kwargs, display_name: str) -> _CallActivation:
    """Builds one _run_driver stack entry for running `node`'s (a FnDef or
    Lambda) body in `local_ctx` - binds params (may raise - e.g. wrong
    argument count - *before* any Frame is pushed, so a failed call never
    shows up in the debugger's stack), pushes a debugger Frame, and primes
    the call body's _run_scoped_block_gen generator (not run yet -
    _run_driver's own loop does that)."""
    _bind_params(node, local_ctx, positional, kwargs, display_name)
    pushed_frame = _call_stack is not None
    if pushed_frame:
        _call_stack.append(Frame(display_name, local_ctx))
    return _CallActivation(_run_scoped_block_gen(node.body, local_ctx), pushed_frame)


def _make_call_activation(fn: Function, positional, kwargs) -> _CallActivation:
    """_build_call_activation for a plain Call to a Function - see
    call_function."""
    return _build_call_activation(fn.node, fn.closure.child(), positional, kwargs, fn.name or "<lambda>")


def _make_overload_activation(overload: MethodOverload, receivers: list, positional, kwargs) -> _CallActivation:
    """_build_call_activation for an ordinary (FnDef-backed, not
    NativeBody/CoDef) message overload - the same `this`/slot seeding
    call_overload's own non-trampolined branch does, see call_overload."""
    this_value = receivers[0] if len(receivers) == 1 else tuple(receivers)
    local_ctx = overload.closure.child()
    if len(receivers) == 1 and isinstance(receivers[0], ClassInstance):
        local_ctx.update(receivers[0].attrs)
    bind_new("this", this_value, local_ctx)
    return _build_call_activation(overload.node, local_ctx, positional, kwargs, overload.node.name)


def _instantiate_gen(cls: "Class", positional, kwargs):
    """The generator-driven twin of instantiate - identical in every way
    (see instantiate's own docstring for the full RAII/ERROR_CLASS
    behaviour) except the init-overload call, if any, is a _TailCall
    (handled by _run_driver, same as any other call - see _TailCall)
    instead of a native call_overload - so a constructor that recursively
    builds more instances, anywhere in `init`'s body (`return
    counter(n - 1)`, `counter(n - 1).total + 1`, ...) is trampolined
    instead of growing the Python stack once per level."""
    if cls is ERROR_CLASS:
        if kwargs or len(positional) != 1:
            raise TypeError("error(...) takes exactly one positional argument (the message)")
        return wyrm_builtins.error(positional[0])

    all_slots = cls.all_slots()
    all_signals = cls.all_signals()
    clash = all_slots.keys() & all_signals.keys()
    if clash:
        # Both live in the same instance.attrs namespace (see SignalValue's
        # docstring) - unlike a slot and a message, which occupy genuinely
        # separate tables (message_table vs. attrs) and can share a name on
        # purpose, a slot and a signal of the same name would otherwise
        # silently clobber each other here (whichever loop below runs
        # last wins), own or inherited either way.
        raise TypeError(
            f"{cls.name!r}: {', '.join(sorted(clash))!r} names both a slot and a "
            "signal - they share one instance namespace, so pick different names"
        )

    inst = ClassInstance(cls)
    for slot_name, (slot_def, owner) in all_slots.items():
        if slot_def.default is not None:
            value = eval_expr(slot_def.default, owner.closure)
        else:
            value = _zero_value(slot_def.type)
        inst.attrs[slot_name] = Variable(value)
    for signal_name in all_signals:
        # Each instance gets its own subscriber list - a signal isn't
        # shared class-wide state any more than a slot's value is (see
        # SignalValue).
        inst.attrs[signal_name] = Variable(SignalValue(signal_name))

    method = _lookup_dunder(cls, "init")
    overload = _try_resolve_overload(method, [inst]) if method is not None else None
    if overload is not None:
        result = yield _TailCall(lambda: _make_overload_activation(overload, [inst], positional, kwargs))
        if wyrm_builtins.is_error(result):
            return result
        return inst
    if positional or kwargs:
        raise TypeError(f"{cls.name}(...) takes no arguments (no applicable 'init')")
    return inst


def _make_instantiate_activation(cls: "Class", positional, kwargs) -> _CallActivation:
    """_instantiate_gen wrapped as a _run_driver activation - never pushes
    its own debugger Frame (see _CallActivation's docstring)."""
    return _CallActivation(_instantiate_gen(cls, positional, kwargs), False)


def _run_driver(build_initial):
    """Runs one Function call, message send, or class instantiation to
    completion without native Python recursion for any further such calls
    reachable from it anywhere in its body's statements/expressions (see
    _eval_expr_gen) - the trampoline call_function, call_overload, and
    instantiate all run through instead of recursing natively. `build_initial` is
    a zero-argument callable answering the first _CallActivation to run
    (_make_call_activation, _make_overload_activation, or
    _make_instantiate_activation) - deferred, like every _TailCall's own
    `build`, so a params-binding failure on the very first call is raised
    here rather than by the caller before this function is even entered,
    keeping every call site (call_function, call_overload, instantiate) and
    every nested _TailCall handled identically.

    Maintains its own explicit stack of _CallActivation objects
    (heap-bounded, not C-stack-bounded); each activation is a suspended
    _run_scoped_block_gen generator for one call's body. A _TailCall
    yielded by the top activation pushes a new one instead of recursing; an
    activation finishing (StopIteration, i.e. its body ran off the end, or
    a ReturnSignal, i.e. an explicit `return`) pops it and feeds its value
    into the new top via .send(); an activation raising any other
    exception pops it and re-raises that same exception *into* the new top
    via .throw(), at the exact point it yielded the _TailCall - so it's
    seen there exactly as if the original, native recursive call had
    raised it directly (defers still run via _run_scoped_block_gen's own
    try/except, and the debugger's exception hook still fires exactly
    once, at the innermost sighting - see _eval_stmt_gen)."""
    stack = [build_initial()]
    send_value = None
    throw_exc = None
    while stack:
        top = stack[-1]
        try:
            if throw_exc is not None:
                exc, throw_exc = throw_exc, None
                step = top.gen.throw(exc)
            else:
                value, send_value = send_value, None
                step = top.gen.send(value)
        except StopIteration as stop:
            result = stop.value
        except ReturnSignal as ret:
            result = ret.value
        except BaseException as exc:
            if top.pushed_frame:
                _call_stack.pop()
            stack.pop()
            if not stack:
                raise
            throw_exc = exc
            continue
        else:
            # step is the _TailCall the top activation just yielded - start
            # the callee's own activation. If building it (binding params)
            # fails, or the explicit stack has grown past _MAX_DRIVER_DEPTH
            # (see its docstring), that failure belongs to the *caller*
            # (deliver it back at the yield point, same as a native call
            # raising there would), not to this driver call directly.
            try:
                if len(stack) >= _MAX_DRIVER_DEPTH:
                    raise RecursionError(
                        f"wyrm-level call depth exceeded {_MAX_DRIVER_DEPTH} "
                        "(likely infinite recursion)"
                    )
                activation = step.build()
            except BaseException as exc:
                throw_exc = exc
                continue
            stack.append(activation)
            continue
        if top.pushed_frame:
            _call_stack.pop()
        stack.pop()
        if not stack:
            return result
        send_value = result
    raise AssertionError("unreachable")  # pragma: no cover


def call_function(fn: Function, positional, kwargs):
    return _run_driver(lambda: _make_call_activation(fn, positional, kwargs))


def call_overload(overload: MethodOverload, receivers: list, positional, kwargs):
    """Calls one resolved Method overload with `this` bound to the
    receiver (or the receiver tuple, for multi-dispatch). For a single
    ClassInstance receiver, its slot Variables are also seeded directly
    into local scope, so the body can read/write them by bare name (e.g.
    `radius`) as well as via `this.radius` - see doc/language-spec.md's
    "slot name directly accesses the internal storage". An ordinary
    FnDef-backed overload (not NativeBody/CoDef, neither of which run a
    body of further Wyrm statements) goes through the same _run_driver
    trampoline call_function uses - this is also what makes a decorator
    invocation (expand_decorated's send_message call) and any other
    send_message/dispatch_message caller trampolined for free, with no
    changes needed there: they all fall through to this one function."""
    this_value = receivers[0] if len(receivers) == 1 else tuple(receivers)
    if isinstance(overload.node, NativeBody):
        return overload.node.fn(this_value, *positional, **kwargs)
    if isinstance(overload.node, ast.CoDef):
        # A message-dispatched coroutine (`co [Cls] name(...)`, invoked via
        # `recv ! name(...)`) - construct the CoroutineInstance rather than
        # running a body, exactly like calling a bare `co` does (see
        # instantiate_coroutine/call_value's Coroutine branch).
        return instantiate_coroutine(overload.node, overload.closure, positional, kwargs, this_value)
    return _run_driver(lambda: _make_overload_activation(overload, receivers, positional, kwargs))


def call_value(func, positional, kwargs):
    # `callable(func)` first, ahead of the Class/Function/Coroutine/
    # BoundMessage isinstance checks below: measured on a self-hosted-parser
    # workload, >97% of calls reaching this function (through corelib
    # combinator code built on plain pair-list primitives - car/cdr/cons/
    # length/reverse/...) are a bare Python callable exposed via
    # expose()/bind_new, arriving here only because _expr_call/eval_expr's
    # Call case already special-cases ContextualBuiltin/Function/Class
    # before ever falling through to call_value. None of
    # Class/Function/Coroutine/BoundMessage/PrimitiveType define __call__,
    # so this reordering can't misroute any of them - callable() is simply
    # False for all of them, same as before.
    if callable(func):
        return func(*positional, **kwargs)
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


def _dispatch_remote_message(receiver, name: str, args_node, ctx: dict):
    """`remote ! name(...)` for a `wyrm_remote.RemoteModule` receiver - a
    blocking IPC round trip (see RemoteModule.call), not a local overload
    resolution, so this bypasses dispatch_message/_resolve_message
    entirely rather than trying to make a remote call look like one more
    MethodOverload kind. No no-parens `remote ! name` form yet (there's no
    local overload to return as a BoundMessage) - a known gap, not a
    silent one.

    When this is reached from inside a `task expr` on the current thread
    (see _current_task_future/ast.TaskSpawn), the call goes asynchronous:
    a plain background thread does the actual blocking round trip and
    resolves the in-flight Future with its result (or failure) instead of
    this thread blocking - the *only* thing that thread does. Outside a
    `task`, this is exactly the synchronous call Phase 4 always was."""
    if args_node is None:
        raise NotImplementedError(
            f"'{receiver.name} ! {name}' (no parens) isn't supported for a "
            f"remote module yet - call it with parens: remote ! {name}(...)"
        )
    positional, kwargs = eval_args(args_node, ctx)
    future = _current_task_future()
    if future is not None:
        def _resolve_in_background():
            try:
                result = receiver.call(name, positional, kwargs)
            except Exception as exc:
                future.fail_with(exc)
            else:
                future.resolve_with(result)

        threading.Thread(target=_resolve_in_background, daemon=True).start()
        return None
    return receiver.call(name, positional, kwargs)


def dispatch_message(name: str, receivers: list, args_node, ctx: dict, module: "str | None" = None):
    """Shared by `recv ! name(...)`, `recv ! name`, and `(a, b) ! name(...)`:
    looks up the generic function in the message namespace (message_table -
    entirely separate from the variable namespace `lookup`/`ctx` indexes),
    resolves the best-matching overload for the given receivers, and either
    calls it immediately (args_node is a list, even an empty one for
    `name()`) or returns a BoundMessage (args_node is None, i.e.
    `recv ! name` with no call parens). `module`, if given, is the `mod` of
    a `recv ! mod::name(...)` qualified selector - see _resolve_message."""
    if args_node is None:
        return BoundMessage(receivers, _resolve_message(name, receivers, ctx, module))
    positional, kwargs = eval_args(args_node, ctx)
    return send_message(name, receivers, positional, kwargs, ctx, module)


def _resolve_message(name: str, receivers: list, ctx: dict, module: "str | None" = None) -> MethodOverload:
    """Looks `name` up in whichever message table actually owns it.

    `module` is `recv ! mod::name(...)`'s qualifier: it resolves `name`
    directly against `mod`'s own canonical message, bypassing the receiving
    module's local visibility entirely - the mechanism doc/addendum.md's
    "Message identity across modules" names for disambiguating two
    same-named messages that collide when both are imported unqualified.

    Otherwise: a single `Module` receiver (`mod ! name(...)`) resolves
    against *that module's own* message_table (module_ctx), not the
    sender's - a module's `fn []`/`signal` declarations are addressed by
    which module defines them, not multi-dispatched across every importer's
    shared namespace the way class methods are. Anything else (a
    ClassInstance, or no receiver at all) resolves against the sender's own
    message_table as before - unchanged."""
    if module is not None:
        mod = lookup(module, ctx)
        if not isinstance(mod, Module):
            raise TypeError(f"{module!r} does not name a module (got {type(mod).__name__})")
        method = message_table(mod.ctx).get(name)
        if method is None:
            raise NameError(f"module {mod.name!r} has no message named {name!r}")
    elif len(receivers) == 1 and isinstance(receivers[0], Module):
        mod = receivers[0]
        method = message_table(mod.ctx).get(name)
        if method is None:
            raise NameError(f"module {mod.name!r} has no message named {name!r}")
    else:
        method = message_table(ctx).get(name)
        if isinstance(method, _AmbiguousMessage):
            raise NameError(
                f"{name!r} is ambiguous: imported from both {method.first!r} and "
                f"{method.second!r} - disambiguate with `recv ! mod::{name}(...)`")
        if method is None:
            raise NameError(f"no message named {name!r}")
    return resolve_overload(method, receivers)


def send_message(name: str, receivers: list, positional, kwargs, ctx: dict, module: "str | None" = None):
    """dispatch_message with the arguments already evaluated - what a
    decorator invocation needs (its arguments are evaluated before the tree
    is built, see expand_decorated) and what a special-method hook like
    `__sexpr` needs (it takes none)."""
    overload = _resolve_message(name, receivers, ctx, module)
    return call_overload(overload, receivers, positional, kwargs)


def has_message_for(name: str, receivers: list, ctx: dict) -> bool:
    """Whether `name` has an overload applicable to `receivers` - the "ask
    first, send only if something answers" test a hook needs, so the
    overwhelming case (nothing answers) costs a lookup rather than a caught
    error."""
    method = message_table(ctx).get(name)
    if method is None or isinstance(method, _AmbiguousMessage):
        return False
    return _try_resolve_overload(method, receivers) is not None


class ContextualBuiltin:
    """A builtin that needs the scope it was called from.

    Almost no builtin does - car/cdr/len and friends are plain Python
    callables that `call_value` invokes with the argument values alone. The
    exception is a builtin whose behaviour depends on the *message*
    namespace, which belongs to the calling module rather than to any value:
    `sexpr(x)` asks whether x's class answers `__sexpr`, and that question
    has no answer without a scope to ask it in. eval_expr's Call case
    recognises this wrapper and passes `ctx` through as the first argument."""

    def __init__(self, fn, name: str):
        self.fn = fn
        self.name = name

    def __repr__(self):
        return f"ContextualBuiltin({self.name!r})"


def sexpr_value(ctx: dict, value):
    """`sexpr(x)` - the canonical s-expression of a tree, in three cases,
    tried in this order (see doc/language-spec.md's Decorators section):

      1. the value's class answers `__sexpr` - it has taken control of what
         its own values mean, and what it answers is the s-expression;
      2. a TreeBase unwraps to the tree it carries;
      3. anything else passes through unchanged, which is what makes this
         the identity on an s-expression that already is one.

    That ordering is what lets one decorator source serve two worlds: here a
    tree is boxed and case 2 carries it, while against a class-based tree
    (a self-hosted parser's own node objects) each node answers `__sexpr`
    and case 1 carries it. Same call, same result, no import either way.

    The hook is applied once and its answer is final - a `__sexpr` answering
    another object is not re-normalized, so a chain cannot loop."""
    if isinstance(value, ClassInstance) and has_message_for("__sexpr", [value], ctx):
        return send_message("__sexpr", [value], [], {}, ctx)
    if is_tree_box(value):
        return sexpr.encode(_fully_expanded(lookup(_TREE_SLOT, value.attrs), ctx))
    return value


def _fully_expanded(node, ctx: dict):
    """`node`, run through expand_decorated until it is no longer an
    unexpanded `Decorated` - what `sexpr()` and `macroexpand()` both answer,
    so a decorator that doesn't care about outside-in visibility can just
    call `sexpr(this)` and get the same fully-resolved tree this interpreter
    always handed decorators before the outside-in model."""
    while isinstance(node, ast.Decorated):
        node = expand_decorated(node, ctx, as_statement=_decorated_is_statement(node))
    return node


def str_value(ctx: dict, value):
    """`str(value)` - the bare rendering (see wyrm_builtins._to_str), except
    a class instance that answers `__str__` controls its own rendering
    first: `str(elem)` becomes `elem ! __str__()`, falling back to the
    built-in bare rendering for anything else - same "ask first, only the
    class's own answer is final" shape as sexpr_value's `__sexpr` hook."""
    if isinstance(value, ClassInstance) and has_message_for("__str__", [value], ctx):
        return send_message("__str__", [value], [], {}, ctx)
    return wyrm_builtins._to_str(value, ctx)


def macroexpand_value(ctx: dict, value):
    """`macroexpand(tree)` - if `tree` (a TreeBase box) holds an unexpanded
    decorator application, runs it (and keeps running while the answer is
    still one), answering the boxed, fully-expanded tree; anything else
    passes through unchanged. Mirrors Common Lisp's `macroexpand`, and is
    what an outer decorator calls to explicitly pull in an inner one it was
    handed raw (see expand_decorated) instead of leaving it unexpanded."""
    if not is_tree_box(value):
        raise TypeError(f"macroexpand() expects a tree (got {type(value).__name__})")
    node = lookup(_TREE_SLOT, value.attrs)
    return tree_box(_fully_expanded(node, ctx))


def expand_decorators(program: ast.Program, ctx: dict) -> ast.Program:
    """`program`, with every `Decorated` node it contains - at any depth,
    statement or expression position - replaced by what it fully expands to
    (see expand_decorated). This is wys.py's "decorators run at compile
    time" pass: a file it writes must carry no `'decorator`/`'decorated`
    node, since it's meant to be readable by a host with no decorator
    machinery at all.

    A top-level `import`/`from-import` is executed for real, in the order it
    appears, so a `static` decorator module has actually run - and its
    messages are reachable - by the time a decorator after it expands,
    exactly as running the program normally would arrange (see
    expand_decorated's NameError case). Nothing else at the top level, or
    nested within it, is executed - only walked, looking for `Decorated`
    nodes to expand - so a decorator inside a function body that's never
    called this way still expands (unlike the interpreter's own lazy,
    first-call expansion), while the function's own side effects don't run
    at compile time. `ctx` should already have whatever populate_globals
    provides."""
    for i, stmt in enumerate(program.body):
        if isinstance(stmt, (ast.Import, ast.FromImport)):
            eval_stmt(stmt, ctx)
        else:
            program.body[i] = _expand_decorators_in(stmt, ctx)
    return program


def _expand_decorators_in(node, ctx: dict):
    if isinstance(node, ast.Decorated):
        return _expand_decorators_in(_fully_expanded(node, ctx), ctx)
    if isinstance(node, ast.Node):
        for f in _dc_fields(node):
            if ast._is_pos_field(f.name):
                continue
            value = getattr(node, f.name)
            if isinstance(value, ast.Node):
                setattr(node, f.name, _expand_decorators_in(value, ctx))
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, ast.Node):
                        value[i] = _expand_decorators_in(item, ctx)
    return node


def register_native_method(name: str, fn, ctx: dict, arity: int = 1) -> None:
    """Registers `fn` (a plain Python callable) as a wildcard message
    overload under `name`, so it's callable via `recv ! name(...)` even
    though `recv` isn't a ClassInstance - used for builtin per-value
    methods like str's substr, which have no Class to hang a real `fn [Cls]
    name(...)` overload on. `arity` is the number of dispatch positions
    (receivers) the wildcard should match; almost always 1. Registers into
    the message namespace (message_table), not `ctx` itself - see
    message_table's docstring."""
    messages = message_table(ctx)
    method = messages.get(name)
    if method is None:
        method = Method(name)
        messages[name] = method
    method.add_overload((None,) * arity, NativeBody(fn), {})


def _lookup_class(name: str, ctx: dict) -> Class:
    cls = lookup(name, ctx)
    if not isinstance(cls, Class):
        raise TypeError(f"{name!r} is not a class (used as a method dispatch type)")
    return cls


def register_overload(name: str, signature, node, closure: dict, target_ctx: dict) -> None:
    """Defines or extends the generic function registered under `name` in
    target_ctx's message namespace (message_table) - never target_ctx
    itself, which stays purely the variable namespace (see message_table's
    docstring). `signature=None` means a plain (non-method) `fn`: it stays
    an ordinary Function bound in target_ctx unless/until `name` is (or
    becomes) a message, at which point per doc/language-spec.md it's
    "promoted" - a *copy* of the existing plain function becomes that
    message's wildcard/"empty type" overload, leaving the original
    variable binding untouched (a variable and a message of the same name
    coexist in their own namespaces from then on). A wildcard's dispatch
    arity (how many receivers it matches) isn't something a bare
    `fn name(...)` states explicitly - it's derived from whichever
    `[Cls, ...]` signature is triggering the promotion (or, if a bare `fn`
    is redefined after the message already exists, from one of its
    existing overloads), since that's the arity actually in use."""
    messages = message_table(target_ctx)
    method = messages.get(name)
    if isinstance(method, _AmbiguousMessage):
        # A local definition always wins over an ambiguous pair of imports:
        # it can't coherently extend either import candidate, so it starts
        # a fresh, locally-owned message under `name`, the same as if
        # nothing had been imported at all - see _AmbiguousMessage's
        # docstring.
        method = None
    if signature is None and method is None:
        bind(name, Function(name, node, closure), target_ctx)
        return
    if method is None:
        owner = None
        try:
            owner = lookup("__name__", target_ctx)
        except NameError:
            pass
        method = Method(name, owner)
        existing = unwrap(target_ctx[name]) if name in target_ctx else None
        if isinstance(existing, Function):
            wildcard_arity = len(signature) if signature is not None else 1
            method.add_overload((None,) * wildcard_arity, existing.node, existing.closure)
        messages[name] = method
    if signature is None:
        wildcard_arity = len(method.overloads[0].signature) if method.overloads else 1
        signature = (None,) * wildcard_arity
        # A bare `fn` always keeps (or gains) its own plain variable binding
        # alongside the wildcard overload, regardless of whether the message
        # already existed before this `fn` was seen - see this function's
        # docstring on "a variable and a message of the same name coexist".
        # Without this, a bare `fn` defined *after* a same-named message was
        # already registered (e.g. a class's `fn [Cls] name` before a later
        # top-level `fn name`) would only be reachable via `!`, while the
        # opposite definition order left it plain-callable too - an
        # order-dependent asymmetry rather than a real distinction.
        bind(name, Function(name, node, closure), target_ctx)
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
    # One nil, two representations - see wyrm_builtins._Nil.__eq__.
    "nil": lambda v: v is None or v is wyrm_builtins.NIL,
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "uint": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    "float": lambda v: isinstance(v, float),
    "str": lambda v: isinstance(v, str),
    "sym": lambda v: isinstance(v, wyrm_builtins.Symbol),
    "list": lambda v: isinstance(v, list),
    "tuple": lambda v: isinstance(v, tuple),
    "dict": lambda v: isinstance(v, dict),
    "pair": lambda v: isinstance(v, wyrm_builtins.Pair),
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


# ---------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------

class DecoratorError(Exception):
    """A decorator that could not be applied: the tree it was handed cannot
    cross, the tree it answered is not a well-formed one, or what came back
    is the wrong sort of thing for where the decorator was written. The
    message names the decorator and, where there is one, the source line -
    the *use* site's line rather than the decorator's own, which is the more
    useful of the two."""


# What may come back in statement position. An expression coming back there
# is wrapped in an ExprStmt (a decorator answering `1 + 1` for a statement
# means the statement `1 + 1`); a statement coming back in *expression*
# position has nowhere to go and is an error instead.
_STATEMENT_NODES = (
    ast.ExprStmt, ast.VarDecl, ast.Assign, ast.StaticDecl, ast.If, ast.While,
    ast.For, ast.Break, ast.Continue, ast.Return, ast.Pass, ast.WithBlock,
    ast.WithSimple, ast.Defer, ast.FnDef, ast.CoDef, ast.ClassDef,
    ast.Import, ast.FromImport,
)


def _decorated_is_statement(node: ast.Decorated) -> bool:
    """Whether `node` sits in statement position, judged from the shape of
    its innermost (peeling through any further unexpanded `Decorated`
    layers) `inner` - the same thing a node's position in the parsed tree
    already fixes, so this just reads it back off the tree rather than
    tracking it separately."""
    inner = node.inner
    while isinstance(inner, ast.Decorated):
        inner = inner.inner
    return isinstance(inner, _STATEMENT_NODES)


def expand_decorated(node: ast.Decorated, ctx: dict, as_statement: bool):
    """`@dec(args) X` -> the tree that gets evaluated in X's place.

    The rewrite happens once per `Decorated` node and is cached on it, which
    is this interpreter's stand-in for the reference implementation's
    "decorators run at compile time": a decorated definition nested inside a
    function is rewritten the first time that function runs, not on every
    call. What the decorator answers is therefore fixed for the life of the
    parsed tree, exactly as a compile-time rewrite would be.

    Nesting is outside-in, like Common Lisp macroexpansion: `@a @b X` hands
    `a` the *raw*, unexpanded `Decorated(b, X)` as `this` - `a` never sees
    `b`'s answer unless it asks for it (via the `macroexpand()` builtin,
    which runs exactly this function). This is what lets an outer decorator
    inspect, rewrite, duplicate or discard an inner decorator application
    instead of always receiving an already-resolved tree; the common case of
    "just give me the resolved tree" is `sexpr(this)`, which auto-expands
    any `Decorated` it finds on the way to encoding (see sexpr_value).

    The decorator itself is an ordinary message send on the boxed tree, so
    dispatch, multiple dispatch and native messages all behave exactly as
    they do at run time rather than being reimplemented here. Reaching one
    written in wyrm needs `import static`, which is what runs its module
    before this one gets here (see eval_import)."""
    cached = getattr(node, "_expanded", None)
    if cached is not None:
        return cached

    decorator = node.decorator
    inner = node.inner

    try:
        positional, kwargs = eval_args(decorator.args, ctx)
        answer = send_message(decorator.name, [tree_box(inner)],
                              positional, kwargs, ctx)
        result = sexpr.decode(sexpr_value(ctx, answer))
    except sexpr.SexprError as exc:
        raise _decorator_error(decorator, str(exc)) from None
    except NameError as exc:
        # The overwhelmingly common mistake: the decorator's module was
        # imported without `static`, so it has not run and its messages are
        # not in this module's namespace yet.
        raise _decorator_error(
            decorator,
            f"{exc} (a decorator must be reachable through `import static`)",
        ) from None

    # A result that is itself an unexpanded `Decorated` (a decorator that
    # answered another decorator application rather than resolving it) is
    # neither statement nor expression yet - eval_stmt/eval_expr expand it
    # the next time they reach it, same as any other `Decorated` node.
    if isinstance(result, ast.Decorated):
        pass
    elif isinstance(result, _STATEMENT_NODES):
        if not as_statement:
            raise _decorator_error(
                decorator,
                f"answered a {type(result).__name__} where an expression was required",
            )
    elif as_statement:
        result = ast.ExprStmt(result, pos=getattr(result, "pos", None))

    node._expanded = result
    return result


def _decorator_error(decorator: ast.Decorator, message: str) -> DecoratorError:
    where = f" at line {decorator.pos[0]}" if decorator.pos else ""
    return DecoratorError(f"@{decorator.name}{where}: {message}")


def _decorator_dump(this, *args, **kwargs):
    """`@__dump X` - prints the s-expression the decorator would receive and
    compiles X unchanged. Arguments are accepted and ignored. Native, so it
    exercises the wire format without needing a decorator written in wyrm
    (and therefore without needing `import static`) - see doc/sexpr-spec.md's
    "Trying it"."""
    from wypoc import wyrm_io

    wyrm_io.wyrm_write(wyrm_io.STDOUT, wyrm_builtins._to_str(
        sexpr.encode(lookup(_TREE_SLOT, this.attrs))) + "\n")
    return this


def _decorator_identity(this, *args, **kwargs):
    """`@__identity X` - rebuilds X's tree from its s-expression and compiles
    that, so every use is a full encode-then-decode round trip. Arguments
    are accepted and ignored."""
    return sexpr.encode(lookup(_TREE_SLOT, this.attrs))


def install_native_decorators(ctx: dict) -> None:
    """Registers `@__dump`/`@__identity` as TreeBase messages. Typed on
    TreeBase rather than registered as wildcards (register_native_method)
    so they only ever answer for a tree."""
    for name, fn in (("__dump", _decorator_dump), ("__identity", _decorator_identity)):
        register_overload(name, (TREE_BASE_CLASS,), NativeBody(fn), {}, ctx)


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
    wyrm-defined), so `error("msg")` is handled directly here instead.

    Runs through _run_driver/_instantiate_gen (the same trampoline
    call_function/call_overload use) rather than doing the work inline, so
    a constructor that recursively builds more instances anywhere in
    `init`'s body - `return counter(n - 1)`, `counter(n - 1).total + 1`,
    ... - is trampolined exactly like any other call instead of growing
    the Python stack once per level."""
    return _run_driver(lambda: _make_instantiate_activation(cls, positional, kwargs))


def _lookup_dunder(cls: Class, name: str) -> "Method | None":
    """A special method (e.g. `__iter__`) applicable to `cls`, if any class
    in scope has defined one - same lookup instantiate() uses for `init`:
    dunders register into the class's own defining scope's message
    namespace (`message_table(cls.closure)`), same as any other message,
    so this is just a name lookup there, not a new mechanism."""
    return message_table(cls.closure).get(name)


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
        if cell.immutable:
            raise TypeError(f"cannot assign to {target.name!r}: bound with 'with' (immutable)")
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


def _eval_if(node: "ast.If", ctx: dict) -> "object":
    """Shared by both `if` as a statement (_eval_stmt_impl) and `if` as an
    expression (eval_expr, via if_expr in wyrm.gram): runs whichever
    branch's condition is first true and answers the value of the last
    statement executed in it (via run_scoped_block, same rule `do:` and
    fn bodies use), or None if no branch ran. Each branch body is its own
    child scope, so a `var`/`:=` inside one branch never leaks into (or
    collides with a redeclare error against) another branch or the
    enclosing scope - see doc/language-spec.md's Variables section."""
    if eval_expr(node.cond, ctx):
        return _run_block(node.body, ctx)
    for clause in node.elifs:
        if eval_expr(clause.cond, ctx):
            return _run_block(clause.body, ctx)
    if node.orelse is not None:
        return _run_block(node.orelse, ctx)
    return None


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
        return wyrm_builtins.Symbol(node.name)
    if isinstance(node, ast.EllipsisExpr):
        return wyrm_builtins.ELLIPSIS
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
        if node.op == "pos":
            return +val
        if node.op == "inv":
            return ~val
        if node.op == "not":
            return not val
        raise NotImplementedError(f"unsupported unary op: {node.op}")
    if isinstance(node, ast.BinOp):
        if node.op == "and":
            left = eval_expr(node.left, ctx)
            return left if not left else eval_expr(node.right, ctx)
        if node.op == "or":
            left = eval_expr(node.left, ctx)
            return left if left else eval_expr(node.right, ctx)
        left = eval_expr(node.left, ctx)
        right = eval_expr(node.right, ctx)
        try:
            return BINOPS[node.op](left, right)
        except KeyError:
            raise NotImplementedError(f"unsupported binary op: {node.op}")
    if isinstance(node, ast.Lambda):
        return Function(None, node, ctx)
    if isinstance(node, ast.Do):
        # `do:` - an anonymous, immediately-executed child scope, usable as
        # an expression: its value is that of the last statement executed
        # in its body (run_scoped_block also arms any `defer`s registered
        # directly in it) - see doc/language-spec.md's "do" section.
        return _run_block(node.body, ctx)
    if isinstance(node, ast.If):
        # `if` used as an expression (e.g. `a := if check { 3 } else { 4 }`)
        # - same node, and same value rule, as the if_stmt case in
        # _eval_stmt_impl: see _eval_if.
        return _eval_if(node, ctx)
    if isinstance(node, ast.Call):
        func = eval_expr(node.func, ctx)
        positional, kwargs = eval_args(node.args, ctx)
        if isinstance(func, ContextualBuiltin):
            return func.fn(ctx, *positional, **kwargs)
        return call_value(func, positional, kwargs)
    if isinstance(node, ast.Decorated):
        return eval_expr(expand_decorated(node, ctx, as_statement=False), ctx)
    if isinstance(node, ast.AstRef):
        # `foo::$ast` - the tree of the definition `foo` names. Reached
        # through the binding rather than through a separate name-to-tree
        # table, which is what makes it describe the definition *after*
        # decoration: a decorated `fn` binds what the decorator answered, so
        # that is the tree found here. It also means `foo = other` leaves
        # the answer describing `other`, which is the documented
        # "describes the definition, not the binding" caveat seen from this
        # side.
        target = eval_expr(node.obj, ctx)
        definition = getattr(target, "node", None)
        if definition is None:
            raise TypeError(
                f"'::${node.field}' needs a fn, co or class definition "
                f"(got {type(target).__name__})"
            )
        return tree_box(definition)
    if isinstance(node, ast.Index):
        obj = eval_expr(node.obj, ctx)
        idx = eval_expr(node.index, ctx)
        if isinstance(obj, str):
            # No unicode support yet - just the ascii code point (a u32) of
            # the single indexed char, per doc/language-spec.md's Lookup
            # Operator ("7"[0] -> the u32 for the char '7', not the char).
            # Out-of-range is a catchable error value, same as the list/
            # tuple case below - e.g. wyrm::parser::tokenizer's own
            # `buffer[cur_pos] catch 'EOF` needs this, not a raw crash.
            try:
                return ord(obj[idx])
            except IndexError as exc:
                return wyrm_builtins.error(str(exc))
        if isinstance(obj, dict):
            # A missing key hands back the Unset error value rather than
            # raising, matching doc/language-spec.md's predefined
            # `Unset: error` type and enabling
            # `d['missing'] catch ...`/`try d['missing']` - see the
            # reference implementation's same divergence from a raw
            # KeyError.
            return obj.get(idx, UNSET)
        # Out-of-range / bad-index lookups (list/tuple/etc) become a
        # catchable error value instead of a raw Python exception, matching
        # the dict case above and doc/language-spec.md's "Unset" error type
        # - e.g. `a := [1, 2]; b := a[12] catch 0;` must produce 0, not
        # crash the interpreter.
        try:
            return obj[idx]
        except (IndexError, TypeError, KeyError) as exc:
            return wyrm_builtins.error(str(exc))
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
        from wypoc.wyrm_remote import RemoteModule
        if isinstance(obj, RemoteModule):
            return obj.signal(node.name)
        raise NotImplementedError(f"'.' is only supported on class instances right now (got {type(obj).__name__})")
    if isinstance(node, ast.Message):
        receiver = eval_expr(node.obj, ctx)
        from wypoc.wyrm_remote import RemoteModule
        if isinstance(receiver, RemoteModule):
            if node.module is not None:
                raise NotImplementedError("a module-qualified message (mod::name) isn't supported on a `thread` receiver")
            return _dispatch_remote_message(receiver, node.name, node.args, ctx)
        return dispatch_message(node.name, [receiver], node.args, ctx, node.module)
    if isinstance(node, ast.MessageTupleExpr):
        receivers = [eval_expr(e, ctx) for e in node.items]
        return dispatch_message(node.name, receivers, node.args, ctx)
    if isinstance(node, ast.ThreadSpawn):
        from wypoc.wyrm_remote import spawn_module_process
        return spawn_module_process(node.path)
    if isinstance(node, ast.TaskSpawn):
        future = Future()
        _push_task_future(future)
        try:
            eval_expr(node.expr, ctx)
        finally:
            _pop_task_future()
        return future
    if isinstance(node, ast.ThisRef):
        return lookup("this", ctx)
    if isinstance(node, ast.Defined):
        if not isinstance(node.symbol, ast.Symbol):
            raise TypeError("defined() takes a symbol literal, e.g. defined('foo)")
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


def eval_stmt(stmt, ctx: dict) -> "object":
    """The one place every statement everywhere passes through - module top
    level, fn/co bodies, if/while/for bodies (a loop re-enters this once per
    iteration, via run_scoped_block) - so it's where a debugger's
    breakpoint/step hook and its exception-stack capture attach (both
    `None`, and both checked before use, when nothing is debugging - see
    the `_call_stack`/`_stmt_hook`/`_exception_hook` globals near the top of
    this module), and also where a raw exception gets tagged with its wyrm
    source location (see WyrmLocatedError/_locate_exc above) regardless of
    whether a debugger is attached. The statement itself is dispatched by
    `_eval_stmt_impl`, below."""
    if _stmt_hook is not None:
        _stmt_hook(stmt, ctx)
    global _last_captured_exc
    try:
        return _eval_stmt_impl(stmt, ctx)
    except (ReturnSignal, BreakSignal, ContinueSignal):
        # Ordinary control flow (every `return`/`break`/`continue` is one
        # of these), not an error - nothing for a debugger to report, and
        # nothing to tag with a location either.
        raise
    except BaseException as exc:
        # Only the *first* eval_stmt frame to see a given exception object
        # captures it - by the time it's re-raised through an outer
        # eval_stmt, _call_stack has already been popped back by the
        # enclosing call's Frame teardown (_run_driver's pop, or a
        # trampolined call's own _eval_stmt_gen - see its docstring), so
        # only the innermost sighting still has the full stack to snapshot
        # (or, for _locate_exc below, the innermost/most precise pos).
        if exc is _last_captured_exc:
            raise
        if _exception_hook is not None and _call_stack is not None:
            _exception_hook(exc, list(_call_stack), stmt.pos)
        located = _locate_exc(stmt, exc)
        _last_captured_exc = located
        if located is exc:
            raise
        raise located from exc


def _eval_stmt_impl(stmt, ctx: dict) -> "object":
    """Executes one statement. Returns its value for `ast.ExprStmt` (a bare
    expression statement) and None for every other statement kind - used by
    eval_block/run_scoped_block to support `do:`'s "value of the last
    statement executed" (see doc/language-spec.md's "do" section). This is
    a POC-level simplification: unlike the spec's fuller "if/while/for also
    produce the value of the last statement executed" note, only a directly
    trailing expression statement is threaded through here - a block ending
    in a compound statement (if/while/for) evaluates to nil."""
    if isinstance(stmt, ast.Decorated):
        return eval_stmt(expand_decorated(stmt, ctx, as_statement=True), ctx)
    if isinstance(stmt, ast.Import):
        eval_import(stmt, ctx)
        return
    if isinstance(stmt, ast.FromImport):
        mod = import_module(stmt.path)
        for name in stmt.names:
            bind_new(name, lookup(name, mod.ctx), ctx)
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
        return eval_expr(stmt.value, ctx)
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
        # Value rule matches every other block (do:, fn body, ...): an `if`
        # statement's value is that of whichever branch ran, so it can be
        # the tail expression of an enclosing `do:`/fn body -
        # e.g. `do: if true { 5 }` -> 5. See _eval_if.
        return _eval_if(stmt, ctx)
    if isinstance(stmt, ast.Continue):
        raise ContinueSignal()
    if isinstance(stmt, ast.Break):
        raise BreakSignal()
    if isinstance(stmt, ast.While):
        # A fresh child scope per iteration, matching `for`'s per-iteration
        # scoping below - a `var`/`:=` local declared in the body doesn't
        # collide with the same declaration on the next iteration, and a
        # `defer` inside the body fires at the end of *that* iteration
        # (see run_scoped_block).
        while eval_expr(stmt.cond, ctx):
            try:
                _run_block(stmt.body, ctx)
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
        # see doc/language-spec.md's "for" section. A `defer` inside the
        # body fires at the end of that iteration (see run_scoped_block).
        broke = False
        last_iter_scope = None
        for item in _iter_values(eval_expr(stmt.iter, ctx), ctx):
            iter_scope = ctx.child()
            iter_scope.declare_new(stmt.var, item)
            last_iter_scope = iter_scope
            try:
                run_scoped_block(stmt.body, iter_scope)
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
            run_scoped_block(stmt.orelse, else_scope)
        return
    if isinstance(stmt, ast.Defer):
        # Registers a deferred body against the *current* scope - run when
        # that scope is torn down (see run_scoped_block), in LIFO order
        # relative to other defers in the same scope. `defer on error` only
        # runs if the scope is exiting via a `return`ed error value or an
        # escaping exception - see doc/language-spec.md's Defer section.
        ctx.defers.append((stmt.on_error, stmt.body))
        return
    if isinstance(stmt, (ast.WithSimple, ast.WithBlock)):
        # `with` declares an immutable binding in the current scope - see
        # doc/language-spec.md's "immutable bindings". `with_block` is
        # sugar for several `with_stmt_simple`s in a row.
        bindings = [stmt] if isinstance(stmt, ast.WithSimple) else stmt.bindings
        for binding in bindings:
            cell = ctx.declare_new(binding.name, eval_expr(binding.value, ctx))
            cell.immutable = True
        return
    if isinstance(stmt, ast.FnDef):
        if stmt.class_target is None:
            signature = None
        elif stmt.class_target == []:
            # `fn [] name(...)` - explicitly the wildcard/"empty type"
            # overload register_overload's own docstring describes (the one
            # a bare `fn` only gets *promoted* to implicitly) - a module
            # message handler, reachable as `mod ! name(...)` from outside
            # (see _resolve_message's Module branch) without being tied to
            # any class the way `fn [SomeClass] name(...)` is.
            signature = (None,)
        else:
            signature = tuple(_lookup_class(n, ctx) for n in stmt.class_target)
        register_overload(stmt.name, signature, stmt, ctx, ctx)
        return
    if isinstance(stmt, ast.SignalDef):
        # Module-scope counterpart of a class's per-instance signal (see
        # SignalValue, _instantiate_gen) - one shared SignalValue for the
        # whole module rather than one per instance, since a module is
        # already a singleton. `emit`/`connect`/`disconnect` need no
        # changes: both already work against any SignalValue wherever it's
        # bound (see ast.Emit below and register_native_method's wildcard
        # "connect"/"disconnect" registration).
        bind_new(stmt.name, SignalValue(stmt.name), ctx)
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
    if isinstance(stmt, ast.Emit):
        sig = lookup(stmt.name, ctx)
        if not isinstance(sig, SignalValue):
            raise TypeError(f"emit {stmt.name!r}: not a signal (got {type(sig).__name__})")
        positional, kwargs = eval_args(stmt.args, ctx)
        # A snapshot, not the live list: a subscriber that connects or
        # disconnects itself (or another callback) mid-emit shouldn't change
        # who this particular emit reaches - the same "who was subscribed
        # when it fired" guarantee Qt's own signals give.
        for callback in list(sig.subscribers):
            call_value(callback, positional, kwargs)
        return
    raise NotImplementedError(f"cannot evaluate statement {type(stmt).__name__}")


def eval_block(stmts, ctx: dict) -> "object":
    """Runs a list of statements directly in the given scope - `ctx` is
    already whatever scope this block belongs to (a fresh child Scope for
    an if/while/for/fn body - see eval_stmt/call_function/etc - or the
    caller's own scope for a straight-through sequence like a module or
    function's top-level statements). Returns the value of the last
    statement executed (see eval_stmt), for `do:`'s benefit."""
    value = None
    for stmt in stmts:
        value = eval_stmt(stmt, ctx)
    return value


def _run_block(body, ctx: dict):
    """The native twin of _run_block_gen - see its docstring."""
    if _body_needs_scope(body):
        return run_scoped_block(body, ctx.child())
    return eval_block(body, ctx)


def _run_defers(scope: "Scope", is_error_exit: bool) -> None:
    """Runs `scope`'s own registered `defer` bodies (see eval_stmt's Defer
    case), most-recently-registered first - see run_scoped_block. `defer on
    error` bodies only run when `is_error_exit` is true."""
    for on_error, body in reversed(scope.defers):
        if on_error and not is_error_exit:
            continue
        eval_block(body, scope.child())


def run_scoped_block(body, scope: "Scope") -> "object":
    """Runs `body` in `scope`, then tears `scope` down - running any
    `defer`s registered directly in it (see doc/language-spec.md's Defer
    section) regardless of how the block's execution ends: falling off the
    end, `break`/`continue`, a propagating `return` (ReturnSignal), or a
    real exception. `defer on error` bodies additionally require the block
    to be exiting via a returned error value or an exception. Returns
    whatever eval_block returned, for `do:`'s benefit."""
    try:
        value = eval_block(body, scope)
    except ReturnSignal as ret:
        if scope.defers:
            _run_defers(scope, wyrm_builtins.is_error(ret.value))
        raise
    except (BreakSignal, ContinueSignal):
        if scope.defers:
            _run_defers(scope, False)
        raise
    except BaseException:
        if scope.defers:
            _run_defers(scope, True)
        raise
    else:
        if scope.defers:
            _run_defers(scope, False)
        return value


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
    if _call_stack is not None:
        _call_stack.append(Frame("<module>", scope))
    try:
        run_scoped_block(program.body, scope)
    finally:
        if _call_stack is not None:
            _call_stack.pop()
    if scope is not ctx:
        ctx.update(scope)
    return ctx
