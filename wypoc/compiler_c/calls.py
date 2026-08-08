"""Call compilation: resolving a callee, boxing its arguments, and emitting
the call.

There is one call shape. A compiled function is an ordinary C function
following the interpreter's native calling convention -

    bool w_mod_f(wyrm_lang_vm* vm, wyrm_value* args, wyrm_uword argc,
                 wyrm_value* out);

- so calling one is an ordinary C call, and a call anywhere in an expression
is a hoisted temporary (see expressions.py). The previous backend targeted
the raw VM's resumable `wyrm_exec_fn` convention, where a call had to split
its enclosing block in two and a tail call needed a shared forwarding
continuation; none of that survives the move, and neither does the
restriction it forced - that a call could only appear as a whole statement or
a bare `return`.

Failure propagates the way the convention says: `false` out, with the
interpreter's own error already recorded, so a compiled caller just returns
`false` too.
"""
from wypoc import ast_nodes as ast

from .context import FnContext
from .errors import err
from .expressions import Value, compile_expr_as
from .handlers import EXPR_HANDLERS
from .native_blocks import is_native_block_call
from .wtypes import wtype


def resolve_callee(ctx: FnContext, call: ast.Call):
    """The (name, FnDef) a call names, with its argument list checked
    against the definition's."""
    if not isinstance(call.func, ast.Name):
        err("only calls to a plain function name are supported by --compile", call)
    callee_name = call.func.id
    callee = ctx.functions.get(callee_name)
    if callee is None:
        err(f"call to unknown/uncompiled function '{callee_name}'", call)
    for a in call.args:
        if isinstance(a, (ast.Kwarg, ast.SpreadPos, ast.SpreadKw)):
            err("keyword/spread arguments not supported by --compile", call)
    if len(call.args) != len(callee.params):
        err(
            f"call to '{callee_name}': expected {len(callee.params)} argument(s), "
            f"got {len(call.args)}", call,
        )
    return callee_name, callee


@EXPR_HANDLERS.register(ast.Call)
def compile_call(ctx: FnContext, call: ast.Call) -> Value:
    """Emit the call and answer the temporary holding its result.

    Arguments are boxed into a `wyrm_value[]` because that is what the
    convention passes; the result is unboxed straight back out, so the
    boxing exists only at the boundary and the surrounding arithmetic stays
    ordinary C."""
    if is_native_block_call(call):
        err("native::block() is a statement, not a value", call)
    callee_name, callee = resolve_callee(ctx, call)

    boxed = []
    for param, arg in zip(callee.params, call.args):
        param_type = wtype(param.type, f"param '{param.name}' of '{callee_name}'")
        boxed.append(param_type.boxed(
            compile_expr_as(ctx, arg, param_type, f"argument '{param.name}'")))

    if boxed:
        args_name = ctx.new_tmp("__args")
        ctx.emit(f"wyrm_value {args_name}[{len(boxed)}] = {{ " + ", ".join(boxed) + " };")
    else:
        args_name = "WYRM_NULL"

    result_type = wtype(callee.ret, f"fn '{callee_name}' return")
    tmp = ctx.new_tmp()
    ctx.emit(f"wyrm_value {tmp};")
    ctx.emit(
        f"if (!{ctx.entry_name(callee_name)}(vm, {args_name}, {len(boxed)}, &{tmp})) "
        f"{{ return false; }}"
    )
    return Value(result_type.unboxed(tmp), result_type)
