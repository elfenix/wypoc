"""CompileError and the shared "fail loud, not silently wrong" raise helper
every compiler_py submodule uses instead of hand-rolling its own message
formatting. Mirrors compiler_c/errors.py's shape exactly, but is a separate,
independent copy - compiler_py and compiler_c never import each other.
"""


class CompileError(Exception):
    pass


def err(msg, node=None):
    if node is not None:
        msg = f"{msg} ({type(node).__name__})"
    raise CompileError(msg)
