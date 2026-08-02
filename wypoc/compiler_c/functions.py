"""Top-level `fn` compilation: pass 1 collects typed locals (statements.
collect_locals), pass 2 emits the entry chunk and recursively compiles the
body via statements.run_stmts. See DESIGN.md's "Chunk model" section."""
from wypoc import ast_nodes as ast

from .context import FnContext
from .errors import err
from .statements import collect_locals, run_stmts
from .wtypes import TYPES, ctype


def compile_fn(fndef: ast.FnDef, functions: dict, module_ident: str):
    """Compile a single `fn` into one non-static entry `wyrm_exec_fn`
    (`w_{module}_{fn}`) plus a `static` chunk per basic block - see
    DESIGN.md's "Chunk model" section for the chunk-naming and jump/call
    scheme. Returns (chunk_texts, chunk_names, uses_forwarder)."""
    if fndef.class_target:
        err(f"fn '{fndef.name}': class-target/message fns not supported by --compile")

    ctx = FnContext(fndef=fndef, functions=functions, module_ident=module_ident)
    ctx.ret_type_name = ctype(fndef.ret, f"fn '{fndef.name}' return") if fndef.ret else None

    for p in fndef.params:
        if not isinstance(p, ast.Param):
            err(f"fn '{fndef.name}': only plain parameters are supported by --compile (no /, *args, **kwargs)")
        if p.default is not None:
            err(f"fn '{fndef.name}' param '{p.name}': default values not supported by --compile")
        ctx.locals[p.name] = ctype(p.type, f"fn '{fndef.name}' param '{p.name}'")

    collect_locals(ctx, fndef.body)
    # Fixed, function-wide slot order every chunk addresses by index for
    # this fn's whole lifetime.
    ctx.local_order = list(ctx.locals.keys())
    ctx.local_index = {name: i for i, name in enumerate(ctx.local_order)}

    ctx.begin_chunk(ctx.entry_name(), static=False)
    # Params arrive as the incoming call's args (already at slots
    # 0..len(params)-1); every other local gets a fresh zero-valued slot.
    for name in ctx.local_order[len(fndef.params):]:
        ctype_, _tag, _field = TYPES[ctx.locals[name]]
        zero = "false" if ctype_ == "bool" else "0"
        ctx.emit(f"wyrm_state_push(state, wyrm_value_word((wyrm_word)({zero})));")

    run_stmts(ctx, fndef.body, (), ctx.emit_done)
    return ctx.chunk_texts, ctx.chunk_names, ctx.uses_forwarder
