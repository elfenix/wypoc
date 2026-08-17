"""wyrm --compile-py: translate a wyrm module (and everything it
transitively imports) into a tree of async Python source files under a
`wyrm` namespace package, plus a sibling entry script.

A narrow v1 slice, grown in stages (see the implementation plan): this
package currently supports plain and external `fn`/`co [Cls, ...]` defs
(async; a `co` returns a Cursor rather than running its body immediately -
see engine.Cursor), `class` (single inheritance, slots, class-body
methods/coroutines, `init`), message dispatch (`!`), `import` (transitive,
multi-file output), `yield`/`yield from`/`next`/`send`/`.value`, `catch`,
decorators (expanded ahead of codegen - see decorators_pass.py), and the
non-control-flow-heavy statement set (Assign/VarDecl/If/While/Return/
ExprStmt). The remaining control-flow forms (For/Try/Defer/With) land in a
later stage. Anything not yet supported raises CompileError - the same
"fail loud, not silently wrong" convention compiler_c uses.

Deliberately independent of wyrm_eval_parse_tree.py's runtime - the
generated Python runtime (engine.py's Machine/Context/Cursor/dispatch) is
its own animal, not meant to interoperate with the tree-walking
interpreter. The one exception is decorators_pass.py, which reuses the
interpreter as a sub-evaluator to expand decorators ahead of codegen -
see its own module docstring, including the current limitation that a
decorator's *own* library module (reached via `import static`, itself
containing no Decorated nodes) still goes through ordinary codegen like
any other import, so a wyrm-written decorator library needs to stay
within this compiler's supported expression/statement subset even though
its content never survives into the compiled output. Only the native
`@__dump`/`@__identity` decorators (no import required) are exercised by
this stage's own tests; a wyrm-written decorator library is follow-up work.
"""
import os

from .errors import CompileError
from .graph import CompileGraph
from .decorators_pass import expand_all_decorators
from .module import compile_entry_module, compile_module, compile_module_with_meta

__all__ = [
    "CompileError", "compile_module", "compile_entry_module", "compile_tree",
]


def compile_tree(entry_script_path: str, output_dir: str) -> None:
    """The `--compile-py <output_dir> script.wy` entry point: compiles
    `entry_script_path` and everything it transitively imports, writing
    the whole tree under `output_dir` (see project_out.write_tree)."""
    from wypoc.parse import parse

    from .project_out import write_tree

    entry_name = os.path.splitext(os.path.basename(entry_script_path))[0]
    with open(entry_script_path, encoding="utf-8") as f:
        entry_src = f.read()
    entry_tree = parse(entry_src, filename=entry_script_path)
    entry_tree = expand_all_decorators(entry_tree, ())

    graph = CompileGraph()
    entry_source = compile_entry_module(entry_tree, entry_name, graph=graph)
    write_tree(graph, entry_source, entry_name, output_dir)
