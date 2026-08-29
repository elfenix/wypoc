"""Errors raised while compiling wyrm source to a bytecode module image.

One rule, applied everywhere in this package: anything the compiler cannot
represent correctly is a `CompileError` naming the construct and where it
came from - never silently-wrong bytes.  A bad image is far more expensive
to debug than a refused compile.
"""


class CompileError(Exception):
    """A construct the bytecode compiler cannot (yet) emit.

    `pos` is the source span the offending construct carries on its AST node
    (`ast_nodes` spans are `(line, col, end_line, end_col)` tuples); it is
    None for problems found while assembling an image by hand, where there is
    no source to point at.
    """

    def __init__(self, message: str, pos=None):
        self.message = message
        self.pos = pos
        super().__init__(self._render())

    def _render(self) -> str:
        if not self.pos:
            return self.message
        line, col = self.pos[0], self.pos[1]
        return f"{self.message} (line {line}, column {col})"


def err(message: str, pos=None):
    """Raise a CompileError; `raise err(...)` reads as a statement at the call site."""
    raise CompileError(message, pos)
