"""Statement compilation.

A wyrm function body compiles to a C function body, statement for statement:
`if` is `if`, `while` is `while`, `break` is `break`. The previous backend had
to build a graph of resumable chunks and jump between them, because the
convention it targeted could not hold a C stack frame across a call; the
convention this one targets can, so the control flow survives intact and the
join-point/continuation machinery is gone.

Two passes remain, and only because `--compile` does no type inference:
`collect_locals` walks the body first so every local's C declaration can be
emitted at the top of the function (C requires a declaration before use, and
a `var` inside a loop body must not be redeclared on each iteration), then
`run_stmts` emits.

Statement kinds are dispatched through STATEMENT_HANDLERS/LOCAL_COLLECT_
HANDLERS, so a new one can be added from this file or another without
touching the runner. Control flow (`if`/`while`/`return`/`break`/`continue`)
stays in the runner itself: each needs to recurse into `run_stmts` for its
own body, which a plain `(ctx, node)` handler signature cannot express.
"""
from wypoc import ast_nodes as ast

from .calls import compile_call  # noqa: F401  (registers the Call expression handler)
from .context import FnContext
from .errors import err
from .expressions import BOOL, compile_expr, compile_expr_as
from .handlers import LOCAL_COLLECT_HANDLERS, STATEMENT_HANDLERS
from .native_blocks import compile_native_block, is_native_block_call

# -- pass 1: collect locals ------------------------------------------------


def collect_locals(ctx: FnContext, stmts):
    for s in stmts:
        collect_locals_stmt(ctx, s)


def collect_locals_stmt(ctx: FnContext, s):
    if isinstance(s, ast.If):
        collect_locals(ctx, s.body)
        for clause in s.elifs:
            collect_locals(ctx, clause.body)
        collect_locals(ctx, s.orelse or [])
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


@LOCAL_COLLECT_HANDLERS.register(ast.VarDecl)
def _collect_var_decl(ctx: FnContext, s: ast.VarDecl):
    # No type inference, so every `var` (and `:=`, which desugars to a
    # VarDecl with no type - see actions.make_assignment_stmt) needs an
    # explicit type to give its C local a representation.
    for t in s.targets:
        if t.type is None:
            err(
                f"local '{t.name}' needs an explicit type for --compile "
                f"(e.g. 'var {t.name}: int', not ':=')", s,
            )
        ctx.declare(t.name, t.type)


@LOCAL_COLLECT_HANDLERS.register(ast.Assign)
def _collect_assign(ctx: FnContext, s: ast.Assign):
    if s.op == "?=":
        # `foo ?= expr` assigns only when `foo` is unset or an error, and a
        # compiled local is neither - _compile_assign would emit an
        # unconditional assignment, which is wrong rather than unsupported.
        err("'?=' not supported by --compile", s)
    for t in s.targets:
        if not isinstance(t, ast.NameTarget):
            err("only plain name targets are supported by --compile", s)
        if t.name not in ctx.locals:
            err(
                f"local '{t.name}' assigned before its type is known "
                f"(declare it first with 'var {t.name}: type')", s,
            )


@LOCAL_COLLECT_HANDLERS.register(ast.ExprStmt)
def _collect_expr_stmt(ctx: FnContext, s: ast.ExprStmt):
    pass


# -- pass 2: emit ----------------------------------------------------------


def run_stmts(ctx: FnContext, stmts):
    """Emit `stmts` into the current C block. Answers True if control cannot
    fall off the end (the block ended in a `return`), so the caller knows
    whether a trailing implicit `return nil` is reachable."""
    for s in stmts:
        if isinstance(s, ast.Pass):
            continue
        if isinstance(s, ast.Return):
            compile_return(ctx, s)
            return True
        if isinstance(s, ast.Break):
            if ctx.loop_depth == 0:
                err("'break' outside of a loop", s)
            ctx.emit("break;")
            return True
        if isinstance(s, ast.Continue):
            if ctx.loop_depth == 0:
                err("'continue' outside of a loop", s)
            ctx.emit("continue;")
            return True
        if isinstance(s, ast.If):
            compile_if_stmt(ctx, s)
            continue
        if isinstance(s, ast.While):
            compile_while_stmt(ctx, s)
            continue
        handler = STATEMENT_HANDLERS.get(s)
        if handler is None:
            err("statement not supported by --compile", s)
        handler(ctx, s)
    return False


def compile_if_stmt(ctx: FnContext, s: ast.If):
    """`if`/`elif`/`else`.

    An `elif`'s condition has to be evaluated *after* the preceding branch is
    known false, so it cannot be hoisted into the same block - which is why
    the chain nests as `else { if (...) ... }` rather than flattening to
    `else if`. That is exactly what an `elif` means, and it is what keeps a
    condition containing a call from running early."""
    ctx.emit(f"if ({_condition(ctx, s.cond)}) {{")
    ctx.indent += 1
    run_stmts(ctx, s.body)
    ctx.indent -= 1

    clauses = list(s.elifs)
    if clauses:
        ctx.emit("} else {")
        ctx.indent += 1
        compile_if_stmt(ctx, ast.If(clauses[0].cond, clauses[0].body,
                                    clauses[1:], s.orelse))
        ctx.indent -= 1
        ctx.emit("}")
        return
    if s.orelse:
        ctx.emit("} else {")
        ctx.indent += 1
        run_stmts(ctx, s.orelse)
        ctx.indent -= 1
    ctx.emit("}")


def compile_while_stmt(ctx: FnContext, s: ast.While):
    """`while`.

    The condition is re-evaluated every iteration, so anything hoisted out of
    it has to be re-run every iteration too - `while (1) { <hoisted> if
    (!cond) break; ... }` rather than `while (cond)`. When nothing was
    hoisted (the common case) the plain form is emitted instead, since it
    reads as what was written."""
    mark = len(ctx.lines)
    condition = _condition(ctx, s.cond)
    hoisted = ctx.lines[mark:]

    if not hoisted:
        ctx.emit(f"while ({condition}) {{")
        ctx.indent += 1
        ctx.loop_depth += 1
        run_stmts(ctx, s.body)
        ctx.loop_depth -= 1
        ctx.indent -= 1
        ctx.emit("}")
        return

    del ctx.lines[mark:]
    ctx.emit("for (;;) {")
    ctx.indent += 1
    for line in hoisted:
        ctx.lines.append(("    " + line) if line else "")
    ctx.emit(f"if (!({condition})) {{ break; }}")
    ctx.loop_depth += 1
    run_stmts(ctx, s.body)
    ctx.loop_depth -= 1
    ctx.indent -= 1
    ctx.emit("}")


def _condition(ctx: FnContext, node) -> str:
    """A condition as a C truth value. Any wyrm scalar is usable as one, so
    this only names the conversion rather than demanding a bool."""
    value = compile_expr(ctx, node)
    return value.expr if value.type is BOOL else f"(({value.expr}) != 0)"


def compile_return(ctx: FnContext, s: ast.Return):
    if s.value is None:
        ctx.emit("*out = lang_value_nil();")
        ctx.emit("return true;")
        return
    if isinstance(s.value, ast.Tuple):
        err("multi-value return not supported by --compile (a fn returns one value)", s)
    if isinstance(s.value, ast.Call) and is_native_block_call(s.value):
        err("native::block() cannot be used as a return value", s)
    if ctx.ret_type is None:
        err(f"fn '{ctx.fndef.name}' has no declared return type but returns a value", s)
    value = compile_expr_as(ctx, s.value, ctx.ret_type, f"fn '{ctx.fndef.name}' return")
    ctx.emit(f"*out = {ctx.ret_type.boxed(value)};")
    ctx.emit("return true;")


@STATEMENT_HANDLERS.register(ast.VarDecl)
def _compile_var_decl(ctx: FnContext, s: ast.VarDecl):
    if s.values is None:
        return  # forward declaration; the C declaration is already emitted
    if len(s.targets) != len(s.values):
        err("declaration target/value count mismatch", s)
    _assign_all(ctx, [t.name for t in s.targets], s.values)


@STATEMENT_HANDLERS.register(ast.Assign)
def _compile_assign(ctx: FnContext, s: ast.Assign):
    if len(s.targets) != len(s.values):
        err("assignment target/value count mismatch", s)
    _assign_all(ctx, [t.name for t in s.targets], s.values)


def _assign_all(ctx: FnContext, names, value_nodes):
    if len(names) == 1:
        target = ctx.type_of(names[0])
        value = compile_expr_as(ctx, value_nodes[0], target, f"'{names[0]}'")
        ctx.emit(f"{names[0]} = {value};")
        return
    # Every right-hand side is evaluated into a temporary first, so
    # `a, b = b, a` swaps rather than assigning `a` twice.
    temporaries = []
    for name, node in zip(names, value_nodes):
        target = ctx.type_of(name)
        tmp = ctx.new_tmp()
        ctx.emit(f"{target.ctype} {tmp} = "
                 f"{compile_expr_as(ctx, node, target, f'{name!r}')};")
        temporaries.append(tmp)
    for name, tmp in zip(names, temporaries):
        ctx.emit(f"{name} = {tmp};")


@STATEMENT_HANDLERS.register(ast.ExprStmt)
def _compile_expr_stmt(ctx: FnContext, s: ast.ExprStmt):
    if isinstance(s.value, ast.Call) and is_native_block_call(s.value):
        compile_native_block(ctx, s.value)
        return
    if isinstance(s.value, ast.Call):
        # A call whose result is discarded: compile it for its effects, and
        # tell C the value is deliberately unused.
        value = compile_expr(ctx, s.value)
        ctx.emit(f"(void)({value.expr});")
        return
    err(
        "an expression statement with no effect is not supported by --compile "
        "(only a call or native::block())", s,
    )
