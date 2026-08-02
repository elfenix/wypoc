"""Statement-list ("block") compilation - the state-machine engine that
turns a `wyrm` function body into chunks, per DESIGN.md's "Chunk model".

If/While/Return/Break/Continue and non-tail call splits are handled
directly in `run_stmts` (they need block_path/remaining_stmts/fallthrough
plumbing that a plain `(ctx, node)` handler signature doesn't carry, and
that set of control-flow forms is expected to stay small - see handlers.py's
module docstring). Every other statement kind (currently Assign, ExprStmt)
is dispatched through STATEMENT_HANDLERS/LOCAL_COLLECT_HANDLERS, so new
plain-statement kinds can be added - from this file or another - without
touching `run_stmts`/`collect_locals_stmt` themselves.
"""
from wypoc import ast_nodes as ast

from .calls import compile_call_split, compile_tail_call, split_call_stmt
from .context import FnContext
from .errors import err
from .expressions import compile_expr
from .handlers import LOCAL_COLLECT_HANDLERS, STATEMENT_HANDLERS
from .native_blocks import compile_native_block, is_native_block_call
from .wtypes import TYPES

# -- pass 1: collect locals (every local must have a known type before use) --


def collect_locals(ctx: FnContext, stmts):
    for s in stmts:
        collect_locals_stmt(ctx, s)


def collect_locals_stmt(ctx: FnContext, s):
    if isinstance(s, ast.If):
        collect_locals(ctx, s.body)
        for e in s.elifs:
            collect_locals(ctx, e.body)
        if s.orelse:
            collect_locals(ctx, s.orelse)
        return
    if isinstance(s, ast.While):
        collect_locals(ctx, s.body)
        return
    if isinstance(s, (ast.Return, ast.Pass, ast.Continue, ast.Break)):
        return
    handler = LOCAL_COLLECT_HANDLERS.get(s)
    if handler is None:
        err("statement not supported by --compile", s)
    handler(ctx, s)


@LOCAL_COLLECT_HANDLERS.register(ast.TypeHint)
def _collect_type_hint(ctx: FnContext, s: ast.TypeHint):
    ctx.declare(s.name, s.type)


@LOCAL_COLLECT_HANDLERS.register(ast.Assign)
def _collect_assign(ctx: FnContext, s: ast.Assign):
    for t in s.targets:
        if not isinstance(t, ast.NameTarget):
            err("only plain name targets are supported by --compile", s)
    if s.type is not None:
        for t in s.targets:
            ctx.declare(t.name, s.type)
    else:
        for t in s.targets:
            if t.name not in ctx.locals:
                err(
                    f"local '{t.name}' assigned before its type is known "
                    f"(add 'name: type' on first assignment)", s,
                )


@LOCAL_COLLECT_HANDLERS.register(ast.ExprStmt)
def _collect_expr_stmt(ctx: FnContext, s: ast.ExprStmt):
    pass


# -- pass 2: emit --


def run_stmts(ctx: FnContext, stmts, block_path, fallthrough):
    """Compile `stmts` into the currently-open chunk, opening/closing
    further chunks as needed for control flow and calls. `fallthrough`
    is the 0-arg callable to invoke (then close the chunk) if control
    falls off the end of `stmts` without an explicit return/break/
    continue/call-split."""
    for i, s in enumerate(stmts):
        if isinstance(s, (ast.Pass, ast.TypeHint)):
            continue
        if isinstance(s, ast.Continue):
            if ctx.continue_target is None:
                err("'continue' outside of a loop", s)
            ctx.continue_target()
            ctx.end_chunk()
            return
        if isinstance(s, ast.Break):
            if ctx.break_target is None:
                err("'break' outside of a loop", s)
            ctx.break_target()
            ctx.end_chunk()
            return
        if isinstance(s, ast.Return):
            compile_return(ctx, s)
            ctx.end_chunk()
            return
        if isinstance(s, ast.If):
            compile_if_stmt(ctx, s, block_path, stmts[i + 1:], fallthrough)
            return
        if isinstance(s, ast.While):
            compile_while_stmt(ctx, s, block_path, stmts[i + 1:], fallthrough)
            return
        split = split_call_stmt(s)
        if split is not None:
            compile_call_split(ctx, split, block_path, stmts[i + 1:], fallthrough, run_stmts)
            return
        handler = STATEMENT_HANDLERS.get(s)
        if handler is None:
            err("statement not supported by --compile", s)
        handler(ctx, s)
    fallthrough()
    ctx.end_chunk()


def continuation_for(ctx: FnContext, remaining_stmts, block_path, fallthrough):
    """Build the 0-arg jump callable a branch/loop-exit should invoke
    to continue with `remaining_stmts` (possibly empty) in block_path,
    followed by `fallthrough`. Returns (jump, materialize) where
    materialize (or None, if no join chunk was needed) must be called
    once, after all users of `jump` have been emitted, to compile the
    join chunk's body."""
    if not remaining_stmts:
        return fallthrough, None
    join_name = ctx.same_block_chunk_name(block_path)

    def jump():
        ctx.emit(f"wyrm_state_set_pending(state, {join_name});")
        ctx.emit("return WYRM_EXEC_CONTINUE;")

    def materialize():
        ctx.begin_chunk(join_name, static=True)
        run_stmts(ctx, remaining_stmts, block_path, fallthrough)

    return jump, materialize


def compile_if_stmt(ctx: FnContext, s: ast.If, block_path, remaining_stmts, fallthrough):
    join, materialize_join = continuation_for(ctx, remaining_stmts, block_path, fallthrough)

    branches = [(s.cond, s.body)] + [(e.cond, e.body) for e in s.elifs]
    branch_targets = [(cond, *ctx.new_child_block(block_path), body) for cond, body in branches]
    else_target = ctx.new_child_block(block_path) if s.orelse else None

    for i, (cond, _path, name, _body) in enumerate(branch_targets):
        kw = "if" if i == 0 else "} else if"
        ctx.emit(f"{kw} ({compile_expr(ctx, cond)}) {{")
        ctx.indent += 1
        ctx.emit(f"wyrm_state_set_pending(state, {name});")
        ctx.emit("return WYRM_EXEC_CONTINUE;")
        ctx.indent -= 1
    ctx.emit("} else {")
    ctx.indent += 1
    if else_target is not None:
        ctx.emit(f"wyrm_state_set_pending(state, {else_target[1]});")
        ctx.emit("return WYRM_EXEC_CONTINUE;")
    else:
        join()
    ctx.indent -= 1
    ctx.emit("}")
    ctx.end_chunk()

    for cond, path, name, body in branch_targets:
        ctx.begin_chunk(name, static=True)
        run_stmts(ctx, body, path, join)
    if else_target is not None:
        else_path, else_name = else_target
        ctx.begin_chunk(else_name, static=True)
        run_stmts(ctx, s.orelse, else_path, join)

    if materialize_join:
        materialize_join()


def compile_while_stmt(ctx: FnContext, s: ast.While, block_path, remaining_stmts, fallthrough):
    after, materialize_after = continuation_for(ctx, remaining_stmts, block_path, fallthrough)

    check_name = ctx.same_block_chunk_name(block_path)
    body_path, body_name = ctx.new_child_block(block_path)

    ctx.emit(f"wyrm_state_set_pending(state, {check_name});")
    ctx.emit("return WYRM_EXEC_CONTINUE;")
    ctx.end_chunk()

    ctx.begin_chunk(check_name, static=True)
    ctx.emit(f"if ({compile_expr(ctx, s.cond)}) {{")
    ctx.indent += 1
    ctx.emit(f"wyrm_state_set_pending(state, {body_name});")
    ctx.emit("return WYRM_EXEC_CONTINUE;")
    ctx.indent -= 1
    ctx.emit("} else {")
    ctx.indent += 1
    after()
    ctx.indent -= 1
    ctx.emit("}")
    ctx.end_chunk()

    def loop_back():
        ctx.emit(f"wyrm_state_set_pending(state, {check_name});")
        ctx.emit("return WYRM_EXEC_CONTINUE;")

    ctx.begin_chunk(body_name, static=True)
    old_break, old_continue = ctx.break_target, ctx.continue_target
    ctx.break_target, ctx.continue_target = after, loop_back
    run_stmts(ctx, s.body, body_path, loop_back)
    ctx.break_target, ctx.continue_target = old_break, old_continue

    if materialize_after:
        materialize_after()


def compile_return(ctx: FnContext, s: ast.Return):
    if s.value is None:
        ctx.emit("return WYRM_EXEC_DONE;")
        return
    if isinstance(s.value, ast.Tuple):
        err("multi-value return not supported by --compile (fn return type is single-valued)", s)
    if isinstance(s.value, ast.Call):
        if is_native_block_call(s.value):
            err("native::block() cannot be used as a return value", s)
        compile_tail_call(ctx, s.value)
        return
    if ctx.ret_type_name is None:
        err(f"fn '{ctx.fndef.name}' has no declared return type but returns a value", s)
    val = compile_expr(ctx, s.value)
    ctx.emit(f"wyrm_state_push_return(state, wyrm_value_word((wyrm_word)({val})));")
    ctx.emit("return WYRM_EXEC_DONE;")


@STATEMENT_HANDLERS.register(ast.Assign)
def _compile_assign(ctx: FnContext, s: ast.Assign):
    if len(s.targets) != len(s.values):
        err("assignment target/value count mismatch", s)
    if len(s.targets) == 1:
        ctx.emit_local_assign(s.targets[0].name, compile_expr(ctx, s.values[0]))
        return
    # Evaluate every RHS into a temp first so `a, b = b, a` works.
    tmp_names = []
    for t, v in zip(s.targets, s.values):
        ctype_, _tag, _field = TYPES[ctx.locals[t.name]]
        tmp = ctx.new_tmp()
        ctx.emit(f"{ctype_} {tmp} = {compile_expr(ctx, v)};")
        tmp_names.append(tmp)
    for t, tmp in zip(s.targets, tmp_names):
        ctx.emit_local_assign(t.name, tmp)


@STATEMENT_HANDLERS.register(ast.ExprStmt)
def _compile_expr_stmt(ctx: FnContext, s: ast.ExprStmt):
    call = s.value
    if isinstance(call, ast.Call) and is_native_block_call(call):
        compile_native_block(ctx, call)
        return
    # Non-native calls are intercepted by split_call_stmt before reaching
    # here, so any Call left is unreachable; keep the fallback generic.
    err("expression statements are not supported by --compile (only native::block() calls)", s)
