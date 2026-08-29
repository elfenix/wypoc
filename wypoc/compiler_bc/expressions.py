"""Expression lowering (spec 7.1).

Every handler has the same contract:

    compile_expr(node, fn, dst=None) -> register reference

It returns the register the value ends up in.  With `dst` given the value
*must* land there; with `dst` None the handler is free to return a register
that already holds the value - a local or a parameter needs no code at all -
or to push a temp and evaluate into it.

Temp discipline follows Appendix A: operands are pushed, and a result is
pushed *above* them rather than reusing their slots, so `"Hello " + name`
lands its operand in L0 and its sum in L1 (`l:2`).  The whole run of temps is
released at the end of the enclosing statement or call window.  That costs a
slot or two in deeply nested arithmetic, and buys a lowering that is trivially
checkable against the worked example; reclaiming operand slots is an
optimization for later, not a v1 requirement.
"""

from wypoc import ast_nodes as ast

# The literal decoders are the interpreter's, deliberately: a literal must
# mean exactly the same thing compiled as it does interpreted, and a second
# copy of the escape table here is precisely the kind of thing that desyncs.
import struct

from wypoc.wyrm_eval_parse_tree import (
    _PRIMITIVE_TYPE_CHECKS,
    eval_char_literal,
    eval_number_literal,
    eval_string_literal,
)

from . import opcodes
from .errors import CompileError
from .handlers import EXPR_HANDLERS, dispatch, expression

# `x is int`: the names the interpreter answers from its own primitive-type
# table rather than by looking up a class (_matches_type). Taken from that
# table so the two can't drift: a name added there is a name `is` keeps
# agreeing about compiled.
PRIMITIVE_TYPE_NAMES = frozenset(_PRIMITIVE_TYPE_CHECKS)

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1

# spec 3.2/3.3: the three-address op each binary operator lowers to.
BINOPS = {
    "+": "add",
    "-": "sub",
    "*": "mul",
    "/": "div",
    "%": "mod",
    "**": "pow",
    "&": "band",
    "|": "bor",
    "^": "bxor",
    "<<": "shl",
    ">>": "shr",
    "==": "eq",
    "!=": "ne",
    "<": "lt",
    "<=": "le",
    ">": "gt",
    ">=": "ge",
    "<=>": "cmp3",
    "in": "in",
}

# spec 3.2: the one-operand ops.  The parser already normalizes `-`/`~` to
# these names, so the table is a straight pass-through.
UNARYOPS = {"neg": "neg", "inv": "inv", "not": "not"}

# `nil` reaches the compiler as an ordinary name, but it is a literal with a
# dedicated opcode rather than a binding to look up (spec 7.1).  The Unset
# value has no literal spelling in the language - `lunset` is reached through
# a forward declaration, `var x: T`.
NAME_LITERALS = {"nil": "lnil"}


def compile_expr(node, fn, dst=None):
    """Lower one expression, returning the register holding its value."""
    return dispatch(EXPR_HANDLERS, node, fn, dst)


def compile_into_window(node, fn) -> int:
    """Lower `node` so its value lands in the next free temp slot.

    Call windows need their arguments in consecutive slots, and most
    expressions naturally evaluate into the top of the temp stack - a nested
    call's own window starts exactly where the argument slot is, which is why
    Appendix A's inner `greet("World")` result is already sitting in the outer
    call's argument register.  A value that lives somewhere else (a local, a
    parameter, a lower temp) is copied up instead.
    """
    slot = fn.mark()
    reg = compile_expr(node, fn)
    if reg == slot and fn.mark() > slot:
        return reg
    fn.free_to(slot)
    target = fn.push()
    fn.emit(opcodes.pack_pairable("move", target, reg))
    return target


def _land(fn, dst, reg):
    """Put a value already in `reg` into `dst`, if the caller demanded one."""
    if dst is None or dst == reg:
        return reg
    fn.emit(opcodes.pack_pairable("move", dst, reg))
    return dst


def _target(fn, dst):
    return fn.push() if dst is None else dst


# --------------------------------------------------------------------------
# literals


@expression(ast.Str)
def _string(node, fn, dst):
    index = fn.module.image.add_static(eval_string_literal(node.value))
    reg = _target(fn, dst)
    fn.emit(opcodes.pack_pairable("lconst", index, reg))
    return reg


@expression(ast.Num)
def _number(node, fn, dst):
    value = eval_number_literal(node.value)
    if isinstance(value, float):
        reg = _target(fn, dst)
        # f32, not f64: the machine's native float is single precision
        # (spec 1), so the literal is rounded here rather than at load time.
        (bits,) = struct.unpack("<I", struct.pack("<f", value))
        fn.emit(opcodes.pack("f32", a0=reg, w1=bits))
        return reg
    if not INT32_MIN <= value <= INT32_MAX:
        # spec 7.1: no bigint on the target, so this is a refusal, not a
        # narrowing.
        raise CompileError(
            f"integer literal {value} does not fit in an i32", node.pos
        )
    reg = _target(fn, dst)
    fn.emit(opcodes.pack_pairable("i8", reg, value))
    return reg


@expression(ast.Bool)
def _bool(node, fn, dst):
    reg = _target(fn, dst)
    fn.emit(opcodes.pack("lbool", a0=reg, f=1 if node.value else 0))
    return reg


@expression(ast.Char)
def _char(node, fn, dst):
    """A char literal is its u32 codepoint - the same value indexing a string
    produces, so the two are interchangeable (see eval_char_literal)."""
    reg = _target(fn, dst)
    fn.emit(opcodes.pack_pairable("i8", reg, eval_char_literal(node.value)))
    return reg


@expression(ast.Symbol)
def _symbol(node, fn, dst):
    index = fn.module.image.add_symbol(node.name)
    reg = _target(fn, dst)
    fn.emit(opcodes.pack_pairable("lsym", index, reg))
    return reg


NOT_CONSTANT = object()


def constant_value(node):
    """The Python value of a literal, or `NOT_CONSTANT` if it is not one.

    Used where the format can only hold a constant - slot and parameter
    defaults - so that anything else is refused rather than half-compiled.
    """
    if isinstance(node, ast.Str):
        return eval_string_literal(node.value)
    if isinstance(node, ast.Num):
        return eval_number_literal(node.value)
    if isinstance(node, ast.Char):
        return eval_char_literal(node.value)
    if isinstance(node, ast.Bool):
        return node.value
    if isinstance(node, ast.Name) and node.id == "nil":
        return None
    return NOT_CONSTANT


# --------------------------------------------------------------------------
# names (the resolution ladder, spec 6.3)


def reg_of(where):
    return where[1]


def _land_free(fn, dst, slot):
    """Read a free global into `dst` - the one shape every external name
    lowers to now."""
    reg = _target(fn, dst)
    fn.emit(opcodes.pack_pairable("gget", slot, reg))
    return reg


def resolve_name(name, fn, dst, pos=None):
    """Lower a bare identifier: local, then parameter/capture, then module
    global, then a free global slot.

    Nothing is ever resolved statically across a module boundary - an
    external name becomes a free global slot filled by whatever supplies it,
    even when the compiler could peek at the dependency (spec 6.3).
    """
    where = fn.storage(name)
    if where is not None:
        kind, slot = where
        if kind == "reg":
            if name in fn.cells:
                # The register holds the box, not the value (spec 8.3).
                out = _target(fn, dst)
                fn.emit(opcodes.pack("getidx", a0=out, a1=reg_of(where), a2=fn.zero))
                return out
            return _land(fn, dst, slot)
        reg = _target(fn, dst)
        if kind == "slot":
            fn.emit(opcodes.pack("getattr", a0=reg, a1=fn.this_reg, a2=slot))
        else:
            fn.emit(opcodes.pack_pairable("gget", slot, reg))
        return reg

    module = fn.module
    if name not in module.builtins and not module.has_wildcard_import:
        # With no wildcard in scope there is nowhere left for the name to come
        # from, so this is a refusal rather than a free slot nothing could
        # ever fill (spec 6.3).
        raise CompileError(f"undefined name {name!r}", pos)
    # A free global: still a plain `gget`, because a name this module does not
    # define is a global slot like any other - one that something else fills
    # (doc/addendum.md). Nothing is resolved across a module boundary here,
    # which was the property the relocation table existed for; the slot keeps
    # it, since the compiler records the name and never the target.
    return _land_free(fn, dst, module.declare_free_global(name))


@expression(ast.Name)
def _name(node, fn, dst):
    literal = NAME_LITERALS.get(node.id)
    if literal is not None and fn.lookup(node.id) is None:
        reg = _target(fn, dst)
        fn.emit(opcodes.pack(literal, a0=reg))
        return reg
    return resolve_name(node.id, fn, dst, node.pos)


# --------------------------------------------------------------------------
# operators


@expression(ast.UnaryOp)
def _unary(node, fn, dst):
    op = UNARYOPS.get(node.op)
    if op is None:
        raise CompileError(
            f"the bytecode compiler does not support the unary {node.op!r} "
            "operator yet",
            node.pos,
        )
    src = compile_expr(node.operand, fn)
    reg = _target(fn, dst)
    fn.emit(opcodes.pack_pairable(op, reg, src))
    return reg


@expression(ast.BinOp)
def _binop(node, fn, dst):
    if node.op in ("and", "or"):
        return _short_circuit(node, fn, dst)
    op = BINOPS.get(node.op)
    if op is None:
        raise CompileError(
            f"the bytecode compiler does not support the {node.op!r} operator yet",
            node.pos,
        )
    lhs = compile_expr(node.left, fn)
    rhs = compile_expr(node.right, fn)
    reg = _target(fn, dst)
    fn.emit(opcodes.pack(op, a0=reg, a1=lhs, a2=rhs))
    return reg


# --------------------------------------------------------------------------
# calls


def _short_circuit(node, fn, dst):
    """`a and b` / `a or b`: the result register holds `a`, and `b` only
    overwrites it when `a` did not already decide the answer (spec 7.1)."""
    reg = _target(fn, dst)
    mark = fn.mark()
    compile_expr(node.left, fn, dst=reg)
    fn.free_to(mark)
    done = fn.new_label()
    fn.emit_jump("jf" if node.op == "and" else "jt", done, cond=reg)
    compile_expr(node.right, fn, dst=reg)
    fn.free_to(mark)
    fn.mark_label(done)
    return reg


@expression(ast.TypeCheck)
def _type_check(node, fn, dst):
    """`x is T` / `x is A | B`: the type operand is a class value, so a union
    becomes a tuple of them and `is` does the rest (spec 7.1)."""
    value = compile_expr(node.value, fn)
    if len(node.types) == 1:
        types = _type_value(node.types[0], fn, None)
    else:
        base = fn.mark()
        for type_expr in node.types:
            _type_value(type_expr, fn, fn.push())
        types = fn.push()
        fn.emit(opcodes.pack("tuple", a0=types, a1=opcodes.L(base), f=len(node.types)))
    reg = _target(fn, dst)
    fn.emit(opcodes.pack("is", a0=reg, a1=value, a2=types))
    return reg


def _type_value(type_expr, fn, dst):
    """Load the class a type annotation names.

    A generic parameter (`list[int]`) collapses to the base type: runtime type
    enforcement only reaches the outer type per the language spec, so carrying
    the parameter into the image would promise a check the VM will not make.

    A primitive type is loaded as its *name* instead - a string constant the
    `is` op reads as "the primitive type called this" (spec 6.3). It has to
    be the name rather than whatever the name is bound to, because the
    interpreter answers these from a name table (_matches_type) and the
    bindings don't line up with it: `error` is a Class, `tuple` and `pair`
    are constructor functions, `str` is a contextual builtin, and `list`,
    `dict` and `uint` are not bound at all. Resolving them as classes is
    what made `error(...) is error` answer false compiled and true
    interpreted, and `x is list` a compile error.

    Like the interpreter, this looks at the *last* component, so a qualified
    `mod::error` is the primitive test too - the same answer either way,
    which is the point.
    """
    parts = list(type_expr.parts)
    if not parts:
        raise CompileError("empty type expression", type_expr.pos)
    if parts[-1] in PRIMITIVE_TYPE_NAMES:
        index = fn.module.image.add_static(parts[-1])
        reg = _target(fn, dst)
        fn.emit(opcodes.pack_pairable("lconst", index, reg))
        return reg
    if len(parts) == 1:
        return resolve_name(parts[0], fn, dst, type_expr.pos)
    # A qualified path is never resolved at compile time (spec 6.3): it
    # becomes a free slot named by the whole path, filled by the import that
    # makes the path reachable.
    return _land_free(fn, dst, fn.module.declare_free_global("::".join(parts)))


@expression(ast.Call)
def _call(node, fn, dst):
    return _land(fn, dst, compile_call(node, fn, 1))


def compile_call(node, fn, nres) -> int:
    if has_spread_or_keyword(node.args):
        return compile_va(node, fn, nres, "call_va")
    return _compile_call(node, fn, nres)


def _compile_call(node, fn, nres) -> int:
    """`f(a, b)`: callee and arguments into a contiguous window, then `call`.

    Results land back at the window base with nil backfill (spec 1), so the
    base register is the call's first result and the argument slots above it
    are released - except the ones a multi-result call still occupies, which
    stay reserved until the caller has read them out.
    """
    if len(node.args) > 128:
        raise CompileError(
            f"{len(node.args)} arguments, over the 128-parameter call limit", node.pos
        )
    if nres > 128:
        raise CompileError(
            f"{nres} results wanted, over the 128-result call limit", node.pos
        )
    base = fn.mark()
    window = fn.push()
    if isinstance(node.func, ast.Name):
        # A name in callee position is a function reference, which is what
        # the free slot records (spec 6.3).
        resolve_name(node.func.id, fn, window, node.func.pos)
    else:
        compile_expr(node.func, fn, dst=window)
    for arg in node.args:
        compile_into_window(arg, fn)
    fn.emit(opcodes.pack("call", a0=window, f=len(node.args), a1=nres))
    held = base + max(nres, 1)
    while fn.mark() < held:
        fn.push()
    fn.free_to(held)
    return window


# --------------------------------------------------------------------------
# collections (spec 7.1)


def _window_of(items, fn):
    """Evaluate `items` into consecutive temps and return `(base, count)`."""
    base = fn.mark()
    for item in items:
        compile_into_window(item, fn)
    return opcodes.L(base), len(items)


def _sequence(node, fn, dst, op, items, limit=None):
    if limit is not None and len(items) > limit:
        raise CompileError(
            f"{len(items)} elements, over the {limit}-element {op} limit", node.pos
        )
    base, count = _window_of(items, fn)
    reg = _target(fn, dst)
    fn.emit(opcodes.pack(op, a0=reg, a1=base, f=count))
    return reg


@expression(ast.Array)
def _array(node, fn, dst):
    return _sequence(node, fn, dst, "list", node.items)


@expression(ast.Tuple)
def _tuple(node, fn, dst):
    # 128 elements is the language's tuple limit; larger data is a list.
    return _sequence(node, fn, dst, "tuple", node.items, limit=128)


@expression(ast.Pair)
def _plist(node, fn, dst):
    return _sequence(node, fn, dst, "plist", node.elements)


@expression(ast.Dict)
def _dict(node, fn, dst):
    """`dict` reads a window of k, v, k, v..., so the entries are flattened
    into one run of temps (spec 3.3)."""
    flattened = []
    for entry in node.entries:
        flattened += [entry.key, entry.value]
    base, _count = _window_of(flattened, fn)
    reg = _target(fn, dst)
    fn.emit(opcodes.pack("dict", a0=reg, a1=base, f=len(node.entries)))
    return reg


# --------------------------------------------------------------------------
# object access


def own_slot_index(node, fn):
    """The fixed slot number for `this.name`, when the optimization applies.

    Slot access inside a method stays symbolic by default, because an
    external superclass makes absolute offsets unknowable at compile time
    (spec 7.1).  Only when the whole inheritance chain is this module's - and
    only when the module asks for it - may an offset be used instead.
    """
    if not isinstance(node.obj, ast.ThisRef) or fn.this_class is None:
        return None
    if not fn.module.slot_optimization:
        return None
    return fn.module.slot_index(fn.this_class, node.name)


@expression(ast.Attr)
def _attr(node, fn, dst):
    slot = own_slot_index(node, fn)
    obj = compile_expr(node.obj, fn)
    reg = _target(fn, dst)
    if slot is not None:
        fn.emit(opcodes.pack("getslot", a0=reg, a1=obj, a2=slot))
        return reg
    symbol = fn.module.image.add_symbol(node.name)
    fn.emit(opcodes.pack("getattr", a0=reg, a1=obj, a2=symbol))
    return reg


@expression(ast.Index)
def _index(node, fn, dst):
    obj = compile_expr(node.obj, fn)
    index = compile_expr(node.index, fn)
    reg = _target(fn, dst)
    fn.emit(opcodes.pack("getidx", a0=reg, a1=obj, a2=index))
    return reg


# --------------------------------------------------------------------------
# closures and inlined blocks


@expression(ast.Lambda)
def _lambda(node, fn, dst):
    # Imported here rather than at module scope: functions.py compiles bodies
    # through statements.py, which is built on this module.
    from .functions import compile_closure_expr

    reg = _target(fn, dst)
    return compile_closure_expr(node, fn, reg, "<lambda>")


@expression(ast.Do)
def _do(node, fn, dst):
    """A `do:` block is inlined, not called: it is only scoping, and the
    compiler resolves scoping statically (spec 7.1).  Its names were given
    slots in this frame by the declaration pass."""
    from .statements import compile_body

    reg = _target(fn, dst)
    compile_body(node.body, fn, reg)
    return reg


# --------------------------------------------------------------------------
# error flow (spec 7.1)


@expression(ast.Try)
def _try(node, fn, dst):
    """`try e`: hand an error straight back to the caller, keeping the value
    otherwise.  `return` is what runs the frame's defers, so this is a real
    return rather than a jump to the epilogue."""
    reg = _target(fn, dst)
    mark = fn.mark()
    compile_expr(node.value, fn, dst=reg)
    fn.free_to(mark)
    ok = fn.new_label()
    fn.emit_jump("jnerr", ok, cond=reg)
    fn.emit_return(reg)
    fn.mark_label(ok)
    return reg


@expression(ast.Catch)
def _catch(node, fn, dst):
    """`e catch h` replaces an error with the handler's value; `e catch
    return v` returns `v` from the enclosing function instead."""
    reg = _target(fn, dst)
    mark = fn.mark()
    compile_expr(node.value, fn, dst=reg)
    fn.free_to(mark)
    done = fn.new_label()
    fn.emit_jump("jnerr", done, cond=reg)
    if isinstance(node.handler, ast.Return):
        if node.handler.value is None:
            out = fn.push()
            fn.emit(opcodes.pack("lnil", a0=out))
        else:
            out = compile_expr(node.handler.value, fn)
        fn.emit_return(out)
        fn.free_to(mark)
    else:
        compile_expr(node.handler, fn, dst=reg)
        fn.free_to(mark)
    fn.mark_label(done)
    return reg


# --------------------------------------------------------------------------
# `this`, messages and namespaces


@expression(ast.ThisRef)
def _this(node, fn, dst):
    if fn.this_reg is None:
        raise CompileError("`this` is only meaningful inside a method", node.pos)
    return _land(fn, dst, fn.this_reg)


def message_index(node, fn):
    """The `messages` entry a message name binds through.

    A single-component path that resolves to nothing pre-existing *creates*
    the message identity, which is how a module's own messages come into
    being (spec 4.8).  `recv ! mod::name` names another module's message
    explicitly instead.
    """
    path = [node.module, node.name] if getattr(node, "module", None) else [node.name]
    return fn.reference(path, node.pos)


def compile_message(node, fn, nres, receivers=None):
    """Lay out a message window and emit `msg`; returns the window base.

    The receiver sits at the base and the arguments follow it, exactly as a
    call's callee does - which is what lets results land back at the base
    (spec 3.3).
    """
    args = node.args or []
    if receivers is None and has_spread_or_keyword(args):
        return compile_va(
            node, fn, nres, "msg_va",
            reloc=message_index(node, fn), receiver=node.obj,
        )
    if len(args) > 128:
        raise CompileError(
            f"{len(args)} arguments, over the 128-parameter call limit", node.pos
        )
    reloc = message_index(node, fn)
    base = fn.mark()
    window = fn.push()
    if receivers is None:
        compile_expr(node.obj, fn, dst=window)
    else:
        # `(a, b) ! name(...)`: the receivers are tupled first, and the tuple
        # is the receiver the VM dispatches on (spec 7.1).
        inner = fn.mark()
        for item in receivers:
            compile_into_window(item, fn)
        fn.emit(
            opcodes.pack(
                "tuple", a0=window, a1=opcodes.L(inner), f=len(receivers)
            )
        )
        fn.free_to(inner)
    for arg in args:
        compile_into_window(arg, fn)
    fn.emit(opcodes.pack("msg", a0=window, f=len(args), a1=reloc, a2=nres))
    held = base + max(nres, 1)
    while fn.mark() < held:
        fn.push()
    fn.free_to(held)
    return window


@expression(ast.Message)
def _message(node, fn, dst):
    if node.args is None:
        # `recv ! name` with no call: the bound closure, not a dispatch.
        recv = compile_expr(node.obj, fn)
        reloc = message_index(node, fn)
        reg = _target(fn, dst)
        fn.emit(opcodes.pack("getmsg", a0=reg, a1=recv, a2=reloc))
        return reg
    return _land(fn, dst, compile_message(node, fn, 1))


@expression(ast.MessageTupleExpr)
def _message_tuple(node, fn, dst):
    return _land(fn, dst, compile_message(node, fn, 1, receivers=node.items))


@expression(ast.SuperCall)
def _super(node, fn, dst):
    """`super(args)` chains to the next-most-general method of the dispatch
    already in progress, so it only means anything inside one."""
    if fn.this_reg is None:
        raise CompileError("`super` is only meaningful inside a method", node.pos)
    base = fn.mark()
    for arg in node.args:
        compile_into_window(arg, fn)
    if not node.args:
        fn.push()  # the window still needs a slot for the result
    fn.emit(opcodes.pack("super", a0=opcodes.L(base), f=len(node.args), a1=1))
    fn.free_to(base + 1)
    return _land(fn, dst, opcodes.L(base))


def scope_path(node):
    """`a::b::c` flattened to `["a", "b", "c"]`, or None if its root is not a
    plain name."""
    parts = [node.name]
    obj = node.obj
    while isinstance(obj, ast.Scope):
        parts.append(obj.name)
        obj = obj.obj
    if not isinstance(obj, ast.Name):
        return None
    parts.append(obj.id)
    parts.reverse()
    return parts


@expression(ast.Scope)
def _scope(node, fn, dst):
    """`a::b` reads a namespace member.

    Three cases, in order: a runtime value on the left is a dynamic lookup
    (`getscope`); a module-local class's own static is the module global it
    was allotted (spec 4.7); anything else is a name from outside this module
    and so a free global slot, never resolved at compile time (spec 6.3).
    """
    parts = scope_path(node)
    if parts is not None and fn.lookup(parts[0]) is None:
        global_index = fn.module.class_static(parts)
        if global_index is not None:
            reg = _target(fn, dst)
            fn.emit(opcodes.pack_pairable("gget", global_index, reg))
            return reg
        if not fn.module.is_local_class(parts[0]):
            return _land_free(
                fn, dst, fn.module.declare_free_global("::".join(parts))
            )
    obj = compile_expr(node.obj, fn)
    symbol = fn.module.image.add_symbol(node.name)
    reg = _target(fn, dst)
    fn.emit(opcodes.pack("getscope", a0=reg, a1=obj, a2=symbol))
    return reg


# --------------------------------------------------------------------------
# coroutines (spec 7.1)


def compile_yield(node, fn, dst):
    """`yield v` suspends with a window of values; the value it evaluates to
    is what the resumer sent, written back at the window base."""
    if not fn.is_coroutine:
        raise CompileError("`yield` is only meaningful inside a `co`", node.pos)
    if node.from_:
        sub = compile_expr(node.value, fn)
        reg = _target(fn, dst)
        fn.emit(opcodes.pack("yield_from", a0=reg, a1=sub))
        return reg
    base = fn.mark()
    if node.value is None:
        fn.emit(opcodes.pack("lnil", a0=fn.push()))
        count = 1
    elif isinstance(node.value, ast.Tuple):
        for item in node.value.items:
            compile_into_window(item, fn)
        count = len(node.value.items)
    else:
        compile_into_window(node.value, fn)
        count = 1
    if count > 128:
        raise CompileError(
            f"{count} yielded values, over the 128-result limit", node.pos
        )
    fn.emit(opcodes.pack("yield", a0=opcodes.L(base), f=count))
    fn.free_to(base + 1)
    return _land(fn, dst, opcodes.L(base))


@expression(ast.Yield)
def _yield(node, fn, dst):
    return compile_yield(node, fn, dst)


# --------------------------------------------------------------------------
# spreads and keyword arguments (spec 7.1)


def has_spread_or_keyword(args) -> bool:
    return any(
        isinstance(arg, (ast.Kwarg, ast.SpreadPos, ast.SpreadKw)) for arg in args
    )


def _collection(fn, op, exprs, dst=None):
    """Build one `tuple`/`dict` from a run of expressions."""
    base = fn.mark()
    for item in exprs:
        compile_into_window(item, fn)
    reg = fn.push() if dst is None else dst
    count = len(exprs) // 2 if op == "dict" else len(exprs)
    fn.emit(opcodes.pack(op, a0=reg, a1=opcodes.L(base), f=count))
    return reg


def _concatenate(fn, segments):
    """Fold segments together with `add`.

    "Runtime concat" (spec 7.1) is `add`: the VM dispatches it through the
    `__add__` family, which is where a tuple's or dict's own join lives.  A
    single segment folds to itself, so the common no-spread case emits no
    extra instruction.
    """
    result = segments[0]
    for segment in segments[1:]:
        joined = fn.push()
        fn.emit(opcodes.pack("add", a0=joined, a1=result, a2=segment))
        result = joined
    return result


def _positional_argument_tuple(args, fn):
    segments = []
    run = []
    for arg in args:
        if isinstance(arg, ast.SpreadPos):
            if run:
                segments.append(_collection(fn, "tuple", run))
                run = []
            segments.append(compile_expr(arg.value, fn))
        elif not isinstance(arg, (ast.Kwarg, ast.SpreadKw)):
            run.append(arg)
    if run or not segments:
        segments.append(_collection(fn, "tuple", run))
    return _concatenate(fn, segments)


def _keyword_argument_dict(args, fn):
    segments = []
    run = []
    for arg in args:
        if isinstance(arg, ast.SpreadKw):
            if run:
                segments.append(_collection(fn, "dict", run))
                run = []
            segments.append(compile_expr(arg.value, fn))
        elif isinstance(arg, ast.Kwarg):
            run += [ast.Str(f'"{arg.name}"', pos=arg.pos), arg.value]
    if run or not segments:
        segments.append(_collection(fn, "dict", run))
    return _concatenate(fn, segments)


def compile_va(node, fn, nres, op, reloc=None, receiver=None):
    """`f(*a, key=1, **k)` and its message twin.

    The window is fixed at three registers - callee (or receiver), the
    positional tuple, the keyword dict - so every spread shape reaches the VM
    the same way (spec 7.1).
    """
    base = fn.mark()
    window = fn.push()
    # The three window slots are reserved before anything is built, so the
    # scratch registers the tuple and dict need sit above them and can be
    # released without touching what the instruction will read.
    positional_slot = fn.push()
    keyword_slot = fn.push()
    if receiver is None:
        if isinstance(node.func, ast.Name):
            resolve_name(node.func.id, fn, window, node.func.pos)
        else:
            compile_expr(node.func, fn, dst=window)
    else:
        compile_expr(receiver, fn, dst=window)

    scratch = fn.mark()
    positional = _positional_argument_tuple(node.args, fn)
    fn.emit(opcodes.pack_pairable("move", positional_slot, positional))
    fn.free_to(scratch)
    keywords = _keyword_argument_dict(node.args, fn)
    fn.emit(opcodes.pack_pairable("move", keyword_slot, keywords))
    fn.free_to(scratch)

    if op == "call_va":
        fn.emit(opcodes.pack(op, a0=window, a1=nres))
    else:
        fn.emit(opcodes.pack(op, a0=window, a1=reloc, a2=nres))
    held = base + max(nres, 3)
    while fn.mark() < held:
        fn.push()
    fn.free_to(base + max(nres, 1))
    return window


# --------------------------------------------------------------------------
# `foo::$ast` - a definition's own tree, as a value


def emit_value(value, fn, dst=None):
    """Emit code building a plain wyrm data value.

    The s-expression of a tree is made of symbols, strings, numbers, bools,
    nil, lists and proper pair lists - every one of which this compiler
    already knows how to build - so a tree that a decorator or a DSL reads at
    run time is constructed by the code rather than carried as an AST.  A
    compiled module holds no ASTs (spec 7.2); it holds the instructions that
    rebuild the one s-expression it was asked for.
    """
    from wypoc.wyrm_builtins import NIL, Pair, Symbol

    if isinstance(value, Symbol):
        # `.name`, not str(): a Symbol prints as `'name`, and the leading
        # quote is its spelling, not part of the name being interned.
        index = fn.module.image.add_symbol(value.name)
        reg = _target(fn, dst)
        fn.emit(opcodes.pack_pairable("lsym", index, reg))
        return reg
    if value is None or value is NIL:
        reg = _target(fn, dst)
        fn.emit(opcodes.pack("lnil", a0=reg))
        return reg
    if isinstance(value, bool):
        reg = _target(fn, dst)
        fn.emit(opcodes.pack("lbool", a0=reg, f=1 if value else 0))
        return reg
    if isinstance(value, int):
        if not INT32_MIN <= value <= INT32_MAX:
            raise CompileError(f"integer {value} in a tree does not fit in an i32")
        reg = _target(fn, dst)
        fn.emit(opcodes.pack_pairable("i8", reg, value))
        return reg
    if isinstance(value, float):
        (bits,) = struct.unpack("<I", struct.pack("<f", value))
        reg = _target(fn, dst)
        fn.emit(opcodes.pack("f32", a0=reg, w1=bits))
        return reg
    if isinstance(value, str):
        index = fn.module.image.add_static(value)
        reg = _target(fn, dst)
        fn.emit(opcodes.pack_pairable("lconst", index, reg))
        return reg
    if isinstance(value, (list, tuple)):
        return _emit_sequence(list(value), fn, dst, "list")
    if isinstance(value, Pair):
        return _emit_sequence(_proper_list(value), fn, dst, "plist")
    raise CompileError(
        f"{type(value).__name__} has no bytecode form; a tree can only hold "
        "symbols, strings, numbers, bools, nil, lists and pair lists"
    )


def _emit_sequence(items, fn, dst, op):
    """Build one list or pair list out of a contiguous window of temps.

    The destination is allocated *before* the window and the window is
    released *after* the instruction reads it. Both halves matter for nesting:
    an element that is itself a sequence borrows temps of its own, and unless
    it hands them back, the next element of this window lands above them and
    the window stops being contiguous - which the instruction has no way to
    say, so it would quietly build the wrong value.
    """
    reg = _target(fn, dst)
    base = fn.mark()
    for item in items:
        emit_value(item, fn, fn.push())
    # An empty window reads no registers, so its base is the destination
    # rather than a slot one past the frame.
    fn.emit(opcodes.pack(op, a0=reg, a1=opcodes.L(base) if items else reg, f=len(items)))
    fn.free_to(base)
    return reg


def _proper_list(pair):
    """A NIL-terminated cons chain flattened to its elements.

    `sexpr.encode` only ever builds proper lists (`_pairs`), so an improper
    tail means something other than an encoded tree got here.
    """
    from wypoc.wyrm_builtins import NIL, Pair

    items = []
    node = pair
    while isinstance(node, Pair):
        items.append(node.car)
        node = node.cdr
    if node is not NIL and node is not None:
        raise CompileError(
            "a pair with an improper tail has no bytecode form; only proper "
            "pair lists do"
        )
    return items


@expression(ast.AstRef)
def _ast_ref(node, fn, dst):
    """`foo::$ast` - the tree of the definition `foo` names.

    Resolved here rather than at run time: the compiler knows every definition
    in the module it is compiling, and decorators have already run, so the
    tree found is the one that would have been bound - the same "describes the
    definition, not the binding" rule the interpreter documents.  A name from
    outside this module has no tree to reach: nothing across a module boundary
    is resolved at compile time (spec 6.3), and a compiled dependency carries
    no ASTs at all.
    """
    from wypoc import sexpr as sexpr_module

    if node.field != "ast":
        raise CompileError(f"unknown definition field `${node.field}`", node.pos)
    if not isinstance(node.obj, ast.Name):
        raise CompileError(
            "`::$ast` needs the name of a definition in this module", node.pos
        )
    definition = fn.module.definitions.get(node.obj.id)
    if definition is None:
        raise CompileError(
            f"`{node.obj.id}::$ast` names no fn, co or class defined in this "
            "module; a compiled module carries no trees to reach across",
            node.pos,
        )
    try:
        encoded = sexpr_module.encode(definition)
    except sexpr_module.SexprError as error:
        raise CompileError(
            f"{node.obj.id}::$ast: {error}", node.pos
        ) from error
    return emit_value(encoded, fn, dst)
