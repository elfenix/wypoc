"""Helper builders used by the generated pegen parser's grammar actions."""
from wypoc.ast_nodes import (
    Assign, BinOp, Call, Index, IndexTarget, Attr, Message, NameTarget, Scope,
    Tuple, VarDecl, VarTarget,
)


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


def make_assignment_stmt(targets, op, values):
    """`:=` is sugar for a `var` declaration with inferred type - every
    target must be a plain (undeclared) name, never an attr/index target,
    since there's nothing to index/attribute into until it's declared. `=`
    and `?=` stay ordinary Assign nodes (targets must already be declared -
    see wyrm_eval_parse_tree.py's assign_target)."""
    if op != ":=":
        return Assign(targets, op, values)
    var_targets = []
    for t in targets:
        if not isinstance(t, NameTarget):
            raise SyntaxError(f"':=' can only declare a plain name, not {t!r}")
        var_targets.append(VarTarget(t.name, None))
    return VarDecl(var_targets, values)


def fold_index_target(base, indices):
    """indices is a list of index expressions (from target's trailing
    `[expr]` suffixes, e.g. `grid[i][j] = x`) -> base wrapped in one
    IndexTarget per suffix, left to right. Yields `base` unchanged when
    there are no suffixes at all (a plain name/attr target)."""
    node = base
    for idx in indices:
        node = IndexTarget(node, idx)
    return node
