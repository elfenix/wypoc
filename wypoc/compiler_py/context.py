"""Per-compile-unit state threaded explicitly through every expressions.py/
statements.py/functions.py handler, mirroring compiler_c/context.py's
FnContext but split into two levels since Python codegen has two distinct
scopes to track: a whole module's assembly state (ModuleCtx) and one
function/coroutine body's emit buffer (FnCtx).
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class ModuleCtx:
    """One `.wy` file's compile state - the lines that will become its
    generated `.py` source, plus the bookkeeping later stages (imports,
    classes, message tables) hang off of. Stage 1 only uses `toplevel_names`
    (redeclaration bookkeeping) and the three emitted-lines buckets."""

    module_path: tuple  # () for the entry script, else e.g. ("std", "io")

    # Three separately-assembled buckets, concatenated in this order by
    # module.py's final assembly step: import statements, then class/fn/co
    # definitions, then the do_import() function's own body lines.
    header_lines: List[str] = field(default_factory=list)
    body_lines: List[str] = field(default_factory=list)
    do_import_lines: List[str] = field(default_factory=list)

    toplevel_names: Set[str] = field(default_factory=set)
    var_names: Set[str] = field(default_factory=set)  # py_ident'd top-level var names, for `global` decls
    classes: Dict[str, tuple] = field(default_factory=dict)  # name -> (fields_name, ctor_name, base_py_or_None, slot_names)
    # raw (un-py_ident'd) wyrm names this module exposes at top level - fn/
    # class/var names - used both for `global` bookkeeping and, from stage
    # 3 on, as the set a wildcard `import mod::*` pulls from.
    public_names: Set[str] = field(default_factory=set)
    # bare wyrm name -> a Python expression string that reads it (e.g.
    # "wyrm.shapes" for a bound module root/leaf, "wyrm.shapes.wy_Circle"
    # for an item-list import) - consulted by FnCtx.resolve_read.
    import_bindings: Dict[str, str] = field(default_factory=dict)
    # (dotted_python_module, is_static) pairs, one per distinct imported
    # module, in first-seen source order - do_import awaits each other
    # module's own do_import in this order before running its own
    # top-level statements (see module.py's _compile_do_import).
    imports: List[tuple] = field(default_factory=list)
    # (message_name, signature_tuple_of_fields_names_or_None, python_fn_name)
    # accumulated by classes.py / module.py's plain-fn promotion pass, then
    # emitted as _TABLE.register(...) calls at the top of do_import.
    messages: List[tuple] = field(default_factory=list)

    def add_body(self, text: str = ""):
        self.body_lines.append(text)

    def add_header(self, text: str):
        self.header_lines.append(text)

    def add_do_import(self, text: str, indent: int = 1):
        self.do_import_lines.append(("    " * indent) + text if text else "")


@dataclass
class FnCtx:
    """One function/coroutine body's emit state.

    `scopes` is a stack of wyrm-name -> python-name dicts, one per
    lexically-nested block currently open - mirroring the interpreter's
    own `ctx.child()` scoping (a fresh child Scope per `if`/`elif`/`else`
    branch, `while`/`for` iteration, `do:` block, and lambda body - see
    wyrm_eval_parse_tree.py's _eval_if/eval_stmt's While/For cases and
    run_scoped_block). `scopes[0]` is the function's own outermost scope
    (params live there); statements.py's `_if`/`_while`/`_for` and
    expressions.py's `_do`/`_if_expr`/`_lambda` push a fresh scope around
    each nested block's own body and pop it on the way out.

    This matters because two *different* wyrm variables can legally share
    a name across nesting levels (an inner `do:`'s own `var acc := nil`
    shadowing an outer block's same-named `acc`, say) while Python's own
    `async def` body has exactly one flat namespace per function - without
    per-block name mangling, the inner declaration would silently
    overwrite the outer one instead of shadowing it, corrupting the outer
    block's value for the rest of its own execution once the inner block
    exits. `declare()` mangles a name only when an *enclosing* scope
    already has a binding for it, so the overwhelmingly common case (no
    name reuse across nesting) still gets the plain, readable `wy_<name>`
    identifier."""

    modctx: ModuleCtx
    lines: List[str] = field(default_factory=list)
    indent: int = 1  # body starts one level in, under `async def ...:`
    loop_depth: int = 0
    is_coroutine: bool = False
    cursor_var: Optional[str] = None  # bound only inside a CoDef body
    scopes: List[Dict[str, str]] = field(default_factory=lambda: [{}])

    # Set only when compiling a single-receiver message/method body (an
    # internal class-body fn, or an external `fn [OneCls] name(...)`) -
    # bare wyrm names matching a slot of that one class resolve through
    # `this_var` instead of an ordinary local/global, mirroring the
    # interpreter's "single ClassInstance receiver's slots are copied into
    # local scope" behavior without needing shared mutable cells: reads
    # AND writes go straight to the instance attribute.
    this_var: Optional[str] = None
    slot_names: Set[str] = field(default_factory=set)  # bare wyrm slot names, this method's class

    # True if this function/coroutine body contains a `defer on error:`
    # anywhere - set once, up front, by functions.py/coroutines.py (see
    # statements.has_error_defer); routes every `return` through the
    # flag-setting form instead of a plain `return <value>` (see
    # statements._return).
    has_error_defer: bool = False

    _tmp: int = 0
    _shadow: int = 0

    def declare(self, name: str) -> str:
        """Registers wyrm name `name` as a local in the *current*
        (innermost) scope, returning the python identifier to use for it.
        If any enclosing scope already binds the same wyrm name, that
        name is still live in Python's flat namespace once this block
        exits (unlike wyrm's own scoping) - so this allocates a fresh,
        mangled python identifier instead of reusing the plain one, to
        keep the two from colliding. Used for `var`/`:=`, a `for` loop's
        own target variable, `with` bindings, and params (each function's
        own outermost scope)."""
        from .naming import py_ident
        base = py_ident(name)
        shadows_outer = any(name in scope for scope in self.scopes[:-1])
        if shadows_outer or name in self.scopes[-1]:
            self._shadow += 1
            python_name = f"{base}__b{self._shadow}"
        else:
            python_name = base
        self.scopes[-1][name] = python_name
        return python_name

    def push_scope(self) -> None:
        self.scopes.append({})

    def pop_scope(self) -> None:
        self.scopes.pop()

    def flat_scope(self) -> Dict[str, str]:
        """All currently-visible wyrm-name -> python-name bindings,
        flattened into one dict (innermost wins) - used to seed a nested
        Lambda/coroutine body's own FnCtx.scopes[0], so its resolve_read
        finds the same python identifiers for whatever it closes over."""
        merged: Dict[str, str] = {}
        for scope in self.scopes:
            merged.update(scope)
        return merged

    def resolve_read(self, name: str) -> str:
        from .naming import py_ident
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        if name in self.slot_names:
            return f"{self.this_var}.{py_ident(name)}"
        if name in self.modctx.import_bindings:
            return self.modctx.import_bindings[name]
        return py_ident(name)

    def resolve_write_target(self, name: str) -> str:
        """Like resolve_read, but never resolves through an import binding
        - reassigning an imported name isn't meaningful (imports bind
        read-only references), so a target with that name falls through to
        an ordinary local/global write, same as a name nothing bound at
        all would."""
        from .naming import py_ident
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        if name in self.slot_names:
            return f"{self.this_var}.{py_ident(name)}"
        return py_ident(name)

    def new_tmp(self, prefix: str = "_t") -> str:
        self._tmp += 1
        return f"{prefix}{self._tmp}"

    def emit(self, text: str = ""):
        self.lines.append(("    " * self.indent) + text if text else "")

    def emit_block(self, text: str):
        for line in text.splitlines():
            self.emit(line)

    def hoist(self, expr_str: str, prefix: str = "_t") -> str:
        """Emits `tmp = <expr_str>` as a preceding statement and returns
        `tmp` - used whenever a construct needs multi-statement expansion
        (Catch, Try, later stages) but appears in expression position."""
        tmp = self.new_tmp(prefix)
        self.emit(f"{tmp} = {expr_str}")
        return tmp

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"
