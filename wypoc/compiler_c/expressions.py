"""Expression compilation: registers one EXPR_HANDLERS entry per supported
`wypoc.ast_nodes` expression type. Add support for a new literal/operator by
registering another handler here - `compile_expr` (used by every other
compiler_c submodule that needs to turn an expression node into a C
expression string) doesn't need to change as the set of supported
expression kinds grows toward interpreter parity."""
from wypoc import ast_nodes as ast

from .context import FnContext
from .errors import err
from .handlers import EXPR_HANDLERS
from .wtypes import BINOPS, is_float_literal


def compile_expr(ctx: FnContext, node) -> str:
    handler = EXPR_HANDLERS.get(node)
    if handler is None:
        err("expression not supported by --compile", node)
    return handler(ctx, node)


@EXPR_HANDLERS.register(ast.Num)
def _num(ctx: FnContext, node: ast.Num) -> str:
    if is_float_literal(node.value):
        err("float literals not supported by --compile (no VM type tag yet)", node)
    return node.value.replace("_", "")


@EXPR_HANDLERS.register(ast.Bool)
def _bool(ctx: FnContext, node: ast.Bool) -> str:
    return "true" if node.value else "false"


@EXPR_HANDLERS.register(ast.Name)
def _name(ctx: FnContext, node: ast.Name) -> str:
    if node.id not in ctx.locals:
        err(f"unknown identifier '{node.id}'", node)
    return ctx.local_ref(node.id)


@EXPR_HANDLERS.register(ast.UnaryOp)
def _unary_op(ctx: FnContext, node: ast.UnaryOp) -> str:
    if node.op == "-":
        return f"(-({compile_expr(ctx, node.operand)}))"
    if node.op == "not":
        return f"(!({compile_expr(ctx, node.operand)}))"
    err(f"unary operator '{node.op}' not supported by --compile", node)


@EXPR_HANDLERS.register(ast.BinOp)
def _bin_op(ctx: FnContext, node: ast.BinOp) -> str:
    if node.op not in BINOPS:
        err(f"operator '{node.op}' not supported by --compile", node)
    return f"({compile_expr(ctx, node.left)} {BINOPS[node.op]} {compile_expr(ctx, node.right)})"


@EXPR_HANDLERS.register(ast.Call)
def _call(ctx: FnContext, node: ast.Call) -> str:
    err(
        "calls nested inside larger expressions are not supported by --compile "
        "(only 'return f(...)', 'x = f(...)', or a bare 'f(...)' statement are)", node,
    )
