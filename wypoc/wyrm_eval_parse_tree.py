"""Tree-walking evaluator prototype over the wypoc AST (wypoc/ast_nodes.py).

Leans entirely on Python's own runtime for values (int/float/str/bool) and
scoping (a plain dict passed in by the caller, just like Python's own
eval()/exec()) rather than modeling wyrm's value representation. Types are
ignored completely - no checking, no coercion beyond what Python does
naturally. This is a proof-of-concept for statement/expression evaluation,
not a real interpreter.
"""
import operator

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
    unevaluated (method bodies aren't run until message dispatch exists)."""

    def __init__(self, name, node: ast.ClassDef, closure: dict, bases: list):
        self.name = name
        self.node = node
        self.closure = closure
        self.bases: list["Class"] = bases
        self.slots: dict = {}
        self.methods: dict = {}
        self.coroutines: dict = {}
        self.init: ast.InitDef | None = None
        for member in node.body:
            if isinstance(member, ast.SlotDef):
                self.slots[member.name] = member
            elif isinstance(member, ast.FnDef):
                self.methods[member.name] = member
            elif isinstance(member, ast.CoDef):
                self.coroutines[member.name] = member
            elif isinstance(member, ast.InitDef):
                self.init = member

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


class ClassInstance:
    """A `new`-constructed object: its class plus its own slot storage."""

    def __init__(self, cls: Class):
        self.cls = cls
        self.attrs: dict = {}

    def __repr__(self):
        attrs = ", ".join(f"{k}={v.value!r}" for k, v in self.attrs.items())
        return f"<{self.cls.name} {attrs}>"


class Coroutine:
    """A user-defined co, stored but not (yet) drivable/resumable."""

    def __init__(self, name, node: ast.CoDef, closure: dict):
        self.name = name
        self.node = node
        self.closure = closure

    def __repr__(self):
        return f"Coroutine({self.name!r})"


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

    module_ctx: dict = {}
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
        bind(stmt.alias, lookup(path[-1], mod.ctx), ctx)
        return
    try:
        mod = import_module(path)
    except ImportError:
        if len(path) < 2:
            raise
        mod = import_module(path[:-1])
        bind(path[-1], lookup(path[-1], mod.ctx), ctx)
        return
    for name, var in mod.ctx.items():
        bind(name, unwrap(var), ctx)


class ReturnSignal(Exception):
    """Unwinds a function body back to call_function on `return`."""

    def __init__(self, value):
        self.value = value


class BreakSignal(Exception):
    """Unwinds a loop body back to the nearest enclosing while/for on `break`."""


class ContinueSignal(Exception):
    """Unwinds a loop body back to the nearest enclosing while/for on `continue`."""


BINOPS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "%": operator.mod,
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


def lookup(name: str, ctx: dict):
    if name not in ctx:
        raise NameError(f"undefined variable {name!r}")
    return unwrap(ctx[name])


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
    bind(name, value, ctx)


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


def _bind_params_and_run(node, local_ctx: dict, positional, kwargs, display_name: str):
    """Shared by call_function and call_overload: binds params/*args/**kwargs
    into local_ctx (already seeded with whatever closure/this/slots the
    caller wants visible), then runs the body and unwinds ReturnSignal."""
    kwargs = dict(kwargs)
    positional = list(positional)

    plain_params = []
    var_positional_name = None
    var_keyword_name = None
    for p in node.params:
        if isinstance(p, ast.PosOnlyMarker):
            continue
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

    try:
        eval_block(node.body, local_ctx)
    except ReturnSignal as ret:
        return ret.value
    return None


def call_function(fn: Function, positional, kwargs):
    local_ctx = dict(fn.closure)
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
    local_ctx = dict(overload.closure)
    if len(receivers) == 1 and isinstance(receivers[0], ClassInstance):
        local_ctx.update(receivers[0].attrs)
    bind_new("this", this_value, local_ctx)
    return _bind_params_and_run(overload.node, local_ctx, positional, kwargs, overload.node.name)


def call_value(func, positional, kwargs):
    if isinstance(func, Function):
        return call_function(func, positional, kwargs)
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


def instantiate(cls: Class, positional, kwargs) -> "ClassInstance":
    """`new Cls(...)`: builds a ClassInstance and fills its slots with their
    declared defaults. Passing constructor args would require running
    init()/a method body with `this` bound to the new instance, which needs
    message dispatch (not implemented yet), so that's left as a clear error
    for now rather than silently ignored."""
    if positional or kwargs:
        raise NotImplementedError(
            f"new {cls.name}(...) with constructor arguments requires calling "
            f"init() via message dispatch, which isn't implemented yet"
        )
    inst = ClassInstance(cls)
    for slot_name, (slot_def, owner) in cls.all_slots().items():
        value = eval_expr(slot_def.default, owner.closure) if slot_def.default is not None else None
        inst.attrs[slot_name] = Variable(value)
    return inst


def assign_target(target, value, ctx: dict) -> None:
    if isinstance(target, ast.NameTarget):
        bind(target.name, value, ctx)
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
        # '(1, 2, 3) -> Pair(1, Pair(2, Pair(3, NIL))); '(1, 2, '. 3) -> an
        # improper list whose final cdr is 3 instead of NIL. Built right to
        # left so each element becomes the car of a fresh cons cell around
        # whatever's already been built (see wyrm_builtins.Pair/cons).
        tail = eval_expr(node.tail, ctx) if node.tail is not None else wyrm_builtins.NIL
        result = tail
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
    if isinstance(node, ast.NewExpr):
        parts = node.type.parts
        if len(parts) > 1:
            cls = lookup(parts[-1], import_module(parts[:-1]).ctx)
        else:
            cls = lookup(parts[0], ctx)
        if not isinstance(cls, Class):
            raise TypeError(f"{'::'.join(parts)} does not name a class")
        positional, kwargs = eval_args(node.args, ctx)
        return instantiate(cls, positional, kwargs)
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
    if isinstance(node, ast.Try):
        value = eval_expr(node.value, ctx)
        if isinstance(value, wyrm_builtins.WyrmError):
            raise ReturnSignal(value)
        return value
    if isinstance(node, ast.Catch):
        value = eval_expr(node.value, ctx)
        if not isinstance(value, wyrm_builtins.WyrmError):
            return value
        if isinstance(node.handler, ast.Return):
            handler_value = eval_expr(node.handler.value, ctx) if node.handler.value is not None else None
            raise ReturnSignal(handler_value)
        return eval_expr(node.handler, ctx)
    raise NotImplementedError(f"cannot evaluate {type(node).__name__}")


def eval_stmt(stmt, ctx: dict) -> None:
    if isinstance(stmt, ast.Import):
        import_module(stmt.path)  # loads the whole chain, caching every prefix along the way
        bind(stmt.path[0], _module_cache[stmt.path[0]], ctx)
        return
    if isinstance(stmt, ast.FromImport):
        mod = import_module(stmt.path)
        for name in stmt.names:
            bind(name, lookup(name, mod.ctx), ctx)
        return
    if isinstance(stmt, ast.Using):
        eval_using(stmt, ctx)
        return
    if isinstance(stmt, ast.Assign):
        values = [eval_expr(v, ctx) for v in stmt.values]
        targets = stmt.targets
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
    if isinstance(stmt, ast.ExprStmt):
        eval_expr(stmt.value, ctx)
        return
    if isinstance(stmt, ast.Pass):
        return
    if isinstance(stmt, ast.Return):
        raise ReturnSignal(eval_expr(stmt.value, ctx) if stmt.value is not None else None)
    if isinstance(stmt, ast.If):
        if eval_expr(stmt.cond, ctx):
            eval_block(stmt.body, ctx)
            return
        for clause in stmt.elifs:
            if eval_expr(clause.cond, ctx):
                eval_block(clause.body, ctx)
                return
        if stmt.orelse is not None:
            eval_block(stmt.orelse, ctx)
        return
    if isinstance(stmt, ast.Continue):
        raise ContinueSignal()
    if isinstance(stmt, ast.Break):
        raise BreakSignal()
    if isinstance(stmt, ast.While):
        while eval_expr(stmt.cond, ctx):
            try:
                eval_block(stmt.body, ctx)
            except ContinueSignal:
                continue
            except BreakSignal:
                break
        return
    if isinstance(stmt, ast.For):
        broke = False
        for item in eval_expr(stmt.iter, ctx):
            bind(stmt.var, item, ctx)
            try:
                eval_block(stmt.body, ctx)
            except ContinueSignal:
                continue
            except BreakSignal:
                broke = True
                break
        if not broke and stmt.orelse is not None:
            eval_block(stmt.orelse, ctx)
        return
    if isinstance(stmt, ast.FnDef):
        signature = None if stmt.class_target is None else tuple(
            _lookup_class(n, ctx) for n in stmt.class_target
        )
        register_overload(stmt.name, signature, stmt, ctx, ctx)
        return
    if isinstance(stmt, ast.CoDef):
        bind(stmt.name, Coroutine(stmt.name, stmt, ctx), ctx)
        return
    if isinstance(stmt, ast.ClassDef):
        bases = [eval_expr(b, ctx) for b in stmt.bases]
        for base, base_expr in zip(bases, stmt.bases):
            if not isinstance(base, Class):
                raise TypeError(f"base class {base_expr!r} does not name a class (got {base!r})")
        cls = Class(stmt.name, stmt, ctx, bases)
        bind(stmt.name, cls, ctx)
        # A class-body method is equivalent to `fn [ThisClass] name(...)`
        # defined externally - see doc/language-spec.md's Messages section.
        for method_name, method_node in cls.methods.items():
            register_overload(method_name, (cls,), method_node, cls.closure, ctx)
        return
    raise NotImplementedError(f"cannot evaluate statement {type(stmt).__name__}")


def eval_block(stmts, ctx: dict) -> None:
    """Runs a list of statements in the given scope directly - wyrm has no
    block-level scoping, only function-level, so if/elif/else bodies share
    the enclosing ctx rather than getting a nested one."""
    for stmt in stmts:
        eval_stmt(stmt, ctx)


def eval_program(program: ast.Program, ctx: dict) -> dict:
    eval_block(program.body, ctx)
    return ctx
