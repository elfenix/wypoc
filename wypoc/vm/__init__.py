"""The wyrm bytecode virtual machine (doc/wyc-format.md).

Loads a `.wyc` image and runs it, reusing the tree walker's runtime rather
than reimplementing wyrm's semantics: calls, message dispatch, the object
model and the builtins are all `wyrm_eval_parse_tree`'s, so compiled and
interpreted modules produce and consume the same values and can call each
other freely.

Built in milestones (gen/bytecode-vm-plan.md), and now complete: every
construct the compiler emits runs, from arithmetic and control flow through
classes, message dispatch, coroutines, imports in both directions - a compiled
module can import an interpreted one and the reverse - and the spread call
forms. What it refuses, it refuses by name (see interp.py).

Conformance is output equivalence: test/test_vm_samples.py runs the whole
sample corpus under both this VM and the tree walker and compares what they
print, and test/test_vm_run.py does the same for every bytecode fixture. The
handful of places the two cannot agree are named individually rather than
waved past.
"""

from .errors import ImageError, LinkError, TrapError, VMError
from .frame import Frame, for_function, for_init
from .image import LoadedImage, load, load_file
from .interp import backfill, call_function, enter, execute, invoke
from .module import LoadedModule
from .run import load_module, run_file, run_image
from .values import BytecodeFunction

__all__ = [
    "BytecodeFunction",
    "Frame",
    "ImageError",
    "LinkError",
    "LoadedImage",
    "LoadedModule",
    "TrapError",
    "VMError",
    "backfill",
    "call_function",
    "enter",
    "execute",
    "for_function",
    "for_init",
    "invoke",
    "load",
    "load_file",
    "load_module",
    "run_file",
    "run_image",
]
