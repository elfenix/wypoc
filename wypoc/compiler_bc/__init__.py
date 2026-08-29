"""The wyrm bytecode compiler: .wy source to a module image (doc/llm-bytecode.md).

Source in, module image out: `compile_module(program, name)` lowers a parsed
program to a `ModuleImage`, which serializes as any of the three
interchangeable containers - the `.wy_a` ASCII listing, the `.wyc` binary, and
`.c` arrays for linking straight into a firmware build.  `wyrm --build-bc`
is the command-line front end.

Two rules run through the whole package.  Anything outside the supported
subset raises a `CompileError` naming the construct and its source position -
never silently-wrong bytes.  And `opcodes.py` is the single source of truth
for the instruction set: the emitters, the verifier, the disassembler, and the
generated C header all read that one table.

Every image `compile_module` returns has passed `verify`, which re-reads it the
way a loader will.  Section 9 of doc/llm-bytecode.md records where the POC's
grammar or interpreter trails the language spec, and how each such lowering is
tested in the meantime.
"""

from .errors import CompileError, err
from .image import ModuleImage, assemble_wya, read_wyc
from .module import compile_module
from .verify import verify

__all__ = [
    "CompileError",
    "err",
    "ModuleImage",
    "assemble_wya",
    "compile_module",
    "read_wyc",
    "verify",
]
