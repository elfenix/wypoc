"""Exercises the canonical s-expression bridge (wypoc/sexpr.py) directly:
the shape each kind encodes to, that every kind round-trips, and that a
construct or an s-expression the format doesn't carry fails by name rather
than producing a half-translated tree."""
import pytest

from wypoc import ast_nodes as ast
from wypoc import sexpr
from wypoc.parse import parse
from wypoc.wyrm_builtins import NIL, Pair, Symbol, _to_str


def encode_first(src: str):
    """The s-expression of `src`'s first statement."""
    return sexpr.encode(parse(src).body[0])


def encoded(src: str) -> str:
    return _to_str(encode_first(src))


# One row per kind in the format, so the table below is also the reference
# for what a decorator sees. Statement kinds are given as whole statements;
# expression kinds ride in on a `:=` whose `'decl` wrapper is part of the
# expected text.
SHAPES = [
    ("x := 41", "$['decl, 'x, $['int, 41]]"),
    ("x := 2.5", "$['decl, 'x, $['float, 2.5]]"),
    ('x := "hi"', "$['decl, 'x, $['str, 'hi']]"),
    ("x := true", "$['decl, 'x, $['true]]"),
    ("x := false", "$['decl, 'x, $['false]]"),
    ("x := nil", "$['decl, 'x, $['nil]]"),
    ("x := ...", "$['decl, 'x, $['ellipsis]]"),
    ("x := 'name", "$['decl, 'x, $['sym, 'name]]"),
    ("x := y", "$['decl, 'x, $['name, 'y]]"),
    ("x := this", "$['decl, 'x, $['name, 'this]]"),
    ("x := [1, 2]", "$['decl, 'x, $['list, [$['int, 1], $['int, 2]]]]"),
    ("x := (1, 2)", "$['decl, 'x, $['tuple, [$['int, 1], $['int, 2]]]]"),
    ("x := $[1, 2]", "$['decl, 'x, $['pairlist, [$['int, 1], $['int, 2]]]]"),
    ('x := {"a": 1}', "$['decl, 'x, $['dict, [$['pair, $['str, 'a'], $['int, 1]]]]]"),
    ("x := a + b", "$['decl, 'x, $['binop, '+, $['name, 'a], $['name, 'b]]]"),
    ("x := a <=> b", "$['decl, 'x, $['binop, '<=>, $['name, 'a], $['name, 'b]]]"),
    ("x := a << b", "$['decl, 'x, $['binop, '<<, $['name, 'a], $['name, 'b]]]"),
    ("x := a >> b", "$['decl, 'x, $['binop, '>>, $['name, 'a], $['name, 'b]]]"),
    ("x := -a", "$['decl, 'x, $['unop, '-, $['name, 'a]]]"),
    ("x := +a", "$['decl, 'x, $['unop, '+, $['name, 'a]]]"),
    ("x := ~a", "$['decl, 'x, $['unop, '~, $['name, 'a]]]"),
    ("x := a and b", "$['decl, 'x, $['and, $['name, 'a], $['name, 'b]]]"),
    ("x := a or b", "$['decl, 'x, $['or, $['name, 'a], $['name, 'b]]]"),
    ("x := not a", "$['decl, 'x, $['not, $['name, 'a]]]"),
    ("x := f(1)", "$['decl, 'x, $['call, $['name, 'f], [$['int, 1]]]]"),
    ("x := a.b", "$['decl, 'x, $['attr, $['name, 'a], 'b]]"),
    ("x := a[0]", "$['decl, 'x, $['index, $['name, 'a], $['int, 0]]]"),
    ("x := o ! m(1)", "$['decl, 'x, $['msg, $['name, 'o], 'm, [$['int, 1]]]]"),
    ("x := m::C", "$['decl, 'x, $['mod_get, $['name, 'm], 'C]]"),
    ("x := v is int", "$['decl, 'x, $['is, $['name, 'v], [$['type, 'int, [], []]]]]"),
    ("x := v is not int",
     "$['decl, 'x, $['not, $['is, $['name, 'v], [$['type, 'int, [], []]]]]]"),
    ("x := try v", "$['decl, 'x, $['try, $['name, 'v]]]"),
    ("x := v catch 0", "$['decl, 'x, $['catch, $['name, 'v], $['int, 0]]]"),
    ("x := v catch return 0",
     "$['decl, 'x, $['catch_return, $['name, 'v], $['int, 0]]]"),
    ("f(x)", "$['expr_stmt, $['call, $['name, 'f], [$['name, 'x]]]]"),
    ("x = 1", "$['assign, $['name, 'x], $['int, 1]]"),
    ("x ?= 1", "$['qassign, $['name, 'x], $['int, 1]]"),
    ("a.b = 1", "$['assign, $['attr, $['name, 'a], 'b], $['int, 1]]"),
    ("a[0] = 1", "$['assign, $['index, $['name, 'a], $['int, 0]], $['int, 1]]"),
    ("break", "$['break, nil]"),
    ("continue", "$['continue]"),
    ("pass", "$['pass]"),
    ("return x", "$['return, $['name, 'x]]"),
    ("with x = 1", "$['with, [$['decl, 'x, $['int, 1]]]]"),
]


@pytest.mark.parametrize("src,expected", SHAPES, ids=[s for s, _ in SHAPES])
def test_kind_encodes_to_its_documented_shape(src, expected):
    assert encoded(src) == expected


@pytest.mark.parametrize("src", [s for s, _ in SHAPES], ids=[s for s, _ in SHAPES])
def test_every_kind_round_trips(src):
    once = encode_first(src)
    twice = sexpr.encode(sexpr.decode(once))
    assert _to_str(once) == _to_str(twice)


MULTILINE = [
    ("do", "x := do:\n    1\n", "$['decl, 'x, $['do, [$['expr_stmt, $['int, 1]]]]]"),
    ("static", "static s = 0\n", "$['static, 's, $['int, 0]]"),
    ("while", "while c:\n    pass\n", "$['while, $['name, 'c], [$['pass]]]"),
    ("for", "for i in xs:\n    pass\n",
     "$['for, 'i, $['name, 'xs], [$['pass]]]"),
    ("if", "if c:\n    pass\n", "$['if, $['name, 'c], [$['pass]], []]"),
    ("defer", "defer:\n    pass\n", "$['defer, [$['pass]]]"),
    ("defer_on", "defer on error:\n    pass\n",
     "$['defer_on, [$['pass]], [$['type, 'error, [], []]]]"),
    ("with block", "with:\n    x = 1\n", "$['with, [$['decl, 'x, $['int, 1]]]]"),
]


@pytest.mark.parametrize("name,src,expected", MULTILINE, ids=[m[0] for m in MULTILINE])
def test_multiline_kinds(name, src, expected):
    assert encoded(src) == expected
    once = encode_first(src)
    assert _to_str(sexpr.encode(sexpr.decode(once))) == _to_str(once)


def test_elif_is_a_nested_if_in_the_else_position():
    """`elif` has no kind of its own: the format's three-field `'if` carries
    a chain as the nested `if` it means."""
    src = "if a:\n    pass\nelif b:\n    pass\nelse:\n    return 1\n"
    assert encoded(src) == (
        "$['if, $['name, 'a], [$['pass]], "
        "[$['if, $['name, 'b], [$['pass]], [$['return, $['int, 1]]]]]]"
    )


def test_fn_signature_shape():
    src = "fn add(a: int, b: str) -> int:\n    return a + b\n"
    assert encoded(src) == (
        "$['fn, 'add, [$['type, 'int, [], []]], nil, nil, "
        "[$['param, 'a, $['type, 'int, [], []]], "
        "$['param, 'b, $['type, 'str, [], []]]], [], "
        "[$['return, $['binop, '+, $['name, 'a], $['name, 'b]]]]]"
    )


def test_rest_parameter_has_its_own_position():
    """Not a flag on a parameter: it leaves the parameter chain on the way
    out and rejoins it, last, on the way back."""
    tree = parse("fn f(a, *others):\n    pass\n").body[0]
    encoded_fn = sexpr.encode(tree)
    fields = sexpr._as_list(encoded_fn, "fn")
    rest, params = fields[3], fields[5]
    assert _to_str(rest) == "$['param, 'others, nil]"
    assert len(params) == 1, "the rest parameter is not among the declared ones"
    back = sexpr.decode(encoded_fn)
    assert [type(p).__name__ for p in back.params] == ["Param", "VarPositional"]


def test_dispatch_types_are_name_nodes():
    src = "fn [Box] describe():\n    pass\n"
    assert "[$['name, 'Box]]" in encoded(src)
    assert sexpr.decode(encode_first(src)).class_target == ["Box"]


def test_qualified_type_splits_into_name_and_qualifier():
    tree = ast.TypeExpr(["std", "io", "File"])
    assert _to_str(sexpr.encode(tree)) == "$['type, 'File, ['std, 'io], []]"
    assert sexpr.decode(sexpr.encode(tree)).parts == ["std", "io", "File"]


def test_type_union_collapses_to_its_first_alternative():
    """The same thing `ann_type` (wyrm.gram) does, so this is the
    interpreter's own behaviour rather than a loss the bridge introduces."""
    union = sexpr.node(
        "type_union",
        [sexpr.encode(ast.TypeExpr(["str"])), sexpr.encode(ast.TypeExpr(["nil"]))],
    )
    assert sexpr.decode_type(union).parts == ["str"]


def test_a_child_list_may_come_back_as_a_pair_list():
    """The encoder always produces a list, but a decorator building one out
    of `cons` has no reason to know which - so both are accepted."""
    as_pairs = sexpr.node("list", sexpr._pairs([sexpr.node("int", 1)]))
    assert sexpr.decode(as_pairs).items[0].value == "1"


# --- failing loudly -------------------------------------------------------

CANNOT_CROSS = [
    ("co f():\n    yield 1\n", "coroutine"),
    ("class Foo:\n    slot a: int\n", "class"),
    ("import a::b\n", "import"),
    ("x := a in b", "'in' operator"),
    ("var a, b = 1, 2", "multiple declaration"),
    ("for i in xs:\n    pass\nelse:\n    pass\n", "for/else"),
    ("fn f(**kw):\n    pass\n", "kwargs"),
]


@pytest.mark.parametrize("src,needle", CANNOT_CROSS, ids=[c[1] for c in CANNOT_CROSS])
def test_a_construct_the_format_lacks_fails_by_name(src, needle):
    with pytest.raises(sexpr.SexprError) as excinfo:
        encode_first(src)
    assert needle in str(excinfo.value)


MALFORMED = [
    (sexpr.node("nosuchkind"), "'nosuchkind is not a node kind"),
    (42, "a node must be a $[...] list, not a int"),
    (sexpr.node("binop", sexpr.node("int", 1), sexpr.node("int", 1),
                sexpr.node("int", 1)), "s-expression is missing its binop"),
    (sexpr.node("binop", Symbol("@"), sexpr.node("int", 1),
                sexpr.node("int", 1)), "'@ is not an operator"),
    (sexpr.node("int", "text"), "value must be a number"),
    (sexpr.node("str", Symbol("s")), "text must be a str"),
    (sexpr.node("int"), "'int takes 1 field(s), not 0"),
    (sexpr.node("break", sexpr.node("int", 1)), "reserved field must be nil"),
    (sexpr.node("fn", Symbol("f"), [], NIL, [sexpr.node("int", 1)], [], [], []),
     "kwargs position is reserved"),
]


@pytest.mark.parametrize("bad,needle", MALFORMED, ids=[str(m[1]) for m in MALFORMED])
def test_a_malformed_s_expression_says_what_is_wrong(bad, needle):
    with pytest.raises(sexpr.SexprError) as excinfo:
        sexpr.decode(bad)
    assert needle in str(excinfo.value)


def test_string_quoting_round_trips_through_the_evaluator():
    """A `'str` decoded and then evaluated yields the characters it came in
    with - which needs quote_string to be the exact inverse of
    eval_string_literal."""
    from wypoc.wyrm_eval_parse_tree import eval_string_literal

    for text in ['plain', 'with "quotes"', "tab\there", "line\nbreak", "back\\slash"]:
        node = sexpr.decode(sexpr.node("str", text))
        assert eval_string_literal(node.value) == text


def test_number_spelling_round_trips_through_the_evaluator():
    from wypoc.wyrm_eval_parse_tree import eval_number_literal

    for value in [0, 41, -7, 2.5, 2.0, 1e100, 0.1]:
        node = sexpr.decode(sexpr.node("float" if isinstance(value, float) else "int",
                                       value))
        assert eval_number_literal(node.value) == value


def test_encoding_carries_no_source_positions():
    """A tree rebuilt from an s-expression reports at the decorator that
    produced it, so the spans are deliberately not in the format."""
    decoded = sexpr.decode(encode_first("x := 1 + 2"))
    assert all(node.pos is None for node in decoded.walk())


def test_a_symbol_is_not_the_string_of_the_same_name():
    assert Symbol("int") != "int"
    assert _to_str(Pair(Symbol("int"), NIL)) == "$['int]"
