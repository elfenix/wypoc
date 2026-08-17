"""`Import` node -> compile-time name bindings + do_import chaining.

Binding a name to an imported module's member is purely a compile-time
decision (see naming's module docstring and Scope's expression handler):
`import_bindings[local_name]` holds a ready-to-splice Python expression
string, either a dotted module reference (`"wyrm.shapes"`) for a bound
root/leaf/alias, or a qualified member reference (`"wyrm.shapes.wy_
Circle"`) for an item-list/wildcard/ambiguous-leaf pull.

A bare `import a::b::c`'s leaf is ambiguous at the syntax level (see
wyrm.gram's note on `import_stmt`/`module_path`) - resolved the same way
the interpreter's eval_import does it: try the whole path as a module
first (via wyrm_modules.resolve_module_file, cheap - no compilation), and
only if that fails, fall back to path[:-1]::path[-1] (module::symbol).
"""
from wypoc import ast_nodes as ast
from wypoc import wyrm_modules

from .errors import err
from .naming import py_ident


def _import_whole_module(modctx, stmt, graph):
    compiled = graph.get_or_compile(stmt.path)
    dotted = compiled.dotted
    modctx.add_header(f"import {dotted}")

    if stmt.items is not None:
        # Item-list imports never merge the source module's message table,
        # `static` or not - matches the interpreter's eval_import exactly
        # (its wildcard-or-items branch only merges in the wildcard case).
        modctx.imports.append((dotted, False))
        for item in stmt.items:
            if item.name not in compiled.public_names:
                err(f"module '{'::'.join(stmt.path)}' has no '{item.name}'", stmt)
            local = item.alias or item.name
            modctx.import_bindings[local] = f"{dotted}.{py_ident(item.name)}"
        return
    if stmt.wildcard:
        # A wildcard import always merges messages, `static` or not -
        # again matching eval_import's wildcard branch, which merges
        # message_table(mod.ctx) unconditionally.
        modctx.imports.append((dotted, True))
        excluded = set(stmt.except_names or [])
        for name in compiled.public_names:
            if name in excluded:
                continue
            modctx.import_bindings[name] = f"{dotted}.{py_ident(name)}"
        return
    if stmt.alias:
        modctx.imports.append((dotted, stmt.static))
        modctx.import_bindings[stmt.alias] = dotted
        return
    # Bare `import a::b::c`: binds the leaf (the module just resolved) and
    # the root (for continued `::` navigation, e.g. a later `a::other`) -
    # see ast_nodes.Import's docstring.
    modctx.imports.append((dotted, stmt.static))
    modctx.import_bindings[stmt.path[-1]] = dotted
    if stmt.path[0] != stmt.path[-1]:
        modctx.import_bindings[stmt.path[0]] = f"wyrm.{stmt.path[0]}"


def resolve_import(modctx, stmt: ast.Import, graph):
    if graph is None:
        err("import is not supported without --compile-py's multi-file "
            "driver (compile_module was called directly)", stmt)

    # Bare-leaf imports are the only ambiguous form - wildcard/item-list/
    # aliased imports always name a module outright.
    if stmt.wildcard or stmt.items is not None or stmt.alias or len(stmt.path) < 2 \
            or wyrm_modules.resolve_module_file(list(stmt.path)) is not None:
        _import_whole_module(modctx, stmt, graph)
        return

    # The whole path isn't a module - fall back to path[:-1]::path[-1]
    # (module::symbol), matching the interpreter's own except-ImportError
    # fallback in eval_import.
    parent_path = stmt.path[:-1]
    symbol = stmt.path[-1]
    compiled = graph.get_or_compile(parent_path)
    dotted = compiled.dotted
    modctx.add_header(f"import {dotted}")
    modctx.imports.append((dotted, False))
    if symbol not in compiled.public_names:
        err(f"module '{'::'.join(parent_path)}' has no '{symbol}'", stmt)
    modctx.import_bindings[stmt.alias or symbol] = f"{dotted}.{py_ident(symbol)}"
