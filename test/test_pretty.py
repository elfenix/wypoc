"""`pretty.py`: the multi-line renderings the REPL answers with unless
`:set compact` is on.

Values are built by evaluating real wyrm source through a REPL session -
that's how they reach a pretty printer in practice, and a `ClassInstance`
in particular isn't worth hand-assembling."""
import pytest

from wypoc.pretty import pretty
from wypoc.repl import Session


@pytest.fixture
def value():
    """Evaluates wyrm source and answers the value it produced."""
    session = Session()

    def build(source: str):
        result = session.evaluate(source)
        assert not result.failed, result.error
        return result.value

    return build


def test_a_pair_list_is_lisp_not_dollar_bracket(value):
    assert pretty(value("$[1, 2, 3]")) == "(1 2 3)"


def test_nested_pair_lists_nest_parens(value):
    assert pretty(value("$[1, $[2, 3], 4]")) == "(1 (2 3) 4)"


def test_an_improper_tail_is_spelled_the_lisp_way(value):
    assert pretty(value("cons(1, 2)")) == "(1 . 2)"
    assert pretty(value("cons(1, cons(2, 3))")) == "(1 2 . 3)"


def test_a_long_pair_list_breaks_aligned_under_its_first_element(value):
    rendered = pretty(value("$[100000, 200000, 300000]"), width=20)
    assert rendered == "(100000\n 200000\n 300000)"


def test_a_dict_that_fits_stays_on_one_line(value):
    assert pretty(value('{"a": 1, "b": 2}')) == "{'a': 1, 'b': 2}"


def test_a_long_dict_breaks_json_style(value):
    rendered = pretty(value('{"a": 1, "b": 2}'), width=10)
    assert rendered == "{\n    'a': 1,\n    'b': 2\n}"


def test_a_long_array_breaks_json_style(value):
    assert pretty(value("[1, 2, 3]"), width=5) == "[\n    1,\n    2,\n    3\n]"


def test_nesting_indents_from_the_line_the_value_starts_on(value):
    rendered = pretty(value('{"outer": {"a": 1, "b": 2}}'), width=24)
    assert rendered == (
        "{\n"
        "    'outer': {\n"
        "        'a': 1,\n"
        "        'b': 2\n"
        "    }\n"
        "}"
    )


def test_empty_containers_have_no_broken_form(value):
    assert pretty(value("{}"), width=1) == "{}"
    assert pretty(value("[]"), width=1) == "[]"


def test_a_class_instance_looks_like_its_class_definition(value):
    value("class Point:\n    slot x: float = 1.0\n    slot y: float = 2.0\n")
    assert pretty(value("Point()")) == "Point:\n    x: 1.0\n    y: 2.0"


def test_a_class_instance_is_a_block_however_short_it_is(value):
    value("class Tiny:\n    slot a: int = 1\n")
    # Nothing about the width makes this one line - the definition shape is
    # the point of the rendering.
    assert pretty(value("Tiny()"), width=200) == "Tiny:\n    a: 1"


def test_a_nested_instance_keeps_its_header_on_the_slot_line(value):
    value("class Point:\n    slot x: float = 1.0\n")
    value("class Line:\n    slot start: Point = Point()\n")
    assert pretty(value("Line()")) == "Line:\n    start: Point:\n        x: 1.0"


def test_a_container_holding_an_instance_has_to_break(value):
    value("class Tiny:\n    slot a: int = 1\n")
    assert pretty(value("[Tiny()]"), width=200) == "[\n    Tiny:\n        a: 1\n]"


def test_everything_else_falls_back_to_the_one_line_spelling(value):
    assert pretty(value("42")) == "42"
    assert pretty(value('"hi"')) == "'hi'"
    assert pretty(value("'sym")) == "'sym"
    assert pretty(value("true")) == "true"
