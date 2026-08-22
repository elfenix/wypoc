"""A static symbol table over a parsed wyrm module.

This is a pure AST pass: it never evaluates anything, never imports another
module, and never touches the filesystem. Give it a `Program` and it tells
you what that file *declares*, what it *references*, and where each of
those things sits in the source - which is what an editor needs for an
outline, a go-to-definition jump, or a hover card.

Why not reuse the evaluator? `wyrm_eval_parse_tree.py` builds its bindings
by running the code: a `Scope` only ever holds what execution has reached
so far, function bodies are opaque until called, and a file with a runtime
error yields a half-populated scope. An editor needs the opposite - every
declaration in the file, including ones in never-called branches, from a
document that may not even run yet. So this is a separate, simpler pass.

## What's here

- `Symbol` - one declaration, with the span to jump to (`name_pos`), the
  span it encloses (`pos`), and a rendered `detail` line for hover/outline.
  Symbols nest: a class's slots and methods are its children.
- `Reference` - one *use* of a name, tagged with how it was used (a plain
  name, a `!` message, a `::` scope access, a type annotation, ...).
- `ImportBinding` - one navigable piece of an `import` statement. A path
  segment, an aliased leaf, an `import mod::(a, b)` item: each is separately
  resolvable, so clicking `io` in `import std::io::open` goes somewhere
  different from clicking `open`. Resolution itself needs the filesystem
  and lives in `symbol_index.py`.
- `SymbolTable.resolve` / `.messages_named` - name lookup, including the
  multi-overload answer `!` dispatch needs.

## Known simplifications

Scope resolution is span containment, not a real lexical chain: a name is
resolved against the declarations of each construct whose source range
encloses the reference, innermost first, then module level. That gets
parameters, locals, and module-level definitions right, which covers what
an editor is asked for in practice; it does not model shadowing order
within a block (a name declared *later* in the same block still matches),
and it has no notion of a name being unbound at the point of use.

Attribute and `::` references resolve by name only - there's no type
inference here, so `a.x` and `b.x` both find every `x`. Callers that need
certainty should prefer `Reference.kind` over guessing.
"""
from dataclasses import dataclass, field
from typing import Optional

from wypoc import ast_nodes as ast
from wypoc.ast_nodes import Span

# Symbol kinds. Deliberately wyrm's own vocabulary rather than LSP's
# SymbolKind enum - lsp.py maps these over, so nothing here depends on a
# protocol type.
FUNCTION = "function"
COROUTINE = "coroutine"
METHOD = "method"
CLASS = "class"
SLOT = "slot"
SIGNAL = "signal"
VARIABLE = "variable"
CONSTANT = "constant"
PARAM = "param"
STATIC = "static"
IMPORT = "import"
MODULE = "module"
# An unnamed scope (a lambda, a `do:` block): it holds declarations, so the
# lookup chain needs it, but it has no name to show in an outline.
ANONYMOUS = "anonymous"

# Reference kinds.
REF_NAME = "name"
REF_MESSAGE = "message"
REF_ATTRIBUTE = "attribute"
REF_SCOPE = "scope"
REF_TYPE = "type"


@dataclass
class Symbol:
    """One declaration. `name_pos` is the identifier alone (where a jump
    lands); `pos` is the whole construct (what it encloses, and what a fold
    or a hover range covers)."""
    name: str
    kind: str
    name_pos: Span
    pos: Span
    node: ast.Node
    detail: str = ""
    container: Optional[str] = None
    # For a message overload (`fn [Canvas, Shape] draw`, or a method
    # declared in a class body): the receiver type names it dispatches on.
    # None for a plain function.
    receivers: Optional[tuple] = None
    # True when the name is only visible *inside* this symbol's own range
    # rather than to its siblings - a `for` loop variable, which the
    # evaluator binds fresh per iteration and drops when the loop ends.
    # (A function or class, by contrast, is visible to its siblings.)
    local_to_range: bool = False
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    @property
    def qualified_name(self) -> str:
        return f"{self.container}::{self.name}" if self.container else self.name


@dataclass
class Reference:
    """One use of a name."""
    name: str
    kind: str
    pos: Span
    node: ast.Node


@dataclass
class ImportBinding:
    """One navigable piece of an `import` statement.

    `import std::io::(open as fopen)` yields three bindings: the `std`
    segment, the `io` segment, and the `open` item. Each carries the module
    path it should resolve against:

      - a path segment resolves to the module named by the path up to and
        including itself (`std`, then `std::io`);
      - an item resolves to the name `open` *inside* module `std::io`;
      - a bare `import a::b::c` leaf is ambiguous - it's a module if one
        exists, otherwise a symbol exported by `a::b`. `ambiguous_leaf`
        marks it so the resolver tries both, in the same order
        `wyrm_eval_parse_tree.eval_import` does at runtime.
    """
    name: str
    pos: Span
    module_path: tuple
    node: ast.Import
    # Set when this binding names something *inside* module_path rather
    # than the module itself.
    symbol_name: Optional[str] = None
    ambiguous_leaf: bool = False
    # The name it's bound under locally, if aliased (`as fopen`).
    local_name: Optional[str] = None
    # Whether this piece actually introduces a name into the importing
    # file's scope. `import a::b::c` binds the root `a` and the leaf `c`
    # but *not* the middle `b` (see eval_import), even though every
    # segment is still separately navigable.
    binds_locally: bool = True

    @property
    def bound_as(self) -> str:
        return self.local_name or self.name


def scope_chain(node) -> Optional[list]:
    """The dotted-out name list of a `::` access - `["std", "io", "println"]`
    for `std::io::println` - or None if the chain doesn't bottom out in a
    plain name (e.g. `f()::x`, which names nothing statically)."""
    parts = [node.name]
    obj = node.obj
    while isinstance(obj, ast.Scope):
        parts.append(obj.name)
        obj = obj.obj
    if not isinstance(obj, ast.Name):
        return None
    parts.append(obj.id)
    return list(reversed(parts))


def span_contains_point(span: Span, line: int, col: int) -> bool:
    """Is (line, col) inside `span`? 1-based line, 0-based column, matching
    ast_nodes.Span. The end is exclusive, so a cursor resting just past a
    token isn't considered inside it."""
    if span is None:
        return False
    return (span[0], span[1]) <= (line, col) < (span[2], span[3])


# ---------------------------------------------------------------------
# Rendering: the one-line summaries an outline entry or hover card shows
# ---------------------------------------------------------------------

def _render_type(type_expr) -> str:
    if type_expr is None:
        return ""
    if isinstance(type_expr, ast.TypeExpr):
        return "::".join(type_expr.parts)
    if isinstance(type_expr, list):  # a union, e.g. `is int | float`
        return " | ".join(_render_type(t) for t in type_expr)
    return str(type_expr)


def _render_param(param) -> str:
    if isinstance(param, ast.VarPositional):
        return f"*{param.name}"
    if isinstance(param, ast.VarKeyword):
        return f"**{param.name}"
    text = param.name
    if param.type is not None:
        text += f": {_render_type(param.type)}"
    if param.default is not None:
        text += " = ..."
    return text


def _render_signature(node, keyword: str) -> str:
    """`fn [Canvas, Shape] draw(c, s) -> nil` - the definition line as
    written, minus the body. This is what hover leads with."""
    parts = [keyword]
    if getattr(node, "class_target", None):
        parts.append(f"[{', '.join(node.class_target)}]")
    params = ", ".join(_render_param(p) for p in node.params)
    intype = getattr(node, "intype", None)
    if intype is not None:
        params = f"<- {_render_type(intype)}" + (f", {params}" if params else "")
    text = " ".join(parts) + f" {node.name}({params})"
    if node.ret is not None:
        text += f" -> {_render_type(node.ret)}"
    return text


def _render_slot(node: ast.SlotDef) -> str:
    text = f"slot {node.name}"
    if node.type is not None:
        text += f": {_render_type(node.type)}"
    if node.default is not None:
        text += " = ..."
    return text


def _render_signal(node: ast.SignalDef) -> str:
    return f"signal {node.name}({', '.join(_render_param(p) for p in node.params)})"


def _render_import(node: ast.Import) -> str:
    text = "import " + "::".join(node.path)
    if node.wildcard:
        text += "::*"
        if node.except_names:
            text += " except " + ", ".join(node.except_names)
    elif node.items:
        items = ", ".join(
            f"{i.name} as {i.alias}" if i.alias else i.name for i in node.items
        )
        text += f"::({items})"
    elif node.alias:
        text += f" as {node.alias}"
    return text


# ---------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------

class _Builder:
    def __init__(self):
        self.roots: list = []
        self.references: list = []
        self.imports: list = []

    # -- symbols ---------------------------------------------------------
    def add(self, into, symbol: Symbol) -> Symbol:
        (self.roots if into is None else into.children).append(symbol)
        return symbol

    def visit_body(self, body, parent: Optional[Symbol], container: Optional[str]):
        for stmt in body or []:
            self.visit_stmt(stmt, parent, container)

    def visit_stmt(self, node, parent: Optional[Symbol], container: Optional[str]):
        if isinstance(node, (ast.FnDef, ast.CoDef)):
            self.visit_callable(node, parent, container)
        elif isinstance(node, ast.ClassDef):
            self.visit_class(node, parent, container)
        elif isinstance(node, ast.SlotDef):
            self.add(parent, Symbol(
                node.name, SLOT, node.name_pos, node.pos, node,
                detail=_render_slot(node), container=container))
            self.visit_expr(node.default, parent, container)
            self.reference_type(node.type)
        elif isinstance(node, ast.SignalDef):
            self.add(parent, Symbol(
                node.name, SIGNAL, node.name_pos, node.pos, node,
                detail=_render_signal(node), container=container))
            for p in node.params:
                self.reference_type(p.type)
        elif isinstance(node, ast.VarDecl):
            for target in node.targets:
                detail = f"var {target.name}"
                if target.type is not None:
                    detail += f": {_render_type(target.type)}"
                self.add(parent, Symbol(
                    target.name, VARIABLE, target.name_pos, target.pos, target,
                    detail=detail, container=container))
                self.reference_type(target.type)
            for value in node.values or []:
                self.visit_expr(value, parent, container)
        elif isinstance(node, ast.StaticDecl):
            detail = f"static {node.name}"
            if node.type is not None:
                detail += f": {_render_type(node.type)}"
            self.add(parent, Symbol(
                node.name, STATIC, node.name_pos, node.pos, node,
                detail=detail, container=container))
            self.reference_type(node.type)
            self.visit_expr(node.default, parent, container)
        elif isinstance(node, (ast.WithSimple, ast.WithBinding)):
            detail = f"with {node.name}"
            if node.type is not None:
                detail += f": {_render_type(node.type)}"
            self.add(parent, Symbol(
                node.name, CONSTANT, node.name_pos, node.pos, node,
                detail=detail, container=container))
            self.reference_type(node.type)
            self.visit_expr(node.value, parent, container)
        elif isinstance(node, ast.WithBlock):
            self.visit_body(node.bindings, parent, container)
        elif isinstance(node, ast.Import):
            self.visit_import(node, parent, container)
        elif isinstance(node, ast.FromImport):
            self.visit_from_import(node, parent, container)
        elif isinstance(node, ast.For):
            # The loop variable is declared fresh per iteration (see the
            # evaluator's Scope handling), so it belongs to the loop, not
            # to the enclosing body.
            loop = self.add(parent, Symbol(
                node.var, VARIABLE, node.var_pos, node.pos, node,
                detail=f"for {node.var}", container=container,
                local_to_range=True))
            self.visit_expr(node.iter, parent, container)
            self.visit_body(node.body, loop, container)
            self.visit_body(node.orelse, loop, container)
        else:
            # Any other statement declares nothing itself, but its
            # sub-expressions and nested blocks still hold references and
            # (via `do:`/lambdas) declarations.
            self.visit_generic(node, parent, container)

    def visit_callable(self, node, parent, container):
        is_method = bool(node.class_target) or container is not None
        keyword = "co" if isinstance(node, ast.CoDef) else "fn"
        kind = COROUTINE if isinstance(node, ast.CoDef) else (METHOD if is_method else FUNCTION)
        receivers = tuple(node.class_target) if node.class_target else (
            (container,) if container else None)
        symbol = self.add(parent, Symbol(
            node.name, kind, node.name_pos, node.pos, node,
            detail=_render_signature(node, keyword), container=container,
            receivers=receivers))
        for param in node.params:
            self.add(symbol, Symbol(
                param.name, PARAM, param.name_pos, param.pos, param,
                detail=_render_param(param), container=node.name))
            if isinstance(param, ast.Param):
                self.reference_type(param.type)
                self.visit_expr(param.default, symbol, container)
        self.reference_type(node.ret)
        self.reference_type(getattr(node, "intype", None))
        self.reference_class_target(node)
        self.visit_body(node.body, symbol, container)

    def visit_class(self, node: ast.ClassDef, parent, container):
        symbol = self.add(parent, Symbol(
            node.name, CLASS, node.name_pos, node.pos, node,
            detail=f"class {node.name}", container=container))
        for base in node.bases:
            self.visit_expr(base, parent, container)
        self.visit_body(node.body, symbol, node.name)

    def visit_import(self, node: ast.Import, parent, container):
        detail = _render_import(node)
        leaf_span = node.path_pos[-1] if node.path_pos else node.pos
        if node.items:
            # `import mod::(a, b as c)` introduces one name per item, so
            # that's what the outline and name lookup should see.
            for item in node.items:
                self.add(parent, Symbol(
                    item.alias or item.name, IMPORT, item.name_pos, node.pos, node,
                    detail=detail, container=container))
        elif not node.wildcard:
            self.add(parent, Symbol(
                node.alias or node.path[-1], IMPORT, leaf_span, node.pos, node,
                detail=detail, container=container))
        segments = tuple(node.path)
        spans = node.path_pos or [None] * len(segments)
        # Every path segment is separately navigable: `std` in
        # `import std::io` goes to std/__init__.wy, `io` to std/io.wy.
        for index, (name, span) in enumerate(zip(segments, spans)):
            is_leaf = index == len(segments) - 1
            self.imports.append(ImportBinding(
                name=name, pos=span, module_path=segments[:index + 1], node=node,
                # Only a bare `import a::b::c` has an ambiguous leaf; the
                # wildcard and item forms name a module for certain, since
                # they select *out of* it.
                ambiguous_leaf=(is_leaf and len(segments) > 1
                                and not node.wildcard and node.items is None),
                local_name=(node.alias if is_leaf else None),
                binds_locally=(index == 0 or (is_leaf and not node.wildcard
                                              and node.items is None)),
            ))
        for item in node.items or []:
            self.imports.append(ImportBinding(
                name=item.name, pos=item.name_pos, module_path=segments, node=node,
                symbol_name=item.name, local_name=item.alias or item.name))

    def visit_from_import(self, node: ast.FromImport, parent, container):
        segments = tuple(node.path)
        spans = node.path_pos or [None] * len(segments)
        for index, (name, span) in enumerate(zip(segments, spans)):
            self.imports.append(ImportBinding(
                name=name, pos=span, module_path=segments[:index + 1], node=node))
        for name, span in zip(node.names, node.names_pos or [None] * len(node.names)):
            self.add(parent, Symbol(
                name, IMPORT, span, node.pos, node,
                detail=f"from {'::'.join(node.path)} import {name}", container=container))
            self.imports.append(ImportBinding(
                name=name, pos=span, module_path=segments, node=node,
                symbol_name=name, local_name=name))

    # -- expressions -----------------------------------------------------
    def visit_expr(self, node, parent, container):
        if node is None:
            return
        self.visit_generic(node, parent, container)

    def visit_generic(self, node, parent, container):
        """Record references, and recurse. `Lambda`/`Do` bodies can declare
        names, so they're routed back through visit_stmt rather than being
        treated as opaque expressions."""
        if not isinstance(node, ast.Node):
            return
        if isinstance(node, ast.Name):
            self.references.append(Reference(node.id, REF_NAME, node.pos, node))
        elif isinstance(node, (ast.Message, ast.MessageTupleExpr)):
            self.references.append(Reference(node.name, REF_MESSAGE, node.name_pos, node))
        elif isinstance(node, ast.Attr):
            self.references.append(Reference(node.name, REF_ATTRIBUTE, node.name_pos, node))
        elif isinstance(node, ast.Scope):
            self.references.append(Reference(node.name, REF_SCOPE, node.name_pos, node))
        elif isinstance(node, ast.Decorator):
            # A decorator's name is a message selector - it resolves against
            # `fn [TreeBase] name(...)` - so `@traced` navigates and
            # completes as the message it is, not as a variable. Falls
            # through to the child walk below so its arguments are visited.
            self.references.append(Reference(node.name, REF_MESSAGE, node.name_pos, node))
        elif isinstance(node, ast.TypeExpr):
            self.reference_type(node)
            return
        elif isinstance(node, (ast.Lambda, ast.Do)):
            # An anonymous scope: it holds its own params/locals for
            # lookup, but has no name worth showing in an outline (lsp.py
            # filters these out by kind).
            anon = self.add(parent, Symbol(
                "fn" if isinstance(node, ast.Lambda) else "do", ANONYMOUS,
                node.pos, node.pos, node, detail="", container=container))
            for param in getattr(node, "params", []):
                self.add(anon, Symbol(
                    param.name, PARAM, param.name_pos, param.pos, param,
                    detail=_render_param(param), container=container))
            self.visit_body(node.body, anon, container)
            return
        elif isinstance(node, (ast.FnDef, ast.CoDef, ast.ClassDef, ast.VarDecl,
                               ast.StaticDecl, ast.For, ast.Import, ast.FromImport,
                               ast.WithSimple, ast.WithBinding, ast.WithBlock,
                               ast.SlotDef, ast.SignalDef)):
            # A declaration reached through a nested block (an `if` body,
            # a `do:` expression) - hand it back to the statement path so
            # it's recorded as a declaration rather than walked over.
            self.visit_stmt(node, parent, container)
            return
        for child in node.children():
            self.visit_generic(child, parent, container)

    def reference_type(self, type_expr):
        """Type annotations mention real, navigable names (`Canvas`,
        `mod::Type`), so each segment is a reference in its own right."""
        if type_expr is None:
            return
        if isinstance(type_expr, list):
            for item in type_expr:
                self.reference_type(item)
            return
        if not isinstance(type_expr, ast.TypeExpr):
            return
        spans = type_expr.parts_pos or [None] * len(type_expr.parts)
        for name, span in zip(type_expr.parts, spans):
            self.references.append(Reference(name, REF_TYPE, span, type_expr))

    def reference_class_target(self, node):
        for name, span in zip(node.class_target or [],
                              node.class_target_pos or []):
            self.references.append(Reference(name, REF_TYPE, span, node))


@dataclass
class SymbolTable:
    """Everything one parsed module declares and references."""
    symbols: list                      # top-level Symbols, each with .children
    references: list
    imports: list
    tree: ast.Program
    path: Optional[str] = None

    # -- lookup ----------------------------------------------------------
    def all_symbols(self):
        for symbol in self.symbols:
            yield from symbol.walk()

    def module_level(self):
        """Declarations at the top level of the file - what another module
        sees when it imports this one."""
        return [s for s in self.symbols if s.kind not in (PARAM, ANONYMOUS)]

    def messages_named(self, name: str) -> list:
        """Every overload of a `!` message. `fn [Canvas] draw` and a
        `Canvas` class body's `fn draw` are both overloads of the same
        generic function, so this is deliberately a list - go-to-definition
        on `shape!draw()` should offer all of them (see the message
        dispatch notes in wypoc/README.md)."""
        return [s for s in self.all_symbols()
                if s.name == name and s.receivers is not None]

    def enclosing(self, line: int, col: int) -> list:
        """The chain of symbols whose source range covers a point, outermost
        first - a class, then its method, then that method's `do:` block."""
        chain = []
        candidates = self.symbols
        while True:
            for symbol in candidates:
                if span_contains_point(symbol.pos, line, col):
                    chain.append(symbol)
                    candidates = symbol.children
                    break
            else:
                return chain

    def resolve(self, name: str, line: int = None, col: int = None) -> list:
        """Definitions a name could refer to at a point, best first.

        Innermost enclosing scope wins (a parameter shadows a module-level
        function of the same name), then outward, then module level. See
        this module's "Known simplifications" on what this approximates."""
        def visible(symbol) -> bool:
            if symbol.name != name or symbol.kind == ANONYMOUS:
                return False
            # A loop variable is only in scope within its own loop.
            return not symbol.local_to_range or (
                line is not None and span_contains_point(symbol.pos, line, col))

        found = []
        if line is not None:
            for symbol in reversed(self.enclosing(line, col)):
                if visible(symbol) and symbol.kind != PARAM:
                    found.append(symbol)
                found.extend(c for c in symbol.children if visible(c))
        found.extend(s for s in self.symbols if visible(s))
        # Overloads of one message are all legitimate answers; anything
        # else is shadowing, so keep only the first (innermost) hit.
        deduped = []
        for symbol in found:
            if symbol not in deduped:
                deduped.append(symbol)
        return deduped

    def reference_at(self, line: int, col: int) -> Optional[Reference]:
        for ref in self.references:
            if span_contains_point(ref.pos, line, col):
                return ref
        return None

    def import_at(self, line: int, col: int) -> Optional[ImportBinding]:
        for binding in self.imports:
            if span_contains_point(binding.pos, line, col):
                return binding
        return None

    def definition_at(self, line: int, col: int) -> Optional[Symbol]:
        """The symbol whose *name* is at this point - i.e. the cursor is on
        a declaration itself rather than on a use of one."""
        for symbol in self.all_symbols():
            if span_contains_point(symbol.name_pos, line, col):
                return symbol
        return None


def build(tree: ast.Program, path: str = None) -> SymbolTable:
    """Build the symbol table for one parsed module."""
    builder = _Builder()
    builder.visit_body(tree.body, None, None)
    return SymbolTable(
        symbols=builder.roots,
        references=builder.references,
        imports=builder.imports,
        tree=tree,
        path=path,
    )
