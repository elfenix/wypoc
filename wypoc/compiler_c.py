"""wyrm --compile: translate a Wyrm module into C, targeting the real wyrm
VM's calling convention (`wyrm_exec_fn` / `wyrm_state*`), following the
call-handling pattern demonstrated in the reference implementation's
worked example (`w_do_a_mul` / `w_multipart`).

This is a narrow v1 slice, not a general compiler: only `fn` defs with
`int`/`bool` params & locals, straight-line arithmetic, `if`/`while`,
calls (tail and non-tail, anywhere a statement may appear) to other
compiled functions in the same module, and the `native::block(...)`
escape hatch from doc/language-spec.md's "Native Code" section. Anything
else raises CompileError with a specific message - the same "fail loud,
not silently wrong" convention wyrm_eval_parse_tree.py uses for its own
known gaps (see wypoc/README.md's "Known gaps").

Every `fn` body is compiled as a graph of small `wyrm_exec_fn` "chunks"
rather than one C function - one non-static entry point, `w_{module}_
{fn}`, plus a `static` chunk per basic block: every `if`/`elif`/`else`
and `while` body is its own chunk, and every non-tail call site splits
its enclosing block into a chunk before the call and one after. Chunks
are named `{module}_{fn}_chunk_b{p0}_b{p1}..._{n}`, where the `b*`
segments are a tree-coordinate path down through nested blocks (empty
for the function's own top-level block) and `n` is a sequence number
among chunks sharing that path. This is intentionally not optimized -
every block gets a real C function and a real state-machine transition,
even ones with no call in them - in exchange for one uniform code path
that already generalizes to arbitrary nesting, per wypoc project notes.

Two kinds of transition connect chunks, both ending in `return
WYRM_EXEC_CONTINUE;`:

- A **same-activation jump** (`wyrm_state_set_pending`) - used for
  `if`/`elif`/`else` branch dispatch, entering/looping a `while`, and
  `break`/`continue`. It only swaps which `wyrm_exec_fn` runs next; it
  never touches the value stack, so looping this way costs nothing per
  iteration (this is *not* `WYRM_EXEC_TAIL_CALL` + `wyrm_stack_
  replace_frame_f`, the "proper" trampoline `fiber.c` supports - there's
  still no worked example of that path to verify frame/stack mechanics
  against; `set_pending` gets the same "no stack growth" property for
  same-function control flow via the demonstrated API surface instead).
- A **real call** (`wyrm_state_call_continue`) - used for an actual call
  into another compiled `fn`. It pushes a fresh frame for the callee, so
  the caller's locals must already live *on* the value stack (not in C
  variables) to survive it: every local gets one fixed slot, established
  once by the entry chunk and addressed via `wyrm_state_value_n(state,
  slot)` from every chunk of the function for its entire lifetime,
  restored to exactly that many slots (`wyrm_state_pop_to_value_count`)
  right after each call's return value is copied out. That keeps a
  function's own stack footprint at a constant size regardless of how
  many calls it makes or how many times a loop containing one runs -
  the loop-inside-a-call-with-no-cleanup-primitive problem this scheme
  depends on `wyrm_state_pop_to_value_count` (a small addition to the
  reference implementation made specifically for this) to solve.

Tail calls (`return f(...)`) are the one case that isn't a plain
same-activation jump or a plain real call: they still compile to
`wyrm_state_call_continue` plus a shared forwarding continuation
(`__wyrm_forward_result`) that relays the callee's result stack-for-
stack back to whatever called *this* function, since a real tail call
should not grow the caller's own footprint the way an ordinary call's
"read result, pop back down" sequence does.
"""
from wypoc import ast_nodes as ast
from wypoc.wyrm_eval_parse_tree import eval_string_literal


class CompileError(Exception):
    pass


# wyrm primitive type name -> (C local type, wyrm_type_tag, wyrm_primitive field).
# float is deliberately absent: include/wyrm/types.h's wyrm_type_tag enum has no
# dedicated FLOAT tag yet, so there's no real encoding to target.
_TYPES = {
    "int": ("wyrm_word", "WYRM_TYPE_TAG_WORD", "word"),
    "bool": ("bool", "WYRM_TYPE_TAG_WORD", "word"),
}

_BINOPS = {
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
    "&": "&", "|": "|", "^": "^",
    "<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "==", "!=": "!=",
    "and": "&&", "or": "||",
}

_NATIVE_PORTIONS = ("HEADER", "TYPES", "CONSTANTS", "PROTOS", "FUNCTIONS")

_FORWARDER_SRC = """\
static wyrm_exec_state __wyrm_forward_result(wyrm_state* state)
{
    wyrm_uword __n = wyrm_state_value_count(state);
    for (wyrm_uword __i = 0; __i < __n; __i++) {
        wyrm_state_push_return(state, *wyrm_state_value_n(state, __i));
    }
    return WYRM_EXEC_DONE;
}
"""


def _c_ident(name: str) -> str:
    """Sanitize an arbitrary module name into a valid C identifier fragment."""
    ident = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    if not ident or ident[0].isdigit():
        ident = f"m_{ident}"
    return ident


def _err(msg, node=None):
    if node is not None:
        msg = f"{msg} ({type(node).__name__})"
    raise CompileError(msg)


def _ctype(type_expr, what) -> str:
    if type_expr is None or len(type_expr.parts) != 1 or type_expr.parts[0] not in _TYPES:
        name = "::".join(type_expr.parts) if type_expr else "<untyped>"
        _err(f"{what} has unsupported type {name!r}; --compile v1 only supports 'int' and 'bool'")
    return type_expr.parts[0]


def _is_float_literal(text: str) -> bool:
    if text.lower().startswith("0x"):
        return False
    return "." in text or "e" in text.lower()


def _is_native_block_call(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Scope)
        and isinstance(call.func.obj, ast.Name)
        and call.func.obj.id == "native"
        and call.func.name == "block"
    )


def _parse_native_block_args(call: ast.Call):
    """Validate + destructure a native::block(portion, inputs, outputs, code)
    call per doc/language-spec.md: all four arguments must be literals."""
    args = call.args
    if len(args) != 4:
        _err("native::block() requires exactly 4 arguments: portion, inputs, outputs, code", call)
    portion, inputs, outputs, code = args
    if not isinstance(portion, ast.Symbol):
        _err("native::block()'s first argument must be a symbol literal, e.g. 'HEADER", call)
    if not isinstance(inputs, ast.Pair) or not isinstance(outputs, ast.Pair):
        _err("native::block()'s input/output arguments must be pair-list literals, e.g. '('a, 'b)", call)
    if inputs.tail is not None or outputs.tail is not None:
        _err("native::block()'s input/output lists must be proper lists (no '.' tail)", call)
    for sym in inputs.elements + outputs.elements:
        if not isinstance(sym, ast.Symbol):
            _err("native::block()'s input/output lists must contain only symbols", call)
    if not isinstance(code, ast.Str):
        _err("native::block()'s 4th argument must be a (raw) string literal", call)
    body = eval_string_literal(code.value)
    return portion.name, [s.name for s in inputs.elements], [s.name for s in outputs.elements], body


class _FnCompiler:
    """Compiles a single `fn` into one non-static entry `wyrm_exec_fn`
    (`w_{module}_{fn}`) plus a `static` chunk per basic block - see this
    module's docstring for the chunk-naming and jump/call scheme."""

    def __init__(self, fndef: ast.FnDef, functions: dict, module_ident: str):
        self.fndef = fndef
        self.functions = functions  # name -> FnDef, for call resolution
        self.module_ident = module_ident
        self.locals: dict = {}  # name -> wyrm type name ("int"/"bool"), fn-wide
        self.lines: list = []
        self.indent = 0
        self.uses_forwarder = False
        self._tmp = 0
        self.chunk_texts: list = []  # completed C function texts, in emission order
        self.chunk_names: list = []  # static chunk names, for module-level protos
        self._block_serial: dict = {}  # block_path -> next chunk serial in that block
        self._child_counter: dict = {}  # block_path -> next child block index
        self._break_target = None  # 0-arg callable, or None outside a loop
        self._continue_target = None

    # -- naming --

    def _new_tmp(self, prefix="__t") -> str:
        self._tmp += 1
        return f"{prefix}{self._tmp}"

    def _entry_name(self, fn_name=None) -> str:
        return f"w_{self.module_ident}_{fn_name if fn_name is not None else self.fndef.name}"

    def _format_chunk_name(self, block_path, serial) -> str:
        prefix = f"{self.module_ident}_{self.fndef.name}_chunk"
        if not block_path:
            return f"{prefix}_{serial}"
        return prefix + "".join(f"_b{p}" for p in block_path) + f"_{serial}"

    def _same_block_chunk_name(self, block_path) -> str:
        """Allocate the next chunk within block_path (a call-split
        continuation or a join point), registering it for a proto."""
        n = self._block_serial.get(block_path, 0) + 1
        self._block_serial[block_path] = n
        name = self._format_chunk_name(block_path, n)
        self.chunk_names.append(name)
        return name

    def _new_child_block(self, parent_path):
        """Allocate a fresh nested block (an if/elif/else/while body) and
        its first chunk. Returns (child_path, first_chunk_name)."""
        n = self._child_counter.get(parent_path, 0)
        self._child_counter[parent_path] = n + 1
        child_path = parent_path + (n,)
        return child_path, self._same_block_chunk_name(child_path)

    # -- chunk buffer management --

    def emit(self, text=""):
        self.lines.append(("    " * self.indent) + text if text else "")

    def _begin_chunk(self, name: str, static: bool):
        self.lines = []
        self.indent = 0
        self.emit(f"{'static ' if static else ''}wyrm_exec_state {name}(wyrm_state* state)")
        self.emit("{")
        self.indent += 1

    def _end_chunk(self):
        self.indent -= 1
        self.emit("}")
        self.chunk_texts.append("\n".join(self.lines) + "\n")

    def _emit_done(self):
        self.emit("return WYRM_EXEC_DONE;")

    # -- pass 1: collect locals (every local must have a known type before use) --

    def _declare(self, name, type_expr):
        ctype_name = _ctype(type_expr, f"local '{name}'")
        if name in self.locals and self.locals[name] != ctype_name:
            _err(f"local '{name}' redeclared with a different type")
        self.locals[name] = ctype_name

    def _collect_locals(self, stmts):
        for s in stmts:
            self._collect_locals_stmt(s)

    def _collect_locals_stmt(self, s):
        if isinstance(s, ast.TypeHint):
            self._declare(s.name, s.type)
        elif isinstance(s, ast.Assign):
            for t in s.targets:
                if not isinstance(t, ast.NameTarget):
                    _err("only plain name targets are supported by --compile", s)
            if s.type is not None:
                for t in s.targets:
                    self._declare(t.name, s.type)
            else:
                for t in s.targets:
                    if t.name not in self.locals:
                        _err(
                            f"local '{t.name}' assigned before its type is known "
                            f"(add 'name: type' on first assignment)", s,
                        )
        elif isinstance(s, ast.If):
            self._collect_locals(s.body)
            for e in s.elifs:
                self._collect_locals(e.body)
            if s.orelse:
                self._collect_locals(s.orelse)
        elif isinstance(s, ast.While):
            self._collect_locals(s.body)
        elif isinstance(s, (ast.Return, ast.Pass, ast.Continue, ast.Break, ast.ExprStmt)):
            pass
        else:
            _err("statement not supported by --compile", s)

    # -- pass 2: emit --

    def compile(self) -> list:
        fn = self.fndef
        if fn.class_target:
            _err(f"fn '{fn.name}': class-target/message fns not supported by --compile")
        self.ret_type_name = _ctype(fn.ret, f"fn '{fn.name}' return") if fn.ret else None

        for p in fn.params:
            if not isinstance(p, ast.Param):
                _err(f"fn '{fn.name}': only plain parameters are supported by --compile (no /, *args, **kwargs)")
            if p.default is not None:
                _err(f"fn '{fn.name}' param '{p.name}': default values not supported by --compile")
            self.locals[p.name] = _ctype(p.type, f"fn '{fn.name}' param '{p.name}'")

        self._collect_locals(fn.body)
        # Fixed, function-wide slot order every chunk addresses by index for
        # this fn's whole lifetime.
        self.local_order = list(self.locals.keys())
        self.local_index = {name: i for i, name in enumerate(self.local_order)}

        self._begin_chunk(self._entry_name(), static=False)
        # Params arrive as the incoming call's args (already at slots
        # 0..len(params)-1); every other local gets a fresh zero-valued slot.
        for name in self.local_order[len(fn.params):]:
            ctype, _tag, _field = _TYPES[self.locals[name]]
            zero = "false" if ctype == "bool" else "0"
            self.emit(f"wyrm_state_push(state, wyrm_value_word((wyrm_word)({zero})));")

        self._run_stmts(fn.body, (), self._emit_done)
        return self.chunk_texts

    def _local_ref(self, name) -> str:
        ctype, _tag, field_ = _TYPES[self.locals[name]]
        idx = self.local_index[name]
        return f"(({ctype})wyrm_state_value_n(state, {idx})->data.{field_})"

    def _emit_local_assign(self, name, value_expr: str):
        _ctype_, _tag, field_ = _TYPES[self.locals[name]]
        idx = self.local_index[name]
        self.emit(f"wyrm_state_value_n(state, {idx})->data.{field_} = (wyrm_word)({value_expr});")

    # -- statement-list (block) compilation --

    def _split_call_stmt(self, s):
        """If `s` is a non-tail call (`f(...)` or `x = f(...)`), return
        (call, target_name|None); otherwise None. Calls nested inside
        bigger expressions still aren't supported (CompileError, from
        `_expr`)."""
        if isinstance(s, ast.ExprStmt) and isinstance(s.value, ast.Call) and not _is_native_block_call(s.value):
            return s.value, None
        if (
            isinstance(s, ast.Assign)
            and len(s.targets) == 1
            and len(s.values) == 1
            and isinstance(s.values[0], ast.Call)
            and not _is_native_block_call(s.values[0])
            and isinstance(s.targets[0], ast.NameTarget)
        ):
            return s.values[0], s.targets[0].name
        return None

    def _run_stmts(self, stmts, block_path, fallthrough):
        """Compile `stmts` into the currently-open chunk, opening/closing
        further chunks as needed for control flow and calls. `fallthrough`
        is the 0-arg callable to invoke (then close the chunk) if control
        falls off the end of `stmts` without an explicit return/break/
        continue/call-split."""
        for i, s in enumerate(stmts):
            if isinstance(s, (ast.Pass, ast.TypeHint)):
                continue
            if isinstance(s, ast.Continue):
                if self._continue_target is None:
                    _err("'continue' outside of a loop", s)
                self._continue_target()
                self._end_chunk()
                return
            if isinstance(s, ast.Break):
                if self._break_target is None:
                    _err("'break' outside of a loop", s)
                self._break_target()
                self._end_chunk()
                return
            if isinstance(s, ast.Return):
                self._compile_return(s)
                self._end_chunk()
                return
            if isinstance(s, ast.If):
                self._compile_if_stmt(s, block_path, stmts[i + 1:], fallthrough)
                return
            if isinstance(s, ast.While):
                self._compile_while_stmt(s, block_path, stmts[i + 1:], fallthrough)
                return
            split = self._split_call_stmt(s)
            if split is not None:
                self._compile_call_split(split, block_path, stmts[i + 1:], fallthrough)
                return
            if isinstance(s, ast.Assign):
                self._compile_assign(s)
                continue
            if isinstance(s, ast.ExprStmt):
                self._compile_expr_stmt(s)
                continue
            _err("statement not supported by --compile", s)
        fallthrough()
        self._end_chunk()

    def _continuation_for(self, remaining_stmts, block_path, fallthrough):
        """Build the 0-arg jump callable a branch/loop-exit should invoke
        to continue with `remaining_stmts` (possibly empty) in block_path,
        followed by `fallthrough`. Returns (jump, materialize) where
        materialize (or None, if no join chunk was needed) must be called
        once, after all users of `jump` have been emitted, to compile the
        join chunk's body."""
        if not remaining_stmts:
            return fallthrough, None
        join_name = self._same_block_chunk_name(block_path)

        def jump():
            self.emit(f"wyrm_state_set_pending(state, {join_name});")
            self.emit("return WYRM_EXEC_CONTINUE;")

        def materialize():
            self._begin_chunk(join_name, static=True)
            self._run_stmts(remaining_stmts, block_path, fallthrough)

        return jump, materialize

    def _compile_if_stmt(self, s: ast.If, block_path, remaining_stmts, fallthrough):
        join, materialize_join = self._continuation_for(remaining_stmts, block_path, fallthrough)

        branches = [(s.cond, s.body)] + [(e.cond, e.body) for e in s.elifs]
        branch_targets = [(cond, *self._new_child_block(block_path), body) for cond, body in branches]
        else_target = self._new_child_block(block_path) if s.orelse else None

        for i, (cond, _path, name, _body) in enumerate(branch_targets):
            kw = "if" if i == 0 else "} else if"
            self.emit(f"{kw} ({self._expr(cond)}) {{")
            self.indent += 1
            self.emit(f"wyrm_state_set_pending(state, {name});")
            self.emit("return WYRM_EXEC_CONTINUE;")
            self.indent -= 1
        self.emit("} else {")
        self.indent += 1
        if else_target is not None:
            self.emit(f"wyrm_state_set_pending(state, {else_target[1]});")
            self.emit("return WYRM_EXEC_CONTINUE;")
        else:
            join()
        self.indent -= 1
        self.emit("}")
        self._end_chunk()

        for cond, path, name, body in branch_targets:
            self._begin_chunk(name, static=True)
            self._run_stmts(body, path, join)
        if else_target is not None:
            else_path, else_name = else_target
            self._begin_chunk(else_name, static=True)
            self._run_stmts(s.orelse, else_path, join)

        if materialize_join:
            materialize_join()

    def _compile_while_stmt(self, s: ast.While, block_path, remaining_stmts, fallthrough):
        after, materialize_after = self._continuation_for(remaining_stmts, block_path, fallthrough)

        check_name = self._same_block_chunk_name(block_path)
        body_path, body_name = self._new_child_block(block_path)

        self.emit(f"wyrm_state_set_pending(state, {check_name});")
        self.emit("return WYRM_EXEC_CONTINUE;")
        self._end_chunk()

        self._begin_chunk(check_name, static=True)
        self.emit(f"if ({self._expr(s.cond)}) {{")
        self.indent += 1
        self.emit(f"wyrm_state_set_pending(state, {body_name});")
        self.emit("return WYRM_EXEC_CONTINUE;")
        self.indent -= 1
        self.emit("} else {")
        self.indent += 1
        after()
        self.indent -= 1
        self.emit("}")
        self._end_chunk()

        def loop_back():
            self.emit(f"wyrm_state_set_pending(state, {check_name});")
            self.emit("return WYRM_EXEC_CONTINUE;")

        self._begin_chunk(body_name, static=True)
        old_break, old_continue = self._break_target, self._continue_target
        self._break_target, self._continue_target = after, loop_back
        self._run_stmts(s.body, body_path, loop_back)
        self._break_target, self._continue_target = old_break, old_continue

        if materialize_after:
            materialize_after()

    def _resolve_callee(self, call: ast.Call):
        if not isinstance(call.func, ast.Name):
            _err("only calls to a plain function name are supported by --compile", call)
        callee_name = call.func.id
        callee = self.functions.get(callee_name)
        if callee is None:
            _err(f"call to unknown/uncompiled function '{callee_name}'", call)
        for a in call.args:
            if isinstance(a, (ast.Kwarg, ast.SpreadPos, ast.SpreadKw)):
                _err("keyword/spread arguments not supported by --compile", call)
        if len(call.args) != len(callee.params):
            _err(
                f"call to '{callee_name}': expected {len(callee.params)} argument(s), "
                f"got {len(call.args)}", call,
            )
        return callee_name, callee

    def _build_args_array(self, call: ast.Call, callee_name: str, callee) -> str:
        """Emit (if needed) a `wyrm_value[]` of the call's evaluated arguments
        and return the C expression to pass as `wyrm_state_call_continue`'s
        `args` parameter (a temp array name, or "NULL")."""
        if not call.args:
            return "NULL"
        entries = []
        for p, a in zip(callee.params, call.args):
            ctype_name = _ctype(p.type, f"param '{p.name}' of '{callee_name}'")
            ctype, tag, field_ = _TYPES[ctype_name]
            entries.append(f"{{ .type = {tag}, .data.{field_} = ({ctype})({self._expr(a)}) }}")
        arr = self._new_tmp("__args")
        self.emit(f"wyrm_value {arr}[{len(entries)}] = {{ " + ", ".join(entries) + " };")
        return arr

    def _compile_call_split(self, split, block_path, remaining_stmts, fallthrough):
        call, target_name = split
        callee_name, callee = self._resolve_callee(call)

        args = self._build_args_array(call, callee_name, callee)
        next_name = self._same_block_chunk_name(block_path)
        self.emit(
            f"wyrm_state_call_continue(state, {next_name}, "
            f"{self._entry_name(callee_name)}, {args}, {len(call.args)});"
        )
        self.emit("return WYRM_EXEC_CONTINUE;")
        self._end_chunk()

        self._begin_chunk(next_name, static=True)
        if target_name is not None:
            ret_idx = len(self.local_order)
            _ctype_, _tag, field_ = _TYPES[self.locals[target_name]]
            idx = self.local_index[target_name]
            self.emit(
                f"wyrm_state_value_n(state, {idx})->data.{field_} = "
                f"wyrm_state_value_n(state, {ret_idx})->data.{field_};"
            )
        self.emit(f"wyrm_state_pop_to_value_count(state, {len(self.local_order)});")
        self._run_stmts(remaining_stmts, block_path, fallthrough)

    def _compile_assign(self, s: ast.Assign):
        if len(s.targets) != len(s.values):
            _err("assignment target/value count mismatch", s)
        if len(s.targets) == 1:
            self._emit_local_assign(s.targets[0].name, self._expr(s.values[0]))
            return
        # Evaluate every RHS into a temp first so `a, b = b, a` works.
        tmp_names = []
        for t, v in zip(s.targets, s.values):
            ctype, _tag, _field = _TYPES[self.locals[t.name]]
            tmp = self._new_tmp()
            self.emit(f"{ctype} {tmp} = {self._expr(v)};")
            tmp_names.append(tmp)
        for t, tmp in zip(s.targets, tmp_names):
            self._emit_local_assign(t.name, tmp)

    def _compile_return(self, s: ast.Return):
        if s.value is None:
            self.emit("return WYRM_EXEC_DONE;")
            return
        if isinstance(s.value, ast.Tuple):
            _err("multi-value return not supported by --compile (fn return type is single-valued)", s)
        if isinstance(s.value, ast.Call):
            if _is_native_block_call(s.value):
                _err("native::block() cannot be used as a return value", s)
            self._compile_tail_call(s.value)
            return
        if self.ret_type_name is None:
            _err(f"fn '{self.fndef.name}' has no declared return type but returns a value", s)
        val = self._expr(s.value)
        self.emit(f"wyrm_state_push_return(state, wyrm_value_word((wyrm_word)({val})));")
        self.emit("return WYRM_EXEC_DONE;")

    def _compile_tail_call(self, call: ast.Call):
        callee_name, callee = self._resolve_callee(call)
        self.uses_forwarder = True
        args = self._build_args_array(call, callee_name, callee)
        self.emit(
            f"wyrm_state_call_continue(state, __wyrm_forward_result, "
            f"{self._entry_name(callee_name)}, {args}, {len(call.args)});"
        )
        self.emit("return WYRM_EXEC_CONTINUE;")

    def _compile_expr_stmt(self, s: ast.ExprStmt):
        call = s.value
        if isinstance(call, ast.Call) and _is_native_block_call(call):
            self._compile_native_block(call)
            return
        # Non-native calls are intercepted by _split_call_stmt before reaching
        # here, so any Call left is unreachable; keep the fallback generic.
        _err("expression statements are not supported by --compile (only native::block() calls)", s)

    def _compile_native_block(self, call: ast.Call):
        _portion, inputs, outputs, body = _parse_native_block_args(call)
        for name in inputs + outputs:
            if name not in self.locals:
                _err(f"native::block() references unknown local/param '{name}'", call)

        # Copy-in/copy-out through a private nested scope, so the spliced C can
        # use the bare symbol names (matching the spec's worked example).
        self.emit("{")
        self.indent += 1
        for name in inputs:
            ctype, _tag, field_ = _TYPES[self.locals[name]]
            idx = self.local_index[name]
            self.emit(f"{ctype} {name} = ({ctype})wyrm_state_value_n(state, {idx})->data.{field_};")
        for name in outputs:
            ctype, _tag, _field = _TYPES[self.locals[name]]
            self.emit(f"{ctype} {name};")
        for line in body.splitlines():
            self.emit(line)
        for name in outputs:
            self._emit_local_assign(name, name)
        self.indent -= 1
        self.emit("}")

    def _expr(self, node) -> str:
        if isinstance(node, ast.Num):
            if _is_float_literal(node.value):
                _err("float literals not supported by --compile (no VM type tag yet)", node)
            return node.value.replace("_", "")
        if isinstance(node, ast.Bool):
            return "true" if node.value else "false"
        if isinstance(node, ast.Name):
            if node.id not in self.locals:
                _err(f"unknown identifier '{node.id}'", node)
            return self._local_ref(node.id)
        if isinstance(node, ast.UnaryOp):
            if node.op == "-":
                return f"(-({self._expr(node.operand)}))"
            if node.op == "not":
                return f"(!({self._expr(node.operand)}))"
            _err(f"unary operator '{node.op}' not supported by --compile", node)
        if isinstance(node, ast.BinOp):
            if node.op not in _BINOPS:
                _err(f"operator '{node.op}' not supported by --compile", node)
            return f"({self._expr(node.left)} {_BINOPS[node.op]} {self._expr(node.right)})"
        if isinstance(node, ast.Call):
            _err(
                "calls nested inside larger expressions are not supported by --compile "
                "(only 'return f(...)', 'x = f(...)', or a bare 'f(...)' statement are)", node,
            )
        _err("expression not supported by --compile", node)


def compile_module(tree: ast.Program, module_name: str) -> str:
    """Compile a parsed Wyrm module (must `import native`) into C source
    text targeting the real wyrm VM calling convention. Raises
    CompileError on anything outside the v1 narrow slice."""
    has_native_import = any(isinstance(s, ast.Import) and s.path == ["native"] for s in tree.body)
    if not has_native_import:
        _err(
            "module does not 'import native'; not eligible for --compile "
            "(only modules that opt in with 'import native' can be compiled)"
        )

    sections = {p: [] for p in _NATIVE_PORTIONS}
    functions_order = []  # [("fn", FnDef) | ("raw", c_text), ...] in source order
    fn_defs = []
    seen_names = set()

    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            if stmt.path != ["native"]:
                _err(
                    f"import of '{'::'.join(stmt.path)}' not supported by --compile "
                    f"(v1 only compiles single-file modules)", stmt,
                )
            continue
        if isinstance(stmt, ast.FnDef):
            if stmt.name in seen_names:
                _err(f"duplicate top-level definition of '{stmt.name}'", stmt)
            seen_names.add(stmt.name)
            fn_defs.append(stmt)
            functions_order.append(("fn", stmt))
            continue
        if isinstance(stmt, ast.ExprStmt) and isinstance(stmt.value, ast.Call) and _is_native_block_call(stmt.value):
            portion, inputs, outputs, body = _parse_native_block_args(stmt.value)
            if portion not in _NATIVE_PORTIONS:
                _err(f"native::block() portion {portion!r} unknown; expected one of {_NATIVE_PORTIONS}", stmt)
            if inputs or outputs:
                _err("top-level native::block() must have empty input/output lists", stmt)
            if portion == "FUNCTIONS":
                functions_order.append(("raw", body))
            else:
                sections[portion].append(body)
            continue
        _err(
            "top-level statement not supported by --compile "
            "(only 'fn' definitions and native::block() calls are allowed)", stmt,
        )

    module_ident = _c_ident(module_name)
    functions = {fn.name: fn for fn in fn_defs}
    compiled = []
    chunk_protos = []
    uses_forwarder = False
    for kind, item in functions_order:
        if kind == "raw":
            compiled.append(item)
        else:
            fc = _FnCompiler(item, functions, module_ident)
            texts = fc.compile()
            uses_forwarder = uses_forwarder or fc.uses_forwarder
            compiled.extend(texts)
            chunk_protos.extend(f"static wyrm_exec_state {name}(wyrm_state* state);" for name in fc.chunk_names)

    protos = [f"wyrm_exec_state w_{module_ident}_{fn.name}(wyrm_state* state);" for fn in fn_defs]
    protos.extend(chunk_protos)
    if uses_forwarder:
        protos.insert(0, "static wyrm_exec_state __wyrm_forward_result(wyrm_state* state);")

    parts = [
        f"/* Generated by wyrm --compile from module '{module_name}' */",
        "#include <wyrm.h>",
        "#include <stdbool.h>",
        "",
    ]
    for section, header in (("HEADER", None), ("TYPES", "TYPES"), ("CONSTANTS", "CONSTANTS")):
        if sections[section]:
            if header:
                parts.append(f"/* {header} */")
            parts.append("\n".join(sections[section]))
            parts.append("")
    parts.append("/* PROTOS */")
    parts.extend(protos)
    if sections["PROTOS"]:
        parts.append("\n".join(sections["PROTOS"]))
    parts.append("")
    parts.append("/* FUNCTIONS */")
    if uses_forwarder:
        parts.append(_FORWARDER_SRC)
    parts.extend(compiled)
    return "\n".join(parts) + "\n"
