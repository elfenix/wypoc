"""Decorator expansion, run once over a module's parsed tree *before*
Python codegen ever sees it - decorators run in the native tree-walking
interpreter, not in this compiler (see the package docstring's "its own
animal" note): a decorator's body is arbitrary wyrm code (dynamically
dispatched, may itself require `import static` state), so re-implementing
decorator evaluation as a static macro-expander here would mean
duplicating the interpreter's own message-dispatch machinery for no
reason - this module just drives the real thing and then strips the
`Decorated` wrapper nodes it leaves behind.

This is the one file in compiler_py allowed to import
wypoc.wyrm_eval_parse_tree - as a sub-evaluator, not as a shared runtime
(the generated Python code never touches it).

**Known v1 limitation**: a `Decorated` node only gets expanded if the
code containing it actually *runs* during the module's own top-level
execution (directly, or transitively through a call the top level makes) -
matching how `expand_decorated`'s result is cached on first-reached rather
than force-computed. A decorator nested somewhere the module's own top
level never calls is left unexpanded by _resolve below; module.py's/
classes.py's _is_macro_only check then skips compiling the specific fn/co
that still contains one, rather than aborting the whole module - see
module.py's docstring for the same treatment `$`-named macro-template
definitions get.
"""
import contextlib
import dataclasses
import io as _io

from wypoc import ast_nodes as ast


@contextlib.contextmanager
def _muted_io():
    """Redirects wyrm's stdin/stdout/stderr handles (wyrm_io._handles) to
    in-memory sinks for the duration of the pre-pass, so a decorator with
    a real side effect - `@__dump` prints the s-expression it receives,
    and any top-level `println`/`print` the module's own code runs before
    reaching a not-yet-expanded decorator - doesn't visibly execute twice
    (once here, once when the generated Python actually runs). Every I/O
    path funnels through wyrm_io._handles eventually - both the ctx-bound
    `__write` builtin and native decorators like `@__dump`, which call
    wyrm_builtins.print_ directly - so patching handles here, rather than
    rebinding names in a scope dict, catches all of it in one place."""
    from wypoc import wyrm_io

    previous = dict(wyrm_io._handles)
    sink = _io.StringIO()
    wyrm_io._handles[wyrm_io.STDIN] = _io.StringIO("")
    wyrm_io._handles[wyrm_io.STDOUT] = sink
    wyrm_io._handles[wyrm_io.STDERR] = sink
    try:
        yield sink
    finally:
        wyrm_io._handles.clear()
        wyrm_io._handles.update(previous)


def _is_pos_field(name: str) -> bool:
    return name == "pos" or name.endswith("_pos")


def _resolve(node):
    """A single child slot's value, with any Decorated node in it (or
    nested inside it) replaced by its expansion.

    A Decorated node the pre-pass's eager top-level run never reached
    (e.g. a `[Cls]` method whose decorator body never actually ran,
    because nothing at module top level called it) is left exactly as-is
    rather than raised on here - module.py's/classes.py's own
    _is_macro_only check walks the fully-substituted tree afterward and
    skips compiling any fn/co whose body still contains one, the same way
    it already skips `$`-named macro-template definitions (see that
    module's docstring). A CompileError from *here* would abort the whole
    module even though only the specific unreached method is affected."""
    if isinstance(node, ast.Decorated):
        expanded = getattr(node, "_expanded", None)
        if expanded is None:
            return node
        return _substitute(expanded)
    if isinstance(node, ast.Node):
        return _substitute(node)
    return node


def _substitute(node: ast.Node) -> ast.Node:
    """Walks `node` in place, replacing every Decorated child (at any
    depth, in a single Node field or a list field) with its expansion.
    Mutates and returns `node` itself, mirroring expand_decorated's own
    "cached on the node" style rather than building a parallel tree."""
    for f in dataclasses.fields(node):
        if _is_pos_field(f.name):
            continue
        value = getattr(node, f.name)
        if isinstance(value, ast.Node):
            setattr(node, f.name, _resolve(value))
        elif isinstance(value, list):
            setattr(node, f.name, [_resolve(item) for item in value])
    return node


def expand_all_decorators(tree: ast.Program, module_path=()) -> ast.Program:
    """Runs `tree` for real through the interpreter (muting I/O), then
    strips every Decorated node it left behind, replacing each with its
    cached expansion. A module with no Decorated nodes anywhere skips the
    interpreter run entirely - the overwhelmingly common case, and the one
    that should stay fast and side-effect-free."""
    if not any(isinstance(n, ast.Decorated) for n in tree.walk()):
        return tree

    from wypoc.wyrm_eval_parse_tree import Scope, eval_program, populate_globals

    ctx = Scope()
    populate_globals(ctx)
    with _muted_io():
        eval_program(tree, ctx)
    return _substitute(tree)
