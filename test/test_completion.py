"""Tests for completion (wypoc/completion.py, surfaced by wypoc/lsp.py).

Two halves, matching how the feature is built: `context_at` reads the trigger
and prefix out of raw text (and must work on source that doesn't parse), and
`complete` answers candidates from a symbol table. The context tests need no
fixtures at all; the candidate tests use a small module tree so `::`, imports
and wildcards have somewhere real to resolve to.
"""
import pytest

from wypoc import completion, symbols
from wypoc.symbol_index import SymbolIndex

DOC = "/work/main.wy"

SOURCE = '''import std::io::*
import shapes as geom

class Box:
    slot value: int = 0
    slot label: str

    fn describe() -> str:
        return this.label

fn [Box] resize_to(n: int):
    pass

fn area(width: int, height: int) -> int:
    scale := 2
    return width * height * scale

b := Box()
'''


def labels(items) -> list:
    return [c.label for c in items]


def at_end(extra: str):
    """(source, line, col) for a cursor at the end of one extra line appended
    to SOURCE - so a test says what it is completing rather than counting
    lines."""
    source = SOURCE + extra
    return source, len(source.splitlines()), len(extra)


@pytest.fixture
def index(tmp_path):
    """A workspace with a `shapes` module beside `std/io`, so a `::` chain and
    a wildcard import have real files to resolve against."""
    (tmp_path / "shapes.wy").write_text(
        "class Circle:\n"
        "    slot radius: int = 1\n"
        "\n"
        "fn [Circle] scale(by: int):\n"
        "    pass\n"
        "\n"
        "fn unit() -> Circle:\n"
        "    return Circle()\n"
    )
    std = tmp_path / "std"
    std.mkdir()
    (std / "__init__.wy").write_text("")
    (std / "io.wy").write_text(
        "fn println(*content):\n"
        "    pass\n"
        "\n"
        "class File:\n"
        "    slot handle: int = -1\n"
        "\n"
        "    fn readall() -> str:\n"
        "        return \"\"\n"
    )
    return SymbolIndex(roots=[str(tmp_path)])


def complete_at(index, source: str, line: int, col: int) -> list:
    return completion.complete(index, source, DOC, line, col)


# --- reading the context out of raw text ----------------------------------

CONTEXTS = [
    ("plain name", "x := val", 8, "", "val"),
    ("empty", "x := ", 5, "", ""),
    ("after a dot", "x := b.la", 9, ".", "la"),
    ("after a bare dot", "x := b.", 7, ".", ""),
    ("after a bang", "x := b!des", 10, "!", "des"),
    ("after a spaced bang", "x := b ! des", 12, "!", "des"),
    ("after a scope", "x := std::io::pri", 17, "::", "pri"),
    ("after a bare scope", "x := std::", 10, "::", ""),
    ("after a dollar", "x := f::$as", 11, "::$", "as"),
    ("after an at", "@trac", 5, "@", "trac"),
]


@pytest.mark.parametrize("name,text,col,trigger,prefix", CONTEXTS,
                         ids=[c[0] for c in CONTEXTS])
def test_context_reads_the_trigger_and_prefix(name, text, col, trigger, prefix):
    ctx = completion.context_at(text, 1, col)
    assert (ctx.trigger, ctx.prefix) == (trigger, prefix)


def test_the_start_column_is_where_the_word_begins():
    """That column is the range an editor replaces, which is why it excludes
    the trigger: `append` should replace `app`, not `!app`."""
    ctx = completion.context_at("x := b!app", 1, 10)
    assert ctx.start_col == 7
    assert "x := b!app"[ctx.start_col:] == "app"


def test_a_decimal_point_is_not_an_attribute_access():
    assert completion.context_at("x := 1.", 1, 7).trigger == ""
    assert completion.context_at("x := 1.5", 1, 8).trigger == ""


def test_a_dot_after_a_name_ending_in_a_digit_is_an_attribute_access():
    """`x1.` is an access; `1.` is a number. The two differ only in whether
    the run of identifier characters starts with a digit."""
    assert completion.context_at("x := b1.", 1, 8).trigger == "."


def test_the_scope_qualifier_is_the_chain_before_it():
    ctx = completion.context_at("x := f(std::io::pri", 1, 19)
    assert ctx.trigger == "::"
    assert ctx.qualifier.endswith("std::io")


def test_context_never_raises_on_a_position_past_the_end():
    assert completion.context_at("", 1, 40).prefix == ""
    assert completion.context_at("abc", 99, 0).prefix == ""


# --- candidates ------------------------------------------------------------

def test_bang_offers_message_selectors(index):
    items = complete_at(index, *at_end("x := b ! "))
    found = labels(items)
    assert "describe" in found, "a class-body method is an overload of its selector"
    assert "resize_to" in found, "so is an external `fn [Box] ...`"
    assert "readall" in found, "and one from an imported module"
    assert "append" in found, "and a native message the interpreter registers"
    assert "area" not in found, "a plain fn is not a message"


def test_a_message_selector_appears_once_with_its_receivers_in_the_detail(index):
    items = complete_at(index, *at_end("x := b ! des"))
    assert labels(items) == ["describe"]
    assert "[Box]" in items[0].detail


def test_at_offers_the_same_selectors_as_bang(index):
    """A decorator's name is a message selector, so `@` completes like `!`."""
    found = labels(complete_at(index, *at_end("@res")))
    assert "resize_to" in found, "this file's own `fn [Box] ...`"
    assert "resize" in found, "and a native message, which is equally callable"


def test_dot_offers_slots(index):
    items = complete_at(index, *at_end("x := b."))
    found = labels(items)
    assert "value" in found and "label" in found
    assert "handle" in found, "a slot from an imported module is offered too"


def test_dot_names_the_class_a_slot_came_from(index):
    """There is no type inference, so `obj.` offers every known slot - the
    detail is what makes a wrong one visible rather than misleading."""
    items = complete_at(index, *at_end("x := b.val"))
    assert labels(items) == ["value"]
    assert "[Box]" in items[0].detail


def test_scope_offers_the_named_modules_members(index):
    items = complete_at(index, *at_end("x := std::io::"))
    found = labels(items)
    assert "println" in found and "File" in found
    assert "value" not in found, "this file's own declarations are not module members"


def test_scope_follows_an_alias_to_the_real_module(index):
    """`import shapes as geom` binds `geom`, so `geom::` has to resolve
    through the import rather than looking for a module called `geom`."""
    found = labels(complete_at(index, *at_end("x := geom::")))
    assert "Circle" in found and "unit" in found


def test_dollar_offers_only_what_is_built(index):
    items = complete_at(index, *at_end("x := area::$"))
    assert labels(items) == ["ast"]


# --- plain names, and their ordering ---------------------------------------

def test_a_plain_name_offers_scope_innermost_first(index):
    source = SOURCE.replace("    return width * height * scale",
                            "    return ")
    line = source.splitlines().index("    return ") + 1
    items = complete_at(index, source, line, len("    return "))
    found = labels(items)
    # The function's own parameters and locals come before anything else;
    # within one rank the order is alphabetical, so that it is stable.
    assert set(found[:3]) == {"scale", "height", "width"}
    assert found.index("area") < found.index("Box"), (
        "the enclosing definition ranks above a sibling one"
    )
    assert found.index("Box") < found.index("println"), (
        "this file's declarations rank above imported ones"
    )


def test_a_wildcard_import_contributes_its_modules_names(index):
    found = labels(complete_at(index, *at_end("x := prin")))
    assert "println" in found, "brought in by `import std::io::*`"
    assert found.index("println") < found.index("print"), (
        "and it ranks above the builtin `print` it resembles"
    )


def test_an_aliased_import_offers_the_local_name(index):
    found = labels(complete_at(index, *at_end("x := geo")))
    assert "geom" in found


def test_builtins_and_keywords_are_offered_last(index):
    items = complete_at(index, *at_end("x := wh"))
    assert labels(items) == ["while"]
    items = complete_at(index, *at_end("x := car"))
    assert labels(items) == ["car"]
    assert items[0].kind == completion.BUILTIN


def test_the_interpreters_globals_come_from_the_interpreter(index):
    """Derived by asking what `populate_globals` installs, so a builtin added
    to wyrm_builtins or corelib/prelude.wy needs no change here."""
    globals_, messages = completion.interpreter_names()
    assert "car" in globals_ and "TreeBase" in globals_ and "sexpr" in globals_
    assert "range" in globals_, "a prelude global, defined in wyrm itself"
    assert "append" in messages and "substr" in messages


def test_a_loop_variable_is_only_offered_inside_its_loop(index):
    """The evaluator binds a `for` variable fresh per iteration and drops it
    when the loop ends (see symbols.Symbol.local_to_range), so completion has
    to scope it the same way."""
    inside = (
        "fn walk(items: int):\n"
        "    for entry in items:\n"
        "        x := \n"
        "    y := 1\n"
    )
    outside = (
        "fn walk(items: int):\n"
        "    for entry in items:\n"
        "        x := 1\n"
        "    y := \n"
    )
    assert "entry" in labels(complete_at(index, inside, 3, 13))
    assert "entry" not in labels(complete_at(index, outside, 4, 9))


# --- the unparseable document ---------------------------------------------

def test_completion_answers_while_the_document_is_invalid(index):
    """The moment a completion is most wanted is mid-identifier, which is
    exactly when the file does not parse."""
    complete_at(index, *at_end("x := b.value"))   # establishes a good parse
    broken, line, _ = at_end("x := b.\nfn (((")   # now thoroughly invalid
    assert "value" in labels(complete_at(index, broken, line - 1, 7))


def test_a_never_parsed_document_repairs_the_dangling_fragment(index):
    """A freshly opened file with a dangling `obj.` has no last-good table to
    fall back on, so the fragment is dropped and the parse retried."""
    fresh = SymbolIndex(roots=index._roots)
    source, line, col = at_end("x := b.")
    items = completion.complete(fresh, source, DOC, line, col)
    assert "value" in labels(items)


def test_a_document_that_cannot_be_repaired_still_answers_builtins(index):
    fresh = SymbolIndex(roots=index._roots)
    items = completion.complete(fresh, "fn (((\nclass ]]]\n", DOC, 2, 9)
    found = labels(items)
    assert found, "never an empty list - builtins and keywords always apply"
    assert "car" in found


# --- the protocol adapter --------------------------------------------------

def test_lsp_items_carry_an_explicit_replacement_range(index, monkeypatch):
    """An editor's own word boundary may include the trigger; an explicit
    range means `append` replaces `app` rather than `!app`."""
    from wypoc import lsp

    monkeypatch.setattr(lsp, "index", index)
    source, line, col = at_end("x := b!des")
    items = lsp.completions(source, DOC, line, col)
    assert [i.label for i in items] == ["describe"]
    edit = items[0].text_edit
    assert (edit.range.start.line, edit.range.start.character) == (line - 1, 7)
    assert (edit.range.end.line, edit.range.end.character) == (line - 1, 10)
    assert edit.new_text == "describe"


def test_lsp_items_preserve_the_computed_order(index, monkeypatch):
    from wypoc import lsp

    monkeypatch.setattr(lsp, "index", index)
    items = lsp.completions(*(lambda s, l, c: (s, DOC, l, c))(*at_end("x := b.")))
    assert [i.sort_text for i in items] == sorted(i.sort_text for i in items)


def test_lsp_maps_every_candidate_kind(index, monkeypatch):
    """A kind with no mapping would silently render as plain text, so check
    the table covers what completion.py can produce."""
    from lsprotocol import types

    from wypoc import lsp

    produced = {
        symbols.FUNCTION, symbols.COROUTINE, symbols.METHOD, symbols.CLASS,
        symbols.SLOT, symbols.VARIABLE, symbols.CONSTANT, symbols.STATIC,
        symbols.PARAM, symbols.IMPORT, symbols.MODULE,
        completion.KEYWORD, completion.BUILTIN, completion.MESSAGE,
        completion.DOLLAR,
    }
    for kind in produced:
        assert kind in lsp._COMPLETION_KINDS, kind
        assert isinstance(lsp._COMPLETION_KINDS[kind], types.CompletionItemKind)
