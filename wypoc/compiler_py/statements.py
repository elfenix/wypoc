"""Statement codegen. compile_stmt(fnctx, node) emits one statement's worth
of lines into fnctx via fnctx.emit(...); compile_block loops over a body.

If/While/For/Defer are handled directly in compile_stmt/compile_block
rather than through STMT_HANDLERS (see handlers.py's docstring) since they
need extra plumbing (loop_depth, a synthesized try/finally). Assign/
VarDecl/ExprStmt/Return/With* go through the registry.
"""
from wypoc import ast_nodes as ast

from .errors import err
from .expressions import compile_expr
from .handlers import STMT_HANDLERS
from .naming import py_ident


def compile_block(fnctx, stmts):
    """Compiles a statement list. A `defer`/`defer on error` found here
    wraps everything *after* it (in this same list) in a synthesized try/
    finally and stops iterating the list itself - see _compile_defer,
    which recurses back into compile_block for the "everything after"
    part, so consecutive defers nest correctly (LIFO - the first-
    registered one's cleanup runs last, exactly matching a real function's
    unwind order) and a defer inside a loop body re-enters its try/finally
    fresh on every iteration (nothing here is loop-aware on purpose - it
    falls out naturally from compile_block being called once per pass
    through the loop's own body)."""
    if not stmts:
        fnctx.emit("pass")
        return
    for i, stmt in enumerate(stmts):
        if isinstance(stmt, ast.Defer):
            _compile_defer(fnctx, stmt, stmts[i + 1:])
            return
        compile_stmt(fnctx, stmt)


def compile_stmt(fnctx, node):
    if isinstance(node, ast.If):
        return _if(fnctx, node)
    if isinstance(node, ast.While):
        return _while(fnctx, node)
    if isinstance(node, ast.For):
        return _for(fnctx, node)
    if isinstance(node, ast.Break):
        if fnctx.loop_depth == 0:
            err("'break' outside a loop", node)
        fnctx.emit("break")
        return
    if isinstance(node, ast.Continue):
        if fnctx.loop_depth == 0:
            err("'continue' outside a loop", node)
        fnctx.emit("continue")
        return
    if isinstance(node, ast.Pass):
        fnctx.emit("pass")
        return
    if isinstance(node, ast.Defer):
        # Only reached if a Defer somehow bypasses compile_block (it
        # shouldn't - every block goes through compile_block) - fail loud
        # rather than silently dropping the deferred cleanup.
        err("'defer' must be a direct statement in a block", node)
    handler = STMT_HANDLERS.get(node)
    if handler is None:
        err("statement not supported by --compile-py", node)
    handler(fnctx, node)


def _compile_defer(fnctx, defer_stmt, rest):
    fnctx.emit("try:")
    fnctx.indent += 1
    compile_block(fnctx, rest)
    fnctx.indent -= 1
    if defer_stmt.on_error:
        fnctx.emit("except BaseException:")
        fnctx.indent += 1
        fnctx.emit("_wy_error_exit = True")
        fnctx.emit("raise")
        fnctx.indent -= 1
    fnctx.emit("finally:")
    fnctx.indent += 1
    if defer_stmt.on_error:
        fnctx.emit("if _wy_error_exit:")
        fnctx.indent += 1
        compile_block(fnctx, defer_stmt.body)
        fnctx.indent -= 1
    else:
        compile_block(fnctx, defer_stmt.body)
    fnctx.indent -= 1


def has_error_defer(body) -> bool:
    """True if `body` (a function/coroutine's own statement list) contains
    a `defer on error:` anywhere within it - checked once, up front, by
    functions.py/coroutines.py, which pre-declare `_wy_error_exit = False`
    and route every `return` through the flag-setting form only when this
    is true (see _return below), rather than paying for it universally."""
    return any(isinstance(n, ast.Defer) and n.on_error
               for stmt in body for n in stmt.walk())


def _locally_declared_names(params, body) -> set:
    names = set()
    for p in params:
        if isinstance(p, (ast.Param, ast.VarPositional, ast.VarKeyword)):
            names.add(p.name)
    for stmt in body:
        for n in stmt.walk():
            if isinstance(n, ast.VarDecl):
                for t in n.targets:
                    if isinstance(t, ast.VarTarget):
                        names.add(t.name)
    return names


def _assigned_names(body) -> set:
    names = set()
    for stmt in body:
        for n in stmt.walk():
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.NameTarget):
                        names.add(t.name)
    return names


def needed_nonlocal_decls(params, body) -> list:
    """Python's local-if-assigned-anywhere rule (see needed_global_decls's
    docstring) applies just as much to a coroutine's nested `_body` closure
    reassigning one of the enclosing `co`'s own *parameters* (e.g. `seed =
    ...` inside a `while` loop, rewriting a `co lcg(seed)`'s own `seed`) -
    `_body` is a real nested Python function, so that assignment shadows
    the parameter with a fresh, unbound-until-assigned local unless
    declared `nonlocal`. Returns the sorted py_ident'd parameter names that
    need it (see coroutines.py's _compile_co_body, the only nested-closure
    body this compiler currently generates)."""
    param_names = {p.name for p in params
                   if isinstance(p, (ast.Param, ast.VarPositional, ast.VarKeyword))}
    from .naming import py_ident as _py_ident
    return sorted(_py_ident(n) for n in (_assigned_names(body) & param_names))


def needed_global_decls(modctx, params, body) -> list:
    """Python makes assigning to a name anywhere in a function treat that
    name as local for the *entire* function - so a plain `x = ...`
    reading and writing a module-top-level `var` (not shadowed by a
    param/`var`/`:=` declared in this same function) needs an explicit
    `global x` or Python raises UnboundLocalError on the read. Returns
    the sorted py_ident'd names functions.py/coroutines.py should declare
    global at the top of the compiled body; a name also locally declared
    somewhere in the same body (shadowing, however partial the branch
    coverage - not perfectly scope-accurate, a known v1 approximation) is
    excluded even if also assigned via plain `=` elsewhere."""
    assigned = _assigned_names(body)
    shadowed = _locally_declared_names(params, body)
    from .naming import py_ident as _py_ident
    return sorted(_py_ident(n) for n in (assigned - shadowed) if _py_ident(n) in modctx.var_names)


def _if(fnctx, node):
    """Each branch is its own fresh child scope, matching _eval_if's own
    `ctx.child()` per branch - see context.FnCtx's docstring for why that
    matters even though only one branch ever actually runs."""
    fnctx.emit(f"if {compile_expr(fnctx, node.cond)}:")
    fnctx.indent += 1
    fnctx.push_scope()
    compile_block(fnctx, node.body)
    fnctx.pop_scope()
    fnctx.indent -= 1
    for clause in node.elifs:
        fnctx.emit(f"elif {compile_expr(fnctx, clause.cond)}:")
        fnctx.indent += 1
        fnctx.push_scope()
        compile_block(fnctx, clause.body)
        fnctx.pop_scope()
        fnctx.indent -= 1
    if node.orelse is not None:
        fnctx.emit("else:")
        fnctx.indent += 1
        fnctx.push_scope()
        compile_block(fnctx, node.orelse)
        fnctx.pop_scope()
        fnctx.indent -= 1


def _while(fnctx, node):
    """A fresh child scope per iteration (see FnCtx's docstring) - one
    scope push covers the body since it's compiled once regardless of how
    many times it actually runs."""
    fnctx.emit(f"while {compile_expr(fnctx, node.cond)}:")
    fnctx.indent += 1
    fnctx.loop_depth += 1
    fnctx.push_scope()
    fnctx.loop_scope_floor.append(len(fnctx.scopes) - 1)
    compile_block(fnctx, node.body)
    fnctx.loop_scope_floor.pop()
    fnctx.pop_scope()
    fnctx.loop_depth -= 1
    fnctx.indent -= 1


def _for(fnctx, node):
    """`for x in expr:` -> `async for wy_x in engine._wy_aiter(expr,
    _TABLE):` - _wy_aiter handles plain containers, a `co`/Cursor, and (via
    the module's own _TABLE, passed through explicitly since the helper
    has no message table of its own) a class instance with a registered
    `__iter__`. `orelse` maps straight onto Python's own for/else, which
    already has the "skipped if the loop exits via break" semantics this
    needs - and, since Python's `for` doesn't introduce its own block scope,
    naturally keeps seeing the same loop variable `orelse` needs to (its
    last-bound value) with no extra bookkeeping. One scope covers the
    target var, body, and orelse together (see FnCtx's docstring) - the
    target is declared into it, matching the interpreter's per-iteration
    child scope that both the body and (for the final iteration) the
    `else` clause share."""
    iterable = compile_expr(fnctx, node.iter)
    fnctx.push_scope()
    fnctx.loop_scope_floor.append(len(fnctx.scopes) - 1)
    target = fnctx.declare(node.var)
    fnctx.emit(f"async for {target} in engine._wy_aiter({iterable}, _TABLE):")
    fnctx.indent += 1
    fnctx.loop_depth += 1
    compile_block(fnctx, node.body)
    fnctx.loop_depth -= 1
    fnctx.indent -= 1
    fnctx.loop_scope_floor.pop()
    if node.orelse is not None:
        fnctx.emit("else:")
        fnctx.indent += 1
        compile_block(fnctx, node.orelse)
        fnctx.indent -= 1
    fnctx.pop_scope()


@STMT_HANDLERS.register(ast.ExprStmt)
def _expr_stmt(fnctx, node):
    fnctx.emit(compile_expr(fnctx, node.value))


@STMT_HANDLERS.register(ast.Yield)
def _yield_stmt(fnctx, node):
    """A bare `yield expr`/`yield from expr` in statement position parses
    as a Yield node directly (like Return/Break), not wrapped in an
    ExprStmt - so it needs its own statement-level registration even
    though expressions.py's Yield handler (registered against the same
    node type, for the `x := yield ...` expression-position use) does the
    actual codegen; Registry.get dispatches on type(node), so the two
    registrations (STMT_HANDLERS vs EXPR_HANDLERS) don't collide."""
    fnctx.emit(compile_expr(fnctx, node))


@STMT_HANDLERS.register(ast.Return)
def _return(fnctx, node):
    value = compile_expr(fnctx, node.value) if node.value is not None else "None"
    if not fnctx.has_error_defer:
        fnctx.emit(f"return {value}")
        return
    # This function has a `defer on error:` somewhere in it - its finally
    # block needs to know whether the function is unwinding because of an
    # error *value* being returned (a real Python exception is caught
    # separately, in _compile_defer's `except` clause), which only a
    # return site itself can determine.
    tmp = fnctx.hoist(value, prefix="_wy_ret")
    fnctx.emit(f"if is_error({tmp}):")
    fnctx.indent += 1
    fnctx.emit("_wy_error_exit = True")
    fnctx.indent -= 1
    fnctx.emit(f"return {tmp}")


@STMT_HANDLERS.register(ast.WithSimple)
def _with_simple(fnctx, node):
    value = compile_expr(fnctx, node.value)
    name = fnctx.declare(node.name)
    fnctx.emit(f"{name} = {value}")


@STMT_HANDLERS.register(ast.WithBlock)
def _with_block(fnctx, node):
    for binding in node.bindings:
        value = compile_expr(fnctx, binding.value)
        name = fnctx.declare(binding.name)
        fnctx.emit(f"{name} = {value}")


def _assign_target_expr(fnctx, target):
    if isinstance(target, ast.NameTarget):
        return fnctx.resolve_write_target(target.name)
    if isinstance(target, ast.AttrTarget):
        if isinstance(target.base, ast.ThisRef):
            if fnctx.this_var is None:
                err("'this' used outside a message/method body", target)
            expr = fnctx.this_var
        else:
            expr = fnctx.resolve_read(target.base)
        for attr in target.attrs:
            expr = f"({expr}).{py_ident(attr)}"
        return expr
    if isinstance(target, ast.IndexTarget):
        base = _assign_target_expr(fnctx, target.base)
        index = compile_expr(fnctx, target.index)
        return f"({base})[{index}]"
    err("only plain name, attribute and index assignment targets are "
        "supported by --compile-py", target)


def _var_target_name(target):
    if not isinstance(target, ast.VarTarget):
        err("unsupported var declaration target", target)
    return target.name


@STMT_HANDLERS.register(ast.StaticDecl)
def _static_decl(fnctx, node):
    """`static x: T = default` - a local whose storage outlives any one
    call, persisting across every call of *this* definition (see
    wyrm_eval_parse_tree.py's `_static_store_for`: "tied to the AST node
    itself... rather than to any one call's local_ctx"). This backend has
    no per-node Python object to hang that storage on the way the
    interpreter does, so it uses the next-closest equivalent: a dedicated
    module-level global, minted fresh per `static` statement (so two
    unrelated functions' same-named `static seed` don't collide - see
    ModuleCtx.static_count), holding engine._STATIC_UNSET until the first
    call initializes it. `global` makes this correct even inside a
    coroutine's nested `_body` closure - unlike a `nonlocal` target (see
    needed_nonlocal_decls), a real module global is visible at any nesting
    depth with just `global`.

    Aliasing the wyrm name directly to the global (rather than copying its
    value into an ordinary local) is what makes an ordinary later
    assignment (`seed = seed * 3877 + ...`) write through to the
    persistent storage instead of just rebinding a local - resolve_read/
    resolve_write_target both check the current scope before falling back
    to a plain local name, so registering it there is enough."""
    modctx = fnctx.modctx
    modctx.static_count += 1
    global_name = f"_wy_static_{modctx.static_count}_{py_ident(node.name)}"
    modctx.add_body(f"{global_name} = engine._STATIC_UNSET")
    fnctx.emit(f"global {global_name}")
    fnctx.emit(f"if {global_name} is engine._STATIC_UNSET:")
    fnctx.indent += 1
    default = compile_expr(fnctx, node.default) if node.default is not None else "None"
    fnctx.emit(f"{global_name} = {default}")
    fnctx.indent -= 1
    fnctx.scopes[-1][node.name] = global_name


@STMT_HANDLERS.register(ast.VarDecl)
def _var_decl(fnctx, node):
    wyrm_names = [_var_target_name(t) for t in node.targets]
    if node.values is None:
        names = [fnctx.declare(n) for n in wyrm_names]
        for n in names:
            fnctx.emit(f"{n} = None")
        return
    values = ", ".join(compile_expr(fnctx, v) for v in node.values)
    names = [fnctx.declare(n) for n in wyrm_names]
    fnctx.emit(f"{', '.join(names)} = {values}")


@STMT_HANDLERS.register(ast.Assign)
def _assign(fnctx, node):
    if node.op != "=":
        err(f"assignment operator {node.op!r} not supported by --compile-py", node)
    names = [_assign_target_expr(fnctx, t) for t in node.targets]
    values = ", ".join(compile_expr(fnctx, v) for v in node.values)
    fnctx.emit(f"{', '.join(names)} = {values}")
