"""Exercises wypoc/wys.py: dumping a compiled ast.Program to its canonical
.wys text and loading it back, decoupled from actually running it - and
wyrm_eval_parse_tree.expand_decorators, the "decorators run at compile time"
pass dumps() requires first."""
import io
import sys

import pytest

from wypoc import ast_nodes as ast
from wypoc import wys
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import (
    Scope, eval_program, expand_decorators, populate_globals,
)


def _ctx():
    ctx = Scope()
    populate_globals(ctx)
    return ctx


def _run(src: str) -> str:
    """`src` evaluated with stdout captured, as text printed."""
    ctx = _ctx()
    tree = parse(src)
    fake = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = fake
    try:
        eval_program(tree, ctx)
    finally:
        sys.stdout = old_stdout
    return fake.getvalue()


def _run_tree(tree: ast.Program) -> str:
    ctx = _ctx()
    fake = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = fake
    try:
        eval_program(tree, ctx)
    finally:
        sys.stdout = old_stdout
    return fake.getvalue()


SRC = """
fn fib(n) {
    if n < 2 {
        return n
    }
    return fib(n - 1) + fib(n - 2)
}

var i = 0
while i < 6 {
    print(fib(i))
    i = i + 1
}

if fib(6) > 5 {
    print("big")
} else {
    print("small")
}
"""


def test_dumps_round_trips_through_loads():
    tree = parse(SRC)
    expand_decorators(tree, _ctx())
    text = wys.dumps(tree)
    loaded = wys.loads(text)
    assert _run_tree(loaded) == _run(SRC)


def test_dumps_output_starts_with_program_form():
    tree = parse(SRC)
    expand_decorators(tree, _ctx())
    assert wys.dumps(tree).startswith("$['program, [")


def test_dumps_spells_strings_as_double_quoted_literals():
    """The wire value carries a string's raw characters as a Python str, and
    _to_str-style display would single-quote it for a human (`'hi'`) - not
    valid wyrm source. dumps() must spell it the way the tokenizer expects a
    string literal, or loads() couldn't read it back."""
    tree = parse('print("hi there")')
    expand_decorators(tree, _ctx())
    text = wys.dumps(tree)
    assert '"hi there"' in text
    assert "'hi there'" not in text


def test_loads_rejects_non_program_expression():
    with pytest.raises(wys.WysError):
        wys.loads("$['int, 1]")


def test_loads_rejects_malformed_text():
    with pytest.raises(wys.WysError):
        wys.loads("not an expression at all {{{")


def test_dumps_refuses_a_raw_decorator():
    """dumps() is for a fully-compiled unit - sexpr.py itself happily
    encodes a raw `'decorated` node (that's what lets an outer decorator
    inspect an unexpanded inner one), so dumps() has to refuse it itself: a
    tree expand_decorators hasn't run over yet can't cross."""
    tree = parse("@__identity fn f() { return 1 }")
    with pytest.raises(wys.WysError):
        wys.dumps(tree)


def test_expand_decorators_removes_every_decorated_node():
    tree = parse("@__identity fn f() { return 1 }\nvar x = @__identity f()")
    expand_decorators(tree, _ctx())
    assert not any(isinstance(n, ast.Decorated) for stmt in tree.body for n in stmt.walk())
    wys.dumps(tree)  # doesn't raise


def test_expand_decorators_reaches_nested_decorators():
    """A decorator doesn't have to sit at the top level - one inside a
    function body (never called here) must still expand, since dumps() has
    to see the whole tree free of them regardless of what a particular run
    would have reached."""
    tree = parse(
        "fn outer() {\n"
        "    var y = @__identity 1\n"
        "    return y\n"
        "}\n"
    )
    expand_decorators(tree, _ctx())
    assert not any(isinstance(n, ast.Decorated) for stmt in tree.body for n in stmt.walk())
    text = wys.dumps(tree)
    assert "decorat" not in text


def test_expand_decorators_runs_imports_for_static_decorators():
    """A decorator reachable only through `import static` needs that import
    to actually run before it expands - expand_decorators executes a
    top-level import for real rather than just walking over it, exactly
    like ordinary execution would by the time it reaches the decorator.
    `decolib`'s `unchanged` decorator (samples/decolib.wy) answers `this`
    untouched, so this only has to prove the module loaded far enough for
    the decorator to be reachable at all, not what it did."""
    tree = parse("import static decolib\n\n@unchanged fn f() { return 1 }\n")
    ctx = _ctx()
    expand_decorators(tree, ctx)
    assert not any(isinstance(n, ast.Decorated) for stmt in tree.body for n in stmt.walk())
    text = wys.dumps(tree)
    assert "'f" in text
