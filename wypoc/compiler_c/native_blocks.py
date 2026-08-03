"""native::block(...) escape hatch from doc/language-spec.md's "Native Code"
section: splices raw C into a module's HEADER/TYPES/CONSTANTS/PROTOS/
FUNCTIONS sections at the top level, or inline into a function body (with
copy-in/copy-out locals) as a statement."""
from wypoc import ast_nodes as ast
from wypoc.wyrm_eval_parse_tree import eval_string_literal

from .context import FnContext
from .errors import err
from .wtypes import TYPES

NATIVE_PORTIONS = ("HEADER", "TYPES", "CONSTANTS", "PROTOS", "FUNCTIONS")


def is_native_block_call(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Scope)
        and isinstance(call.func.obj, ast.Name)
        and call.func.obj.id == "native"
        and call.func.name == "block"
    )


def parse_native_block_args(call: ast.Call):
    """Validate + destructure a native::block(portion, inputs, outputs, code)
    call per doc/language-spec.md: all four arguments must be literals."""
    args = call.args
    if len(args) != 4:
        err("native::block() requires exactly 4 arguments: portion, inputs, outputs, code", call)
    portion, inputs, outputs, code = args
    if not isinstance(portion, ast.Symbol):
        err("native::block()'s first argument must be a symbol literal, e.g. 'HEADER", call)
    if not isinstance(inputs, ast.Pair) or not isinstance(outputs, ast.Pair):
        err("native::block()'s input/output arguments must be pair-list literals, e.g. ['a, 'b]", call)
    if inputs.tail is not None or outputs.tail is not None:
        err("native::block()'s input/output lists must be proper lists (no '.' tail)", call)
    for sym in inputs.elements + outputs.elements:
        if not isinstance(sym, ast.Symbol):
            err("native::block()'s input/output lists must contain only symbols", call)
    if not isinstance(code, ast.Str):
        err("native::block()'s 4th argument must be a (raw) string literal", call)
    body = eval_string_literal(code.value)
    return portion.name, [s.name for s in inputs.elements], [s.name for s in outputs.elements], body


def compile_native_block(ctx: FnContext, call: ast.Call):
    """Splice a `native::block(...)` call into the currently-open chunk.
    Copy-in/copy-out through a private nested scope, so the spliced C can
    use the bare symbol names (matching the spec's worked example)."""
    _portion, inputs, outputs, body = parse_native_block_args(call)
    for name in inputs + outputs:
        if name not in ctx.locals:
            err(f"native::block() references unknown local/param '{name}'", call)

    ctx.emit("{")
    ctx.indent += 1
    for name in inputs:
        ctype_, _tag, field_ = TYPES[ctx.locals[name]]
        idx = ctx.local_index[name]
        ctx.emit(f"{ctype_} {name} = ({ctype_})wyrm_state_value_n(state, {idx})->data.{field_};")
    for name in outputs:
        ctype_, _tag, _field = TYPES[ctx.locals[name]]
        ctx.emit(f"{ctype_} {name};")
    for line in body.splitlines():
        ctx.emit(line)
    for name in outputs:
        ctx.emit_local_assign(name, name)
    ctx.indent -= 1
    ctx.emit("}")
