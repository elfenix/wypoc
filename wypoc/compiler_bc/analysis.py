"""Static questions about a body of code, asked before any of it is lowered.

Three of them, all answered by walking the tree:

* what names does this scope **declare** (so slots can be assigned in one
  pass, before the first instruction is emitted),
* what names does a nested function read that it does not declare itself -
  its **free** names, which become its captures,
* what names are **assigned** anywhere in or below a scope, which is what
  decides whether a captured variable needs a cell (spec 8.3).

The walk stops at a function boundary where scoping does: a nested `fn`,
lambda or `defer` block has a scope of its own, and its body is examined
separately.  A `do:` block is *not* a boundary here - it is inlined into the
enclosing frame, so the names it declares are the enclosing frame's to
allocate.
"""

from wypoc import ast_nodes as ast

# Constructs that introduce a scope of their own, and so end the walk.
# `Defer` is one of them: its block is compiled as a closure, so what it
# reads it captures, and what it assigns it assigns through a cell.
SCOPE_BOUNDARIES = (ast.FnDef, ast.CoDef, ast.Lambda, ast.ClassDef, ast.Defer)


def walk_scope(node):
    """`node` and every descendant in the same scope, boundaries included but
    not entered."""
    yield node
    if isinstance(node, SCOPE_BOUNDARIES):
        return
    for child in node.children():
        yield from walk_scope(child)


def walk_body(body):
    for node in body:
        yield from walk_scope(node)


def declared_names(body):
    """The names `body` introduces, in source order and without duplicates.

    Declarations are `var`/`:=` targets, `for` loop variables, and plain
    function definitions (whose name binds in the scope that contains them).
    A dispatched `fn [T] name` is not one - it declares a method, not a name.
    """
    names = {}
    for node in walk_body(body):
        if isinstance(node, ast.VarDecl):
            for target in node.targets:
                if isinstance(target, ast.VarTarget):
                    names.setdefault(target.name, None)
        elif isinstance(node, ast.For):
            names.setdefault(node.var, None)
        elif isinstance(node, (ast.FnDef, ast.CoDef)) and node.name:
            # `fn [T] name` binds nothing: it registers a method, reached by
            # sending `name` rather than by naming it (spec 7.2).
            if not node.class_target:
                names.setdefault(node.name, None)
    return list(names)


def block_bodies(node):
    """The statement lists `node` opens as scopes of its own (spec: "declaring
    a name visible from an enclosing scope … shadows it for the duration of
    the inner scope").

    A `do:`, a loop body, an `if`/`elif`/`else` arm: each is a scope, so a
    `var` inside one is not the enclosing block's and does not overwrite an
    outer name it happens to repeat. Function-shaped scopes are not here -
    they are SCOPE_BOUNDARIES, compiled as frames of their own.
    """
    if isinstance(node, ast.If):
        yield node.body
        for clause in node.elifs:
            yield clause.body
        if node.orelse:
            yield node.orelse
    elif isinstance(node, (ast.While, ast.Do)):
        yield node.body
    elif isinstance(node, ast.For):
        yield node.body
        if node.orelse:
            yield node.orelse


def walk_block(node):
    """`node` and every descendant in the *same block scope* - stopping at a
    function boundary as walk_scope does, and also at any nested block body,
    which is a scope of its own."""
    yield node
    if isinstance(node, SCOPE_BOUNDARIES):
        return
    nested = {id(stmt) for body in block_bodies(node) for stmt in body}
    for child in node.children():
        if id(child) not in nested:
            yield from walk_block(child)


def own_declared_names(body):
    """`declared_names`, but only the names this block declares itself.

    A `var` inside a nested `do:`/`if`/loop body belongs to that block, not
    to this one - see block_scopes, which allocates those separately. A
    `for` variable belongs to its loop rather than to the block the loop
    sits in (spec: "after the loop statement completes, the loop variable is
    out of scope"), so it is not here either.
    """
    names = {}
    for node in body:
        for inner in walk_block(node):
            if isinstance(inner, ast.VarDecl):
                for target in inner.targets:
                    if isinstance(target, ast.VarTarget):
                        names.setdefault(target.name, None)
            elif isinstance(inner, (ast.FnDef, ast.CoDef)) and inner.name:
                if not inner.class_target:
                    names.setdefault(inner.name, None)
    return list(names)


def block_scopes(body, enclosing=frozenset()):
    """Every scope nested inside `body`, outermost first, as
    `(owner, [names], enclosing names)`.

    `owner` is the statement list the scope is - which is what identifies it
    when that list is compiled - except for a `for`, whose owner is the loop
    node itself: its variable is in scope for the body *and* the else clause
    (spec: "within the else clause, the loop variable remains bound"), so the
    loop, not either list, is what owns it.

    `enclosing` is what scopes *containing* this one bind, so a caller can
    tell a declaration that shadows from one that merely repeats a sibling's
    name. Siblings do not see each other, which is why this recurses rather
    than accumulating down a flat list.
    """
    scopes = []
    for node in body:
        for inner in walk_block(node):
            loop = enclosing
            if isinstance(inner, ast.For):
                scopes.append((inner, [inner.var], enclosing))
                loop = enclosing | {inner.var}
            for nested in block_bodies(inner):
                names = own_declared_names(nested)
                scopes.append((nested, names, loop))
                scopes.extend(block_scopes(nested, loop | set(names)))
    return scopes


def free_names(params, body):
    """The names `body` reads without declaring, in first-encounter order.

    A doubly-nested function's own free names pass through: a closure two
    levels down still has to reach its variable, and the frame in between is
    what hands it over.
    """
    bound = set(params) | set(declared_names(body))
    used = {}

    def note(name):
        if name not in bound:
            used.setdefault(name, None)

    for node in walk_body(body):
        if isinstance(node, ast.Name):
            note(node.id)
        elif isinstance(node, ast.NameTarget):
            note(node.name)
        elif isinstance(node, ast.AttrTarget) and isinstance(node.base, str):
            note(node.base)
        elif isinstance(node, (ast.FnDef, ast.CoDef, ast.Lambda)):
            for name in free_names(_param_names(node.params), node.body):
                note(name)
        elif isinstance(node, ast.Defer):
            for name in free_names((), node.body):
                note(name)
    return list(used)


def assigned_names(body):
    """Every name assigned anywhere in or below `body`.

    This one deliberately does enter nested functions: a `defer` block or a
    closure writing to an enclosing variable is exactly what makes that
    variable need a cell.  It over-approximates - an inner function that
    declares and assigns its *own* `total` marks the outer `total` too - which
    costs a cell that is never shared, never correctness.
    """
    names = set()
    for statement in body:
        for node in statement.walk():
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.NameTarget):
                        names.add(target.name)
    return names


def _param_names(params):
    return [
        param.name
        for param in params
        if isinstance(param, (ast.Param, ast.VarPositional, ast.VarKeyword))
    ]


def cell_names(params, body):
    """The names in this frame that must live in a cell (spec 8.3).

    A variable needs one when it is both captured by a nested scope and
    assigned somewhere: the capture copies a register, so without a shared
    box the two frames would drift apart after the first write.
    """
    captured = set()
    for node in walk_body(body):
        if isinstance(node, (ast.FnDef, ast.CoDef, ast.Lambda)):
            captured.update(free_names(_param_names(node.params), node.body))
        elif isinstance(node, ast.Defer):
            captured.update(free_names((), node.body))
    if not captured:
        return set()
    mine = set(params) | set(declared_names(body))
    return captured & mine & assigned_names(body)
