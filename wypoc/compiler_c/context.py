"""Per-`fn` compiler state, threaded explicitly as the first argument through
every statements.py/expressions.py/calls.py handler function, in place of
the `self` a single monolithic compiler class used to provide implicitly.

See DESIGN.md's "Chunk model" section for why a function compiles to one
entry `wyrm_exec_fn` plus a `static` chunk per basic block, and how the
same-activation-jump vs real-call transitions this class's bookkeeping
methods (`same_block_chunk_name`, `new_child_block`, ...) support actually
work.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from wypoc import ast_nodes as ast

from .errors import err
from .wtypes import TYPES, ctype

Continuation = Callable[[], None]


@dataclass
class FnContext:
    fndef: ast.FnDef
    functions: dict  # name -> FnDef, for call resolution
    module_ident: str

    locals: Dict[str, str] = field(default_factory=dict)  # name -> wyrm type name, fn-wide
    lines: List[str] = field(default_factory=list)
    indent: int = 0
    uses_forwarder: bool = False

    chunk_texts: List[str] = field(default_factory=list)  # completed C function texts, emission order
    chunk_names: List[str] = field(default_factory=list)  # static chunk names, for module-level protos

    local_order: List[str] = field(default_factory=list)  # fixed function-wide slot order
    local_index: Dict[str, int] = field(default_factory=dict)
    ret_type_name: Optional[str] = None

    break_target: Optional[Continuation] = None  # 0-arg callable, or None outside a loop
    continue_target: Optional[Continuation] = None

    _tmp: int = 0
    _block_serial: Dict[tuple, int] = field(default_factory=dict)  # block_path -> next chunk serial
    _child_counter: Dict[tuple, int] = field(default_factory=dict)  # block_path -> next child block index

    # -- naming --

    def new_tmp(self, prefix="__t") -> str:
        self._tmp += 1
        return f"{prefix}{self._tmp}"

    def entry_name(self, fn_name: Optional[str] = None) -> str:
        return f"w_{self.module_ident}_{fn_name if fn_name is not None else self.fndef.name}"

    def _format_chunk_name(self, block_path: tuple, serial: int) -> str:
        prefix = f"{self.module_ident}_{self.fndef.name}_chunk"
        if not block_path:
            return f"{prefix}_{serial}"
        return prefix + "".join(f"_b{p}" for p in block_path) + f"_{serial}"

    def same_block_chunk_name(self, block_path: tuple) -> str:
        """Allocate the next chunk within block_path (a call-split
        continuation or a join point), registering it for a proto."""
        n = self._block_serial.get(block_path, 0) + 1
        self._block_serial[block_path] = n
        name = self._format_chunk_name(block_path, n)
        self.chunk_names.append(name)
        return name

    def new_child_block(self, parent_path: tuple) -> Tuple[tuple, str]:
        """Allocate a fresh nested block (an if/elif/else/while body) and
        its first chunk. Returns (child_path, first_chunk_name)."""
        n = self._child_counter.get(parent_path, 0)
        self._child_counter[parent_path] = n + 1
        child_path = parent_path + (n,)
        return child_path, self.same_block_chunk_name(child_path)

    # -- chunk buffer management --

    def emit(self, text: str = ""):
        self.lines.append(("    " * self.indent) + text if text else "")

    def begin_chunk(self, name: str, static: bool):
        self.lines = []
        self.indent = 0
        self.emit(f"{'static ' if static else ''}wyrm_exec_state {name}(wyrm_state* state)")
        self.emit("{")
        self.indent += 1

    def end_chunk(self):
        self.indent -= 1
        self.emit("}")
        self.chunk_texts.append("\n".join(self.lines) + "\n")

    def emit_done(self):
        self.emit("return WYRM_EXEC_DONE;")

    # -- locals (every local must have a known type before use) --

    def declare(self, name: str, type_expr):
        ctype_name = ctype(type_expr, f"local '{name}'")
        if name in self.locals and self.locals[name] != ctype_name:
            err(f"local '{name}' redeclared with a different type")
        self.locals[name] = ctype_name

    def local_ref(self, name: str) -> str:
        ctype_, _tag, field_ = TYPES[self.locals[name]]
        idx = self.local_index[name]
        return f"(({ctype_})wyrm_state_value_n(state, {idx})->data.{field_})"

    def emit_local_assign(self, name: str, value_expr: str):
        _ctype, _tag, field_ = TYPES[self.locals[name]]
        idx = self.local_index[name]
        self.emit(f"wyrm_state_value_n(state, {idx})->data.{field_} = (wyrm_word)({value_expr});")
