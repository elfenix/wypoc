"""The top-level walk: module init code, the function layout, the image.

Init code is emitted in the order spec 6.2 fixes: the hoisted import sequence
first, then the single `resolve` that binds every name the init routine
references, then the top-level statements in source order, then a zero-value
`return`.  Function bodies are compiled where they appear - which is what puts
the pools in source order - and laid out after init once their sizes are
known.
"""

import os

from wypoc import ast_nodes as ast

from . import opcodes
from .context import ModuleContext
from .errors import CompileError
from .functions import compile_function
from .analysis import own_declared_names
from .classes import compile_class
from .handlers import TOPLEVEL_HANDLERS, dispatch, supported, toplevel
from .image import (
    FN_COROUTINE,
    FN_MESSAGE,
)
from .statements import compile_statement
from .verify import verify

def compile_module(
    program,
    module_name,
    source_file=None,
    check=True,
    slot_optimization=False,
    debug=True,
    stub_unlowered=True,
) -> "ModuleImage":
    """Compile a parsed program into a module image.

    `check` runs the structural verifier over the finished image (spec 8.4);
    it is on by default because a lowering bug is far cheaper to find here
    than in a VM.

    `slot_optimization` turns on `getslot`/`setslot` for slot access whose
    whole inheritance chain is module-local (spec 7.1).  Off by default: the
    check is implemented, but there is no VM yet to run the result against,
    and symbolic access is always correct.

    `debug` emits the optional line table (spec 4.10); `--strip` turns it off.

    `stub_unlowered` decides what happens to a *function body* that will not
    lower: with it on (the default) the function becomes a trapping stub and
    the module still compiles, with the reason recorded on the image; with it
    off the whole module is refused.  Anything outside a function body is a
    refusal either way.
    """
    module = ModuleContext(module_name, source_file)
    module.slot_optimization = slot_optimization
    module.emit_debug = debug
    module.stub_unlowered = stub_unlowered
    program = _expand_decorators(program, module_name)
    _collect_definitions(program, module)
    init = module.init

    _declare_module_names(program.body, module)
    bindings = _hoist_imports(program.body, module)
    _bind_imported_names(bindings, module)

    for node in program.body:
        mark = init.mark()
        if supported(TOPLEVEL_HANDLERS, node):
            dispatch(TOPLEVEL_HANDLERS, node, module)
        else:
            compile_statement(node, init)
        _flush_statics(module)
        init.free_to(mark)

    # Module init returns no values: the loader calls it for its effects
    # (spec 1).
    init.emit(opcodes.pack("return", a0=0, f=0))
    init.patch_labels()

    image = _assemble(module)
    if check:
        verify(image)
    return image


def _collect_definitions(program, module):
    """Index every fn, co and class in the module by name, for `foo::$ast`.

    Built from the expanded tree, so a decorated definition is indexed as what
    the decorator answered - which is the tree its binding would hold, and so
    the one `$ast` should describe.
    """
    for node in program.walk():
        if isinstance(node, (ast.FnDef, ast.CoDef, ast.ClassDef)) and node.name:
            module.definitions.setdefault(node.name, node)


def _expand_decorators(program, module_name):
    """Decorators run at compile time (spec 7.2).

    The POC already has the machinery - `sexpr.py` plus the evaluator - and
    it is the same pass `--dump-wys` uses, so a decorator sees exactly the
    tree it would there.  Lowering never meets a `Decorated` node, and every
    name reference the expansion produced is emitted afterwards, resolving
    like handwritten code (spec 6.3).

    The scope is only built when there is something to expand: seeding it
    parses corelib's prelude, and a module with no decorators should not pay
    for that.
    """
    if not any(isinstance(node, ast.Decorated) for node in program.walk()):
        return program
    from wypoc.wyrm_eval_parse_tree import expand_decorators, populate_globals

    scope = {}
    try:
        populate_globals(scope, module_name)
        return expand_decorators(program, scope)
    except CompileError:
        raise
    except Exception as error:
        # Expansion runs real code - the decorator's, and any `import` above
        # it - so it can fail for reasons that are not the compiler's. It is
        # still a compile-time failure, and it says so rather than escaping
        # as a traceback.
        raise CompileError(
            f"decorator expansion failed: {type(error).__name__}: {error}"
        ) from error


def _flush_statics(module):
    """Run the `static` initializers the item just compiled declared.

    "Bound once where the owner is created" (spec 7.2): a function's statics
    are initialized in module init, right after the closure that publishes
    it, not on every call.
    """
    from .statements import store_reg

    init = module.init
    pending, module.pending_statics = module.pending_statics, []
    for index, value, pos in pending:
        mark = init.mark()
        reg = init.push()
        compile_expr_into(value, init, reg)
        init.emit(opcodes.pack_pairable("gset", index, reg))
        init.free_to(mark)


def compile_expr_into(node, fn, reg):
    from .expressions import compile_expr

    compile_expr(node, fn, dst=reg)


def _declare_module_names(body, module):
    """Every name the module's top level introduces becomes a global slot,
    declared before any code is lowered - `fn` definitions included.

    Doing it up front is what lets a function body reference a global defined
    further down the file - the interpreter allows that too, since the body
    only runs once init has populated the slot.  Order is source order, so the
    slot numbering is deterministic.
    """
    for node in body:
        if isinstance(node, ast.Import):
            for name in _import_bindings(node):
                module.declare_global(name)
        elif isinstance(node, ast.FromImport):
            module.declare_global(node.path[0])
            module.declare_global(node.path[-1])
            for name in node.names:
                module.declare_global(name)
        elif isinstance(node, ast.ClassDef):
            module.declare_global(node.name)
    own = own_declared_names(body)
    for name in own:
        module.declare_global(name)
    # A `var` inside a top-level `do:`/`if`/loop body is that block's, not the
    # module's: it gets storage of its own, is not exported, and goes out of
    # scope with the block - exactly as it would inside a function.
    module.init.allocate_block_scopes(body, set(module.globals) | set(own))


def _hoist_imports(body, module):
    """Emit every import ahead of the `resolve` point (spec 6.2).

    Each import triggers its dependency to load and run its own init, and the
    module object is stored in the global named after it - the root package
    and the leaf, which is what the interpreter binds too.  Names imported
    *out* of a module (`import a::(x, y)`) are ordinary name references, so
    they cannot be read until `resolve` has run; they are returned here and
    bound immediately after it.
    """
    pending = []
    for node in body:
        if isinstance(node, ast.FromImport):
            _import_module(node.path, None, node.pos, module)
            pending += [(node.path, name, name, node.pos) for name in node.names]
        elif isinstance(node, ast.Import):
            pending += _import(node, module)
    return pending


def _import(node, module):
    if node.wildcard:
        _import_star(node, module)
        return []
    if node.static:
        # spec 7.2: `import static` lowers exactly like `import`. `static`
        # says the dependency is wanted at compile time only - a module of
        # decorators, static functions or AST models - and should not become
        # a runtime dependency of this one; what differs is therefore a set
        # of usage restrictions the language spec states in prose and this
        # compiler does not yet enforce (see doc/llm-bytecode.md 7.2).
        # Notably it is *not* a message-namespace operation: the tree walker
        # adopts the dependency's message table here, which is an artifact of
        # its per-module tables (see _adopt_messages) rather than something
        # to reproduce.
        module.static_imports.add(node.alias or node.path[-1])
    _import_module(node.path, node.alias, node.pos, module)
    if not node.items:
        return []
    return [(node.path, item.name, item.alias or item.name, node.pos)
            for item in node.items]


def _import_star(node, module):
    """`import a::b::*`: load the module, then register its namespace for the
    resolution search (spec 6.2).

    Nothing is tabled. The path is a constant string, and the except-list is
    a window of interned symbols the instruction reads the way `tuple` reads
    its items - so a wildcard import needs no entry anywhere, which is just as
    well, since its per-statement except-list was the one thing that could
    never be deduplicated with anything else.

    From here on an identifier the compiler cannot place compiles to a free
    global slot instead of a refusal - the wildcard might supply it, and only
    fill time can say (spec 6.3).
    """
    init = module.init
    _import_module(node.path, None, node.pos, module, bind_leaf=False)
    path = module.image.add_static("::".join(node.path))
    mark = init.mark()
    base = init.mark()
    excepts = list(node.except_names or ())
    for name in excepts:
        init.emit(
            opcodes.pack_pairable("lsym", module.image.add_symbol(name), init.push())
        )
    init.emit(
        opcodes.pack("import_star", a0=path, a1=opcodes.L(base), f=len(excepts))
    )
    init.free_to(mark)
    module.has_wildcard_import = True


def _import_module(path, alias, pos, module, bind_leaf=True):
    """`import a::b::c`: load each prefix in turn, then bind the root package
    and the leaf (or its alias) to module globals."""
    init = module.init
    mark = init.mark()
    reg = init.push()
    for depth in range(1, len(path) + 1):
        # The path is a constant string, not a table entry: `import` reads it,
        # splits it, and hands it to the loader. That is all a relocation of
        # kind `module` ever was.
        index = module.image.add_static("::".join(path[:depth]))
        init.emit(opcodes.pack_pairable("import", index, reg))
        if depth == 1 and (len(path) > 1 or not bind_leaf):
            init.emit(
                opcodes.pack_pairable("gset", module.global_index(path[0]), reg)
            )
    if bind_leaf:
        init.emit(
            opcodes.pack_pairable(
                "gset", module.global_index(alias or path[-1]), reg
            )
        )
    init.free_to(mark)


def _bind_imported_names(pending, module):
    """`import a::(x, y as z)`: each imported name is a reference into the
    dependency, read once into the global it was given."""
    init = module.init
    for path, name, binding, pos in pending:
        # A free slot named by the whole path, filled by the `import` above it
        # that made `path` reachable - then copied into the global the import
        # statement binds. Two slots rather than one because the two are
        # different names: `import a::(x as y)` gives this module a `y`, and
        # `a::x` is what it was read from.
        source = module.declare_free_global("::".join(list(path) + [name]))
        mark = init.mark()
        reg = init.push()
        init.emit(opcodes.pack_pairable("gget", source, reg))
        init.emit(opcodes.pack_pairable("gset", module.global_index(binding), reg))
        init.free_to(mark)


def _assemble(module):
    """Lay out init at offset 0, then each function body, and fill in the
    image's remaining header fields."""
    image = module.image
    init = module.init
    image.code = list(init.code)
    for index, fn in module.pending:
        entry = image.functions[index]
        entry.code_offset = len(image.code)
        image.code.extend(fn.code)
    # Compiler-side only: the verifier checks each scope against the messages
    # it reads, but nothing is serialized. Binding is not batched any more, so
    # an image has no use for the set (doc/addendum.md).
    image.module_uses = list(init.uses)
    image.init_nlocals = init.high
    if module.emit_debug:
        image.debug = _debug_section(module, image)
    return image


def _debug_section(module, image):
    """The optional line table: code word offset -> source line (spec 4.10).

    Each body records offsets relative to itself, so they are shifted to
    absolute ones here, once the layout is settled.
    """
    lines = dict(module.init.lines)
    for index, fn in module.pending:
        base = image.functions[index].code_offset
        for offset, line in fn.lines.items():
            lines[base + offset] = line
    section = {"ln": {str(offset): line for offset, line in sorted(lines.items())}}
    if module.source_file:
        # The file's name, not the path it happened to be compiled from: an
        # absolute build path would make the image differ between machines
        # for no reason a debugger cares about (spec 4.10).
        section = {"f": os.path.basename(module.source_file), **section}
    return section


def _import_bindings(node):
    """The module-global names one `import` statement binds."""
    if node.wildcard:
        # A wildcard binds no leaf name: its whole namespace joins the
        # resolution search instead.
        return [node.path[0]]
    names = [node.path[0], node.alias or node.path[-1]]
    for item in node.items or []:
        names.append(item.alias or item.name)
    return names


@toplevel(ast.Import, ast.FromImport)
def _import_stmt(node, module):
    """Imports were hoisted and emitted before `resolve`; nothing is left to
    do where the statement actually appears."""


@toplevel(ast.ClassDef)
def _class_def(node, module):
    """A top-level class becomes a classes-table entry plus, in init, the
    `class` that realizes it and a `gset` into the global of its name."""
    index = compile_class(node, module)
    init = module.init
    reg = init.push()
    init.emit(opcodes.pack("class", a0=reg, a1=index))
    init.emit(opcodes.pack_pairable("gset", module.global_index(node.name), reg))
    _class_statics(node, module)


def _class_statics(node, module):
    """A class static's initializer runs in module init, like any other
    top-level effect (spec 4.7)."""
    from .statements import store_expr

    for member in node.body:
        if isinstance(member, ast.StaticDecl) and member.default is not None:
            store_expr(f"{node.name}::{member.name}", member.default,
                       module.init, member.pos)


@toplevel(ast.FnDef, ast.CoDef)
def _fn_def(node, module):
    """A top-level `fn` or `co` becomes a function entry plus, in init, a
    closure over it stored into the module global of its name (spec 7.2).

    A `co` is compiled as an ordinary function carrying the coroutine flag;
    calling one constructs the coroutine, which is the VM's business.
    """
    if node.class_target:
        _dispatched_fn(node, module)
        return
    index, _captures = compile_function(node, module)
    global_index = module.declare_global(node.name)
    init = module.init
    reg = init.push()
    init.emit(opcodes.pack("closure", a0=reg, a1=index, a2=0, f=0))
    init.emit(opcodes.pack_pairable("gset", global_index, reg))


def _dispatched_fn(node, module):
    """`fn [T1, T2] name(...)`: a method registered for multiple dispatch
    outside any class body (spec 7.2).

    It is not a module global - it is reached by sending `name`, so init
    registers it against the message identity and the types it dispatches on.
    """
    from .functions import compile_callable

    init = module.init
    dispatch = [module.name_slot(type_name) for type_name in node.class_target]
    if len(dispatch) > 16:
        raise CompileError(
            f"{node.name} dispatches on {len(dispatch)} types, over the "
            "16-value limit",
            node.pos,
        )
    # With a single receiver type that this module defines, bare slot names in
    # the body mean that class's slots, exactly as they do inside its body.
    # Multiple dispatch has more than one `this`, so no such name is unambiguous.
    single = node.class_target[0] if len(node.class_target) == 1 else None
    flags = FN_MESSAGE
    if isinstance(node, ast.CoDef):
        flags |= FN_COROUTINE
    index, _captures = compile_callable(
        module,
        f"{node.name}!",
        node.params,
        node.body,
        node.pos,
        flags=flags,
        dispatch=dispatch,
        this_class=single if module.is_local_class(single) else None,
    )
    message = init.reference([node.name], node.pos)

    mark = init.mark()
    closure = init.push()
    init.emit(opcodes.pack("closure", a0=closure, a1=index, a2=0, f=0))
    base = init.mark()
    for slot in dispatch:
        init.emit(opcodes.pack_pairable("gget", slot, init.push()))
    types = init.push()
    init.emit(
        opcodes.pack("tuple", a0=types, a1=opcodes.L(base), f=len(dispatch))
    )
    init.emit(opcodes.pack("reg_msg", a0=message, a1=closure, a2=types))
    init.free_to(mark)
