"""Dispatch registries, keyed by `wypoc.ast_nodes` class.

Three registries, mirroring the three positions a node can appear in: an
expression (produces a value in a register), a statement (produces effects),
and a top-level item (contributes to module init and the module's tables).
The other half of the two-pass shape - working out which names a body
declares, before any code is emitted - is a tree walk rather than a registry;
it lives in `analysis.py`.

A node class with no entry is not a gap to paper over: `dispatch` raises a
`CompileError` naming the construct and its source position, which is the
whole fail-loud contract of this package.  Milestones add handlers; they
never add silent fallbacks.
"""

from .errors import CompileError

EXPR_HANDLERS = {}
STATEMENT_HANDLERS = {}
TOPLEVEL_HANDLERS = {}

# Source-level spellings for the constructs whose class name would not tell a
# reader which line of their program is at fault.
CONSTRUCT_NAMES = {
    "Assign": "assignment",
    "AttrTarget": "attribute assignment",
    "Catch": "`catch`",
    "Char": "a character literal",
    "ClassDef": "`class`",
    "CoDef": "`co`",
    "Decorated": "a decorator",
    "Defer": "`defer`",
    "Dict": "a dict literal",
    "Emit": "`emit`",
    "EllipsisExpr": "`...`",
    "For": "`for`",
    "FromImport": "`from ... import`",
    "If": "`if`",
    "Import": "`import`",
    "IndexTarget": "index assignment",
    "Lambda": "a lambda",
    "Message": "message send (`!`)",
    "Num": "a number literal",
    "SetIfUnset": "`?=`",
    "SignalDef": "`signal`",
    "StaticDecl": "`static`",
    "ThreadSpawn": "`thread`",
    "TaskSpawn": "`task`",
    "Try": "`try`",
    "VarDecl": "`var`",
    "While": "`while`",
    "WithBlock": "`with`",
    "WithBinding": "`with`",
    "WithSimple": "`with`",
    "Yield": "`yield`",
}


# Constructs the POC still parses and runs but that the language has since
# dropped.  These are not gaps the compiler will close later, so they get a
# message that does not say "yet" - lowering them would be compiling to a
# language that no longer exists.
REMOVED_CONSTRUCTS = {"WithSimple", "WithBinding", "WithBlock"}


def construct_name(node) -> str:
    kind = type(node).__name__
    return CONSTRUCT_NAMES.get(kind, kind)


def unsupported(node) -> CompileError:
    """The refusal for a construct with no handler, worded for its reason."""
    kind = type(node).__name__
    name = construct_name(node)
    if kind in REMOVED_CONSTRUCTS:
        message = (
            f"{name} has been removed from the language; the bytecode compiler "
            "does not lower it"
        )
    elif kind == "EllipsisExpr":
        # `...` is a POC grammar addition, not a language-spec value: decorator
        # templates use it to mark where the decorated body is substituted.
        # One reaching the compiler means a template was never expanded.
        message = (
            "`...` is a decorator-template placeholder, not a value; one left "
            "here means the template was never expanded"
        )
    else:
        message = f"the bytecode compiler does not support {name} yet"
    return CompileError(message, getattr(node, "pos", None))


def _register(registry, node_types):
    def decorate(function):
        for node_type in node_types:
            if node_type in registry:
                raise RuntimeError(f"{node_type.__name__} is registered twice")
            registry[node_type] = function
        return function

    return decorate


def expression(*node_types):
    return _register(EXPR_HANDLERS, node_types)


def statement(*node_types):
    return _register(STATEMENT_HANDLERS, node_types)


def toplevel(*node_types):
    return _register(TOPLEVEL_HANDLERS, node_types)


def dispatch(registry, node, *args, **kwargs):
    handler = registry.get(type(node))
    if handler is None:
        raise unsupported(node)
    return handler(node, *args, **kwargs)


def supported(registry, node) -> bool:
    return type(node) in registry
