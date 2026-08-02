"""wyrm --compile: translate a Wyrm module into C, targeting the real wyrm
VM's calling convention (`wyrm_exec_fn` / `wyrm_state*`), following the
call-handling pattern demonstrated in the reference implementation's
worked example (`w_do_a_mul` / `w_multipart`).

This is a narrow v1 slice, not a general compiler: only `fn` defs with
`int`/`bool` params & locals, straight-line arithmetic, `if`/`while`,
calls (tail and non-tail) to other compiled functions in the same
module, and the `native::block(...)` escape hatch from
doc/language-spec.md's "Native Code" section. Anything else raises
CompileError with a specific message - the same "fail loud, not silently
wrong" convention wyrm_eval_parse_tree.py uses for its own known gaps
(see wypoc/README.md's "Known gaps").

Every compiled `fn` becomes one non-static entry point,
`w_{module}_{fn}`, plus zero or more `static` continuation chunks,
`{module}_{fn}_chunk_{n}`, each a full `wyrm_exec_fn`. A chunk boundary
is introduced at every *non-tail* call site (`x = f(...)` or a bare
`f(...)` statement) that appears directly in a function's top-level
statement list - calls nested inside `if`/`while` bodies or larger
expressions are not split and remain unsupported (CompileError). Each
call compiles to `wyrm_state_call_continue(state, next_chunk, callee,
args, argc); return WYRM_EXEC_CONTINUE;`, preceded by pushing every
local the function has declared so far onto the state's value stack (in
a fixed, function-wide order) so the next chunk can recover them by
index once the callee's return value(s) land on top - this mirrors how
the reference implementation's `wyrm_stack_push/pop_continuation_f`
preserve a caller's frame beneath a callee's. This "preserve everything,
every time" scheme is intentionally unoptimized (no liveness analysis)
in exchange for being simple and uniform; see chunk_id-numbering below.

Tail calls (`return f(...)`) still compile to `wyrm_state_call_continue`
+ a shared forwarding continuation (`__wyrm_forward_result`), not
`WYRM_EXEC_TAIL_CALL` + `wyrm_stack_replace_frame_f` - the latter is the
"proper" zero-overhead tail call the reference implementation's fiber
trampoline supports, but there is no worked example of it to verify
frame/stack mechanics against (only the CONTINUE-style pattern is
demonstrated). Generating unverified raw-stack-manipulation code for a
feature this new isn't worth the risk; the forwarder is a correct, if
slightly less efficient, alternative confined entirely to the
demonstrated API surface.
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
    (`w_{module}_{fn}`) plus zero or more `static` continuation chunks
    (`{module}_{fn}_chunk_{n}`), one per non-tail call site."""

    def __init__(self, fndef: ast.FnDef, functions: dict, module_ident: str):
        self.fndef = fndef
        self.functions = functions  # name -> FnDef, for call resolution
        self.module_ident = module_ident
        self.locals: dict = {}  # name -> wyrm type name ("int"/"bool")
        self.lines: list = []
        self.indent = 0
        self.uses_forwarder = False
        self._tmp = 0
        self._chunk_counter = 0
        self.chunk_texts: list = []  # completed C function texts, entry first
        self.chunk_names: list = []  # static chunk names, for module-level protos

    def _new_tmp(self, prefix="__t") -> str:
        self._tmp += 1
        return f"{prefix}{self._tmp}"

    def _entry_name(self, fn_name=None) -> str:
        return f"w_{self.module_ident}_{fn_name if fn_name is not None else self.fndef.name}"

    def _new_chunk_name(self) -> str:
        self._chunk_counter += 1
        return f"{self.module_ident}_{self.fndef.name}_chunk_{self._chunk_counter}"

    def emit(self, text=""):
        self.lines.append(("    " * self.indent) + text if text else "")

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
        # Fixed, function-wide order every chunk boundary preserves/restores by index.
        self.local_order = list(self.locals.keys())

        param_names = {p.name for p in fn.params}
        self._begin_chunk(self._entry_name(), static=False)
        for i, p in enumerate(fn.params):
            ctype, _tag, field_ = _TYPES[self.locals[p.name]]
            self.emit(f"{ctype} {p.name} = ({ctype})wyrm_state_value_n(state, {i})->data.{field_};")
        for name, ctype_name in self.locals.items():
            if name in param_names:
                continue
            ctype, _tag, _field = _TYPES[ctype_name]
            zero = "false" if ctype == "bool" else "0"
            self.emit(f"{ctype} {name} = {zero};")

        self._compile_top_level(fn.body)
        if not self._definitely_returns(fn.body):
            self.emit("return WYRM_EXEC_DONE;")
        self._end_chunk()

        return self.chunk_texts

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

    def _definitely_returns(self, stmts) -> bool:
        if not stmts:
            return False
        last = stmts[-1]
        if isinstance(last, ast.Return):
            return True
        if isinstance(last, ast.If) and last.orelse is not None:
            branches = [last.body] + [e.body for e in last.elifs] + [last.orelse]
            return all(self._definitely_returns(b) for b in branches)
        return False

    def _compile_block(self, stmts):
        for s in stmts:
            self._compile_stmt(s)

    def _split_call_stmt(self, s):
        """If `s` is a non-tail call directly in a top-level statement list
        (`f(...)` or `x = f(...)`), return (call, target_name|None);
        otherwise None. Calls nested in bigger expressions or inside
        if/while bodies fall through to the normal (erroring) path."""
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

    def _compile_top_level(self, stmts):
        for s in stmts:
            split = self._split_call_stmt(s)
            if split is None:
                self._compile_stmt(s)
            else:
                call, target_name = split
                self._compile_call_split(call, target_name)

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

    def _compile_call_split(self, call: ast.Call, target_name):
        callee_name, callee = self._resolve_callee(call)

        # Preserve every local declared so far (fixed function-wide order) by
        # pushing it onto the state's value stack ahead of the call - it
        # survives beneath the callee's frame and is recovered by index in
        # the next chunk, alongside the callee's return value(s).
        for name in self.local_order:
            self.emit(f"wyrm_state_push(state, wyrm_value_word((wyrm_word)({name})));")

        args = self._build_args_array(call, callee_name, callee)
        argc = len(call.args)
        next_chunk = self._new_chunk_name()
        self.emit(f"wyrm_state_call_continue(state, {next_chunk}, {self._entry_name(callee_name)}, {args}, {argc});")
        self.emit("return WYRM_EXEC_CONTINUE;")
        self._end_chunk()

        self._begin_chunk(next_chunk, static=True)
        self.chunk_names.append(next_chunk)
        for i, name in enumerate(self.local_order):
            ctype, _tag, field_ = _TYPES[self.locals[name]]
            self.emit(f"{ctype} {name} = ({ctype})wyrm_state_value_n(state, {i})->data.{field_};")
        if target_name is not None:
            ctype, _tag, field_ = _TYPES[self.locals[target_name]]
            ret_idx = len(self.local_order)
            self.emit(f"{target_name} = ({ctype})wyrm_state_value_n(state, {ret_idx})->data.{field_};")

    def _compile_stmt(self, s):
        if isinstance(s, (ast.Pass, ast.TypeHint)):
            return
        if isinstance(s, ast.Continue):
            self.emit("continue;")
            return
        if isinstance(s, ast.Break):
            self.emit("break;")
            return
        if isinstance(s, ast.Return):
            self._compile_return(s)
            return
        if isinstance(s, ast.Assign):
            self._compile_assign(s)
            return
        if isinstance(s, ast.If):
            self._compile_if(s)
            return
        if isinstance(s, ast.While):
            self.emit(f"while ({self._expr(s.cond)}) {{")
            self.indent += 1
            self._compile_block(s.body)
            self.indent -= 1
            self.emit("}")
            return
        if isinstance(s, ast.ExprStmt):
            self._compile_expr_stmt(s)
            return
        _err("statement not supported by --compile", s)

    def _compile_if(self, s: ast.If):
        self.emit(f"if ({self._expr(s.cond)}) {{")
        self.indent += 1
        self._compile_block(s.body)
        self.indent -= 1
        for e in s.elifs:
            self.emit(f"}} else if ({self._expr(e.cond)}) {{")
            self.indent += 1
            self._compile_block(e.body)
            self.indent -= 1
        if s.orelse:
            self.emit("} else {")
            self.indent += 1
            self._compile_block(s.orelse)
            self.indent -= 1
        self.emit("}")

    def _compile_assign(self, s: ast.Assign):
        if len(s.targets) != len(s.values):
            _err("assignment target/value count mismatch", s)
        if len(s.targets) == 1:
            self.emit(f"{s.targets[0].name} = {self._expr(s.values[0])};")
            return
        # Evaluate every RHS into a temp first so `a, b = b, a` works.
        tmp_names = []
        for t, v in zip(s.targets, s.values):
            ctype, _tag, _field = _TYPES[self.locals[t.name]]
            tmp = self._new_tmp()
            self.emit(f"{ctype} {tmp} = {self._expr(v)};")
            tmp_names.append(tmp)
        for t, tmp in zip(s.targets, tmp_names):
            self.emit(f"{t.name} = {tmp};")

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
        if isinstance(call, ast.Call):
            _err(
                "calls nested inside if/while bodies are not supported by --compile "
                "(only calls directly in a function's top-level statement list are)", call,
            )
        _err("expression statements are not supported by --compile (only native::block() calls)", s)

    def _compile_native_block(self, call: ast.Call):
        _portion, inputs, outputs, body = _parse_native_block_args(call)
        for name in inputs + outputs:
            if name not in self.locals:
                _err(f"native::block() references unknown local/param '{name}'", call)

        # Copy-in/copy-out through a private nested scope, so the spliced C can
        # use the bare symbol names (matching the spec's worked example)
        # without a same-name self-init hazard against the enclosing locals
        # (`{ int a = a; }` is undefined behavior in C - the inner `a` is
        # already in scope at its own initializer).
        self.emit("{")
        self.indent += 1
        in_tmp = {}
        for name in inputs:
            ctype, _tag, _field = _TYPES[self.locals[name]]
            tmp = self._new_tmp("__native_in")
            self.emit(f"{ctype} {tmp} = {name};")
            in_tmp[name] = tmp
        out_tmp = {}
        for name in outputs:
            ctype, _tag, _field = _TYPES[self.locals[name]]
            tmp = self._new_tmp("__native_out")
            self.emit(f"{ctype} {tmp};")
            out_tmp[name] = tmp

        self.emit("{")
        self.indent += 1
        for name in inputs:
            ctype, _tag, _field = _TYPES[self.locals[name]]
            self.emit(f"{ctype} {name} = {in_tmp[name]};")
        for name in outputs:
            ctype, _tag, _field = _TYPES[self.locals[name]]
            self.emit(f"{ctype} {name};")
        for line in body.splitlines():
            self.emit(line)
        for name in outputs:
            self.emit(f"{out_tmp[name]} = {name};")
        self.indent -= 1
        self.emit("}")

        for name in outputs:
            self.emit(f"{name} = {out_tmp[name]};")
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
            return node.id
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
