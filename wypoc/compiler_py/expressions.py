"""Expression codegen: compile_expr(fnctx, node) -> a Python expression
string. One handler per supported ast.Expr node kind, registered into
EXPR_HANDLERS. Unlike compiler_c, a call can appear directly in expression
position with no hoisting needed - `await foo(x)` is itself a valid Python
expression - so this module is considerably simpler than its C counterpart.
"""
from typing import Dict

from wypoc import ast_nodes as ast

from . import literals
from .errors import err
from .handlers import EXPR_HANDLERS
from .naming import py_ident

# wyrm builtin name -> (python callable the engine/builtins provide, is_async)
# Grown incrementally; anything not listed here that isn't a locally-defined
# name falls back to being treated as an ordinary wy_-prefixed reference.
SYNC_BUILTINS = {
    "print": "engine.wy_print",
    "error": "engine.error",
    "len": "engine.wy_len",
    "cons": "engine.wy_cons",
    "pair": "engine.wy_cons",
    "car": "engine.wy_car",
    "cdr": "engine.wy_cdr",
    "reverse": "engine.wy_reverse",
    "__open": "engine.wy_open",
    "__read": "engine.wy_read",
    "__write": "engine.wy_write",
    "__lseek": "engine.wy_lseek",
    "__dup2": "engine.wy_dup2",
    "__close": "engine.wy_close",
    "__flush": "engine.wy_flush",
    "str": "engine.wy_str",
    "int": "engine.wy_int",
    "float": "engine.wy_float",
    "bool": "engine.wy_bool",
    "sym": "engine.wy_sym",
}

# A handful of bare names that are language-level literals, not variables -
# resolved before falling through to fnctx.resolve_read. `nil` maps onto
# plain Python None rather than a dedicated sentinel: nothing in this
# backend yet represents wyrm's cons/pair-list value space (see literals.py
# and the package docstring's known gaps), and every current use of `nil`
# (e.g. sending it into a coroutine to signal "no more values", checked
# with `is int`/`is nil`) works identically against None.
NAME_LITERALS = {
    "nil": "None",
    "__STDIN": "engine.WY_STDIN",
    "__STDOUT": "engine.WY_STDOUT",
    "__STDERR": "engine.WY_STDERR",
}

# wyrm primitive type name -> the isinstance() check its TypeCheck codegen
# needs. int/uint both use Python int; bool is excluded explicitly since
# Python's bool is a subclass of int and wyrm's `is int`/`is bool` are
# distinct checks the interpreter also keeps apart.
_PRIMITIVE_TYPE_CHECKS = {
    "bool": "isinstance({v}, bool)",
    "int": "(isinstance({v}, int) and not isinstance({v}, bool))",
    "uint": "(isinstance({v}, int) and not isinstance({v}, bool))",
    "float": "isinstance({v}, float)",
    "str": "isinstance({v}, str)",
    "error": "engine.is_error({v})",
}

BINOPS = {
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%", "**": "**",
    "&": "&", "|": "|", "^": "^", "<<": "<<", ">>": ">>",
    "<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "==", "!=": "!=",
    "and": "and", "or": "or", "in": "in",
}

UNARY_OPS = {"neg": "-", "pos": "+", "inv": "~", "not": "not "}


def compile_expr(fnctx, node) -> str:
    handler = EXPR_HANDLERS.get(node)
    if handler is None:
        err("expression not supported by --compile-py", node)
    return handler(fnctx, node)


def compile_args(fnctx, args) -> str:
    parts = []
    for a in args:
        if isinstance(a, ast.Kwarg):
            parts.append(f"{py_ident(a.name)}={compile_expr(fnctx, a.value)}")
        elif isinstance(a, ast.SpreadPos):
            parts.append(f"*{compile_expr(fnctx, a.value)}")
        elif isinstance(a, ast.SpreadKw):
            parts.append(f"**{compile_expr(fnctx, a.value)}")
        else:
            parts.append(compile_expr(fnctx, a))
    return ", ".join(parts)


@EXPR_HANDLERS.register(ast.Num)
def _num(fnctx, node):
    return repr(literals.num_value(node.value))


@EXPR_HANDLERS.register(ast.Str)
def _str(fnctx, node):
    return repr(literals.str_value(node.value))


@EXPR_HANDLERS.register(ast.Char)
def _char(fnctx, node):
    return repr(literals.char_value(node.value))


@EXPR_HANDLERS.register(ast.Bool)
def _bool(fnctx, node):
    return "True" if node.value else "False"


@EXPR_HANDLERS.register(ast.Name)
def _name(fnctx, node):
    if node.id in NAME_LITERALS:
        return NAME_LITERALS[node.id]
    if node.id in SYNC_BUILTINS:
        return SYNC_BUILTINS[node.id]
    return fnctx.resolve_read(node.id)


@EXPR_HANDLERS.register(ast.ThisRef)
def _this_ref(fnctx, node):
    if fnctx.this_var is None:
        err("'this' used outside a message/method body", node)
    return fnctx.this_var


@EXPR_HANDLERS.register(ast.Attr)
def _attr(fnctx, node):
    obj = compile_expr(fnctx, node.obj)
    if node.name == "value":
        # `.value` is only special on a Cursor (a running/finished
        # coroutine) - everything else with a slot literally named
        # "value" means that slot, same ambiguity the interpreter itself
        # resolves with a runtime isinstance check (see its ast.Attr case
        # in eval_expr), not a compile-time one, since we don't know an
        # expression's type statically.
        return f"engine.wy_attr_value({obj})"
    return f"({obj}).{py_ident(node.name)}"


@EXPR_HANDLERS.register(ast.Scope)
def _scope(fnctx, node):
    """`a::b` - a static namespace lookup, not a runtime message/property
    access. `node.obj` compiles to whatever Python reference names the
    bound module/package (see imports.py - import_bindings maps a bound
    root/leaf name straight to its dotted `wyrm...` reference), and `::b`
    is then plain Python attribute access on it - which works transitively
    for a whole `a::b::c` chain because importing `wyrm.a.b.c` anywhere in
    the program already makes Python set `b` as an attribute of `wyrm.a`
    and `c` as an attribute of `wyrm.a.b`, the same way `import os.path`
    makes `os.path` reachable through plain `os` afterward."""
    return f"({compile_expr(fnctx, node.obj)}).{py_ident(node.name)}"


@EXPR_HANDLERS.register(ast.Message)
def _message(fnctx, node):
    obj = compile_expr(fnctx, node.obj)
    bound = f'dispatch({obj}, {node.name!r}, _TABLE)'
    if node.args is None:
        return bound
    return f"(await {bound}({compile_args(fnctx, node.args)}))"


@EXPR_HANDLERS.register(ast.MessageTupleExpr)
def _message_tuple(fnctx, node):
    receivers = ", ".join(compile_expr(fnctx, item) for item in node.items)
    bound = f'dispatch(engine.Receivers([{receivers}]), {node.name!r}, _TABLE)'
    if node.args is None:
        return bound
    return f"(await {bound}({compile_args(fnctx, node.args)}))"


@EXPR_HANDLERS.register(ast.TypeCheck)
def _type_check(fnctx, node):
    """`value is T | U`. Deliberately re-evaluates `compile_expr(node.
    value)` once per type in the union rather than hoisting it into a
    temp: hoisting would need a preceding statement, which is unsafe for a
    TypeCheck appearing in a `while` condition (recomputed every
    iteration) until stage 6's control-flow work gives conditions their
    own hoisted-recompute handling (see compiler_c's DESIGN.md for the
    analogous C problem). Fine for the plain-Name checks every current
    caller uses; a TypeCheck on an expression with side effects would
    double them, a known v1 gap."""
    from .naming import fields_class_name
    checks = []
    for t in node.types:
        if len(t.parts) != 1:
            err("qualified type paths not yet supported by --compile-py's TypeCheck", node)
        name = t.parts[0]
        value = compile_expr(fnctx, node.value)
        if name in _PRIMITIVE_TYPE_CHECKS:
            checks.append(_PRIMITIVE_TYPE_CHECKS[name].format(v=value))
        else:
            checks.append(f"isinstance({value}, {fields_class_name(name)})")
    return "(" + " or ".join(checks) + ")"


@EXPR_HANDLERS.register(ast.Array)
def _array(fnctx, node):
    return "[" + ", ".join(compile_expr(fnctx, item) for item in node.items) + "]"


@EXPR_HANDLERS.register(ast.Dict)
def _dict(fnctx, node):
    entries = ", ".join(
        f"{compile_expr(fnctx, entry.key)}: {compile_expr(fnctx, entry.value)}"
        for entry in node.entries
    )
    return "{" + entries + "}"


@EXPR_HANDLERS.register(ast.Tuple)
def _tuple(fnctx, node):
    items = [compile_expr(fnctx, item) for item in node.items]
    if len(items) == 1:
        return f"({items[0]},)"
    return "(" + ", ".join(items) + ")"


@EXPR_HANDLERS.register(ast.Pair)
def _pair(fnctx, node):
    """`$[a, b, c]` -> a right-to-left cons chain, NIL(None)-terminated -
    matches the interpreter's own ast.Pair evaluation exactly (see
    wyrm_eval_parse_tree.py's eval_expr)."""
    result = "None"
    for item in reversed(node.elements):
        result = f"engine.Pair({compile_expr(fnctx, item)}, {result})"
    return result


@EXPR_HANDLERS.register(ast.Symbol)
def _symbol(fnctx, node):
    return f"engine.Symbol({node.name!r})"


@EXPR_HANDLERS.register(ast.Index)
def _index(fnctx, node):
    """`obj[index]` - routed through engine.wy_index rather than plain
    Python subscription, so an out-of-range/missing index answers a
    catchable error value instead of raising (matches the interpreter -
    see wy_index's docstring)."""
    obj = compile_expr(fnctx, node.obj)
    index = compile_expr(fnctx, node.index)
    return f"engine.wy_index({obj}, {index})"


@EXPR_HANDLERS.register(ast.Try)
def _try(fnctx, node):
    """`try expr`: propagate-on-error - the enclosing function returns
    immediately with the same error value rather than continuing. Like
    Catch, hoists into a preceding `if`, so not yet safe inside a `while`
    condition."""
    tmp = fnctx.hoist(compile_expr(fnctx, node.value), prefix="_try")
    fnctx.emit(f"if is_error({tmp}):")
    fnctx.indent += 1
    fnctx.emit(f"return {tmp}")
    fnctx.indent -= 1
    return tmp


@EXPR_HANDLERS.register(ast.Lambda)
def _lambda(fnctx, node):
    """`fn(params) { body }` used as a value. Every wyrm function compiles
    to an async def (see the package docstring), and Python has no
    anonymous-async-function syntax, so a lambda is hoisted out into a
    synthesized nested `async def` definition emitted right before the
    statement it appears in - the expression itself then evaluates to a
    plain reference to that nested function's name, which an ordinary
    Python closure captures from the enclosing scope with no extra
    plumbing (unlike this backend's class instances, which carry their
    slots explicitly).

    When the lambda is built inside a while/for loop, "the enclosing
    scope" includes names that are only a single, reused Python binding
    across every iteration (see context.FnCtx.loop_scope_floor) - closing
    over one of those directly would give every lambda built across the
    loop's iterations the same, Python-late-bound value (whatever that
    variable holds by the time the lambda is actually called) rather than
    the value live in the iteration that built it. Wrapping the nested
    `async def` in a plain sync factory whose parameters default to those
    names (`def _capture1(wy_i=wy_i): ...`) snapshots them the moment the
    factory itself runs - once per iteration, exactly when the wyrm-level
    closure is created - the same trick a human writes by hand for this
    exact problem in Python."""
    from .context import FnCtx
    from .functions import compile_params
    from .statements import compile_block

    name = fnctx.new_tmp("_lambda")
    captures: Dict[str, str] = {}
    if fnctx.loop_scope_floor:
        for scope in fnctx.scopes[fnctx.loop_scope_floor[0]:]:
            captures.update(scope)

    if captures:
        factory = fnctx.new_tmp("_capture")
        params = ", ".join(f"{py_name}={py_name}" for py_name in captures.values())
        fnctx.emit(f"def {factory}({params}):")
        fnctx.indent += 1

    inner = FnCtx(modctx=fnctx.modctx, this_var=fnctx.this_var,
                  slot_names=fnctx.slot_names, scopes=[fnctx.flat_scope()],
                  indent=fnctx.indent + 1, is_coroutine=fnctx.is_coroutine,
                  cursor_var=fnctx.cursor_var)
    param_str = compile_params(inner, node.params)
    fnctx.emit(f"async def {name}({param_str}):")
    compile_block(inner, node.body)
    fnctx.lines.extend(inner.lines)

    if captures:
        fnctx.emit(f"return {name}")
        fnctx.indent -= 1
        call = fnctx.new_tmp("_lambda")
        fnctx.emit(f"{call} = {factory}()")
        return call
    return name


def _last_stmt_value(fnctx, last) -> str:
    """The Python expression for `last`'s own value, when `last` is a
    block's final statement and the block is used as a value - mirrors
    eval_block's "value = eval_stmt(stmt, ctx)" rule, where *every*
    statement (not just an expression statement) has a value, since
    eval_stmt itself returns one. Only ExprStmt and If are recognized as
    value-producing here (the two shapes decorator-macro-expanded code
    and hand-written `do:`/`if`-as-expression blocks actually produce);
    anything else keeps this POC's documented v1 simplification (see
    samples/eval_defer_with_do.wy's header comment) - it still runs for
    effect, but answers None rather than its real value."""
    if isinstance(last, ast.ExprStmt):
        return compile_expr(fnctx, last.value)
    if isinstance(last, ast.If):
        return compile_expr(fnctx, last)
    from .statements import compile_stmt
    compile_stmt(fnctx, last)
    return "None"


@EXPR_HANDLERS.register(ast.Do)
def _do(fnctx, node):
    """`do:` block used as an expression - a fresh child scope, matching
    the interpreter's own `run_scoped_block(node.body, ctx.child())` (see
    context.FnCtx's docstring). See _last_stmt_value for which kinds of
    trailing statement thread their value through."""
    from .statements import compile_stmt

    if not node.body:
        return "None"
    fnctx.push_scope()
    *init_stmts, last = node.body
    for stmt in init_stmts:
        compile_stmt(fnctx, stmt)
    value = _last_stmt_value(fnctx, last)
    fnctx.pop_scope()
    return value


def _compile_value_block(fnctx, tmp, body):
    """Like _do's body handling, but assigning into a pre-hoisted `tmp`
    instead of returning an expression string directly - shared by
    _if_expr's branches, each of which needs its own assignment into the
    same outer temp rather than each becoming its own expression. Also
    pushes its own fresh child scope, one per branch (see _do)."""
    from .statements import compile_stmt

    fnctx.push_scope()
    if not body:
        fnctx.emit("pass")
        fnctx.pop_scope()
        return
    *init_stmts, last = body
    for stmt in init_stmts:
        compile_stmt(fnctx, stmt)
    fnctx.emit(f"{tmp} = {_last_stmt_value(fnctx, last)}")
    fnctx.pop_scope()


@EXPR_HANDLERS.register(ast.If)
def _if_expr(fnctx, node):
    """`if cond {a} else {b}` used as a value (e.g. `x := if c {1} else
    {2}`, or - as decorator-macro-expanded code sometimes produces - an
    If as a Do block's own trailing statement). Statement-position `If`
    never reaches here (statements.compile_stmt dispatches ast.If
    directly, ahead of any registry lookup) - this only fires when an If
    node sits in expression position. Same per-branch "only a directly
    trailing expression statement threads its value through"
    simplification as _do/_compile_value_block; a branch that isn't taken
    (or no branch matches at all) leaves `tmp` at its pre-hoisted None,
    matching the interpreter's own "no branch ran" value."""
    tmp = fnctx.hoist("None", prefix="_ifv")
    fnctx.emit(f"if {compile_expr(fnctx, node.cond)}:")
    fnctx.indent += 1
    _compile_value_block(fnctx, tmp, node.body)
    fnctx.indent -= 1
    for clause in node.elifs:
        fnctx.emit(f"elif {compile_expr(fnctx, clause.cond)}:")
        fnctx.indent += 1
        _compile_value_block(fnctx, tmp, clause.body)
        fnctx.indent -= 1
    if node.orelse is not None:
        fnctx.emit("else:")
        fnctx.indent += 1
        _compile_value_block(fnctx, tmp, node.orelse)
        fnctx.indent -= 1
    return tmp


@EXPR_HANDLERS.register(ast.Catch)
def _catch(fnctx, node):
    """`value catch handler`: value's own compile_expr call happens before
    the hoist (see FnCtx.hoist), so a Catch that's itself the value of an
    outer Catch/Message compiles fine; `handler` may be an ordinary
    expression (the fallback value) or a bare `return X` (an early exit
    instead of a value) - ast_nodes.Catch's `handler: Union[Expr,
    Return]`. Like TypeCheck, this hoists into a preceding `if` statement,
    so it's not yet safe inside a `while` condition (stage 6)."""
    tmp = fnctx.hoist(compile_expr(fnctx, node.value), prefix="_catch")
    fnctx.emit(f"if is_error({tmp}):")
    fnctx.indent += 1
    if isinstance(node.handler, ast.Return):
        value = compile_expr(fnctx, node.handler.value) if node.handler.value is not None else "None"
        fnctx.emit(f"return {value}")
    else:
        fnctx.emit(f"{tmp} = {compile_expr(fnctx, node.handler)}")
    fnctx.indent -= 1
    return tmp


@EXPR_HANDLERS.register(ast.UnaryOp)
def _unary(fnctx, node):
    op = UNARY_OPS.get(node.op)
    if op is None:
        err(f"unary operator {node.op!r} not supported by --compile-py", node)
    return f"({op}{compile_expr(fnctx, node.operand)})"


@EXPR_HANDLERS.register(ast.BinOp)
def _binop(fnctx, node):
    if node.op == "<=>":
        left = compile_expr(fnctx, node.left)
        right = compile_expr(fnctx, node.right)
        return f"(({left} > {right}) - ({left} < {right}))"
    if node.op == "/":
        return f"engine.wy_div({compile_expr(fnctx, node.left)}, {compile_expr(fnctx, node.right)})"
    if node.op == "%":
        return f"engine.wy_mod({compile_expr(fnctx, node.left)}, {compile_expr(fnctx, node.right)})"
    op = BINOPS.get(node.op)
    if op is None:
        err(f"operator {node.op!r} not supported by --compile-py", node)
    return f"({compile_expr(fnctx, node.left)} {op} {compile_expr(fnctx, node.right)})"


def _call_next(fnctx, node):
    if len(node.args) != 1:
        err("next() takes exactly one argument", node)
    return f"(await ({compile_expr(fnctx, node.args[0])}).next())"


def _call_send(fnctx, node):
    if len(node.args) != 2:
        err("send() takes exactly two arguments", node)
    obj = compile_expr(fnctx, node.args[0])
    value = compile_expr(fnctx, node.args[1])
    return f"(await ({obj}).send({value}))"


# `next`/`send` aren't ordinary functions dispatched with `await
# name(...)` - they're per-Cursor method calls (see engine.Cursor), so
# each gets its own small rewrite instead of a SYNC_BUILTINS entry.
CALL_REWRITES = {"next": _call_next, "send": _call_send}


def _is_native_block_call(node) -> bool:
    return (
        isinstance(node.func, ast.Scope)
        and isinstance(node.func.obj, ast.Name)
        and node.func.obj.id == "native"
        and node.func.name == "block"
    )


@EXPR_HANDLERS.register(ast.Call)
def _call(fnctx, node):
    if _is_native_block_call(node):
        err("native::block() (the C backend's escape hatch) is not "
            "supported by --compile-py", node)
    if isinstance(node.func, ast.Name) and node.func.id in CALL_REWRITES:
        return CALL_REWRITES[node.func.id](fnctx, node)
    callee_is_builtin = isinstance(node.func, ast.Name) and node.func.id in SYNC_BUILTINS
    callee = compile_expr(fnctx, node.func)
    args = compile_args(fnctx, node.args)
    call = f"{callee}({args})"
    return call if callee_is_builtin else f"(await {call})"


@EXPR_HANDLERS.register(ast.Yield)
def _yield(fnctx, node):
    if fnctx.cursor_var is None:
        err("'yield' used outside a coroutine body", node)
    if node.from_:
        return _compile_yield_from(fnctx, node)
    value = compile_expr(fnctx, node.value) if node.value is not None else "None"
    return f"(await {fnctx.cursor_var}.yield_({value}))"


def _compile_yield_from(fnctx, node):
    """`yield from sub`: forwards every value `sub` yields (relaying
    whatever's sent back in) until `sub` finishes, then evaluates to
    `sub`'s own return value - mirrors the interpreter's _yield_from
    exactly (see wyrm_eval_parse_tree.py). Needs a loop, so - like Catch/
    Try will in stage 6 - it hoists: emits statements via fnctx.emit and
    returns a temp holding the final value, rather than a single
    expression string.

    Drives the delegate with Cursor._advance (the raw primitive), not the
    public .next()/.send() - those discard the delegate's actual
    completion value in favor of a StopIteration sentinel (see Cursor.next
    ), but `yield from` needs that real value, both to relay non-final
    yields and to become its own expression's result once the delegate
    finishes (see _yield_from in wyrm_eval_parse_tree.py). _advance
    already behaves like a bare "start" when called on a not-yet-started
    cursor regardless of what's passed as send_value, so driving with it
    uniformly needs no separate first-call special case."""
    inner_expr = compile_expr(fnctx, node.value)
    inner = fnctx.new_tmp("_yf_inner")
    sent = fnctx.new_tmp("_yf_sent")
    value = fnctx.new_tmp("_yf_value")
    fnctx.emit(f"{inner} = {inner_expr}")
    fnctx.emit(f"{sent} = None")
    fnctx.emit("while True:")
    fnctx.indent += 1
    fnctx.emit(f"{value} = await {inner}._advance({sent})")
    fnctx.emit(f"if {inner}._finished:")
    fnctx.indent += 1
    fnctx.emit("break")
    fnctx.indent -= 1
    fnctx.emit(f"{sent} = await {fnctx.cursor_var}.yield_({value})")
    fnctx.indent -= 1
    return value
