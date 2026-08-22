"""CoDef -> `async def wy_<name>(...) -> Cursor` codegen.

Calling a compiled coroutine does *not* run its body - it builds and
returns an engine.Cursor wrapping a nested `_body(cursor)` coroutine,
matching the interpreter's lazy-start CoroutineInstance (params/`this` are
bound eagerly; the body only starts running on the first next()/send()).
`yield`/`yield from` are compiled by expressions.py (registered against
ast.Yield), using `fnctx.cursor_var` to reach the enclosing Cursor - see
that module for the exact codegen.

Shares its parameter/this/slot-name plumbing with functions.py's
_compile_body via the same two-entry-point shape (compile_co for a plain
top-level `co`, compile_co_method for a class-body/external message-
dispatched one) rather than importing functions.py's private helper
directly, since a coroutine's body compiles into a nested function, not
the outer one functions.compile_params expects to emit params onto.
"""
from wypoc import ast_nodes as ast

from .context import FnCtx
from .errors import err
from .functions import compile_params
from .naming import py_ident
from .statements import (
    compile_block, has_error_defer, needed_global_decls, needed_nonlocal_decls,
)


def _compile_co_body(modctx, py_name, params, body, *, this_var=None, slot_names=None):
    outer = FnCtx(modctx=modctx, this_var=this_var, slot_names=slot_names or set())
    leading = [this_var] if this_var is not None else []
    param_str = compile_params(outer, params)
    all_params = ", ".join(p for p in (leading + ([param_str] if param_str else [])) if p)

    inner = FnCtx(modctx=modctx, this_var=this_var, slot_names=slot_names or set(),
                   scopes=[outer.flat_scope()], cursor_var="_cursor", indent=2,
                   is_coroutine=True, has_error_defer=has_error_defer(body))
    nonlocal_decls = needed_nonlocal_decls(params, body)
    if nonlocal_decls:
        inner.emit(f"nonlocal {', '.join(nonlocal_decls)}")
    global_decls = needed_global_decls(modctx, params, body)
    if global_decls:
        inner.emit(f"global {', '.join(global_decls)}")
    if inner.has_error_defer:
        inner.emit("_wy_error_exit = False")
    compile_block(inner, body)

    lines = [f"async def {py_name}({all_params}) -> Cursor:",
              "    async def _body(_cursor):"]
    lines.extend(inner.lines)
    lines.append("    return Cursor(_body)")
    return "\n".join(lines)


def compile_co(modctx, codef: ast.CoDef) -> str:
    if codef.class_target is not None:
        err(f"co '{codef.name}': external/multi-dispatch coroutine "
            "methods must be compiled via compile_co_method, not compile_co", codef)
    return _compile_co_body(modctx, py_ident(codef.name), codef.params, codef.body)


def compile_co_method(modctx, codef: ast.CoDef, py_name: str, slot_names=None) -> str:
    """`slot_names` should only be passed (non-None) when the coroutine
    has exactly one class target - see classes.py's caller."""
    return _compile_co_body(modctx, py_name, codef.params, codef.body,
                             this_var="wy_this", slot_names=slot_names)
