"""wyrm --compile: translate a Wyrm module into C, targeting the real wyrm
VM's calling convention (`wyrm_exec_fn` / `wyrm_state*`), following the
call-handling pattern demonstrated in the reference implementation's worked
example (`w_do_a_mul` / `w_multipart`).

This is a narrow v1 slice, not a general compiler: only `fn` defs with
`int`/`bool` params & locals, straight-line arithmetic, `if`/`while`, calls
(tail and non-tail, anywhere a statement may appear) to other compiled
functions in the same module, the `native::block(...)` escape hatch from
doc/language-spec.md's "Native Code" section, and bare `class` defs (typed
slots only - no bases, defaults, slot options, or methods, `init` included)
compiled to a
`w_{module}_{class}` builder returning a `wyrm_class*`. Anything else raises
CompileError with a specific message - the same "fail loud, not silently
wrong" convention wyrm_eval_parse_tree.py uses for its own known gaps (see
wypoc/README.md's "Known gaps").

This package is organized by compiler concern, not by AST shape - see
DESIGN.md (in this directory) for what each submodule owns and how they're
allowed to depend on each other.
"""
from .errors import CompileError
from .module import compile_module

__all__ = ["CompileError", "compile_module"]
