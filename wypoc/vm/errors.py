"""Errors the bytecode VM raises.

Three kinds, deliberately distinguished, because they mean very different
things about who is at fault:

* `ImageError` - the image is malformed. The producer is at fault, or the
  bytes are damaged. Raised only at load.
* `LinkError` - the image is well formed but says something that cannot be
  satisfied here: a dependency that will not load, a section referring past
  the end of a table.
* `TrapError` - the *program* did something wrong at run time, or reached a
  `trap` instruction. Carries the opcode and code offset so a line can be
  recovered from the debug table.

Everything the VM refuses is one of these. A bare Python exception escaping
the interpreter loop is a VM bug, and tests treat it as one.
"""


class VMError(Exception):
    """Base for everything this package raises deliberately."""


class ImageError(VMError):
    """A malformed image: bad magic, bad version, a broken directory, a
    section that is not what its id says it is."""


class LinkError(VMError):
    """A well-formed image that cannot be linked here."""


class TrapError(VMError):
    """The running program halted: a `trap` instruction, or an operation the
    VM cannot perform.

    `offset` is the code word offset of the instruction, which the `debug`
    section's line table turns back into a source line.
    """

    def __init__(self, message, offset=None, opcode=None, module=None, source=None):
        self.offset = offset
        self.opcode = opcode
        self.module = module
        self.source = source  # "file.wy:12", from the debug line table
        super().__init__(self._render(message))

    def _render(self, message):
        where = []
        if self.module:
            where.append(self.module)
        if self.source:
            where.append(self.source)
        if self.offset is not None:
            where.append(f"word {self.offset}")
        return f"{message} ({', '.join(where)})" if where else message
