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
