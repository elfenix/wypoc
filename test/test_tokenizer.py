"""Direct tokenizer tests for raw string scanning, in particular raw
strings whose body spans multiple physical lines (see _scan_raw_string
in wyrm_tokenizer.py)."""
import token

import pytest

from wypoc.wyrm_tokenizer import TokenizeError, generate_tokens


def _string_tokens(src: str):
    return [t for t in generate_tokens(src) if t.type == token.STRING]


def test_raw_string_single_line_unchanged():
    toks = _string_tokens('x = R"(one line)"\n')
    assert len(toks) == 1
    assert toks[0].string == 'R"(one line)"'


def test_raw_string_multiline():
    src = 'x = R"C(\nline one\nline two )C"\n'
    toks = _string_tokens(src)
    assert len(toks) == 1
    assert toks[0].string == 'R"C(\nline one\nline two )C"'


def test_raw_string_multiline_resumes_tokenizing_after_close():
    src = 'x = R"C(\nfoo\n)C"\ny = 5\n'
    names = [t.string for t in generate_tokens(src) if t.type == token.NAME]
    assert names == ["x", "y"]


def test_raw_string_unterminated_multiline_raises():
    with pytest.raises(TokenizeError):
        list(generate_tokens('x = R"C(\nno closer here\n'))


def _number_tokens(src: str):
    return [t.string for t in generate_tokens(src) if t.type == token.NUMBER]


def test_binary_literal():
    assert _number_tokens("0b1010_0000\n") == ["0b1010_0000"]
    assert _number_tokens("0B11\n") == ["0B11"]


def test_exponent_digits_allow_underscore_separator():
    assert _number_tokens("1e1_0\n") == ["1e1_0"]


# --------------------------------------------------------------------------
# `$` as an identifier character, and the operators added alongside it:
# unary `~`/`+`, and the `<<`/`>>` shifts.
# --------------------------------------------------------------------------

def _significant(src: str):
    """(type, text) for every token that carries text of its own."""
    return [(t.type, t.string) for t in generate_tokens(src)
            if t.type in (token.NAME, token.OP, token.NUMBER, token.STRING)]


def _names(src: str):
    return [t.string for t in generate_tokens(src) if t.type == token.NAME]


@pytest.mark.parametrize("src,expected", [
    ("$ast\n", ["$ast"]),
    ("$foo\n", ["$foo"]),
    ("a$b\n", ["a$b"]),
    ("reg$0\n", ["reg$0"]),
    ("x$\n", ["x$"]),
    ("foo::$ast\n", ["foo", "$ast"]),
])
def test_dollar_is_an_identifier_character(src, expected):
    assert _names(src) == expected


def test_the_pair_list_sigil_is_still_an_operator():
    # `$` only starts a name when a name follows it, so `$[` is unchanged.
    assert _significant("$[1, 2]\n")[:3] == [
        (token.OP, "$"), (token.OP, "["), (token.NUMBER, "1")]


def test_a_bare_dollar_is_still_an_operator():
    assert _significant("$ x\n") == [(token.OP, "$"), (token.NAME, "x")]


@pytest.mark.parametrize("src,ops", [
    ("a << b\n", ["<<"]),
    ("a >> b\n", [">>"]),
    ("a >>= b\n", [">>", "="]),      # no compound-assign form; two tokens
    ("a <= b\n", ["<="]),            # the shifts don't shadow the comparisons
    ("a <=> b\n", ["<=", ">"]),
    ("~a\n", ["~"]),
    ("a <-b\n", ["<-"]),             # nor the arrows
])
def test_shift_and_complement_operators_are_lexed(src, ops):
    assert [t for kind, t in _significant(src) if kind == token.OP] == ops


@pytest.mark.parametrize("text", ["'<<", "'>>", "'~", "'$ast"])
def test_the_new_operators_and_dollar_names_can_be_symbols(text):
    toks = _string_tokens(text + "\n")
    assert len(toks) == 1 and toks[0].string == text
