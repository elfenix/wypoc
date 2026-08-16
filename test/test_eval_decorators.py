"""Runs samples/decorators.wy - the decorator conformance script - and checks
what it printed.

The sample is the interesting part: it drives every node kind through
`@__identity` (a full encode/decode round trip), then uses decorators written
in wyrm (samples/decolib.wy, reached through `import static`) to rewrite
bodies, read signatures, splice templates, and answer objects with a
`__sexpr` hook. This file is the assertion side of that; the tests below it
cover the failure modes the sample can't, since a sample that raised wouldn't
finish printing.
"""
import io
import sys

import pytest

from conftest import SAMPLES_DIR, eval_sample
from wypoc import wyrm_builtins, wyrm_io
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import (
    DecoratorError, Scope, clear_module_cache, eval_program, populate_globals,
)


@pytest.fixture(scope="module")
def output():
    """Every line samples/decorators.wy printed, in order. Module-scoped: the
    sample is a single narrative and re-running it per test would re-run
    every decorator too."""
    clear_module_cache()
    fake = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = fake
    wyrm_io._reset_std_handles()
    try:
        eval_sample("decorators.wy")
    finally:
        sys.stdout = old_stdout
        wyrm_io._reset_std_handles()
    return fake.getvalue().splitlines()


@pytest.fixture(scope="module")
def printed(output):
    """The `label: value` lines, as a mapping. The sample prints one per
    kind, which makes a lookup by label far more readable than an index."""
    result = {}
    for line in output:
        if ": " in line:
            label, _, value = line.partition(": ")
            result.setdefault(label.strip(), value)
    return result


# --- the round trip, kind by kind -----------------------------------------

# Every one of these went out as an s-expression and came back as a tree,
# so a matching value is proof the round trip preserved meaning.
ROUND_TRIPPED = {
    "int": "7",
    "float": "2.5",
    "str": "hello",
    "true": "true",
    "false": "false",
    "nil": "nil",
    "sym": "name",
    "list": "[1, 2, 3]",
    "name": "6",
    "binop +": "7",
    "binop -": "5",
    "binop *": "12",
    "binop /": "3.0",
    "binop %": "2",
    "binop **": "36",
    "binop &": "2",
    "binop |": "7",
    "binop ^": "7",
    "binop ==": "true",
    "binop !=": "false",
    "binop <": "true",
    "binop >": "false",
    "binop <=": "true",
    "binop >=": "true",
    "binop <=>": "-1",
    "unop -": "-6",
    "not": "true",
    "and": "false",
    "or": "true",
    "tuple": "(1, 2)",
    "grouped": "7",
    "pairlist": "$[1, 2]",
    "dict": "{'a': 1}",
    "is": "true",
    "do": "42",
    "call": "8",
    "index": "20",
    "attr": "12",
    "msg": "12",
    "mod_get": "<class Circle>",
    "args": "6",
}


@pytest.mark.parametrize("label,expected", ROUND_TRIPPED.items(), ids=list(ROUND_TRIPPED))
def test_expression_kinds_round_trip(printed, label, expected):
    assert printed[label] == expected


DEFINITIONS = {
    "fn": "5",                    # a rebuilt definition still binds its name
    "typed": "15",                # types are carried, and ignored
    "fn braced": "12",            # a brace-delimited body crosses too
    "message": "box 12",          # `fn [Box]` keeps its dispatch type
    "rest params": "4",           # *args survives its own position
    "control": "44",              # while/if/elif/else/for/break/continue
    "failure": "3 true 42",       # defer, defer on error, catch, is, do
    "guarded": "10.0",            # try
    "guarded zero": "divided by zero",   # catch return
    "nested fn": "10",            # a definition nested in a crossing body
    "nested": "5",                # @__dump @__identity - innermost first
    "nested scope": "101",        # a decorated definition inside a function
}


@pytest.mark.parametrize("label,expected", DEFINITIONS.items(), ids=list(DEFINITIONS))
def test_definitions_round_trip(printed, label, expected):
    assert printed[label] == expected


def test_a_static_local_survives_the_round_trip(printed, output):
    """`static seen = 0` keeps its once-per-definition storage after being
    rebuilt, so the second call sees the first call's write."""
    assert printed["bindings"] == "20"
    assert [line for line in output if line.startswith("bindings again")] == [
        "bindings again: 21"
    ]


def test_dump_prints_the_s_expression_it_would_receive(output):
    assert output[0] == "$['int, 41]"
    assert output[1] == "$['str, 'text']"
    assert output[2] == "dumped: 41 text", "and compiles the original tree"


def test_dump_shows_a_signature_with_its_types(output):
    dumped = [line for line in output if line.startswith("$['fn, 'typed")]
    assert dumped == [
        "$['fn, 'typed, [$['type, 'int, [], []]], nil, nil, "
        "[$['param, 'a, $['type, 'int, [], []]], "
        "$['param, 'b, $['type, 'str, [], []]], "
        "$['param, 'c, $['type, 'Circle, ['shapes], []]]], [], "
        "[$['return, $['name, 'a]]]]"
    ]


def test_dump_shows_the_rest_parameter_in_its_own_position(output):
    dumped = [line for line in output if line.startswith("$['fn, 'spread")]
    assert dumped == [
        "$['fn, 'spread, [$['type, 'int, [], []]], $['param, 'others, nil], nil, "
        "[$['param, 'a, $['type, 'int, [], []]]], [], "
        "[$['return, $['name, 'a]]]]"
    ]


# --- decorators written in wyrm -------------------------------------------

WYRM_DECORATORS = {
    "unchanged": "42",                  # answers the tree it was given
    "traced result": "42",              # a println prepended to the body
    "constant": "99",                   # the body replaced outright
    "arity": "3",                       # the signature read, not rewritten
    "param types": "['int', '?', 'Circle']",   # declared types read
    "scaled": "15",                     # an expression rewritten
    "layered": "7",                     # composed with another decorator
    "framed result": "42",              # a template spliced around the body
}


@pytest.mark.parametrize("label,expected", WYRM_DECORATORS.items(),
                         ids=list(WYRM_DECORATORS))
def test_decorators_written_in_wyrm(printed, label, expected):
    assert printed[label] == expected


def test_a_prepended_statement_runs_before_the_body(output):
    assert output.index("traced: entering") < output.index("traced result: 42")


def test_a_template_wraps_the_body_it_spliced(output):
    """`_framed` puts a `defer` before its `...`, so the body runs between
    the template's two prints - splicing, not wrapping."""
    assert [line for line in output if "framed" in line or "template" in line] == [
        "template: entering",
        "framed: body running on 21",
        "template: leaving",
        "framed result: 42",
    ]


def test_a_template_may_be_named_at_the_use_site(output):
    """`@using_template(banner::$ast)` - a `$ast` is as available at
    decoration time as a literal is, so it can be an argument."""
    assert [line for line in output if "banner" in line or "announced" in line] == [
        "banner: open",
        "announced: body",
        "banner: close",
    ]


# --- $ast and the __sexpr hook --------------------------------------------

AST_AND_HOOK = {
    "$ast head": "fn",
    "$ast name": "plain",
    "$ast is a tree": "true",
    "$ast across modules": "fn",
    "hook in class body": "$['int, 7]",
    "hook from outside": "$['int, 10]",
    "hook inherited": "$['int, 10]",
    "no hook is identity": "$['int, 3]",
    "a TreeBase still unwraps": "fn",
    "returned object": "42",
}


@pytest.mark.parametrize("label,expected", AST_AND_HOOK.items(), ids=list(AST_AND_HOOK))
def test_ast_references_and_the_sexpr_hook(printed, label, expected):
    assert printed[label] == expected


# --- the `parse` builtin ---------------------------------------------------
#
# `parse(source)` boxes `source`'s own tree(s) exactly like `$ast` does, so
# `sexpr()` reads them the same way; these exercise it directly rather than
# through samples/decorators.wy, since it isn't part of that narrative.

def _sexpr_of(src: str) -> str:
    """Evaluates `sexpr(parse(src))`, as `display()` would show it - the
    same `$[...]` form samples/decorators.wy's own sexpr assertions use."""
    ctx = run(f'x := sexpr(parse("{src}"))\n')
    return wyrm_builtins.display(ctx["x"].value)


def test_parse_of_one_statement_unboxes_to_its_own_tree():
    assert _sexpr_of("v := 5") == "$['decl, 'v, $['int, 5]]"


def test_parse_of_one_expression_unboxes_to_its_own_tree():
    assert _sexpr_of("1 + 2") == "$['expr_stmt, $['binop, '+, $['int, 1], $['int, 2]]]"


def test_parse_of_several_statements_is_a_list_of_boxed_trees():
    ctx = run('xs := parse("a := 1\\nb := 2")\nx := sexpr(xs[0])\ny := sexpr(xs[1])\n')
    assert isinstance(ctx["xs"].value, list)
    assert len(ctx["xs"].value) == 2
    assert wyrm_builtins.display(ctx["x"].value) == "$['decl, 'a, $['int, 1]]"
    assert wyrm_builtins.display(ctx["y"].value) == "$['decl, 'b, $['int, 2]]"


def test_parse_of_blank_source_is_nil():
    ctx = run('x := parse("   ")\n')
    assert ctx["x"].value is wyrm_builtins.NIL


def test_parse_of_bad_syntax_raises_syntax_error():
    with pytest.raises(SyntaxError):
        run('parse("v := ")\n')


# --- failure modes --------------------------------------------------------

def run(src: str) -> dict:
    """Evaluate `src` as a top-level script, with samples/ as the script
    root so `import static decolib` resolves."""
    ctx = Scope()
    populate_globals(ctx)
    eval_program(parse(src), ctx)
    return ctx


def test_an_unknown_decorator_points_at_import_static():
    with pytest.raises(DecoratorError) as excinfo:
        run("@nosuchthing fn f():\n    pass\n")
    message = str(excinfo.value)
    assert "@nosuchthing" in message
    assert "import static" in message, (
        "the common cause is a module imported without `static`, so say so"
    )


def test_the_error_names_the_decorator_and_the_use_site_line():
    with pytest.raises(DecoratorError) as excinfo:
        run("\n\n@nosuchthing fn f():\n    pass\n")
    assert "@nosuchthing at line 3" in str(excinfo.value)


def test_a_decorator_answering_a_non_tree_is_an_error():
    src = (
        "import static decolib\n"
        "fn [TreeBase] broken():\n"
        "    return 41\n"
        "@broken fn f():\n"
        "    pass\n"
    )
    with pytest.raises(DecoratorError) as excinfo:
        run(src)
    assert "@broken" in str(excinfo.value)
    assert "must be a $[...] list" in str(excinfo.value)


def test_a_decorator_answering_a_statement_in_expression_position_is_an_error():
    src = (
        "fn [TreeBase] stmtish():\n"
        "    return $['pass]\n"
        "x := @stmtish 1\n"
    )
    with pytest.raises(DecoratorError) as excinfo:
        run(src)
    assert "where an expression was required" in str(excinfo.value)


def test_a_decorator_handed_a_tree_that_cannot_cross_names_the_construct():
    src = (
        "fn [TreeBase] any_tree():\n"
        "    return this\n"
        "@any_tree co counter(n):\n"
        "    yield n\n"
    )
    with pytest.raises(DecoratorError) as excinfo:
        run(src)
    assert "a coroutine cannot cross into a decorator yet" in str(excinfo.value)


def test_a_qualified_decorator_name_is_a_syntax_error():
    """A decorator name becomes a message selector, and a selector is never
    a path - so `::` keeps its one meaning."""
    with pytest.raises(SyntaxError):
        parse("@mod::dec fn f():\n    pass\n")


def test_parens_after_the_name_are_always_the_argument_list():
    """`@d (expr)` reads `(expr)` as arguments and then finds no operand;
    parenthesising an operand takes the empty list first."""
    with pytest.raises(SyntaxError):
        parse("x := @d (1 + 2)\n")
    decorated = parse("x := @d() (1 + 2)\n").body[0].values[0]
    assert decorated.decorator.args == []
    assert type(decorated.inner).__name__ == "BinOp"


def test_a_decorator_binds_as_tightly_as_unary_minus():
    """`@d f(x) + 1` decorates `f(x)`, not the sum."""
    value = parse("x := @d y + 1\n").body[0].values[0]
    assert type(value).__name__ == "BinOp"
    assert type(value.left).__name__ == "Decorated"


def test_stacked_decorators_nest_right_associatively():
    inner = parse("x := @a @b y\n").body[0].values[0]
    assert inner.decorator.name == "a"
    assert inner.inner.decorator.name == "b"
    assert type(inner.inner.inner).__name__ == "Name"


def test_the_rewrite_happens_once_per_decorated_node():
    """The interpreter's stand-in for "decorators run at compile time": a
    decorated definition nested in a function is rewritten on the first call
    and reused on every one after."""
    src = (
        "counter := [0]\n"
        "fn [TreeBase] counted():\n"
        "    counter ! append(1)\n"
        "    return this\n"
        "fn outer():\n"
        "    @counted fn inner(x):\n"
        "        return x\n"
        "    return inner(1)\n"
        "outer()\n"
        "outer()\n"
        "outer()\n"
    )
    ctx = run(src)
    assert len(ctx["counter"].value) == 2, (
        "the decorator ran once (the list starts with one element)"
    )


def test_import_static_adopts_the_modules_messages():
    """A plain import leaves a decorator unreachable - it is a selector, not
    a path - which is the whole reason `static` exists."""
    plain = "import decolib\n@unchanged fn f():\n    pass\n"
    with pytest.raises(DecoratorError):
        run(plain)
    clear_module_cache()
    run("import static decolib\n@unchanged fn f():\n    pass\n")


def test_a_local_definition_wins_over_an_adopted_one():
    src = (
        "fn [TreeBase] unchanged():\n"
        "    return $['fn, 'f, [], nil, nil, [], [], [$['return, $['int, 7]]]]\n"
        "import static decolib\n"
        "@unchanged fn f():\n"
        "    return 0\n"
    )
    ctx = run(src)
    from wypoc.wyrm_eval_parse_tree import call_value

    assert call_value(ctx["f"].value, [], {}) == 7


def test_script_root_makes_a_neighbouring_module_importable(tmp_path, monkeypatch):
    """A decorator library sitting next to the script is importable with no
    WYRM_PATH set at all - the same rule Python's own sys.path[0] follows."""
    from wypoc import wyrm_modules

    (tmp_path / "lib.wy").write_text(
        "fn [TreeBase] to_nine():\n"
        "    return $['fn, 'g, [], nil, nil, [], [], [$['return, $['int, 9]]]]\n"
    )
    script = tmp_path / "main.wy"
    script.write_text("import static lib\n@to_nine fn g():\n    return 0\n")

    clear_module_cache()
    previous = wyrm_modules.set_script_root(str(tmp_path))
    try:
        ctx = run(script.read_text())
    finally:
        wyrm_modules.set_script_root(previous)
        clear_module_cache()

    from wypoc.wyrm_eval_parse_tree import call_value

    assert call_value(ctx["g"].value, [], {}) == 9


def test_samples_dir_is_where_the_conformance_script_lives():
    import os

    assert os.path.isfile(os.path.join(SAMPLES_DIR, "decolib.wy"))
