"""Statement lowering (spec 7.2).

Statements are compiled in two passes over a body: `declare_names` walks it
first and gives every name it introduces a home - an L slot inside a function,
a module global slot at the top level - then the handlers here emit code with
the temp stack starting directly above the named locals.  The order matters: a
local declared after temps had been allocated would land on one.

Statements have values.  A function's value is its last statement's, so every
handler takes a `result` register, which is non-None only for the statement in
that position.  Constructs that produce no value of their own get a `lnil`
into it instead, which is what the interpreter answers for a loop, a
declaration, or an `if` whose condition was false.
"""

from wypoc import ast_nodes as ast

from . import opcodes
from .errors import CompileError
from .expressions import compile_call, compile_expr, compile_into_window
from .handlers import (
    STATEMENT_HANDLERS,
    construct_name,
    dispatch,
    expression,
    statement,
)

# Constructs whose own lowering writes the statement's value; everything else
# is worth `lnil` when it lands in the value position.
VALUE_PRODUCING = (ast.ExprStmt, ast.If, ast.Yield)

# Constructs that never fall through to the next statement, so a `lnil` ahead
# of one would be dead code.
NO_FALLTHROUGH = (ast.Return, ast.Break, ast.Continue)


def compile_statement(node, fn, result=None):
    # Every instruction a statement emits belongs to the statement's line;
    # that is the whole of the debug line table (spec 4.10).
    if node.pos:
        fn.line = node.pos[0]
    return dispatch(STATEMENT_HANDLERS, node, fn, result)


def compile_body(body, fn, result=None):
    """Compile a statement list, releasing each statement's temps after it.

    `result`, when given, is the register the block's value lands in - which
    is the last statement's value, or nil if that statement has none.

    A statement list is also a scope: whatever the declaration pass allotted
    for it is in force here and gone afterwards, so an inner `var` shadows an
    outer name of the same spelling rather than writing through to it, and is
    not reachable once the block ends. A frame's own top-level body has no
    scope of its own to push - its names are the frame's.
    """
    pushed = fn.push_block(body)
    try:
        for index, node in enumerate(body):
            last = index == len(body) - 1
            wants_value = last and result is not None
            mark = fn.mark()
            if wants_value and not isinstance(node, VALUE_PRODUCING):
                if not isinstance(node, NO_FALLTHROUGH):
                    fn.emit(opcodes.pack("lnil", a0=result))
                compile_statement(node, fn)
            else:
                compile_statement(node, fn, result if wants_value else None)
            fn.free_to(mark)
    finally:
        if pushed:
            fn.pop_block()


# --------------------------------------------------------------------------
# assignment targets


def target_name(target) -> str:
    if isinstance(target, (ast.NameTarget, ast.VarTarget)):
        return target.name
    raise CompileError(
        f"the bytecode compiler does not support {construct_name(target)} yet",
        getattr(target, "pos", None),
    )


def store_target(target, value, fn, pos):
    """Assign `value` to one target of any shape (spec 7.2)."""
    if isinstance(target, (ast.NameTarget, ast.VarTarget)):
        store_expr(target.name, value, fn, pos)
    elif isinstance(target, ast.AttrTarget):
        _store_attr(target, value, fn, pos)
    elif isinstance(target, ast.IndexTarget):
        _store_index(target, value, fn, pos)
    else:
        raise CompileError(
            f"the bytecode compiler does not support {construct_name(target)} yet",
            getattr(target, "pos", pos),
        )


def store_target_reg(target, reg, fn, pos):
    """Assign a value that already sits in `reg` to one target."""
    if isinstance(target, (ast.NameTarget, ast.VarTarget)):
        store_reg(target.name, reg, fn, pos)
        return
    _emit_store(fn, *_target_base(target, fn, pos), reg)


def _store_attr(target, value, fn, pos):
    _store_through(target, value, fn, pos)


def _store_index(target, value, fn, pos):
    _store_through(target, value, fn, pos)


def _store_through(target, value, fn, pos):
    obj, symbol, index, slot = _target_base(target, fn, pos)
    mark = fn.mark()
    reg = fn.push()
    compile_expr(value, fn, dst=reg)
    _emit_store(fn, obj, symbol, index, slot, reg)
    fn.free_to(mark)


def _emit_store(fn, obj, symbol, index, slot, reg):
    if slot is not None:
        fn.emit(opcodes.pack("setslot", a0=obj, a1=slot, a2=reg))
    elif symbol is not None:
        fn.emit(opcodes.pack("setattr", a0=obj, a1=symbol, a2=reg))
    else:
        fn.emit(opcodes.pack("setidx", a0=obj, a1=index, a2=reg))


def _target_base(target, fn, pos):
    """Evaluate everything left of the final `.name`/`[i]` of a target.

    `a.b.c = v` reads `a.b` and stores into its `c`; `grid[i][j] = v` reads
    `grid[i]` and stores at `j`.  Returns `(object register, symbol index or
    None, index register or None)`.
    """
    if isinstance(target, ast.AttrTarget):
        # `AttrTarget.base` is either a bare name or `this` (the only two
        # things the grammar lets start an attribute target).
        this_target = not isinstance(target.base, str)
        if this_target:
            obj = compile_expr(target.base, fn)
        else:
            obj = compile_expr(ast.Name(target.base, pos=target.pos), fn)
        for name in target.attrs[:-1]:
            symbol = fn.module.image.add_symbol(name)
            step = fn.push()
            fn.emit(opcodes.pack("getattr", a0=step, a1=obj, a2=symbol))
            obj = step
            this_target = False
        last = target.attrs[-1]
        if this_target and fn.this_class is not None and fn.module.slot_optimization:
            slot = fn.module.slot_index(fn.this_class, last)
            if slot is not None:
                return obj, None, None, slot
        return obj, fn.module.image.add_symbol(last), None, None
    if isinstance(target, ast.IndexTarget):
        obj = _target_value(target.base, fn, pos)
        return obj, None, compile_expr(target.index, fn), None
    raise CompileError(
        f"the bytecode compiler does not support {construct_name(target)} yet",
        getattr(target, "pos", pos),
    )


def _target_value(target, fn, pos):
    """A target read as an expression - what `grid[i][j] = v` needs of
    `grid[i]` before it can store into it."""
    if isinstance(target, ast.NameTarget):
        return compile_expr(ast.Name(target.name, pos=target.pos), fn)
    obj, symbol, index, slot = _target_base(target, fn, pos)
    reg = fn.push()
    if slot is not None:
        fn.emit(opcodes.pack("getslot", a0=reg, a1=obj, a2=slot))
    elif symbol is not None:
        fn.emit(opcodes.pack("getattr", a0=reg, a1=obj, a2=symbol))
    else:
        fn.emit(opcodes.pack("getidx", a0=reg, a1=obj, a2=index))
    return reg


def store_expr(name, value, fn, pos):
    """Evaluate `value` into whatever `name` names."""
    kind, slot = _storage(name, fn, pos)
    if kind == "reg" and name not in fn.cells:
        compile_expr(value, fn, dst=slot)
        return
    mark = fn.mark()
    reg = fn.push()
    compile_expr(value, fn, dst=reg)
    store_reg(name, reg, fn, pos)
    fn.free_to(mark)


def store_reg(name, reg, fn, pos):
    """Put a value that already sits in `reg` into whatever `name` names."""
    kind, slot = _storage(name, fn, pos)
    if kind == "slot":
        fn.emit(opcodes.pack("setattr", a0=fn.this_reg, a1=slot, a2=reg))
    elif kind == "global":
        fn.emit(opcodes.pack_pairable("gset", slot, reg))
    elif name in fn.cells:
        # The slot holds the box; the write goes into it, so every frame
        # sharing the box sees it (spec 8.3).
        fn.emit(opcodes.pack("setidx", a0=slot, a1=fn.zero, a2=reg))
    elif slot != reg:
        fn.emit(opcodes.pack_pairable("move", slot, reg))


def _storage(name, fn, pos):
    where = fn.storage(name)
    if where is None:
        # A name this module does not own is a refusal. Writing into another
        # module (`mod::x = e`) has no lowering: a free slot is this module's
        # copy of a name, not a handle on the other module's storage, so
        # assigning to one would change nothing anybody else can see.
        raise CompileError(
            f"cannot assign to {name!r}: it is not declared in this module", pos
        )
    return where


# --------------------------------------------------------------------------
# declarations


@statement(ast.VarDecl)
def _var_decl(node, fn, result):
    if node.values is None:
        # `var x: T` - bound to Unset until first assignment.
        for target in node.targets:
            _store_unset(target_name(target), fn, node.pos)
        return
    _assign(node.targets, node.values, fn, node.pos)


@statement(ast.Assign)
def _assign_stmt(node, fn, result):
    if node.op == "?=":
        if len(node.targets) != 1 or len(node.values) != 1:
            raise CompileError("`?=` takes one target and one value", node.pos)
        _set_if_unset(target_name(node.targets[0]), node.values[0], fn, node.pos)
        return
    if node.op != "=":
        raise CompileError(
            f"the bytecode compiler does not support the {node.op!r} assignment yet",
            node.pos,
        )
    _assign(node.targets, node.values, fn, node.pos)


def _assign(targets, values, fn, pos):
    if len(values) == len(targets):
        _assign_pairwise(targets, values, fn, pos)
        return
    if len(values) != 1:
        raise CompileError(
            f"{len(targets)} target(s) but {len(values)} value(s)", pos
        )
    value = values[0]
    if isinstance(value, ast.Call):
        # A call asked for as many results as there are targets: the VM
        # backfills missing ones with nil and drops extras (spec 1), so the
        # window is the whole mechanism.
        _assign_from_call(targets, value, fn, pos)
        return
    if isinstance(value, ast.Tuple) and len(value.items) == len(targets):
        # A literal tuple on the right is decomposed rather than built - no
        # tuple is materialized just to be taken apart again (spec 7.2).
        _assign_pairwise(targets, value.items, fn, pos)
        return
    _assign_by_unpacking(targets, value, fn, pos)


def _assign_pairwise(targets, values, fn, pos):
    """Assign values to targets position by position.

    With more than one target every value is evaluated *before* any target is
    written, because the two sides may overlap - `a, b = b, a` is a swap, not
    two assignments in a row.
    """
    if len(targets) == 1:
        store_target(targets[0], values[0], fn, pos)
        return
    mark = fn.mark()
    staged = []
    for value in values:
        reg = fn.push()
        compile_expr(value, fn, dst=reg)
        staged.append(reg)
    for target, reg in zip(targets, staged):
        store_target_reg(target, reg, fn, pos)
    fn.free_to(mark)


def _assign_from_call(targets, call, fn, pos):
    base = fn.mark()
    compile_call(call, fn, len(targets))
    for offset, target in enumerate(targets):
        store_target_reg(target, opcodes.L(base + offset), fn, pos)
    fn.free_to(base)


def _assign_by_unpacking(targets, value, fn, pos):
    """`a, b = e`: pull exactly len(targets) items out of one indexable.

    On a length or type mismatch every destination register receives the
    error value (spec 3.3), so the failure shows up at each use rather than
    at the unpack.
    """
    mark = fn.mark()
    source = compile_expr(value, fn)
    base = fn.mark()
    for _ in targets:
        fn.push()
    fn.emit(
        opcodes.pack("unpack", a0=opcodes.L(base), a1=source, f=len(targets))
    )
    for offset, target in enumerate(targets):
        store_target_reg(target, opcodes.L(base + offset), fn, pos)
    fn.free_to(mark)


def _store_unset(name, fn, pos):
    kind, slot = _storage(name, fn, pos)
    if kind == "reg" and name not in fn.cells:
        fn.emit(opcodes.pack("lunset", a0=slot))
        return
    mark = fn.mark()
    reg = fn.push()
    fn.emit(opcodes.pack("lunset", a0=reg))
    store_reg(name, reg, fn, pos)
    fn.free_to(mark)


def _set_if_unset(name, value, fn, pos):
    """`x ?= e`: assign only when x currently holds an error (spec 7.2)."""
    kind, slot = _storage(name, fn, pos)
    done = fn.new_label()
    if kind == "reg" and name not in fn.cells:
        fn.emit_jump("jnerr", done, cond=slot)
        mark = fn.mark()
        compile_expr(value, fn, dst=slot)
        fn.free_to(mark)
    elif kind == "reg":
        mark = fn.mark()
        reg = fn.push()
        fn.emit(opcodes.pack("getidx", a0=reg, a1=slot, a2=fn.zero))
        fn.emit_jump("jnerr", done, cond=reg)
        compile_expr(value, fn, dst=reg)
        fn.emit(opcodes.pack("setidx", a0=slot, a1=fn.zero, a2=reg))
        fn.free_to(mark)
    else:
        mark = fn.mark()
        reg = fn.push()
        fn.emit(opcodes.pack_pairable("gget", slot, reg))
        fn.emit_jump("jnerr", done, cond=reg)
        compile_expr(value, fn, dst=reg)
        fn.emit(opcodes.pack_pairable("gset", slot, reg))
        fn.free_to(mark)
    fn.mark_label(done)


# --------------------------------------------------------------------------
# simple statements


@statement(ast.Pass)
def _pass(node, fn, result):
    """`pass` compiles to nothing (D6); `noop` exists for patching, not this."""


@statement(ast.ExprStmt)
def _expr_stmt(node, fn, result):
    compile_expr(node.value, fn, dst=result)


@statement(ast.Return)
def _return(node, fn, result):
    """`return e` returns one value from wherever it landed; `return a, b`
    returns a window of them; bare `return` returns a single nil, so a caller
    asking for one result gets nil rather than a zero-value backfill."""
    if node.value is None:
        reg = fn.push()
        fn.emit(opcodes.pack("lnil", a0=reg))
        fn.emit_return(reg)
        return
    if isinstance(node.value, ast.Tuple):
        # `return 1, 2` and `return (1, 2)` are the same tree in this AST, so
        # a parenthesized tuple return is lowered as a multi-value return.
        base = fn.mark()
        for item in node.value.items:
            compile_into_window(item, fn)
        count = len(node.value.items)
        if count > 128:
            raise CompileError(
                f"{count} return values, over the 128-result limit", node.pos
            )
        fn.max_results = max(fn.max_results, count)
        fn.emit_return(opcodes.L(base), count)
        return
    fn.emit_return(compile_expr(node.value, fn))


# --------------------------------------------------------------------------
# control flow


@statement(ast.If)
def _if(node, fn, result):
    clauses = [(node.cond, node.body)]
    clauses += [(clause.cond, clause.body) for clause in node.elifs]
    end = fn.new_label()
    if result is not None and not node.orelse:
        # Nothing may run at all, and a skipped `if` is nil (spec 7.2).
        fn.emit(opcodes.pack("lnil", a0=result))
    for index, (cond, body) in enumerate(clauses):
        last = index == len(clauses) - 1
        skip = fn.new_label()
        mark = fn.mark()
        fn.emit_jump("jf", skip, cond=compile_expr(cond, fn))
        fn.free_to(mark)
        compile_body(body, fn, result)
        if not last or node.orelse:
            fn.emit_jump("jmp", end)
        fn.mark_label(skip)
    if node.orelse:
        compile_body(node.orelse, fn, result)
    fn.mark_label(end)


@expression(ast.If)
def _if_expr(node, fn, dst):
    """`if` is an expression as well as a statement (wyrm.gram's `if_expr`),
    and it is the same node either way - so it is the same lowering, with the
    branch values landing in the register the expression was asked for."""
    reg = fn.push() if dst is None else dst
    _if(node, fn, reg)
    return reg


@statement(ast.While)
def _while(node, fn, result):
    head = fn.new_label()
    end = fn.new_label()
    fn.mark_label(head)
    mark = fn.mark()
    fn.emit_jump("jf", end, cond=compile_expr(node.cond, fn))
    fn.free_to(mark)
    fn.loops.append((head, end))
    compile_body(node.body, fn)
    fn.loops.pop()
    fn.emit_jump("jmp", head)
    fn.mark_label(end)


@statement(ast.For)
def _for(node, fn, result):
    """`for x in e / else`: `iter` once, then `itnext` per turn, which jumps
    to the else clause when the iterator is exhausted (spec 7.2).  `break`
    jumps *past* the else clause; exhaustion jumps *to* it.

    The loop variable is a declaration scoped to the loop, not to the block
    the loop sits in: it is bound for the body and for the else clause (which
    reads the final iteration's value), and out of scope afterwards - so the
    scope pushed here is the loop's own, and the body pushes its own inside
    it."""
    pushed = fn.push_block(node)
    try:
        _compile_for(node, fn)
    finally:
        if pushed:
            fn.pop_block()


def _compile_for(node, fn):
    mark = fn.mark()
    source = compile_expr(node.iter, fn)
    iterator = fn.push()
    fn.emit(opcodes.pack("iter", a0=iterator, a1=source))

    kind, slot = _storage(node.var, fn, node.var_pos or node.pos)
    target = slot if kind == "reg" else fn.push()

    head = fn.new_label()
    exhausted = fn.new_label()
    end = fn.new_label()
    fn.mark_label(head)
    offset = fn.emit(opcodes.pack("itnext", a0=target, a1=iterator, a2=0))
    fn.patch_site(exhausted, offset + 2, offset + 1, "a2")
    if kind == "global":
        fn.emit(opcodes.pack_pairable("gset", slot, target))

    fn.loops.append((head, end))
    compile_body(node.body, fn)
    fn.loops.pop()
    fn.emit_jump("jmp", head)

    fn.mark_label(exhausted)
    if node.orelse:
        compile_body(node.orelse, fn)
    fn.mark_label(end)
    fn.free_to(mark)


@statement(ast.Break)
def _break(node, fn, result):
    if not fn.loops:
        raise CompileError("`break` outside a loop", node.pos)
    fn.emit_jump("jmp", fn.loops[-1][1])


@statement(ast.Continue)
def _continue(node, fn, result):
    if not fn.loops:
        raise CompileError("`continue` outside a loop", node.pos)
    fn.emit_jump("jmp", fn.loops[-1][0])


# --------------------------------------------------------------------------
# nested definitions and deferred blocks


@statement(ast.Yield)
def _yield_stmt(node, fn, result):
    from .expressions import compile_yield

    compile_yield(node, fn, result)


@statement(ast.StaticDecl)
def _static_decl(node, fn, result):
    """A `static` declaration allots a module global and its initializer runs
    once, in module init, where its owner is created - so nothing is emitted
    where the declaration itself appears (spec 7.2)."""
    if node.name not in fn.statics:
        raise CompileError(
            "`static` is only meaningful inside a function or class body",
            node.pos,
        )


@statement(ast.FnDef, ast.CoDef)
def _nested_fn(node, fn, result):
    """A `fn` or `co` inside a function body binds a closure to a local
    (spec 7.1)."""
    # Imported here rather than at module scope: functions.py compiles bodies
    # through this module.
    from .functions import compile_nested_fn

    compile_nested_fn(node, fn)


@statement(ast.Defer)
def _defer(node, fn, result):
    from .functions import compile_defer

    compile_defer(node, fn)
