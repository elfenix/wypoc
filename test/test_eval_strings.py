"""Parses samples/eval_strings.wy and checks raw string extraction,
including a raw string whose body spans multiple physical lines."""
import pytest

from conftest import eval_sample
from wypoc.wyrm_eval_parse_tree import Variable

EXPECTED = {
    "plain": "hello",
    "raw_single_line": "one line",
    "raw_multiline": "\n#include <stdio.h>\nint main() { return 0; }\n",
}


@pytest.fixture(scope="module")
def ctx():
    return eval_sample("eval_strings.wy")


@pytest.mark.parametrize("name,expected", EXPECTED.items(), ids=list(EXPECTED))
def test_string_value(ctx, name, expected):
    var = ctx.get(name)
    assert isinstance(var, Variable), f"{name} missing from context (got {var!r})"
    assert var.value == expected
