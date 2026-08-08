"""Expression compilation: one EXPR_HANDLERS entry per supported
`wypoc.ast_nodes` expression type.

`compile_expr` answers a `Value` - a C expression string plus the wyrm type
it has - and may *emit statements first*. That second half is what lets a
call appear anywhere in an expression: the call becomes a statement assigning
a temporary, and the temporary is the expression handed back. Everything that
compiles an expression therefore has to be somewhere a statement can be
emitted, which every caller here already is.

The one construct that interacts badly with hoisting is short-circuiting:
`a and f(b)` must not call `f` when `a` is false, but a hoisted call runs
unconditionally. `_bin_op` detects that case and lowers it to a temporary
plus an `if` instead of C's `&&`, so the short circuit survives.

Add support for a new literal/operator by registering another handler here;
no caller of `compile_expr` changes as the set grows.
"""
from typing import NamedTuple

from wypoc import ast_nodes as ast

from .context import FnContext
from .errors import err
from .handlers import EXPR_HANDLERS
from .wtypes import BINOPS, INTEGER_ONLY_BINOPS, TYPES, is_float_literal

BOOL = TYPES["bool"]
INT = TYPES["int"]
FLOAT = TYPES["float"]

COMPARISONS = frozenset({"<", "<=", ">", ">=", "==", "!="})
LOGICAL = frozenset({"and", "or"})


class Value(NamedTuple):
    """A compiled expression: the C text, and the wyrm type it evaluates to.

    Carrying the type is what makes mixed arithmetic correct - `1 + 2.5` has
    to box as a float, and `%` has to be refused on one - rather than every
    result being assumed to be a machine word."""

    expr: str
    type: "object"   # a wtypes.WType


def compile_expr(ctx: FnContext, node) -> Value:
    handler = EXPR_HANDLERS.get(node)
    if handler is None:
        err("expression not supported by --compile", node)
    return handler(ctx, node)


def compile_expr_as(ctx: FnContext, node, target, what: str) -> str:
    """`node` compiled and converted to `target`'s C type - the form needed
    wherever a value has to land in a declared slot (a local, a parameter,
    the result). Widening int to float is fine; narrowing the other way
    would silently truncate, so it is refused."""
    value = compile_expr(ctx, node)
    if value.type is target:
        return value.expr
    if target is FLOAT and value.type is not FLOAT:
        return f"(({target.ctype})({value.expr}))"
    if target is not FLOAT and value.type is FLOAT:
        err(f"{what}: a float value does not fit a '{target.name}'", node)
    # int/uint/bool are all integral; an explicit cast is the whole
    # conversion and C's own rules cover the rest.
    return f"(({target.ctype})({value.expr}))"


@EXPR_HANDLERS.register(ast.Num)
def _num(ctx: FnContext, node: ast.Num) -> Value:
    text = node.value.replace("_", "")
    if is_float_literal(text):
        return Value(text, FLOAT)
    if text.lower().startswith("0b"):
        # C11 has no binary-literal syntax (0b... is a GNU extension) - emit
        # the equivalent decimal constant rather than relying on it.
        return Value(str(int(text, 2)), INT)
    return Value(text, INT)


@EXPR_HANDLERS.register(ast.Bool)
def _bool(ctx: FnContext, node: ast.Bool) -> Value:
    return Value("true" if node.value else "false", BOOL)


@EXPR_HANDLERS.register(ast.Name)
def _name(ctx: FnContext, node: ast.Name) -> Value:
    if node.id not in ctx.locals:
        err(f"unknown identifier '{node.id}'", node)
    # A local is an ordinary C variable now, so its name *is* the expression.
    return Value(node.id, ctx.type_of(node.id))


@EXPR_HANDLERS.register(ast.UnaryOp)
def _unary_op(ctx: FnContext, node: ast.UnaryOp) -> Value:
    # The AST spells negation `neg`, not `-` (see wyrm.gram's unary_expr).
    if node.op == "neg":
        operand = compile_expr(ctx, node.operand)
        if operand.type is BOOL:
            err("unary '-' on a bool is not supported by --compile", node)
        return Value(f"(-({operand.expr}))", operand.type)
    if node.op == "not":
        return Value(f"(!({compile_expr(ctx, node.operand).expr}))", BOOL)
    err(f"unary operator '{node.op}' not supported by --compile", node)


def _arith_type(left: Value, right: Value):
    """The result type of an arithmetic binop: float if either side is one,
    otherwise the left operand's integral type."""
    if left.type is FLOAT or right.type is FLOAT:
        return FLOAT
    return INT if left.type is BOOL else left.type


@EXPR_HANDLERS.register(ast.BinOp)
def _bin_op(ctx: FnContext, node: ast.BinOp) -> Value:
    if node.op not in BINOPS:
        err(f"operator '{node.op}' not supported by --compile", node)
    if node.op in LOGICAL:
        return _logical_op(ctx, node)

    left = compile_expr(ctx, node.left)
    right = compile_expr(ctx, node.right)
    if node.op in INTEGER_ONLY_BINOPS and (left.type is FLOAT or right.type is FLOAT):
        err(f"operator '{node.op}' has no float form in C", node)
    result = BOOL if node.op in COMPARISONS else _arith_type(left, right)
    return Value(f"({left.expr} {BINOPS[node.op]} {right.expr})", result)


def _logical_op(ctx: FnContext, node: ast.BinOp) -> Value:
    """`and`/`or`. C's `&&`/`||` are the right lowering as long as the right
    operand is a pure expression; if compiling it emits statements (a call),
    those would run before the branch was even taken, so the short circuit is
    rebuilt explicitly around a temporary instead."""
    left = compile_expr(ctx, node.left)
    mark = len(ctx.lines)
    right = compile_expr(ctx, node.right)
    hoisted = ctx.lines[mark:]
    if not hoisted:
        return Value(f"({left.expr} {BINOPS[node.op]} {right.expr})", BOOL)

    del ctx.lines[mark:]
    tmp = ctx.new_tmp("__sc")
    ctx.emit(f"bool {tmp} = ({left.expr}) ? true : false;")
    ctx.emit(f"if ({'' if node.op == 'and' else '!'}{tmp}) {{")
    ctx.indent += 1
    # One extra level, preserving whatever nesting the hoisted lines already
    # had among themselves.
    for line in hoisted:
        ctx.lines.append(("    " + line) if line else "")
    ctx.emit(f"{tmp} = ({right.expr}) ? true : false;")
    ctx.indent -= 1
    ctx.emit("}")
    return Value(tmp, BOOL)
