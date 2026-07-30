"""AST node types for the wypoc Wyrm prototype parser.

Every node is a dataclass; `Node.__str__` recursively renders any node as a
compact `NodeName(field=value, ...)` form (lists/None/etc. formatted
sensibly), so any parsed tree can be printed straight away without a
separate pretty-printer per node type.
"""
from dataclasses import dataclass, fields
from typing import Optional, Union


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

    def __str__(self) -> str:
        parts = [
            f"{f.name}={_fmt(getattr(self, f.name))}"
            for f in fields(self)
            if f.name != "pos"
        ]
        return f"{type(self).__name__}({', '.join(parts)})"


# ---------------------------------------------------------------------
# Program / statements
# ---------------------------------------------------------------------

@dataclass
class Program(Node):
    body: list


@dataclass
class Pass(Node):
    pos: Optional[tuple] = None


@dataclass
class ExprStmt(Node):
    value: "Expr"


@dataclass
class Return(Node):
    value: Optional["Expr"]


@dataclass
class Yield(Node):
    value: Optional["Expr"]


@dataclass
class ElifClause(Node):
    cond: "Expr"
    body: list


@dataclass
class If(Node):
    cond: "Expr"
    body: list
    elifs: list
    orelse: Optional[list]


@dataclass
class While(Node):
    cond: "Expr"
    body: list


@dataclass
class For(Node):
    var: str
    iter: "Expr"
    body: list
    orelse: Optional[list]


@dataclass
class Continue(Node):
    pos: Optional[tuple] = None


@dataclass
class Break(Node):
    pos: Optional[tuple] = None


@dataclass
class NameTarget(Node):
    name: str


@dataclass
class AttrTarget(Node):
    base: Union[str, "ThisRef"]
    attrs: list


@dataclass
class Assign(Node):
    targets: list
    type: Optional["TypeExpr"]
    op: str
    values: list


@dataclass
class TypeHint(Node):
    name: str
    type: "TypeExpr"


@dataclass
class StaticRef(Node):
    name: str
    pos: Optional[tuple] = None


@dataclass
class TypeExpr(Node):
    parts: list


@dataclass
class WithSimple(Node):
    name: str
    type: Optional[TypeExpr]
    value: "Expr"


@dataclass
class WithBinding(Node):
    name: str
    type: Optional[TypeExpr]
    value: "Expr"


@dataclass
class WithBlock(Node):
    bindings: list


# ---------------------------------------------------------------------
# Modules / imports
# ---------------------------------------------------------------------

@dataclass
class Import(Node):
    path: list


@dataclass
class FromImport(Node):
    path: list
    names: list


@dataclass
class Using(Node):
    path: list
    alias: Optional[str] = None


# ---------------------------------------------------------------------
# Functions / coroutines
# ---------------------------------------------------------------------

@dataclass
class Param(Node):
    name: str
    type: Optional[TypeExpr]
    default: Optional["Expr"]


@dataclass
class PosOnlyMarker(Node):
    pos: Optional[tuple] = None


@dataclass
class VarPositional(Node):
    name: str


@dataclass
class VarKeyword(Node):
    name: str


@dataclass
class FnDef(Node):
    class_target: Optional[list]  # list of type names, e.g. ["Cls1", "Cls2"] for multi-dispatch
    name: str
    params: list
    ret: Optional[TypeExpr]
    body: list


@dataclass
class CoDef(Node):
    class_target: Optional[list]  # list of type names, e.g. ["Cls1", "Cls2"] for multi-dispatch
    name: str
    params: list
    intype: Optional[TypeExpr]
    ret: Optional[TypeExpr]
    body: list


# ---------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------

@dataclass
class ClassDef(Node):
    name: str
    bases: list
    body: list


@dataclass
class SlotOption(Node):
    kind: str
    value: "Union[Expr, str]"


@dataclass
class SlotDef(Node):
    name: str
    type: Optional[TypeExpr]
    default: Optional["Expr"]
    options: Optional[list]


@dataclass
class InitDef(Node):
    params: list
    body: list


# ---------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------

@dataclass
class Num(Node):
    value: str
    pos: Optional[tuple] = None


@dataclass
class Str(Node):
    value: str
    pos: Optional[tuple] = None


@dataclass
class Char(Node):
    value: str
    pos: Optional[tuple] = None


@dataclass
class Bool(Node):
    value: bool
    pos: Optional[tuple] = None


@dataclass
class Symbol(Node):
    name: str
    pos: Optional[tuple] = None


@dataclass
class Name(Node):
    id: str
    pos: Optional[tuple] = None


@dataclass
class ThisRef(Node):
    pos: Optional[tuple] = None


@dataclass
class SuperCall(Node):
    args: list


@dataclass
class Defined(Node):
    symbol: Symbol


@dataclass
class Lambda(Node):
    params: list
    body: list


@dataclass
class NewExpr(Node):
    type: TypeExpr
    args: list


@dataclass
class Array(Node):
    items: list


@dataclass
class Pair(Node):
    elements: list
    tail: Optional["Expr"]


@dataclass
class Tuple(Node):
    items: list


@dataclass
class DictEntry(Node):
    key: "Expr"
    value: "Expr"


@dataclass
class Dict(Node):
    entries: list


@dataclass
class MessageTupleExpr(Node):
    items: list
    name: str
    args: Optional[list]


@dataclass
class Kwarg(Node):
    name: str
    value: "Expr"


@dataclass
class SpreadPos(Node):
    value: "Expr"


@dataclass
class SpreadKw(Node):
    value: "Expr"


@dataclass
class UnaryOp(Node):
    op: str
    operand: "Expr"


@dataclass
class BinOp(Node):
    op: str
    left: "Expr"
    right: "Expr"


@dataclass
class SetIfUnset(Node):
    target: "Expr"
    value: "Expr"


@dataclass
class Call(Node):
    func: "Expr"
    args: list


@dataclass
class Index(Node):
    obj: "Expr"
    index: "Expr"


@dataclass
class Attr(Node):
    obj: "Expr"
    name: str


@dataclass
class Message(Node):
    obj: "Expr"
    name: str
    args: Optional[list]


@dataclass
class Scope(Node):
    obj: "Expr"
    name: str


Expr = Union[
    Num, Str, Char, Bool, Symbol, Name, ThisRef, SuperCall, Defined, Lambda,
    NewExpr, Array, Pair, Tuple, Dict, MessageTupleExpr, UnaryOp, BinOp,
    SetIfUnset, Call, Index, Attr, Message, Scope, Yield,
]
