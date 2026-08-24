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
# expression kinds ride in on a `:=` whose `'define` wrapper is part of the
# expected text.
SHAPES = [
    ("x := 41", "$['define, 'x, $['type, 'auto], $['int, 41]]"),
    ("x := 2.5", "$['define, 'x, $['type, 'auto], $['float, 2.5]]"),
    ('x := "hi"', '$[\'define, \'x, $[\'type, \'auto], $[\'str, "hi"]]'),
    ("x := true", "$['define, 'x, $['type, 'auto], $['true]]"),
    ("x := false", "$['define, 'x, $['type, 'auto], $['false]]"),
    ("x := nil", "$['define, 'x, $['type, 'auto], $['nil]]"),
    ("x := ...", "$['define, 'x, $['type, 'auto], $['ellipsis]]"),
    ("x := 'name", "$['define, 'x, $['type, 'auto], $['sym, 'name]]"),
    ("x := y", "$['define, 'x, $['type, 'auto], $['name, 'y]]"),
    ("x := this", "$['define, 'x, $['type, 'auto], $['name, 'this]]"),
    ("x := [1, 2]", "$['define, 'x, $['type, 'auto], $['list, [$['int, 1], $['int, 2]]]]"),
    ("x := (1, 2)", "$['define, 'x, $['type, 'auto], $['tuple, [$['int, 1], $['int, 2]]]]"),
    ("x := $[1, 2]", "$['define, 'x, $['type, 'auto], $['pairlist, [$['int, 1], $['int, 2]]]]"),
    ('x := {"a": 1}', '$[\'define, \'x, $[\'type, \'auto], $[\'dict, [$[\'pair, $[\'str, "a"], $[\'int, 1]]]]]'),
    ("x := a + b", "$['define, 'x, $['type, 'auto], $['binop, '+, $['name, 'a], $['name, 'b]]]"),
    ("x := a <=> b", "$['define, 'x, $['type, 'auto], $['binop, '<=>, $['name, 'a], $['name, 'b]]]"),
    ("x := a << b", "$['define, 'x, $['type, 'auto], $['binop, '<<, $['name, 'a], $['name, 'b]]]"),
    ("x := a >> b", "$['define, 'x, $['type, 'auto], $['binop, '>>, $['name, 'a], $['name, 'b]]]"),
    ("x := -a", "$['define, 'x, $['type, 'auto], $['unop, '-, $['name, 'a]]]"),
    ("x := +a", "$['define, 'x, $['type, 'auto], $['unop, '+, $['name, 'a]]]"),
    ("x := ~a", "$['define, 'x, $['type, 'auto], $['unop, '~, $['name, 'a]]]"),
    ("x := a and b", "$['define, 'x, $['type, 'auto], $['and, $['name, 'a], $['name, 'b]]]"),
    ("x := a or b", "$['define, 'x, $['type, 'auto], $['or, $['name, 'a], $['name, 'b]]]"),
    ("x := not a", "$['define, 'x, $['type, 'auto], $['not, $['name, 'a]]]"),
    ("x := f(1)", "$['define, 'x, $['type, 'auto], $['call, $['name, 'f], [$['int, 1]]]]"),
    ("x := a.b", "$['define, 'x, $['type, 'auto], $['attr, $['name, 'a], 'b]]"),
    ("x := a[0]", "$['define, 'x, $['type, 'auto], $['index, $['name, 'a], $['int, 0]]]"),
    ("x := o ! m(1)", "$['define, 'x, $['type, 'auto], $['msg, $['name, 'o], 'm, [$['int, 1]]]]"),
    ("x := m::C", "$['define, 'x, $['type, 'auto], $['mod_get, $['name, 'm], 'C]]"),
    ("x := v is int", "$['define, 'x, $['type, 'auto], $['is, $['name, 'v], [$['type, 'int]]]]"),
    ("x := v is not int",
     "$['define, 'x, $['type, 'auto], $['not, $['is, $['name, 'v], [$['type, 'int]]]]]"),
    ("x := try v", "$['define, 'x, $['type, 'auto], $['try, $['name, 'v]]]"),
    ("x := v catch 0", "$['define, 'x, $['type, 'auto], $['catch, $['name, 'v], $['int, 0]]]"),
    ("x := v catch return 0",
     "$['define, 'x, $['type, 'auto], $['catch_return, $['name, 'v], $['int, 0]]]"),
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
    ("import a::b",
     "$['import, [$['name, 'a], $['name, 'b]], $['false], nil, nil, $['false], nil]"),
    ("import a::b as c",
     "$['import, [$['name, 'a], $['name, 'b]], $['false], $['name, 'c], nil, "
     "$['false], nil]"),
    ("import a::(x, y as z)",
     "$['import, [$['name, 'a]], $['false], nil, "
     "[$['import_item, 'x, nil], $['import_item, 'y, $['name, 'z]]], $['false], nil]"),
    ("import a::* except b",
     "$['import, [$['name, 'a]], $['false], nil, nil, $['true], [$['name, 'b]]]"),
    ("import static a::b",
     "$['import, [$['name, 'a], $['name, 'b]], $['true], nil, nil, $['false], nil]"),
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
    ("do", "x := do:\n    1\n", "$['define, 'x, $['type, 'auto], $['do, [$['expr_stmt, $['int, 1]]]]]"),
    ("static", "static s = 0\n", "$['static, 's, $['int, 0]]"),
    ("while", "while c:\n    pass\n", "$['while, $['name, 'c], [$['pass]]]"),
    ("for", "for i in xs:\n    pass\n",
     "$['for, 'i, $['name, 'xs], [$['pass]]]"),
    ("if", "if c:\n    pass\n", "$['if, $['name, 'c], [$['pass]], []]"),
    ("defer", "defer:\n    pass\n", "$['defer, [$['pass]]]"),
    ("defer_on", "defer on error:\n    pass\n",
     "$['defer_on, [$['pass]], [$['type, 'error]]]"),
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
        "$['fn, 'add, [$['type, 'int]], nil, nil, "
        "[$['param, 'a, $['type, 'int]], "
        "$['param, 'b, $['type, 'str]]], [], "
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


def test_qualified_type_splits_into_a_qualified_name():
    tree = ast.TypeExpr(["std", "io", "File"])
    assert _to_str(sexpr.encode(tree)) == "$['type, $['qualified_name, 'std, 'io, 'File]]"
    assert sexpr.decode_type(sexpr.encode(tree)).parts == ["std", "io", "File"]


def test_unannotated_var_target_is_type_auto():
    """`x := 1` (no annotation) encodes its target's type as `$['type,
    'auto]`, matching the reference parser's `_mk_type_expression` default -
    see `sexpr._encode_var_type`."""
    assert encoded("x := 1") == "$['define, 'x, $['type, 'auto], $['int, 1]]"
    assert sexpr.decode(encode_first("x := 1")).targets[0].type is None


def test_var_with_explicit_type():
    assert encoded("var count: int = 0") == (
        "$['define, 'count, $['type, 'int], $['int, 0]]"
    )


def test_multi_target_var_is_define_values():
    """`var a: int, b: float = 4, 4.2` - one `'define_values` carrying a
    `[name, type]` pair per target and the whole init tuple, matching the
    reference parser's `_build_define` (wy/wyrm/parser/parser.wy)."""
    src = "var a: int, b: float = 4, 4.2"
    assert encoded(src) == (
        "$['define_values, [['a, $['type, 'int]], ['b, $['type, 'float]]], "
        "$['tuple, [$['int, 4], $['float, 4.2]]]]"
    )
    back = sexpr.decode(encode_first(src))
    assert [(t.name, t.type.parts) for t in back.targets] == [
        ("a", ["int"]), ("b", ["float"]),
    ]
    assert [v.value for v in back.values] == ["4", "4.2"]


def test_every_kind_round_trips_multi_target_var():
    once = encode_first("var a: int, b: float = 4, 4.2")
    twice = sexpr.encode(sexpr.decode(once))
    assert _to_str(once) == _to_str(twice)


def test_module_wraps_a_programs_statements_directly():
    """`'module` splices its statements as direct siblings rather than a
    single list-valued field - see `sexpr._encode_program`."""
    tree = parse("x := 1\ny := 2\n")
    assert _to_str(sexpr.encode(tree)) == (
        "$['module, $['define, 'x, $['type, 'auto], $['int, 1]], "
        "$['define, 'y, $['type, 'auto], $['int, 2]]]"
    )
    back = sexpr.decode(sexpr.encode(tree))
    assert [type(s).__name__ for s in back.body] == ["VarDecl", "VarDecl"]


def test_a_child_list_may_come_back_as_a_pair_list():
    """The encoder always produces a list, but a decorator building one out
    of `cons` has no reason to know which - so both are accepted."""
    as_pairs = sexpr.node("list", sexpr._pairs([sexpr.node("int", 1)]))
    assert sexpr.decode(as_pairs).items[0].value == "1"


# --- failing loudly -------------------------------------------------------

CANNOT_CROSS = [
    ("co f():\n    yield 1\n", "coroutine"),
    ("class Foo:\n    slot a: int\n", "class"),
    ("from a::b import x\n", "from-import"),
    ("x := a in b", "'in' operator"),
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
