"""Zero-dependency dispatch registries that let compiler_c's submodules add
support for new AST node types without importing each other directly or
growing a shared if/elif chain.

Each registry is a `node type -> handler` table for one dispatch point.
A submodule registers into a registry with `@REGISTRY.register(ast.SomeNode)`
on a plain function; the module that owns that dispatch point (expressions.py
for EXPR_HANDLERS, statements.py for STATEMENT_HANDLERS/LOCAL_COLLECT_
HANDLERS, module.py for TOPLEVEL_HANDLERS) looks the handler up by
`type(node)` and raises CompileError itself when nothing is registered -
this file only owns the lookup mechanism, not compiler behavior.

Not every dispatch point in the compiler goes through a registry: If/While/
Return/Break/Continue/call-splits are handled directly in statements.py's
own runner instead, because they need extra control-flow plumbing (block_
path/remaining_stmts/fallthrough) that a plain `(ctx, node)` handler
signature doesn't carry, and because that set of control-flow forms is
expected to stay small and central rather than grow the way expression and
plain-statement kinds will as --compile chases feature parity with the
interpreter. See DESIGN.md's "Dispatch" section.

This file must not import any other compiler_c submodule at runtime - only
`typing.TYPE_CHECKING` imports, used purely for handler type hints, so every
other submodule can depend on this one without risking a cycle.
"""
from typing import Callable, Dict, Generic, Optional, TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from wypoc import ast_nodes as ast

    from .context import FnContext

ResultT = TypeVar("ResultT")


class Registry(Generic[ResultT]):
    """A `node type -> handler` table for one dispatch point. `name` is only
    used to make duplicate-registration errors readable."""

    def __init__(self, name: str):
        self.name = name
        self._handlers: Dict[type, Callable[..., ResultT]] = {}

    def register(self, node_type: type):
        def decorator(fn: Callable[..., ResultT]) -> Callable[..., ResultT]:
            if node_type in self._handlers:
                raise RuntimeError(
                    f"duplicate {self.name} handler registered for {node_type.__name__}"
                )
            self._handlers[node_type] = fn
            return fn
        return decorator

    def get(self, node) -> Optional[Callable[..., ResultT]]:
        return self._handlers.get(type(node))


# Handles the statement kinds that don't need control-flow plumbing (see
# module docstring) - currently Assign and ExprStmt.
StmtHandler = Callable[["FnContext", "ast.Node"], None]
STATEMENT_HANDLERS: "Registry[None]" = Registry("statement")

# Compiles one expression node to a C expression string.
ExprHandler = Callable[["FnContext", "ast.Node"], str]
EXPR_HANDLERS: "Registry[str]" = Registry("expression")

# Pass 1 (before any code is emitted): record a statement's typed locals, if
# any, onto ctx.locals.
LocalCollectHandler = Callable[["FnContext", "ast.Node"], None]
LOCAL_COLLECT_HANDLERS: "Registry[None]" = Registry("local-collect")

# Top-level (module-body) statement dispatch: signature is intentionally
# looser (`...`) since handlers close over whatever module-assembly state
# module.py's compile_module needs to accumulate into (sections, fn_defs,
# class_defs, seen_names, ...) rather than a single shared context object.
TopLevelHandler = Callable[..., None]
TOPLEVEL_HANDLERS: "Registry[None]" = Registry("top-level")
