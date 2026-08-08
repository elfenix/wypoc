"""Exercises wypoc.lsp's feature functions directly - no JSON-RPC/stdio
server involved, since each is deliberately factored out as a plain
function of (source, position) for exactly this reason."""
import os

import pytest
from lsprotocol import types

from conftest import SAMPLES_DIR
from wypoc.lsp import (
    definition_locations, diagnostics_for_source, document_symbols, hover_for,
)

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


def test_diagnostic_covers_the_whole_offending_token():
    """A zero-width or one-character range next to the problem is easy to
    miss in an editor; the range should cover the token the parse actually
    choked on (see wypoc.parse.syntax_error)."""
    diags = diagnostics_for_source("x = foo(:\n")
    assert len(diags) == 1
    rng = diags[0].range
    assert (rng.start.line, rng.start.character) == (0, 8), "starts at the ':'"
    assert (rng.end.line, rng.end.character) == (0, 9), "ends after the ':'"


def test_diagnostic_names_the_unexpected_token():
    """"invalid syntax" tells the reader nothing they can act on; the
    message should at least say what the parser tripped over."""
    assert "':'" in diagnostics_for_source("x = foo(:\n")[0].message
    assert "end of line" in diagnostics_for_source("x = 1 +\n")[0].message


def test_tokenizer_error_still_gets_a_visible_range():
    """Errors raised by the tokenizer (not the parser) carry no end
    position, so the range falls back to one character rather than
    collapsing to a zero-width point."""
    diags = diagnostics_for_source('x = "unterminated\n')
    assert len(diags) == 1
    rng = diags[0].range
    assert (rng.end.line, rng.end.character) > (rng.start.line, rng.start.character)
    assert "unterminated" in diags[0].message


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


# ---------------------------------------------------------------------
# Navigation features (documentSymbol / definition / hover)
#
# These go through lsp.py's own protocol adapters, so they cover the
# LSP-shaped conversions - 0-based positions, Locations, file:// URIs -
# that test_symbols.py and test_symbol_index.py deliberately don't.
# Cross-file cases import from the real bundled corelib, which resolves
# through DEFAULT_COREPATH with no WYRM_PATH set.
# ---------------------------------------------------------------------

DOC_PATH = "/tmp/wyrm-lsp-test/main.wy"


def point_of(source, text, occurrence=0):
    """(1-based line, 0-based col) - what lsp.py's handlers pass on."""
    seen = -1
    for lineno, line in enumerate(source.splitlines(), start=1):
        start = 0
        while (col := line.find(text, start)) != -1:
            seen += 1
            if seen == occurrence:
                return lineno, col
            start = col + 1
    raise AssertionError(f"{text!r} #{occurrence} not in source")


def test_document_symbols_nest_like_the_code():
    source = (
        "class Point:\n    slot x: int = 0\n\n    fn shift(dx):\n        pass\n\n"
        "fn main():\n    pass\n"
    )
    outline = document_symbols(source, DOC_PATH)
    assert [s.name for s in outline] == ["Point", "main"]
    point = outline[0]
    assert [(c.name, c.kind) for c in point.children] == [
        ("x", types.SymbolKind.Field),
        ("shift", types.SymbolKind.Method),
    ]
    assert point.detail == "class Point"
    assert point.selection_range.start.line == 0, "selection range is the name alone"
    assert point.range.end.line == 4, "full range covers the body"


def test_document_symbols_omit_parameters():
    """Parameters are in the symbol table (definition/hover need them) but
    would be noise in an outline."""
    outline = document_symbols("fn f(a, b):\n    pass\n", DOC_PATH)
    assert outline[0].children == []


def test_document_symbols_of_a_broken_file_are_empty():
    assert document_symbols("fn broken(:\n", DOC_PATH) == []


def test_definition_jumps_into_another_file_through_an_import():
    source = "import std::io\n"
    locations = definition_locations(source, DOC_PATH, *point_of(source, "io"))
    assert len(locations) == 1
    assert locations[0].uri.startswith("file://")
    assert locations[0].uri.endswith("/corelib/std/io.wy")


def test_definition_on_an_imported_name_lands_on_its_declaration():
    source = "import std::io::(println)\n\nprintln(1)\n"
    locations = definition_locations(source, DOC_PATH, *point_of(source, "println", 1))
    assert len(locations) == 1
    assert locations[0].uri.endswith("/corelib/std/io.wy")
    # 0-based, unlike the (1-based line) spans the symbol table works in.
    assert locations[0].range.start.line == 11


def test_definition_within_one_file_is_zero_based():
    source = "fn helper():\n    pass\n\nhelper()\n"
    location = definition_locations(source, DOC_PATH, *point_of(source, "helper", 1))[0]
    assert location.uri.endswith("/main.wy")
    assert (location.range.start.line, location.range.start.character) == (0, 3)


def test_definition_on_nothing_returns_no_locations():
    assert definition_locations("x := 1\n", DOC_PATH, 1, 3) == []


def test_hover_reports_a_signature_and_a_range():
    source = "fn scale(factor: float) -> float:\n    return factor\n\nscale(2.0)\n"
    hover = hover_for(source, DOC_PATH, *point_of(source, "scale", 1))
    assert "fn scale(factor: float) -> float" in hover.contents.value
    assert hover.range.start.line == 3, "the range highlights the use, not the definition"


def test_hover_on_an_import_names_the_module_file():
    source = "import std::io\n"
    hover = hover_for(source, DOC_PATH, *point_of(source, "io"))
    assert "module" in hover.contents.value
    assert "std::io" in hover.contents.value


def test_hover_on_a_broken_document_is_none():
    assert hover_for("fn broken(:\n", DOC_PATH, 1, 4) is None
