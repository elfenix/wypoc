"""Call compilation: resolving a callee, building its `wyrm_value[]` args
array, and the two call shapes --compile emits - a non-tail call that splits
its enclosing block into a chunk before/after (`compile_call_split`), and a
tail call that forwards its callee's result straight to our own caller via
the shared `__wyrm_forward_result` continuation (`compile_tail_call`).

`compile_call_split` takes the block's `run_stmts` function as a parameter
rather than importing statements.py directly, since statements.py is the
one that calls into this module (to compile a call it finds mid-block) -
importing it back here would be a cycle. See DESIGN.md's "Dispatch" section.
"""
from wypoc import ast_nodes as ast

from .context import FnContext
from .errors import err
from .expressions import compile_expr
from .native_blocks import is_native_block_call
from .wtypes import TYPES, ctype

FORWARDER_SRC = """\
static wyrm_exec_state __wyrm_forward_result(wyrm_state* state)
{
    wyrm_uword __n = wyrm_state_value_count(state);
    for (wyrm_uword __i = 0; __i < __n; __i++) {
        wyrm_state_push_return(state, *wyrm_state_value_n(state, __i));
    }
    return WYRM_EXEC_DONE;
}
"""


def split_call_stmt(s):
    """If `s` is a non-tail call (`f(...)`, `x = f(...)`, or `var x: t =
    f(...)`/`x := f(...)`), return (call, target_name|None); otherwise
    None. Calls nested inside bigger expressions still aren't supported
    (CompileError, from compile_expr)."""
    if isinstance(s, ast.ExprStmt) and isinstance(s.value, ast.Call) and not is_native_block_call(s.value):
        return s.value, None
    if (
        isinstance(s, ast.Assign)
        and len(s.targets) == 1
        and len(s.values) == 1
        and isinstance(s.values[0], ast.Call)
        and not is_native_block_call(s.values[0])
        and isinstance(s.targets[0], ast.NameTarget)
    ):
        return s.values[0], s.targets[0].name
    if (
        isinstance(s, ast.VarDecl)
        and s.values is not None
        and len(s.targets) == 1
        and len(s.values) == 1
        and isinstance(s.values[0], ast.Call)
        and not is_native_block_call(s.values[0])
    ):
        return s.values[0], s.targets[0].name
    return None


def resolve_callee(ctx: FnContext, call: ast.Call):
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


def build_args_array(ctx: FnContext, call: ast.Call, callee_name: str, callee) -> str:
    """Emit (if needed) a `wyrm_value[]` of the call's evaluated arguments
    and return the C expression to pass as `wyrm_state_call_continue`'s
    `args` parameter (a temp array name, or "NULL")."""
    if not call.args:
        return "NULL"
    entries = []
    for p, a in zip(callee.params, call.args):
        ctype_name = ctype(p.type, f"param '{p.name}' of '{callee_name}'")
        ctype_, tag, field_ = TYPES[ctype_name]
        entries.append(f"{{ .type = {tag}, .data.{field_} = ({ctype_})({compile_expr(ctx, a)}) }}")
    arr = ctx.new_tmp("__args")
    ctx.emit(f"wyrm_value {arr}[{len(entries)}] = {{ " + ", ".join(entries) + " };")
    return arr


def compile_call_split(ctx: FnContext, split, block_path, remaining_stmts, fallthrough, run_stmts):
    call, target_name = split
    callee_name, callee = resolve_callee(ctx, call)

    args = build_args_array(ctx, call, callee_name, callee)
    next_name = ctx.same_block_chunk_name(block_path)
    ctx.emit(
        f"wyrm_state_call_continue(state, {next_name}, "
        f"{ctx.entry_name(callee_name)}, {args}, {len(call.args)});"
    )
    ctx.emit("return WYRM_EXEC_CONTINUE;")
    ctx.end_chunk()

    ctx.begin_chunk(next_name, static=True)
    if target_name is not None:
        ret_idx = len(ctx.local_order)
        _ctype, _tag, field_ = TYPES[ctx.locals[target_name]]
        idx = ctx.local_index[target_name]
        ctx.emit(
            f"wyrm_state_value_n(state, {idx})->data.{field_} = "
            f"wyrm_state_value_n(state, {ret_idx})->data.{field_};"
        )
    ctx.emit(f"wyrm_state_pop_to_value_count(state, {len(ctx.local_order)});")
    run_stmts(ctx, remaining_stmts, block_path, fallthrough)


def compile_tail_call(ctx: FnContext, call: ast.Call):
    callee_name, callee = resolve_callee(ctx, call)
    ctx.uses_forwarder = True
    args = build_args_array(ctx, call, callee_name, callee)
    ctx.emit(
        f"wyrm_state_call_continue(state, __wyrm_forward_result, "
        f"{ctx.entry_name(callee_name)}, {args}, {len(call.args)});"
    )
    ctx.emit("return WYRM_EXEC_CONTINUE;")
