"""Helper builders used by the generated pegen parser's grammar actions."""
from wypoc.ast_nodes import BinOp, Call, Index, IndexTarget, Attr, Message, Scope, Tuple


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


def fold_index_target(base, indices):
    """indices is a list of index expressions (from target's trailing
    `[expr]` suffixes, e.g. `grid[i][j] = x`) -> base wrapped in one
    IndexTarget per suffix, left to right. Yields `base` unchanged when
    there are no suffixes at all (a plain name/attr target)."""
    node = base
    for idx in indices:
        node = IndexTarget(node, idx)
    return node
