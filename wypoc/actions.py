"""Helper builders used by the generated pegen parser's grammar actions."""
from wypoc.ast_nodes import (
    Assign, BinOp, Char, IndexTarget, NameTarget, Str, Symbol, Tuple,
    TypeExpr, VarDecl, VarTarget, merge_spans,
)


# ---------------------------------------------------------------------
# Source positions
#
# Most spans come from pegen's own `LOCATIONS` magic, which the generator
# expands into the whole rule's start/end (see tools/generate_parser.py for
# the formatting that makes it a `pos=` keyword argument). These helpers
# cover the two cases LOCATIONS can't: the span of one specific NAME token
# inside a rule, and the span of a construct whose extent isn't a single
# rule's extent (a left-folded operator chain).
# ---------------------------------------------------------------------

def tok_pos(tok):
    """The (line, col, end_line, end_col) span of a single token - used for
    the `name_pos` of a declared or referenced identifier, which is what a
    go-to-definition jump should land on rather than the whole construct."""
    return (tok.start[0], tok.start[1], tok.end[0], tok.end[1])


def names(toks):
    """The identifier text of a list of NAME tokens. Grammar rules that
    collect dotted/`::`-separated name lists (module_path, class_target,
    ...) yield raw tokens so their spans survive; this and `spans` split
    that back into the two parallel lists the AST nodes store."""
    return [t.string for t in toks]


def spans(toks):
    return [tok_pos(t) for t in toks]


def tail(pairs):
    """The second element of each (separator, item) pair - what a
    `first=X rest=(SEP X)*` capture leaves behind once the separators are
    dropped."""
    return [item for _, item in pairs]


def make_type_expr(toks, pos=None):
    """A TypeExpr from its `::`-separated NAME tokens, keeping one span per
    segment so `mod::Type` can resolve `mod` and `Type` independently."""
    return TypeExpr(names(toks), parts_pos=spans(toks), pos=pos)


def with_pos(node, pos=None):
    """Attach a span to an already-built node, for the rare rule that can
    only work out its own extent one level up from where it was built (see
    wyrm.gram's `start`)."""
    node.pos = pos
    return node


def _widen(node, start_pos):
    """Extend `node`'s span leftward to start at `start_pos` - for nodes
    built by folding, where the node's own action only saw the operator and
    right-hand side, not where the whole chain began."""
    node.pos = merge_spans(start_pos, node.pos)
    return node


def fold_left(first, rest):
    """rest is a list of (op_string, operand) pairs -> left-assoc BinOp chain."""
    node = first
    for op, operand in rest:
        start = getattr(node, "pos", None)
        node = BinOp(op, node, operand, pos=getattr(operand, "pos", None))
        _widen(node, start)
    return node


def make_tuple(first, more, trailing, pos=None):
    return Tuple([first] + list(more), pos=pos)


def fold_postfix(first, ops):
    """ops is a list of partially-applied postfix node constructors, each
    taking the accumulated base expression as its first argument."""
    node = first
    for make_op in ops:
        start = getattr(node, "pos", None)
        node = _widen(make_op(node), start)
    return node


def make_assignment_stmt(targets, op, values, pos=None):
    """`:=` is sugar for a `var` declaration with inferred type - every
    target must be a plain (undeclared) name, never an attr/index target,
    since there's nothing to index/attribute into until it's declared. `=`
    and `?=` stay ordinary Assign nodes (targets must already be declared -
    see wyrm_eval_parse_tree.py's assign_target)."""
    if op != ":=":
        return Assign(targets, op, values, pos=pos)
    var_targets = []
    for t in targets:
        if not isinstance(t, NameTarget):
            raise SyntaxError(f"':=' can only declare a plain name, not {t!r}")
        var_targets.append(VarTarget(t.name, None, name_pos=t.name_pos, pos=t.pos))
    return VarDecl(var_targets, values, pos=pos)


def make_string_literal(text, pos=None):
    """One of the three literal kinds the tokenizer routes through
    `token.STRING`, told apart by the leading character: `'` a symbol, `\\` a
    character, anything else a string. See wyrm_tokenizer's `_scan_symbol`
    and `_scan_char` for why all three share one wire type."""
    if text[0] == "'":
        return Symbol(text[1:], pos=pos)
    if text[0] == "\\":
        return Char(text, pos=pos)
    return Str(text, pos=pos)


def make_static_import(node):
    """Marks an `import` node as `import static ...` - the module must be
    run before the importing code that uses it, and its messages join the
    importing module's message namespace (see ast_nodes.Import)."""
    node.static = True
    return node


def fold_index_target(base, indices):
    """indices is a list of index expressions (from target's trailing
    `[expr]` suffixes, e.g. `grid[i][j] = x`) -> base wrapped in one
    IndexTarget per suffix, left to right. Yields `base` unchanged when
    there are no suffixes at all (a plain name/attr target)."""
    node = base
    for idx in indices:
        node = IndexTarget(node, idx, pos=merge_spans(
            getattr(node, "pos", None), getattr(idx, "pos", None)))
    return node
