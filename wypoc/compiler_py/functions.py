"""FnDef -> `async def wy_<name>(...)` codegen. Every wyrm function compiles
to an async def, per the user's explicit requirement that *all* wyrm
functions - fn and co alike - become async, a deliberate deviation from the
tree-walking interpreter's sync-fn/threaded-co split (see compiler_py's
package docstring).

Two entry points share one body compiler:
- compile_fn: a plain top-level `fn` -> `async def wy_<name>(...)`.
- compile_method: a message overload's body (an internal class-body `fn`,
  or an external `fn [Cls, ...] name(...)`) -> a standalone function named
  by naming.message_fn_name, taking an explicit leading `this` parameter
  and (for exactly one class target) slot-name-aware body compilation - see
  context.FnCtx.resolve_read/resolve_write_target.
"""
from wypoc import ast_nodes as ast

from .context import FnCtx
from .errors import err
from .expressions import compile_expr
from .naming import py_ident
from .statements import compile_block, has_error_defer, needed_global_decls


def compile_params(fnctx, params) -> str:
    parts = []
    for p in params:
        if isinstance(p, ast.Param):
            name = fnctx.declare(p.name)
            if p.default is not None:
                parts.append(f"{name}={compile_expr(fnctx, p.default)}")
            else:
                parts.append(name)
        elif isinstance(p, ast.VarPositional):
            name = fnctx.declare(p.name)
            parts.append(f"*{name}")
        elif isinstance(p, ast.VarKeyword):
            name = fnctx.declare(p.name)
            parts.append(f"**{name}")
        else:
            err("unsupported parameter form", p)
    return ", ".join(parts)


def _compile_body(modctx, py_name, params, body, *, this_var=None, slot_names=None):
    fnctx = FnCtx(modctx=modctx, this_var=this_var, slot_names=slot_names or set(),
                  has_error_defer=has_error_defer(body))
    leading = [this_var] if this_var is not None else []
    param_str = compile_params(fnctx, params)
    all_params = ", ".join(p for p in (leading + ([param_str] if param_str else [])) if p)
    header = f"async def {py_name}({all_params}):"
    global_decls = needed_global_decls(modctx, params, body)
    if global_decls:
        fnctx.emit(f"global {', '.join(global_decls)}")
    if fnctx.has_error_defer:
        fnctx.emit("_wy_error_exit = False")
    compile_block(fnctx, body)
    return "\n".join([header] + fnctx.lines)


def compile_fn(modctx, fndef: ast.FnDef) -> str:
    if fndef.class_target is not None:
        err(f"fn '{fndef.name}': external/multi-dispatch methods must be "
            "compiled via compile_method, not compile_fn", fndef)
    return _compile_body(modctx, py_ident(fndef.name), fndef.params, fndef.body)


def compile_method(modctx, fndef: ast.FnDef, py_name: str, slot_names=None) -> str:
    """`slot_names` should only be passed (non-None) when the method has
    exactly one class target - see classes.py's caller."""
    return _compile_body(modctx, py_name, fndef.params, fndef.body,
                          this_var="wy_this", slot_names=slot_names)
