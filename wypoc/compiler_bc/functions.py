"""Function compilation: parameters, captures, body, epilogue, table entry.

A function body is compiled into its own `FnContext` buffer and laid out after
module init (see `module.py`), so its code offset is patched in at assembly
time.  Its table index, though, is reserved *before* the body is compiled -
module init needs it for the `closure` that publishes a top-level function,
and a body that refers to itself needs it too.

Nested functions are where the interesting work is.  A nested `fn`, a lambda
and a `defer` block are all compiled the same way: work out what the body
reads from the enclosing frame (its captures), compile it as an ordinary
function whose P frame carries those captures after its parameters, then emit
a `closure` in the enclosing frame that copies them in.  A captured variable
that anyone assigns is held in a cell instead, so the copy shares a box rather
than a value (spec 8.3).
"""

from wypoc import ast_nodes as ast

from . import opcodes
from .analysis import cell_names, free_names, own_declared_names
from .context import FnContext
from .errors import CompileError
from .image import FN_COROUTINE, FN_KWARGS, FN_VARARGS
from .statements import compile_body, store_reg


def compile_function(node, module, name=None, flags=0, enclosing=None):
    """Compile a `fn` definition, returning `(function index, capture names)`.

    `enclosing` is the frame this definition sits in, if any; the names its
    body reads from that frame become its captures, in first-encounter order.
    """
    if node.class_target:
        raise CompileError(
            "the bytecode compiler does not support dispatch on a class "
            "target (`fn [T] name`) yet",
            node.pos,
        )
    if isinstance(node, ast.CoDef):
        flags |= FN_COROUTINE
    return compile_callable(
        module,
        name or node.name,
        node.params,
        node.body,
        node.pos,
        flags=flags,
        enclosing=enclosing,
    )


def compile_callable(
    module,
    name,
    params,
    body,
    pos,
    flags=0,
    enclosing=None,
    dispatch=(),
    this_class=None,
):
    """Compile any parameter list + body into a function table entry.

    Shared by `fn` definitions, lambdas, `defer` blocks and class methods -
    all of them a body that runs in a frame of its own over a P frame of
    `this` values, parameters and captures, in that order (spec 1).
    """
    params, entries, param_flags = param_spec(params, module)
    flags |= param_flags
    if len(params) > 128:
        raise CompileError(
            f"{name} declares {len(params)} parameters, over the 128-parameter limit",
            pos,
        )
    captures = _captures(params, body, enclosing)
    dispatch = list(dispatch)
    fn = FnContext(
        module,
        name,
        params=params,
        captures=captures,
        this_count=len(dispatch),
        this_class=this_class,
    )
    fn.cells = _cells(params, body, captures, enclosing)
    fn.is_coroutine = bool(flags & FN_COROUTINE)
    _declare_statics(fn, name, body, module)

    index = module.image.add_function(
        name,
        params=entries,
        nlocals=0,
        code_offset=0,
        flags=flags,
        ncaptures=len(captures),
        dispatch=dispatch,
    )

    reason = _compile_or_stub(fn, params, body, module)

    entry = module.image.functions[index]
    if reason is not None:
        module.image.unlowered.append((name, reason))
    entry.nlocals = fn.high
    entry.nresults = fn.max_results
    entry.uses = list(fn.uses)
    module.add_pending(index, fn)
    return index, captures


def param_spec(params, module=None):
    """`(names, entries, flags)` for a parameter list.

    `entries` is what the functions table records - each parameter's name and,
    when it has one, the static-pool index of its default.  A `*args` or
    `**kwargs` parameter is an ordinary P slot that arrives pre-collected;
    only the flag says so (spec 4.6).
    """
    from .expressions import NOT_CONSTANT, constant_value

    names = []
    entries = []
    flags = 0
    seen_collector = False
    for param in params:
        if isinstance(param, ast.VarPositional):
            flags |= FN_VARARGS
            seen_collector = True
            names.append(param.name)
            entries.append((param.name, None))
            continue
        if isinstance(param, ast.VarKeyword):
            flags |= FN_KWARGS
            seen_collector = True
            names.append(param.name)
            entries.append((param.name, None))
            continue
        if seen_collector:
            raise CompileError(
                f"parameter {param.name!r} follows a collecting parameter",
                param.pos,
            )
        default = None
        if param.default is not None:
            # spec 4.6: a default is a static pool reference, so only a
            # constant can be one.  Evaluating an arbitrary expression per
            # call is compiler work with no format change behind it
            # (Appendix C), and refusing beats guessing.
            value = constant_value(param.default)
            if value is NOT_CONSTANT or module is None:
                raise CompileError(
                    f"parameter {param.name!r}: a default must be a constant",
                    param.pos,
                )
            default = module.image.add_static(value)
        names.append(param.name)
        entries.append((param.name, default))
    return names, entries, flags


def param_names(params, module=None):
    return param_spec(params, module)[0]


def _compile_or_stub(fn, params, body, module):
    """Compile the body, or leave a stub that traps if it will not lower.

    Some functions are templates, never called: `_dsl.wy`'s `fn $token_kind`
    uses `this` in a plain function purely so a DSL can splice its tree into a
    class.  Refusing the whole module over one of those would make the module
    uncompilable for code that never runs, while compiling it as if it were
    callable would be worse.  So the function keeps its table entry - its tree
    is still reachable through `foo::$ast` - and its body becomes a `trap`,
    which is exactly what calling it is: unreachable code reached.

    The reason is recorded on the image and reported by the CLI; nothing is
    swallowed.  `stub_unlowered=False` turns this off, which is what the test
    suite uses so a genuine compiler gap still fails loudly.
    """
    try:
        _allocate_slots(fn, params, body)
        _prologue(fn, params)
        _body_and_epilogue(body, fn)
        fn.patch_labels()
    except CompileError as error:
        if not module.stub_unlowered:
            raise
        _make_stub(fn)
        return str(error)
    return None


def _make_stub(fn):
    """Replace whatever was emitted with a single `trap`.

    Everything the abandoned attempt left behind is dropped with it - the
    labels it never patched, and above all its referenced-name set, which
    would otherwise have the VM resolve names the body no longer reads.
    """
    fn.code.clear()
    fn.lines.clear()
    fn.labels.clear()
    fn.uses.clear()
    fn.loops.clear()
    fn.top = fn.nnamed
    fn.line = None
    fn._emitted_line = None
    # trap code 0: unreachable (spec 3.1).
    fn.emit(opcodes.pack("trap", f=0))


def _declare_statics(fn, name, body, module):
    """`static x: T = e` in a function body is a module global.

    It is module-lifetime, bound once where its owner is created rather than
    on every call (spec 7.2), so the slot is allotted here and the
    initializer is queued for module init.
    """
    from .analysis import walk_body

    for node in walk_body(body):
        if not isinstance(node, ast.StaticDecl):
            continue
        index = module.declare_global(f"{name}::{node.name}")
        fn.statics[node.name] = index
        if node.default is not None:
            module.pending_statics.append((index, node.default, node.pos))


def _captures(params, body, enclosing):
    """The free names of `body` that the enclosing frame can actually hand
    over.  Anything else - a module global, a builtin, an external name - is
    reached the same way it would be from any other scope, not captured."""
    if enclosing is None:
        return []
    return [
        name
        for name in free_names(params, body)
        if enclosing.lookup(name) is not None
    ]


def _cells(params, body, captures, enclosing):
    """Which of this frame's names live in a cell.

    Two sources: names this body declares that a nested scope captures and
    assigns, and captures that were already cells in the enclosing frame -
    the P slot holds that same box, so reads and writes here go through it
    too.
    """
    cells = cell_names(params, body)
    if enclosing is not None:
        cells |= {name for name in captures if name in enclosing.cells}
    return cells


def _allocate_slots(fn, params, body):
    """Give every name in the frame its L slot, before a line is emitted.

    A parameter that needs a cell gets a local slot of its own to hold the
    cell - the P slot still carries the incoming value, and the local shadows
    it from then on.

    The frame's own level is declared here; each nested block scope gets its
    own slots too (`allocate_block_scopes`), so an inner `var` shadows an
    outer one for the block's duration instead of writing through to it.
    """
    for name in params:
        if name in fn.cells:
            fn.declare_local(name)
    if fn.cells:
        fn.reserve_zero()
    own = own_declared_names(body)
    for name in own:
        fn.declare_local(name)
    fn.allocate_block_scopes(body, set(params) | set(fn.captures) | set(own))


def _prologue(fn, params):
    """Set up the frame's own cells at entry.

    Every cell this frame owns is created here rather than at its
    declaration, so that every write to a cell variable is a plain `setidx`
    with no "has the box been made yet?" case anywhere in the lowering.  A
    *captured* cell is not this frame's to create: it arrives in a P slot
    already holding the box the enclosing frame made.
    """
    if not fn.cells:
        return
    fn.emit(opcodes.pack_pairable("i8", fn.zero, 0))
    for name in sorted(fn.cells - set(fn.captures)):
        cell = fn.lookup(name)
        mark = fn.mark()
        seed = fn.push()
        if name in params:
            # The parameter's incoming value seeds its cell; windows live in
            # the L frame, so it is copied down before being boxed.
            fn.emit(opcodes.pack_pairable("move", seed, fn.params[name]))
        else:
            fn.emit(opcodes.pack("lnil", a0=seed))
        # A one-element pair list is the box: `getidx`/`setidx` at index 0
        # read and write it, and the VM needs no boxing opcode of its own.
        fn.emit(opcodes.pack("plist", a0=cell, a1=seed, f=1))
        fn.free_to(mark)


def _body_and_epilogue(body, fn):
    """Every path out of a function returns, carrying the body's value.

    A body whose last statement is already a `return` needs no epilogue.
    Otherwise the function's value is that last statement's - which is what
    the interpreter answers too - so the body gets a result register `R`
    reserved below its temps and the epilogue returns it.
    """
    if body and isinstance(body[-1], ast.Return):
        compile_body(body, fn)
        return
    result = fn.push()
    compile_body(body, fn, result)
    fn.emit_return(result)


# --------------------------------------------------------------------------
# closures


def emit_closure(fn, index, captures, dst):
    """Emit a `closure` over function `index`, copying `captures` into it.

    The captured registers have to be contiguous, so they are copied into a
    fresh window first.  A captured *cell* is copied as the cell - that is the
    whole point of the box: both frames end up pointing at the same one.
    """
    if not captures:
        fn.emit(opcodes.pack("closure", a0=dst, a1=index, a2=0, f=0))
        return
    base = fn.mark()
    for name in captures:
        slot = fn.push()
        fn.emit(opcodes.pack_pairable("move", slot, fn.lookup(name)))
    fn.emit(
        opcodes.pack(
            "closure", a0=dst, a1=index, a2=opcodes.L(base), f=len(captures)
        )
    )
    fn.free_to(base)


def compile_closure_expr(node, fn, dst, name):
    """Compile a nested `fn`/lambda in place, leaving the closure in `dst`."""
    index, captures = compile_callable(
        fn.module,
        name,
        node.params,
        node.body,
        node.pos,
        flags=FN_COROUTINE if isinstance(node, ast.CoDef) else 0,
        enclosing=fn,
    )
    emit_closure(fn, index, captures, dst)
    return dst


def compile_nested_fn(node, fn, pos=None):
    """A nested `fn name(...)` statement: build the closure, bind the name."""
    mark = fn.mark()
    reg = fn.push()
    compile_closure_expr(node, fn, reg, node.name)
    store_reg(node.name, reg, fn, pos or node.pos)
    fn.free_to(mark)


def compile_defer(node, fn):
    """A `defer:` block is a zero-parameter closure armed for this frame.

    v1 arms at frame granularity - the block runs when the function returns,
    a deliberate narrowing of the language spec's "containing block" (spec
    7.2, Appendix C).
    """
    index, captures = compile_callable(
        fn.module, "<defer>", [], node.body, node.pos, enclosing=fn
    )
    mark = fn.mark()
    reg = fn.push()
    emit_closure(fn, index, captures, reg)
    fn.emit(opcodes.pack("defer_reg", a0=reg, f=1 if node.on_error else 0))
    fn.free_to(mark)
