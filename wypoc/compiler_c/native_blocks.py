"""native::block(...) escape hatch from doc/language-spec.md's "Native Code"
section: splices raw C into a module's HEADER/TYPES/CONSTANTS/PROTOS/
FUNCTIONS sections at the top level, or inline into a function body - where
it reads and writes the enclosing function's locals by name - as a
statement."""
from wypoc import ast_nodes as ast
from wypoc.wyrm_eval_parse_tree import eval_string_literal

from .context import FnContext
from .errors import err

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
        err("native::block()'s input/output arguments must be pair-list literals, e.g. $['a, 'b]", call)
    for sym in inputs.elements + outputs.elements:
        if not isinstance(sym, ast.Symbol):
            err("native::block()'s input/output lists must contain only symbols", call)
    if not isinstance(code, ast.Str):
        err("native::block()'s 4th argument must be a (raw) string literal", call)
    body = eval_string_literal(code.value)
    return portion.name, [s.name for s in inputs.elements], [s.name for s in outputs.elements], body


def compile_native_block(ctx: FnContext, call: ast.Call):
    """Splice a `native::block(...)` call into the function body.

    The spliced C reads and writes the named locals by their bare names,
    which is what the spec's worked example does. Under this calling
    convention a wyrm local *is* a C local of the same name, so that needs no
    copy-in/copy-out marshalling at all - the declared input/output lists are
    checked (naming an unknown local is an error rather than C the compiler
    would reject far from its cause) and then serve as documentation of what
    the block touches. The block still gets a scope of its own, so a
    temporary it declares can't collide with anything around it."""
    _portion, inputs, outputs, body = parse_native_block_args(call)
    for name in inputs + outputs:
        if name not in ctx.locals:
            err(f"native::block() references unknown local/param '{name}'", call)

    if inputs or outputs:
        ctx.emit(f"/* native: reads {_names(inputs)}, writes {_names(outputs)} */")
    ctx.emit("{")
    ctx.indent += 1
    ctx.emit_block(body)
    ctx.indent -= 1
    ctx.emit("}")


def _names(names) -> str:
    return ", ".join(names) if names else "nothing"
