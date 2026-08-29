"""Tracing helpers: what an instruction is, in the words a listing uses.

A thin wrapper over `compiler_bc.opcodes` rather than a second disassembler -
the compiler's table is the single source of truth for the instruction set,
and a VM that renders instructions its own way would drift from the listings
its images ship with.
"""

from wypoc.compiler_bc import opcodes


def one(module, offset) -> str:
    """The instruction at `offset`, rendered as a listing line."""
    text, _words = opcodes.disassemble_one(module.image.code, offset, _Pools(module))
    line = module.image.source_line(offset)
    # The debug section's own file name when it has one; a stripped image
    # falls back to the module name (spec 8.9).
    origin = module.image.source_file or module.name
    where = f"{origin}:{line}" if line is not None else origin
    return f"{offset:5}  {text:<44} ; {where}"


def body(module, function) -> list:
    """Every instruction of one function, as listing lines."""
    code = module.image.code
    end = _end_of(module, function)
    out, offset = [], function.code_offset
    while offset < end:
        text, words = opcodes.disassemble_one(code, offset, _Pools(module))
        out.append(f"{offset:5}  {text}")
        offset += words
    return out


def _end_of(module, function):
    later = [
        other.code_offset
        for other in module.image.functions
        if other.code_offset > function.code_offset
    ]
    return min(later) if later else len(module.image.code)


class _Pools:
    """The `describe` hook `opcodes.disassemble` uses for its pool hints."""

    def __init__(self, module):
        self.module = module

    def describe(self, kind, index):
        image = self.module.image
        try:
            if kind == "static":
                return repr(image.statics[index])
            if kind == "symbol":
                return f'"{image.symbols[index]}"'
            if kind == "message":
                return image.messages[index].spell(image.symbols)
            if kind == "function":
                return image.functions[index].name
            if kind == "class":
                return image.classes[index].name
        except IndexError:
            return None
        return None
