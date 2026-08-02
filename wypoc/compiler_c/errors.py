"""CompileError and the shared "fail loud, not silently wrong" raise helper
every compiler_c submodule uses instead of hand-rolling its own message
formatting. Zero-dependency (only used by everything else in the package)."""


class CompileError(Exception):
    pass


def err(msg, node=None):
    if node is not None:
        msg = f"{msg} ({type(node).__name__})"
    raise CompileError(msg)
