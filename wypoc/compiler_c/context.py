"""Per-`fn` compiler state, threaded explicitly as the first argument through
every statements.py/expressions.py/calls.py handler, in place of the `self` a
monolithic compiler class would provide implicitly.

One wyrm `fn` compiles to exactly one C function, so what this has to track
is small: the declared locals and their types, the emit buffer and its
indent, and a counter for temporaries. There is no chunk graph and no
value-stack slot allocation - see DESIGN.md's "Calling convention" for what
replaced them and why.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from wypoc import ast_nodes as ast

from .errors import err
from .wtypes import WType, wtype


@dataclass
class FnContext:
    fndef: ast.FnDef
    functions: dict           # name -> FnDef, for call resolution
    module_ident: str

    locals: Dict[str, WType] = field(default_factory=dict)
    lines: List[str] = field(default_factory=list)
    indent: int = 0
    ret_type: Optional[WType] = None
    loop_depth: int = 0       # >0 inside a while body, so break/continue are legal

    _tmp: int = 0

    # -- naming --

    def new_tmp(self, prefix: str = "__t") -> str:
        self._tmp += 1
        return f"{prefix}{self._tmp}"

    def entry_name(self, fn_name: Optional[str] = None) -> str:
        """The C name of a compiled `fn`. Namespaced by module so two
        compiled modules can be linked into one binary."""
        return f"w_{self.module_ident}_{fn_name if fn_name is not None else self.fndef.name}"

    # -- emit buffer --

    def emit(self, text: str = ""):
        self.lines.append(("    " * self.indent) + text if text else "")

    def emit_block(self, text: str):
        """Several lines of already-formatted C (a spliced native block),
        each re-indented to the current level."""
        for line in text.splitlines():
            self.emit(line)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    # -- locals --

    def declare(self, name: str, type_expr, what: Optional[str] = None) -> WType:
        declared = wtype(type_expr, what or f"local '{name}'")
        existing = self.locals.get(name)
        if existing is not None and existing is not declared:
            err(f"local '{name}' redeclared as '{declared.name}' (was '{existing.name}')")
        self.locals[name] = declared
        return declared

    def type_of(self, name: str) -> WType:
        wt = self.locals.get(name)
        if wt is None:
            err(f"unknown identifier '{name}'")
        return wt
