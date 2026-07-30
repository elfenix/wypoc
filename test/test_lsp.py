"""Exercises wypoc.lsp.diagnostics_for_source directly - no JSON-RPC/stdio
server involved, since that logic is deliberately factored out as a plain
function for testability."""
import os

import pytest

from conftest import SAMPLES_DIR
from wypoc.lsp import diagnostics_for_source

SAMPLE_NAMES = sorted(n for n in os.listdir(SAMPLES_DIR) if n.endswith(".wy"))


def test_clean_source_has_no_diagnostics():
    assert diagnostics_for_source("x = 1\n") == []


def test_syntax_error_produces_one_diagnostic():
    diags = diagnostics_for_source("x = 1 +\n")
    assert len(diags) == 1
    d = diags[0]
    assert d.range.start.line == 0, "diagnostic is on line 0 (0-indexed)"
    assert d.severity is not None
    assert d.message, "diagnostic has a non-empty message"


def test_later_line_error_reports_right_line():
    multiline = "fn foo():\n    x = 1 +\n"
    diags = diagnostics_for_source(multiline)
    assert len(diags) == 1 and diags[0].range.start.line == 1, (
        f"a later-line error is reported on the right (0-indexed) line: {diags}"
    )


# Every bundled sample is known-good (test_grammar.py covers this too, from
# the grammar side); the LSP server should agree there's nothing to report
# for any of them.
@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_sample_has_no_diagnostics(name):
    with open(os.path.join(SAMPLES_DIR, name)) as f:
        src = f.read()
    assert diagnostics_for_source(src) == []
