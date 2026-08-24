"""The canonical s-expression wire format: `wypoc.ast_nodes` <-> pair lists.

This is the bridge a syntax tree crosses in and out of wyrm code. A decorator
receives the s-expression of what it decorates and answers the s-expression to
compile instead (see `doc/decorators.md`), so both directions have to agree
exactly — which is why they read one table (`ROWS`) rather than two switches.

## Shape

A node is a pair list whose head is a symbol naming the kind, followed by
that kind's fields in a fixed order::

    $['kind, field, field, ...]

`$['binop, '+, $['int, 1], $['int, 2]]` is `1 + 2`. Child *lists* are plain
lists (`[...]`) rather than pair lists so `for` walks them directly, though a
pair list handed back in a child-list position is accepted too.

**There are no boolean fields, by design:** a distinction in what a node *is*
becomes a kind of its own (`'defer` / `'defer_on`, `'catch` / `'catch_return`,
`'true` / `'false`), and a distinction in what role a child *plays* becomes a
position (a `*rest` parameter). Where this AST spells such a distinction as a
field, the row carries the value that field takes — `ROWS`' `when`/`sets`
pair is the one place the two spellings meet.

## Adding a node kind

Add one row to `ROWS`: the AST class, the head symbol, and its fields in
order. Both directions read the row, so there is no second place to keep in
step. A kind whose AST shape isn't a flat list of independent fields gets an
entry in `_ENCODERS`/`_DECODERS` instead; those are the handful listed under
"Irregular kinds" below.

## Where this differs from the reference implementation

That implementation's AST is one uniform node type with `a`/`b`/`c`/`d` child
slots, so its table names slots. This one has a dataclass per construct, so
the table names fields — same table, spelled against a different tree. Three
consequences worth knowing:

* **`nil` and `this` are ordinary names here.** wypoc has no `Nil` node (`nil`
  is a builtin binding) and spells `this` as its own `ThisRef` node. Both
  cross as the kinds the format defines (`'nil`, and `'name` with the name
  `this`), so a decorator sees the format's shape either way.
* **Types are carried, and lossily.** A type crosses as a bare-name node
  (`$['int]`) or, qualified, `$['qualified_name, seg, ..., name]` - the same
  shapes the reference parser's `qualified_name` action produces (see
  `_encode_type`/`decode_type`). This grammar's `ann_type` (wyrm.gram) keeps
  only the first arm of a `->` return-type union, so a multi-type result
  cannot cross. Nothing downstream enforces types, here or there.
* **No source positions.** A node carries no line or token range, so a tree
  rebuilt from an s-expression reports at the decorator that produced it. The
  AST's `pos` fields come back `None`, which every consumer already tolerates
  (see ast_nodes' module docstring).
"""
from wypoc import ast_nodes as ast
from wypoc.wyrm_builtins import NIL, Pair, Symbol

# The operators a `'binop`/`'unop` may name. The one place a wyrm operator is
# spelled as a symbol; `UnaryOp` names its operators (`neg`, `pos`, `inv`)
# rather than spelling them, so that arm needs the two spellings mapped (see
# `_UNOP_TO_OP`).
OPERATORS = (
    "+", "-", "*", "/", "%", "**", "&", "|", "^", "<<", ">>",
    "==", "!=", "<", ">", "<=", ">=", "<=>",
)

_UNOP_TO_OP = {"neg": "-", "pos": "+", "inv": "~"}
_OP_TO_UNOP = {op: name for name, op in _UNOP_TO_OP.items()}


class SexprError(Exception):
    """A tree that cannot cross, in either direction: a construct the format
    does not carry, or an s-expression that is not a well-formed node. The
    message names the construct or the mistake; the caller adds the decorator
    and the line (see wyrm_eval_parse_tree.expand_decorated)."""


# Constructs with no kind in the format, named in the diagnostic rather than
# reported as an unrecognised class. Ordered as doc/sexpr-spec.md's "Not in
# the format yet" lists them.
_CANNOT_CROSS = {
    ast.CoDef: "a coroutine cannot cross into a decorator yet",
    ast.Yield: "a yield cannot cross into a decorator yet",
    ast.ClassDef: "a class cannot cross into a decorator yet",
    ast.SlotDef: "a slot cannot cross into a decorator yet",
    ast.SignalDef: "a signal cannot cross into a decorator yet",
    ast.Emit: "an emit cannot cross into a decorator yet",
    ast.FromImport: "a from-import cannot cross into a decorator yet",
    ast.SuperCall: "super() cannot cross into a decorator yet",
    ast.Lambda: "an anonymous fn cannot cross into a decorator yet",
    ast.Char: "a character literal cannot cross into a decorator yet",
    ast.Defined: "defined() cannot cross into a decorator yet",
    ast.MessageTupleExpr: "a tuple message send cannot cross into a decorator yet",
    ast.SetIfUnset: "'?=' as an expression cannot cross into a decorator yet",
    ast.AstRef: "a $ast reference cannot cross into a decorator yet",
    ast.Kwarg: "a keyword argument cannot cross into a decorator yet",
    ast.SpreadPos: "a spread argument cannot cross into a decorator yet",
    ast.SpreadKw: "a spread argument cannot cross into a decorator yet",
}

# ---------------------------------------------------------------------
# Field kinds
#
# A field names an AST attribute and how it is spelled on the wire. Each is
# a pair of pure functions, so a row reads the same in both directions.
# ---------------------------------------------------------------------

SYM = "sym"            # a str attribute      -> symbol
TEXT = "text"          # a str attribute      -> str
NODE = "node"          # a child node         -> node, or nil when absent
NODES = "nodes"        # a list of children   -> list of nodes
NAMES = "names"        # a list of str        -> list of 'name nodes
TYPES = "types"        # a list of TypeExpr   -> list of type nodes
TYPE = "type"          # an optional TypeExpr -> type node, or nil when absent
RESERVED = "reserved"  # always nil, and only nil is accepted back


class F:
    """One field of one row: the AST attribute, and its wire spelling."""

    __slots__ = ("attr", "kind")

    def __init__(self, attr: str, kind: str):
        self.attr = attr
        self.kind = kind


class Row:
    """One node kind. `when` selects among rows sharing an AST class, and
    `sets` is what that same distinction assigns back on the way in - the
    format has no flags, so a flag in the AST becomes two rows here.
    `defaults` supplies the constructor arguments the format doesn't carry."""

    __slots__ = ("cls", "kind", "fields", "when", "sets", "defaults")

    def __init__(self, cls, kind, *fields, when=None, sets=None, defaults=None):
        self.cls = cls
        self.kind = kind
        self.fields = fields
        self.when = when
        self.sets = sets or {}
        self.defaults = defaults or {}


ROWS = (
    # --- literals and names ------------------------------------------------
    # `'int`/`'float`/`'str` are irregular: those nodes hold raw token text
    # (see "Strings and numbers" below), so they live in _ENCODERS/_DECODERS.
    Row(ast.Bool, "true", when=lambda n: n.value is True, sets={"value": True}),
    Row(ast.Bool, "false", when=lambda n: n.value is False, sets={"value": False}),
    Row(ast.EllipsisExpr, "ellipsis"),
    Row(ast.Symbol, "sym", F("name", SYM)),
    Row(ast.Name, "name", F("id", SYM)),

    # --- expressions -------------------------------------------------------
    Row(ast.Array, "list", F("items", NODES)),
    Row(ast.Tuple, "tuple", F("items", NODES)),
    Row(ast.DictEntry, "pair", F("key", NODE), F("value", NODE)),
    Row(ast.Pair, "pairlist", F("elements", NODES)),
    Row(ast.Dict, "dict", F("entries", NODES)),
    Row(ast.UnaryOp, "not", F("operand", NODE),
        when=lambda n: n.op == "not", sets={"op": "not"}),
    Row(ast.BinOp, "and", F("left", NODE), F("right", NODE),
        when=lambda n: n.op == "and", sets={"op": "and"}),
    Row(ast.BinOp, "or", F("left", NODE), F("right", NODE),
        when=lambda n: n.op == "or", sets={"op": "or"}),
    Row(ast.Call, "call", F("func", NODE), F("args", NODES)),
    Row(ast.Attr, "attr", F("obj", NODE), F("name", SYM)),
    Row(ast.Index, "index", F("obj", NODE), F("index", NODE)),
    Row(ast.Message, "msg", F("obj", NODE), F("name", SYM), F("args", NODES)),
    Row(ast.Scope, "mod_get", F("obj", NODE), F("name", SYM)),
    Row(ast.TypeCheck, "is", F("value", NODE), F("types", TYPES)),
    Row(ast.Do, "do", F("body", NODES)),
    Row(ast.Try, "try", F("value", NODE)),

    # --- statements --------------------------------------------------------
    Row(ast.ExprStmt, "expr_stmt", F("value", NODE)),
    Row(ast.StaticDecl, "static", F("name", SYM), F("default", NODE),
        defaults={"type": None}),
    Row(ast.While, "while", F("cond", NODE), F("body", NODES)),
    # `'break` carries a value in the format; `break` takes none in this
    # grammar (see wyrm.gram's break_stmt), so the position is always nil.
    Row(ast.Break, "break", F("value", RESERVED)),
    Row(ast.Continue, "continue"),
    Row(ast.Return, "return", F("value", NODE)),
    Row(ast.Pass, "pass"),
    Row(ast.WithBlock, "with", F("bindings", NODES)),

    # --- definitions -------------------------------------------------------
    Row(ast.Param, "param", F("name", SYM), F("type", TYPE),
        defaults={"default": None}),

    # --- decorators ----------------------------------------------------
    # An unexpanded decorator application, crossing raw - this is what makes
    # `macroexpand()` possible: an outer decorator's `this` can itself be a
    # `'decorated` node (see wyrm_eval_parse_tree.expand_decorated, which no
    # longer pre-expands a stacked decorator's inner before boxing it).
    # `has_parens`/positions are decoded with sensible defaults since the
    # format has no field for them - they only ever affected parsing.
    Row(ast.Decorator, "decorator", F("name", SYM), F("args", NODES),
        defaults={"has_parens": True}),
    Row(ast.Decorated, "decorated", F("decorator", NODE), F("inner", NODE)),
)

_ROWS_BY_CLASS: dict = {}
for _row in ROWS:
    _ROWS_BY_CLASS.setdefault(_row.cls, []).append(_row)

_ROWS_BY_KIND = {_row.kind: _row for _row in ROWS}


# ---------------------------------------------------------------------
# Pair-list plumbing
# ---------------------------------------------------------------------

def _pairs(items) -> object:
    """A proper pair list of `items` - what `$[...]` builds."""
    result = NIL
    for item in reversed(list(items)):
        result = Pair(item, result)
    return result


def node(kind: str, *fields) -> object:
    """`$['kind, field, ...]` - one s-expression node."""
    return _pairs([Symbol(kind)] + list(fields))


def _as_list(value, what: str) -> list:
    """A child-list field's elements. Both a list and a pair list are
    accepted coming back in (the encoder always produces a list), since a
    decorator building one out of `cons` has no reason to know which."""
    if isinstance(value, list):
        return list(value)
    if value is NIL or value is None:
        return []
    if isinstance(value, Pair):
        out = []
        cursor = value
        while isinstance(cursor, Pair):
            out.append(cursor.car)
            cursor = cursor.cdr
        if cursor is not NIL and cursor is not None:
            raise SexprError(f"{what} must be a proper list")
        return out
    raise SexprError(f"{what} must be a list, not a {_type_name(value)}")


def _type_name(value) -> str:
    if value is None or value is NIL:
        return "nil"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, Symbol):
        return "sym"
    return type(value).__name__


def _fields_of(sexpr) -> list:
    """A node's kind symbol and its fields, checked only as far as "this is
    a `$[...]` list whose head is a symbol"."""
    if not isinstance(sexpr, Pair):
        raise SexprError(f"a node must be a $[...] list, not a {_type_name(sexpr)}")
    items = _as_list(sexpr, "a node")
    if not isinstance(items[0], Symbol):
        raise SexprError(
            f"a node's head must be a kind symbol, not a {_type_name(items[0])}"
        )
    return items[0].name, items[1:]


# ---------------------------------------------------------------------
# Strings and numbers
#
# Both hold raw token text in the AST (see ast_nodes.Str/Num), because the
# tokenizer hands the parser the source characters and the evaluator is what
# interprets them. Crossing therefore means interpreting on the way out and
# re-spelling on the way back in.
# ---------------------------------------------------------------------

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t",
            "\r": "\\r", "\0": "\\0"}


def quote_string(text: str) -> str:
    """`text` as the source spelling of a string literal - the inverse of
    wyrm_eval_parse_tree.eval_string_literal, so a `'str` node decoded and
    then evaluated yields the characters it came in with."""
    return '"' + "".join(_ESCAPES.get(c, c) for c in text) + '"'


def spell_number(value) -> str:
    """`value` as the source spelling of a number literal, such that
    eval_number_literal reads back the same int or float. `repr` on a float
    is exact and always carries a `.` or an `e`, which is what tells the two
    apart when the text is read again."""
    return str(value) if isinstance(value, int) else repr(value)


# ---------------------------------------------------------------------
# Targets
#
# An assignment's left-hand side is its own small family of nodes here
# (NameTarget/AttrTarget/IndexTarget), while the format has only the
# expression kinds - `x`, `a.b`, `a[0]`. The two conversions below are the
# whole of that difference.
# ---------------------------------------------------------------------

def target_to_expr(target):
    if isinstance(target, ast.NameTarget):
        return ast.Name(target.name)
    if isinstance(target, ast.AttrTarget):
        base = (ast.ThisRef() if isinstance(target.base, ast.ThisRef)
                else ast.Name(target.base))
        for name in target.attrs:
            base = ast.Attr(base, name)
        return base
    if isinstance(target, ast.IndexTarget):
        return ast.Index(target_to_expr(target.base), target.index)
    raise SexprError(f"{type(target).__name__} is not an assignable target")


def expr_to_target(expr):
    if isinstance(expr, ast.Name):
        return ast.NameTarget(expr.id)
    if isinstance(expr, ast.Index):
        return ast.IndexTarget(expr_to_target(expr.obj), expr.index)
    if isinstance(expr, ast.Attr):
        attrs = []
        cursor = expr
        while isinstance(cursor, ast.Attr):
            attrs.append(cursor.name)
            cursor = cursor.obj
        attrs.reverse()
        if isinstance(cursor, ast.ThisRef):
            return ast.AttrTarget(ast.ThisRef(), attrs)
        if isinstance(cursor, ast.Name):
            return ast.AttrTarget(cursor.id, attrs)
        raise SexprError(
            "an assignment target's attribute chain must start at a name or `this`"
        )
    raise SexprError(f"a {_type_name(expr)} is not an assignable target")


# ---------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------

def encode(tree):
    """The canonical s-expression of one AST node."""
    if tree is None:
        return NIL
    encoder = _ENCODERS.get(type(tree))
    if encoder is not None:
        return encoder(tree)
    message = _CANNOT_CROSS.get(type(tree))
    if message is not None:
        raise SexprError(message)
    rows = _ROWS_BY_CLASS.get(type(tree))
    if rows is None:
        raise SexprError(f"{type(tree).__name__} has no s-expression kind")
    for row in rows:
        if row.when is None or row.when(tree):
            return _encode_row(row, tree)
    raise SexprError(
        f"{type(tree).__name__} has no s-expression kind for this form"
    )


def _encode_row(row: Row, tree) -> object:
    fields = [Symbol(row.kind)]
    for field in row.fields:
        fields.append(_encode_field(field, tree))
    return _pairs(fields)


def _encode_field(field: F, tree):
    if field.kind is RESERVED:
        return NIL
    value = getattr(tree, field.attr, None)
    if field.kind is SYM:
        return Symbol(value) if value is not None else NIL
    if field.kind is TEXT:
        return value
    if field.kind is NODE:
        return encode(value)
    if field.kind is NODES:
        return [encode(child) for child in (value or ())]
    if field.kind is NAMES:
        return [node("name", Symbol(name)) for name in (value or ())]
    if field.kind is TYPES:
        return [_encode_type(t) for t in (value or ())]
    if field.kind is TYPE:
        return NIL if value is None else _encode_type(value)
    raise AssertionError(f"unknown field kind {field.kind!r}")


def _encode_num(tree: ast.Num):
    from wypoc.wyrm_eval_parse_tree import eval_number_literal

    value = eval_number_literal(tree.value)
    return node("float" if isinstance(value, float) else "int", value)


def _encode_str(tree: ast.Str):
    from wypoc.wyrm_eval_parse_tree import eval_string_literal

    return node("str", eval_string_literal(tree.value))


def _encode_type(tree):
    """A concrete type: always `$['type, X]`, the same wrapper the `$['type,
    'auto]` no-annotation sentinel uses (see `_encode_var_type`) - `X` is a
    bare name symbol (`$['type, 'int]`, `$['type, 'nil]`) for one
    unqualified name, or a nested `$['qualified_name, seg, ...]` for a
    `::`-qualified one (a single segment collapses to just that segment,
    matching the reference implementation's own `qualified_name` parser
    action, wy/wyrm/parser/parser.wy's `_mk_qualified_name`)."""
    if not isinstance(tree, ast.TypeExpr) or not tree.parts:
        raise SexprError("a type must be a name, optionally qualified")
    if len(tree.parts) == 1:
        return node("type", Symbol(tree.parts[0]))
    return node("type", node("qualified_name", *[Symbol(part) for part in tree.parts]))


def _encode_var_type(type_expr):
    """A `var`/`define` target's type position: `$['type, 'auto]` when no
    annotation was written, the ordinary type shape otherwise - matching
    `_mk_type_expression`'s default in the reference parser."""
    if type_expr is None:
        return node("type", Symbol("auto"))
    return _encode_type(type_expr)


def _encode_binop(tree: ast.BinOp):
    if tree.op in ("and", "or"):
        return _encode_row(_ROWS_BY_KIND[tree.op], tree)
    if tree.op not in OPERATORS:
        raise SexprError(
            f"the {tree.op!r} operator cannot cross into a decorator yet"
        )
    return node("binop", Symbol(tree.op), encode(tree.left), encode(tree.right))


def _encode_unaryop(tree: ast.UnaryOp):
    if tree.op == "not":
        return _encode_row(_ROWS_BY_KIND["not"], tree)
    op = _UNOP_TO_OP.get(tree.op)
    if op is None:
        raise SexprError(
            f"the unary {tree.op!r} operator cannot cross into a decorator yet"
        )
    return node("unop", Symbol(op), encode(tree.operand))


def _encode_catch(tree: ast.Catch):
    """`v catch h` and `v catch return h` are two kinds, not one carrying a
    flag - so the handler crosses as the expression it is either way, and
    the kind says what happens to it."""
    if isinstance(tree.handler, ast.Return):
        return node("catch_return", encode(tree.value), encode(tree.handler.value))
    return node("catch", encode(tree.value), encode(tree.handler))


def _encode_if(tree: ast.If):
    """`elif` has no kind of its own: an `elif` chain is the nested `if` it
    means, in the else position, which is how the format's three-field `'if`
    carries arbitrarily many branches."""
    orelse = list(tree.orelse or ())
    for clause in reversed(tree.elifs or ()):
        orelse = [ast.If(clause.cond, clause.body, [], orelse or None)]
    return node("if", encode(tree.cond),
                [encode(s) for s in tree.body],
                [encode(s) for s in orelse])


def _encode_for(tree: ast.For):
    if tree.orelse is not None:
        raise SexprError("a for/else cannot cross into a decorator yet")
    return node("for", Symbol(tree.var), encode(tree.iter),
                [encode(s) for s in tree.body])


def _encode_var_decl(tree: ast.VarDecl):
    """`'define, name, type, value` for one target - the reference
    implementation's `_build_define` (wy/wyrm/parser/parser.wy). Several
    targets at once (`var a, b = 1, 2`) becomes `'define_values`: a list of
    `[name, type]` pairs plus the single init expression, wrapped in a
    `'tuple` node when there's more than one value - `_build_define` always
    hands `'define_values` one init expression, never one per target."""
    if len(tree.targets) == 1:
        target = tree.targets[0]
        value = tree.values[0] if tree.values else None
        return node("define", Symbol(target.name), _encode_var_type(target.type),
                    encode(value))
    pairs = [[Symbol(target.name), _encode_var_type(target.type)]
             for target in tree.targets]
    values = tree.values
    if not values:
        value_node = NIL
    elif len(values) == 1:
        value_node = encode(values[0])
    else:
        value_node = encode(ast.Tuple(values))
    return node("define_values", pairs, value_node)


def _encode_assign(tree: ast.Assign):
    if len(tree.targets) != 1 or len(tree.values) != 1:
        raise SexprError("a multiple assignment cannot cross into a decorator yet")
    kind = "qassign" if tree.op == "?=" else "assign"
    return node(kind, encode(target_to_expr(tree.targets[0])),
                encode(tree.values[0]))


def _encode_with_simple(tree: ast.WithSimple):
    """A single `with x = 1` is the one-binding case of the block form, so
    both cross as `'with` over a list of `'decl`s."""
    return node("with", [_encode_with_binding(tree)])


def _encode_with_binding(binding):
    return node("decl", Symbol(binding.name), encode(binding.value))


def _encode_defer(tree: ast.Defer):
    body = [encode(s) for s in tree.body]
    if not tree.on_error:
        return node("defer", body)
    # `defer on error` is the only guard this grammar spells, so the type
    # list it crosses with has exactly one entry. The format keeps the list
    # because the design allows for more.
    return node("defer_on", body, [_encode_type(ast.TypeExpr(["error"]))])


def _encode_fn(tree: ast.FnDef):
    """`'fn`: name, result types, `*rest`, `**kwargs`, parameters, dispatch
    types, body. The rest parameter leaves the parameter list on the way out
    and rejoins it, last, on the way back - which is what retires the
    is-rest flag the format used to carry."""
    params, rest = [], NIL
    for param in tree.params:
        if isinstance(param, ast.VarPositional):
            rest = node("param", Symbol(param.name), NIL)
        elif isinstance(param, ast.VarKeyword):
            raise SexprError(
                "**kwargs cannot cross into a decorator yet "
                "(the format reserves the position, and it is always nil)"
            )
        else:
            params.append(encode(param))
    results = [_encode_type(tree.ret)] if tree.ret is not None else []
    dispatch = [node("name", Symbol(n)) for n in (tree.class_target or ())]
    return node("fn", Symbol(tree.name), results, rest, NIL, params, dispatch,
                [encode(s) for s in tree.body])


def _encode_import(tree: ast.Import):
    """`'import`: path, then the four ways an import can narrow what it
    binds (alias / items / wildcard / except_names - mutually exclusive per
    ast_nodes.Import's docstring, so at most one of the three positions past
    `static` is ever non-nil) plus `static`. Booleans as plain fields is the
    one deliberate exception to "no boolean fields" in this table - `static`
    and `wildcard` don't correspond to distinct node *shapes` the way every
    other flag in this format does, so splitting them into kinds would only
    multiply the rows without adding a distinction worth making."""
    path = [node("name", Symbol(p)) for p in tree.path]
    alias = node("name", Symbol(tree.alias)) if tree.alias else NIL
    items = ([node("import_item", Symbol(i.name),
                    node("name", Symbol(i.alias)) if i.alias else NIL)
              for i in tree.items] if tree.items else NIL)
    except_names = ([node("name", Symbol(n)) for n in tree.except_names]
                     if tree.except_names else NIL)
    return node("import", path, node("true") if tree.static else node("false"),
                alias, items, node("true") if tree.wildcard else node("false"),
                except_names)


def _encode_program(tree: ast.Program):
    """`'module` splices its statements directly as siblings of the head
    symbol (`$['module, stmt, stmt, ...]`) rather than carrying them in a
    single list-valued field, matching the reference parser's `_mk_module`
    (`cons('module, expr)` over the already-flat statement list)."""
    return _pairs([Symbol("module")] + [encode(s) for s in tree.body])


_ENCODERS = {
    ast.Num: _encode_num,
    ast.Str: _encode_str,
    ast.BinOp: _encode_binop,
    ast.UnaryOp: _encode_unaryop,
    ast.Catch: _encode_catch,
    ast.If: _encode_if,
    ast.For: _encode_for,
    ast.VarDecl: _encode_var_decl,
    ast.Assign: _encode_assign,
    ast.WithSimple: _encode_with_simple,
    ast.WithBinding: _encode_with_binding,
    ast.Defer: _encode_defer,
    ast.FnDef: _encode_fn,
    ast.Import: _encode_import,
    ast.TypeExpr: _encode_type,
    ast.Program: _encode_program,
    ast.ThisRef: lambda tree: node("name", Symbol("this")),
    ast.Name: lambda tree: (node("nil") if tree.id == "nil"
                            else node("name", Symbol(tree.id))),
}


# ---------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------

def decode(sexpr):
    """One AST node from its canonical s-expression. Raises `SexprError`
    naming the mistake - the kind, or the field that was missing or of the
    wrong shape."""
    kind, fields = _fields_of(sexpr)
    decoder = _DECODERS.get(kind)
    if decoder is not None:
        return decoder(kind, fields)
    row = _ROWS_BY_KIND.get(kind)
    if row is None:
        raise SexprError(f"'{kind} is not a node kind")
    return _decode_row(row, kind, fields)


def _decode_row(row: Row, kind: str, fields: list):
    if len(fields) != len(row.fields):
        raise SexprError(
            f"'{kind} takes {len(row.fields)} field(s), not {len(fields)}"
        )
    kwargs = dict(row.defaults)
    kwargs.update(row.sets)
    for field, value in zip(row.fields, fields):
        if field.kind is RESERVED:
            if value is not NIL and value is not None:
                raise SexprError(f"'{kind}'s reserved field must be nil")
            continue
        kwargs[field.attr] = _decode_field(field, value, kind)
    return row.cls(**kwargs)


def _decode_field(field: F, value, kind: str):
    if field.kind is SYM:
        if value is NIL or value is None:
            return None
        if not isinstance(value, Symbol):
            raise SexprError(
                f"'{kind}'s {field.attr} must be a symbol, not a {_type_name(value)}"
            )
        return value.name
    if field.kind is TEXT:
        if not isinstance(value, str) or isinstance(value, Symbol):
            raise SexprError(
                f"'{kind}'s {field.attr} must be a str, not a {_type_name(value)}"
            )
        return value
    if field.kind is NODE:
        return None if value is NIL or value is None else decode(value)
    if field.kind is NODES:
        return [decode(child) for child in _as_list(value, f"'{kind}'s {field.attr}")]
    if field.kind is NAMES:
        return [_decode_dispatch_name(child)
                for child in _as_list(value, f"'{kind}'s {field.attr}")]
    if field.kind is TYPES:
        return [decode_type(child)
                for child in _as_list(value, f"'{kind}'s {field.attr}")]
    if field.kind is TYPE:
        return None if value is NIL or value is None else decode_type(value)
    raise AssertionError(f"unknown field kind {field.kind!r}")


def _decode_dispatch_name(sexpr) -> str:
    """A dispatch type in a `'fn`'s signature: a `'name` node, per
    doc/sexpr-spec.md's "entries are `'name` nodes"."""
    kind, fields = _fields_of(sexpr)
    if kind != "name" or len(fields) != 1 or not isinstance(fields[0], Symbol):
        raise SexprError("a dispatch type must be a $['name, 'Cls] node")
    return fields[0].name


def decode_type(sexpr) -> ast.TypeExpr:
    """`$['type, X]` back into this AST's `TypeExpr` - the counterpart of
    `_encode_type`. `X` is a bare name symbol for one unqualified name, or a
    nested `'qualified_name` for a `::`-qualified one. Never sees `X ==
    'auto`: that sentinel ("no annotation was written") is only valid in a
    `var`/`define` target's type position, decoded by `_decode_var_type`
    instead."""
    kind, fields = _fields_of(sexpr)
    if kind != "type":
        raise SexprError(f"'{kind} is not a type")
    if len(fields) != 1:
        raise SexprError(f"'type takes 1 field(s), not {len(fields)}")
    value = fields[0]
    if isinstance(value, Symbol):
        if value.name == "auto":
            raise SexprError("'type, 'auto is only valid for an unannotated var/define target")
        return ast.TypeExpr([value.name])
    q_kind, q_fields = _fields_of(value)
    if q_kind != "qualified_name":
        raise SexprError("'type's field must be a symbol or a 'qualified_name")
    segments = []
    for segment in q_fields:
        if not isinstance(segment, Symbol):
            raise SexprError("a 'qualified_name's segments must be symbols")
        segments.append(segment.name)
    if not segments:
        raise SexprError("a 'qualified_name needs at least one segment")
    return ast.TypeExpr(segments)


def _decode_var_type(sexpr):
    """A `var`/`define` target's type position: `None` for the `$['type,
    'auto]` sentinel (no annotation was written), `decode_type` otherwise."""
    kind, fields = _fields_of(sexpr)
    if kind == "type" and len(fields) == 1 and isinstance(fields[0], Symbol) \
            and fields[0].name == "auto":
        return None
    return decode_type(sexpr)


def _expect(fields: list, count: int, kind: str) -> list:
    if len(fields) != count:
        raise SexprError(f"'{kind} takes {count} field(s), not {len(fields)}")
    return fields


def _decode_num(kind: str, fields: list):
    value, = _expect(fields, 1, kind)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SexprError(f"'{kind}'s value must be a number, not a {_type_name(value)}")
    if kind == "int" and not isinstance(value, int):
        raise SexprError("'int's value must be an int")
    return ast.Num(spell_number(value))


def _decode_str(kind: str, fields: list):
    text, = _expect(fields, 1, kind)
    if not isinstance(text, str) or isinstance(text, Symbol):
        raise SexprError(f"'str's text must be a str, not a {_type_name(text)}")
    return ast.Str(quote_string(text))


def _decode_child(value, what: str):
    if value is NIL or value is None:
        return None
    return decode(value)


def _decode_binop(kind: str, fields: list):
    op, left, right = _expect(fields, 3, kind)
    if not isinstance(op, Symbol):
        raise SexprError("s-expression is missing its binop")
    if op.name not in OPERATORS:
        raise SexprError(f"'{op.name} is not an operator")
    return ast.BinOp(op.name, decode(left), decode(right))


def _decode_unop(kind: str, fields: list):
    op, operand = _expect(fields, 2, kind)
    if not isinstance(op, Symbol) or op.name not in _OP_TO_UNOP:
        spelled = ", ".join(f"'{o}" for o in _OP_TO_UNOP)
        raise SexprError(f"'unop's operator must be one of {spelled}")
    return ast.UnaryOp(_OP_TO_UNOP[op.name], decode(operand))


def _decode_catch(kind: str, fields: list):
    value, handler = _expect(fields, 2, kind)
    handled = decode(handler)
    if kind == "catch_return":
        handled = ast.Return(handled)
    return ast.Catch(decode(value), handled)


def _decode_if(kind: str, fields: list):
    cond, then, orelse = _expect(fields, 3, kind)
    body = [decode(s) for s in _as_list(then, "'if's then branch")]
    else_body = [decode(s) for s in _as_list(orelse, "'if's else branch")]
    return ast.If(decode(cond), body, [], else_body or None)


def _decode_for(kind: str, fields: list):
    var, iterable, body = _expect(fields, 3, kind)
    if not isinstance(var, Symbol):
        raise SexprError("'for's variable must be a symbol")
    return ast.For(var.name, decode(iterable),
                   [decode(s) for s in _as_list(body, "'for's body")], None)


def _decode_target_name(value, kind: str) -> str:
    if not isinstance(value, Symbol):
        raise SexprError(f"'{kind}'s name must be a symbol, not a {_type_name(value)}")
    return value.name


def _decode_define(kind: str, fields: list):
    name, type_sexpr, value = _expect(fields, 3, kind)
    target = ast.VarTarget(_decode_target_name(name, kind), _decode_var_type(type_sexpr))
    decoded = _decode_child(value, "'define's value")
    return ast.VarDecl([target], None if decoded is None else [decoded])


def _decode_define_values(kind: str, fields: list):
    pairs, value = _expect(fields, 2, kind)
    targets = []
    for entry in _as_list(pairs, "'define_values's targets"):
        entry_fields = _as_list(entry, "'define_values's target")
        if len(entry_fields) != 2:
            raise SexprError("a 'define_values target must be $[name, type]")
        name, type_sexpr = entry_fields
        targets.append(
            ast.VarTarget(_decode_target_name(name, kind), _decode_var_type(type_sexpr))
        )
    if not targets:
        raise SexprError("'define_values needs at least one target")
    decoded = _decode_child(value, "'define_values's value")
    if decoded is None:
        values = None
    elif isinstance(decoded, ast.Tuple):
        if len(decoded.items) != len(targets):
            raise SexprError("'define_values's value tuple must match its targets")
        values = decoded.items
    else:
        raise SexprError("'define_values's value must be a 'tuple")
    return ast.VarDecl(targets, values)


def _decode_assign(kind: str, fields: list):
    target, value = _expect(fields, 2, kind)
    op = "?=" if kind == "qassign" else "="
    return ast.Assign([expr_to_target(decode(target))], op, [decode(value)])


def _decode_with(kind: str, fields: list):
    declarations, = _expect(fields, 1, kind)
    bindings = []
    for entry in _as_list(declarations, "'with's declarations"):
        entry_kind, entry_fields = _fields_of(entry)
        if entry_kind != "decl":
            raise SexprError("a 'with's declarations are 'decl nodes")
        name, value = _expect(entry_fields, 2, "decl")
        if not isinstance(name, Symbol):
            raise SexprError("'decl's name must be a symbol")
        bindings.append(ast.WithBinding(name.name, None, decode(value)))
    return ast.WithBlock(bindings)


def _decode_defer(kind: str, fields: list):
    if kind == "defer":
        body, = _expect(fields, 1, kind)
        return ast.Defer(False, [decode(s) for s in _as_list(body, "'defer's body")])
    body, types = _expect(fields, 2, kind)
    # The type list is carried but not honoured: `defer on error` is the only
    # guard this grammar spells (see wyrm.gram's defer_stmt), so a
    # `'defer_on` naming anything else still compiles to that one form.
    for entry in _as_list(types, "'defer_on's type alternatives"):
        decode_type(entry)
    return ast.Defer(True, [decode(s) for s in _as_list(body, "'defer_on's body")])


def _decode_fn(kind: str, fields: list):
    name, results, rest, kwargs, params, dispatch, body = _expect(fields, 7, kind)
    if not isinstance(name, Symbol):
        raise SexprError("'fn's name must be a symbol")
    if kwargs is not NIL and kwargs is not None:
        raise SexprError(
            "'fn's kwargs position is reserved and must be nil "
            "(the language has no keyword arguments)"
        )
    decoded = [decode(p) for p in _as_list(params, "'fn's parameters")]
    for param in decoded:
        if not isinstance(param, ast.Param):
            raise SexprError("'fn's parameters are 'param nodes")
    # The rest parameter rejoins the chain last, which is where the parser
    # would have put it: `fn f(a, *others)`.
    if rest is not NIL and rest is not None:
        rest_kind, rest_fields = _fields_of(rest)
        if rest_kind != "param":
            raise SexprError("'fn's rest parameter must be a 'param node")
        rest_name = rest_fields[0] if rest_fields else NIL
        if not isinstance(rest_name, Symbol):
            raise SexprError("'fn's rest parameter needs a name")
        decoded.append(ast.VarPositional(rest_name.name))
    result_types = _as_list(results, "'fn's result types")
    if len(result_types) > 1:
        raise SexprError(
            "a multi-value result cannot cross into a decorator yet "
            "(this grammar records one return type)"
        )
    dispatch_names = [_decode_dispatch_name(d)
                      for d in _as_list(dispatch, "'fn's dispatch types")]
    return ast.FnDef(
        dispatch_names or None,
        name.name,
        decoded,
        decode_type(result_types[0]) if result_types else None,
        [decode(s) for s in _as_list(body, "'fn's body")],
    )


def _decode_flag(value, kind: str, field: str) -> bool:
    flag_kind, flag_fields = _fields_of(value)
    if flag_kind == "true":
        return True
    if flag_kind == "false":
        return False
    raise SexprError(f"'{kind}'s {field} must be 'true or 'false")


def _decode_import_item(entry):
    entry_kind, entry_fields = _fields_of(entry)
    if entry_kind != "import_item":
        raise SexprError("an import's items are 'import_item nodes")
    name, alias = _expect(entry_fields, 2, "import_item")
    if not isinstance(name, Symbol):
        raise SexprError("'import_item's name must be a symbol")
    return ast.ImportItem(name.name,
                          _decode_dispatch_name(alias) if alias not in (NIL, None) else None)


def _decode_import(kind: str, fields: list):
    path, static, alias, items, wildcard, except_names = _expect(fields, 6, kind)
    path_names = [_decode_dispatch_name(p) for p in _as_list(path, "'import's path")]
    alias_v = _decode_dispatch_name(alias) if alias not in (NIL, None) else None
    items_v = ([_decode_import_item(i) for i in _as_list(items, "'import's items")]
               if items not in (NIL, None) else None)
    except_v = ([_decode_dispatch_name(n)
                for n in _as_list(except_names, "'import's except_names")]
                if except_names not in (NIL, None) else None)
    return ast.Import(path_names, alias_v, items_v,
                      _decode_flag(wildcard, kind, "wildcard"),
                      _decode_flag(static, kind, "static"), except_v)


def _decode_module(kind: str, fields: list):
    """`'module`'s statements are spliced in directly (see `_encode_program`
    for why), so `fields` - already just "everything after the head symbol"
    per `_fields_of` - is the statement list as-is."""
    return ast.Program([decode(s) for s in fields])


_DECODERS = {
    "int": _decode_num,
    "float": _decode_num,
    "str": _decode_str,
    "nil": lambda kind, fields: ast.Name("nil"),
    "import": _decode_import,
    "binop": _decode_binop,
    "unop": _decode_unop,
    "catch": _decode_catch,
    "catch_return": _decode_catch,
    "if": _decode_if,
    "for": _decode_for,
    "define": _decode_define,
    "define_values": _decode_define_values,
    "assign": _decode_assign,
    "qassign": _decode_assign,
    "with": _decode_with,
    "defer": _decode_defer,
    "defer_on": _decode_defer,
    "fn": _decode_fn,
    "module": _decode_module,
    # `this` is its own node here and a plain name there, so the general
    # `'name` row can't serve both - this one splits them by name.
    "name": lambda kind, fields: (
        ast.ThisRef() if (len(fields) == 1 and isinstance(fields[0], Symbol)
                          and fields[0].name == "this")
        else _decode_row(_ROWS_BY_KIND["name"], kind, fields)
    ),
}
