"""Top-level `fn` compilation: one wyrm function to one C function.

The shape is the interpreter's native calling convention -

    bool w_{module}_{fn}(wyrm_lang_vm* vm, wyrm_value* args,
                         wyrm_uword argc, wyrm_value* out)

- returning `false` when it failed (with the interpreter's error already
recorded) and the result through `*out` otherwise.

The body is a prologue plus the compiled statements. The prologue does the
three things the convention leaves to the callee: check the argument count,
unbox each parameter into a C local of its declared type (rejecting a value
of the wrong tag, since arguments arrive dynamically typed), and declare
every other local up front - C wants a declaration before use, and a `var`
inside a loop must not be redeclared on each iteration.
"""
from wypoc import ast_nodes as ast

from .context import FnContext
from .errors import err
from .statements import collect_locals, run_stmts
from .wtypes import wtype


def compile_fn(fndef: ast.FnDef, functions: dict, module_ident: str) -> str:
    """Compile one `fn` to its C function text."""
    if fndef.class_target:
        err(f"fn '{fndef.name}': message fns (`fn [Cls] ...`) not supported by --compile")

    ctx = FnContext(fndef=fndef, functions=functions, module_ident=module_ident)
    ctx.ret_type = wtype(fndef.ret, f"fn '{fndef.name}' return") if fndef.ret else None

    params = []
    for p in fndef.params:
        if not isinstance(p, ast.Param):
            err(f"fn '{fndef.name}': only plain parameters are supported by --compile "
                f"(no *args/**kwargs)")
        if p.default is not None:
            err(f"fn '{fndef.name}' param '{p.name}': default values not supported by --compile")
        params.append((p.name, ctx.declare(
            p.name, p.type, f"fn '{fndef.name}' param '{p.name}'")))

    collect_locals(ctx, fndef.body)
    body_locals = [(name, wt) for name, wt in ctx.locals.items()
                   if name not in dict(params)]

    ctx.emit(f"bool {ctx.entry_name()}(wyrm_lang_vm* vm, wyrm_value* args, "
             f"wyrm_uword argc, wyrm_value* out)")
    ctx.emit("{")
    ctx.indent += 1
    _emit_prologue(ctx, fndef, params, body_locals)

    returned = run_stmts(ctx, fndef.body)
    if not returned:
        # Falling off the end answers nil, the same value the interpreter
        # gives a function with no `return`.
        ctx.emit("*out = lang_value_nil();")
        ctx.emit("return true;")
    ctx.indent -= 1
    ctx.emit("}")
    return ctx.text()


def _emit_prologue(ctx: FnContext, fndef: ast.FnDef, params, body_locals):
    if not params:
        # Nothing reads them, and C would warn about it.
        ctx.emit("(void)args;")
    ctx.emit(f"if (argc != {len(params)}) {{")
    ctx.indent += 1
    ctx.emit(f'lang_vm_runtime_error(vm, "{fndef.name}() takes {len(params)} '
             f'argument(s), not %u", (unsigned)argc);')
    ctx.emit("return false;")
    ctx.indent -= 1
    ctx.emit("}")

    for i, (name, wt) in enumerate(params):
        # Arguments arrive dynamically typed, so a compiled function has to
        # check the tag itself rather than trust the caller - this is the one
        # place a compiled module meets values it did not produce.
        ctx.emit(f"if (args[{i}].type != {wt.tag}) {{")
        ctx.indent += 1
        ctx.emit(f'lang_vm_runtime_error(vm, "{fndef.name}(): argument '
                 f'\'{name}\' must be a {wt.name}");')
        ctx.emit("return false;")
        ctx.indent -= 1
        ctx.emit("}")
        ctx.emit(f"{wt.ctype} {name} = {wt.unboxed(f'args[{i}]')};")

    for name, wt in body_locals:
        ctx.emit(f"{wt.ctype} {name} = {wt.zero};")
    if params or body_locals:
        ctx.emit()
