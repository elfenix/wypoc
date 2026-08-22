"""AST node types for the wypoc Wyrm prototype parser.

Every node is a dataclass; `Node.__str__` recursively renders any node as a
compact `NodeName(field=value, ...)` form (lists/None/etc. formatted
sensibly), so any parsed tree can be printed straight away without a
separate pretty-printer per node type.

## Source positions

Every node carries a `pos` span covering the whole construct, and nodes
whose identifier is a bare `str` field (rather than a child node) also
carry a `name_pos` for just that identifier - `FnDef.pos` spans the entire
function including its body, while `FnDef.name_pos` is the handful of
columns a "go to definition" jump should actually land on and highlight.
Where a node holds a *list* of bare names (`Import.path`,
`TypeExpr.parts`, `FnDef.class_target`, ...) there's a parallel
`<field>_pos` list with one span per name, positionally aligned with it.

A span is a 4-tuple, `(line, col, end_line, end_col)`, with **1-based
lines and 0-based columns** - the convention `tokenize.TokenInfo` already
uses, since that's where these come from. LSP wants 0-based lines, so
converting means subtracting 1 from the line numbers only (see
`lsp.py`'s `_range`).

Spans are populated by the grammar actions in `wyrm.gram`, via pegen's
`LOCATIONS` magic plus `actions.tok_pos` for individual name tokens. They
are always optional: a node built by hand (a test, a desugaring) has
`pos=None`, and every consumer must tolerate that.

`__str__` deliberately hides all of it - position fields would drown out
the actual tree shape in `wypoc/parse.py`'s printed output, and the test
suite compares against those strings.
"""
from dataclasses import dataclass, fields
from typing import Optional, Union

# A source span: (line, col, end_line, end_col), 1-based line, 0-based col.
Span = Optional[tuple]


def merge_spans(start: Span, end: Span) -> Span:
    """One span covering both, e.g. an infix operator's whole expression
    from its left operand's start to its right operand's end. Tolerates
    either side being None (a hand-built node), returning whichever is
    known, or None if neither is."""
    if start is None:
        return end
    if end is None:
        return start
    return (start[0], start[1], end[2], end[3])


def _is_pos_field(name: str) -> bool:
    return name == "pos" or name.endswith("_pos")


def _is_hidden_field(name: str) -> bool:
    """Fields __str__ leaves out - position spans (too noisy) and `doc`
    (can be many lines of prose, would drown out the tree shape the same
    way a span would)."""
    return _is_pos_field(name) or name == "doc"


def _fmt(v) -> str:
    if isinstance(v, Node):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if isinstance(v, tuple):
        return "(" + ", ".join(_fmt(x) for x in v) + ")"
    if v is None:
        return "None"
    if isinstance(v, str):
        return repr(v)
    return str(v)


class Node:
    """Base class: gives every dataclass node a recursive, readable __str__."""

    # Every concrete node subclass gets a small, dense int tag, assigned in
    # class-definition order - a "bytecode value" for the node's own kind.
    # The evaluator dispatches on `node.TAG` (an O(1) array index) instead
    # of an `isinstance` chain - see wyrm_eval_parse_tree.py's
    # _EXPR_SIMPLE_DISPATCH/_EXPR_GEN_DISPATCH. Auto-assigned rather than
    # hand-numbered so adding/removing/reordering node classes here can
    # never desync a tag from its class; the numbering is only ever used
    # within one process's lifetime (never serialized), so it doesn't need
    # to be stable across runs.
    _next_tag = 0

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.TAG = Node._next_tag
        Node._next_tag += 1

    def __str__(self) -> str:
        parts = [
            f"{f.name}={_fmt(getattr(self, f.name))}"
            for f in fields(self)
            if not _is_hidden_field(f.name)
        ]
        return f"{type(self).__name__}({', '.join(parts)})"

    def children(self):
        """Every direct child Node, in field order (flattening list fields).
        Lets a consumer walk the tree generically - which is what a symbol
        table or a position-to-node lookup needs - without a visitor method
        per node type, the same way __str__ avoids a printer per node."""
        for f in fields(self):
            if _is_pos_field(f.name):
                continue
            value = getattr(self, f.name)
            if isinstance(value, Node):
                yield value
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, Node):
                        yield item

    def walk(self):
        """This node and every descendant, depth-first."""
        yield self
        for child in self.children():
            yield from child.walk()


# ---------------------------------------------------------------------
# Program / statements
# ---------------------------------------------------------------------

@dataclass
class Program(Node):
    body: list
    pos: Span = None
    doc: Optional[str] = None


@dataclass
class Pass(Node):
    pos: Span = None


@dataclass
class ExprStmt(Node):
    value: "Expr"
    pos: Span = None


@dataclass
class Return(Node):
    value: Optional["Expr"]
    pos: Span = None


@dataclass
class Yield(Node):
    value: Optional["Expr"]
    from_: bool = False
    pos: Span = None


@dataclass
class ElifClause(Node):
    cond: "Expr"
    body: list
    pos: Span = None


@dataclass
class If(Node):
    cond: "Expr"
    body: list
    elifs: list
    orelse: Optional[list]
    pos: Span = None


@dataclass
class While(Node):
    cond: "Expr"
    body: list
    pos: Span = None


@dataclass
class For(Node):
    var: str
    iter: "Expr"
    body: list
    orelse: Optional[list]
    var_pos: Span = None
    pos: Span = None


@dataclass
class Continue(Node):
    pos: Span = None


@dataclass
class Break(Node):
    pos: Span = None


@dataclass
class Defer(Node):
    on_error: bool
    body: list
    pos: Span = None


@dataclass
class Decorator(Node):
    """`@name(args)` or bare `@name` - see doc/language-spec.md's
    Decorators section.

    `name` is a single, *unqualified* identifier: a decorator is invoked as
    a message on the decorated tree, and a message selector is never a
    path, so `@a::b x` is a syntax error rather than a second meaning for
    `::`. `args` are the call's arguments, evaluated before the decorator
    runs. `has_parens` records whether an argument list was written at all,
    which is what makes `@d (expr)` read `(expr)` as the arguments while
    `@d() (expr)` decorates the parenthesized expression."""
    name: str
    args: list
    has_parens: bool = False
    name_pos: Span = None
    pos: Span = None


@dataclass
class Decorated(Node):
    """A statement or expression preceded by one `@decorator(...)`.

    Stacked decorators nest (`Decorated(dec1, Decorated(dec2, inner))`) and
    resolve innermost first, so an outer decorator sees whatever the inner
    one answered. `inner` is a statement node when the decorator was
    written in statement position and an expression node otherwise."""
    decorator: "Decorator"
    inner: "Node"
    pos: Span = None


@dataclass
class NameTarget(Node):
    name: str
    name_pos: Span = None
    pos: Span = None


@dataclass
class AttrTarget(Node):
    base: Union[str, "ThisRef"]
    attrs: list
    base_pos: Span = None
    attrs_pos: Optional[list] = None
    pos: Span = None


@dataclass
class IndexTarget(Node):
    """`base[index] = value` - `base` is itself a target (NameTarget,
    AttrTarget, or another IndexTarget, so `grid[i][j] = x` nests two of
    these), letting an array/dict be mutated in place rather than only
    ever rebuilt. See wyrm_eval_parse_tree.py's assign_target."""
    base: "Node"
    index: "Expr"
    pos: Span = None


@dataclass
class Assign(Node):
    """Plain assignment (`=`) or set-if-unset (`?=`) to already-declared
    target(s) - see doc/language-spec.md's Variables section. `:=` never
    produces this node; it's sugar for a `var` declaration (see VarDecl,
    built by actions.make_assignment_stmt)."""
    targets: list
    op: str
    values: list
    pos: Span = None


@dataclass
class VarTarget(Node):
    name: str
    type: Optional["TypeExpr"]
    name_pos: Span = None
    pos: Span = None


@dataclass
class VarDecl(Node):
    """`var` declaration statement, and the `:=` shorthand (inferred-type,
    single or multi-target) desugars to this too. `values` is None for a
    forward declaration (`var foo: int`) - the target(s) are bound to the
    Unset error value until first assignment."""
    targets: list  # list[VarTarget]
    values: Optional[list]
    pos: Span = None


@dataclass
class StaticDecl(Node):
    name: str
    type: Optional["TypeExpr"]
    default: Optional["Expr"]
    name_pos: Span = None
    pos: Span = None


@dataclass
class TypeExpr(Node):
    parts: list
    parts_pos: Optional[list] = None
    pos: Span = None


@dataclass
class WithSimple(Node):
    name: str
    type: Optional[TypeExpr]
    value: "Expr"
    name_pos: Span = None
    pos: Span = None


@dataclass
class WithBinding(Node):
    name: str
    type: Optional[TypeExpr]
    value: "Expr"
    name_pos: Span = None
    pos: Span = None


@dataclass
class WithBlock(Node):
    bindings: list
    pos: Span = None


# ---------------------------------------------------------------------
# Modules / imports
# ---------------------------------------------------------------------

@dataclass
class ImportItem(Node):
    """One entry of an `import mod::(a, b as c)` parenthesized list."""
    name: str
    alias: Optional[str] = None
    name_pos: Span = None
    alias_pos: Span = None
    pos: Span = None


@dataclass
class Import(Node):
    """Every `import` form (see doc/language-spec.md's "Modules and
    Imports" - the old, now-removed `using` keyword's bulk/aliased/listed
    imports are all expressed through this one node):

      import a::b::c            - path=["a","b","c"]
      import a::b::c as x       - path=[...], alias="x"
      import a::b::(x, y as z)  - path=["a","b"], items=[ImportItem("x"), ImportItem("y","z")]
      import a::b::*            - path=["a","b"], wildcard=True
      import a::b::* except x   - path=["a","b"], wildcard=True, except_names=["x"]
      import static a::b        - path=["a","b"], static=True

    `alias`, `items`, and `wildcard` are mutually exclusive; `static`
    combines with any of them.

    `static` marks a module that must be *run* before the importing module
    reaches the code that uses it, and whose messages join the importing
    module's message namespace - which is what makes a decorator defined
    there callable (see doc/language-spec.md's Decorators section).

    `path_pos` has one span per `path` segment, so a jump from `io` in
    `import std::io` can target that segment alone rather than the whole
    statement."""
    path: list
    alias: Optional[str] = None
    items: Optional[list] = None
    wildcard: bool = False
    static: bool = False
    except_names: Optional[list] = None
    path_pos: Optional[list] = None
    alias_pos: Span = None
    except_names_pos: Optional[list] = None
    pos: Span = None


@dataclass
class FromImport(Node):
    path: list
    names: list
    path_pos: Optional[list] = None
    names_pos: Optional[list] = None
    pos: Span = None


@dataclass
class ThreadSpawn(Node):
    """`thread a::b::c` - spawns `a::b::c` fresh on its own OS process (see
    wyrm_remote.py), evaluating to a RemoteModule value. Same `path`/
    `path_pos` shape as Import - `wyrm_modules.resolve_module_file` resolves
    it, never `import_module`'s cache (a `thread`-spawned module runs in a
    process of its own, not the caller's)."""
    path: list
    path_pos: Optional[list] = None
    pos: Span = None


@dataclass
class TaskSpawn(Node):
    """`task expr` - evaluates to a Future for `expr`'s eventual result
    (see wyrm_eval_parse_tree.py's ast.TaskSpawn case and
    _dispatch_remote_message). Deliberately narrow: only a
    `remote ! name(...)` call reached while evaluating `expr` actually
    goes asynchronous - anything else in `expr` just runs synchronously,
    with its own return value discarded (the Future is never resolved by
    it)."""
    expr: "Expr"
    pos: Span = None


# ---------------------------------------------------------------------
# Functions / coroutines
# ---------------------------------------------------------------------

@dataclass
class Param(Node):
    name: str
    type: Optional[TypeExpr]
    default: Optional["Expr"]
    name_pos: Span = None
    pos: Span = None


@dataclass
class VarPositional(Node):
    name: str
    name_pos: Span = None
    pos: Span = None


@dataclass
class VarKeyword(Node):
    name: str
    name_pos: Span = None
    pos: Span = None


@dataclass
class FnDef(Node):
    class_target: Optional[list]  # list of type names, e.g. ["Cls1", "Cls2"] for multi-dispatch
    name: str
    params: list
    ret: Optional[TypeExpr]
    body: list
    class_target_pos: Optional[list] = None
    name_pos: Span = None
    pos: Span = None
    doc: Optional[str] = None


@dataclass
class CoDef(Node):
    class_target: Optional[list]  # list of type names, e.g. ["Cls1", "Cls2"] for multi-dispatch
    name: str
    params: list
    intype: Optional[TypeExpr]
    ret: Optional[TypeExpr]
    body: list
    class_target_pos: Optional[list] = None
    name_pos: Span = None
    pos: Span = None
    doc: Optional[str] = None


# ---------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------

@dataclass
class ClassDef(Node):
    name: str
    bases: list
    body: list
    name_pos: Span = None
    pos: Span = None
    doc: Optional[str] = None


@dataclass
class SlotOption(Node):
    kind: str
    value: "Union[Expr, str]"
    pos: Span = None


@dataclass
class SlotDef(Node):
    name: str
    type: Optional[TypeExpr]
    default: Optional["Expr"]
    options: Optional[list]
    name_pos: Span = None
    pos: Span = None


@dataclass
class SignalDef(Node):
    """`signal name(item: int, item: str)` - a class member alongside slots
    (see wyrm.gram's class_member_item/class_member_brace), giving each
    instance its own subscriber list under that name (see
    wyrm_eval_parse_tree.Class.all_signals/SignalValue). `params` reuses
    fn_def's own param_list grammar rule/Param nodes purely as a documented
    signature - nothing type-checks an `emit` call's arguments against it,
    the same way an ordinary `fn`'s param types aren't enforced at runtime."""
    name: str
    params: list
    name_pos: Span = None
    pos: Span = None


@dataclass
class Emit(Node):
    """`emit name(args...)` - looks `name` up exactly like a bare slot
    reference (this.name or plain name inside a method body - see
    wyrm_eval_parse_tree.eval_stmt's Emit case), then synchronously calls
    every subscriber connected to it."""
    name: str
    args: list
    name_pos: Span = None
    pos: Span = None


# No InitDef: a class constructor is just a FnDef named "init" living in
# ClassDef.body like any other method (see wyrm_eval_parse_tree.Class).

# ---------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------

@dataclass
class Num(Node):
    value: str
    pos: Span = None


@dataclass
class Str(Node):
    value: str
    pos: Span = None


@dataclass
class Char(Node):
    value: str
    pos: Span = None


@dataclass
class Bool(Node):
    value: bool
    pos: Span = None


@dataclass
class Symbol(Node):
    name: str
    pos: Span = None


@dataclass
class EllipsisExpr(Node):
    """`...` - the placeholder literal. Named `EllipsisExpr` rather than
    `Ellipsis` so it doesn't shadow Python's own builtin inside this
    module; the s-expression kind it maps to is `'ellipsis`."""
    pos: Span = None


@dataclass
class Name(Node):
    id: str
    pos: Span = None


@dataclass
class AstRef(Node):
    """`foo::$ast` - the tree of the definition `foo` names, as a value.

    `::` rather than `.` because this resolves a *name* statically in a
    namespace: the runtime value of `foo` is a closure, which is not the
    thing being asked for. `$ast` is the first of a reserved `$`-family
    (`$name`, `$line`, `$doc` are not built); anything else after `$` is a
    parse error rather than a silently different meaning."""
    obj: "Expr"
    field: str = "ast"
    pos: Span = None


@dataclass
class ThisRef(Node):
    pos: Span = None


@dataclass
class SuperCall(Node):
    args: list
    pos: Span = None


@dataclass
class Defined(Node):
    symbol: Symbol
    pos: Span = None


@dataclass
class Lambda(Node):
    params: list
    body: list
    pos: Span = None


@dataclass
class Do(Node):
    """`do:` block used as an expression - see wyrm_eval_parse_tree.py's
    eval_expr (runs the body in a fresh child scope, same as any other
    block, and evaluates to the value of the last statement executed)."""
    body: list
    pos: Span = None


# No NewExpr: constructing a class is an ordinary Call whose func evaluates
# to a Class value (see wyrm_eval_parse_tree.call_value's Class branch).


@dataclass
class Array(Node):
    items: list
    pos: Span = None


@dataclass
class Pair(Node):
    elements: list
    pos: Span = None


@dataclass
class Tuple(Node):
    items: list
    pos: Span = None


@dataclass
class DictEntry(Node):
    key: "Expr"
    value: "Expr"
    pos: Span = None


@dataclass
class Dict(Node):
    entries: list
    pos: Span = None


@dataclass
class MessageTupleExpr(Node):
    items: list
    name: str
    args: Optional[list]
    name_pos: Span = None
    pos: Span = None


@dataclass
class Kwarg(Node):
    name: str
    value: "Expr"
    name_pos: Span = None
    pos: Span = None


@dataclass
class SpreadPos(Node):
    value: "Expr"
    pos: Span = None


@dataclass
class SpreadKw(Node):
    value: "Expr"
    pos: Span = None


@dataclass
class UnaryOp(Node):
    op: str
    operand: "Expr"
    pos: Span = None


@dataclass
class BinOp(Node):
    op: str
    left: "Expr"
    right: "Expr"
    pos: Span = None


@dataclass
class SetIfUnset(Node):
    target: "Expr"
    value: "Expr"
    pos: Span = None


@dataclass
class Call(Node):
    func: "Expr"
    args: list
    pos: Span = None


@dataclass
class Index(Node):
    obj: "Expr"
    index: "Expr"
    pos: Span = None


@dataclass
class Attr(Node):
    obj: "Expr"
    name: str
    name_pos: Span = None
    pos: Span = None


@dataclass
class Message(Node):
    obj: "Expr"
    name: str
    args: Optional[list]
    name_pos: Span = None
    pos: Span = None


@dataclass
class Scope(Node):
    obj: "Expr"
    name: str
    name_pos: Span = None
    pos: Span = None


@dataclass
class Try(Node):
    value: "Expr"
    pos: Span = None


@dataclass
class Catch(Node):
    value: "Expr"
    handler: "Union[Expr, Return]"
    pos: Span = None


@dataclass
class TypeCheck(Node):
    value: "Expr"
    types: list  # list[TypeExpr]; union via `is int | float`
    pos: Span = None


Expr = Union[
    Num, Str, Char, Bool, Symbol, EllipsisExpr, Name, AstRef, ThisRef,
    SuperCall, Defined, Lambda, Do, Array, Pair, Tuple, Dict,
    MessageTupleExpr, UnaryOp, BinOp, SetIfUnset, Call, Index, Attr,
    Message, Scope, Yield, Try, Catch, TypeCheck, Decorated,
]
