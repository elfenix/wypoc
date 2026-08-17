"""Zero-dependency dispatch registries, mirroring compiler_c/handlers.py's
Registry pattern exactly (an independent copy - compiler_py never imports
compiler_c or vice versa).

Each registry is a `node type -> handler` table for one dispatch point.
A submodule registers into a registry with `@REGISTRY.register(ast.SomeNode)`
on a plain function; the owning dispatch point looks the handler up by
`type(node)` and raises CompileError itself when nothing is registered.

Not every dispatch point goes through a registry: If/While/For/Try/Catch/
Defer/With are handled directly in statements.py's own runner instead,
because they need extra plumbing (loop depth, hoisting, synthesized
try/finally) that a plain `(fnctx, node)` handler signature doesn't carry.
"""
from typing import Callable, Dict, Generic, Optional, TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from wypoc import ast_nodes as ast

    from .context import FnCtx, ModuleCtx

ResultT = TypeVar("ResultT")


class Registry(Generic[ResultT]):
    """A `node type -> handler` table for one dispatch point. `name` is
    only used to make duplicate-registration errors readable."""

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


# Compiles one expression node to a Python expression string.
ExprHandler = Callable[["FnCtx", "ast.Node"], str]
EXPR_HANDLERS: "Registry[str]" = Registry("expression")

# Statement kinds that don't need control-flow plumbing (see module
# docstring) - emits directly via fnctx.emit(...).
StmtHandler = Callable[["FnCtx", "ast.Node"], None]
STMT_HANDLERS: "Registry[None]" = Registry("statement")

# Module-body (top-level) statement dispatch: signature is intentionally
# looser (`...`) since handlers close over whatever module-assembly state
# module.py's compile_module needs to accumulate into a ModuleCtx.
TopLevelHandler = Callable[..., None]
TOPLEVEL_HANDLERS: "Registry[None]" = Registry("top-level")
