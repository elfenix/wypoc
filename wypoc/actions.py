"""Helper builders used by the generated pegen parser's grammar actions."""
from wypoc.ast_nodes import BinOp, Call, Index, Attr, Message, Scope, Tuple


def fold_left(first, rest):
    """rest is a list of (op_string, operand) pairs -> left-assoc BinOp chain."""
    node = first
    for op, operand in rest:
        node = BinOp(op, node, operand)
    return node


def make_tuple(first, more, trailing):
    return Tuple([first] + list(more))


def fold_postfix(first, ops):
    """ops is a list of partially-applied postfix node constructors, each
    taking the accumulated base expression as its first argument."""
    node = first
    for make_op in ops:
        node = make_op(node)
    return node
