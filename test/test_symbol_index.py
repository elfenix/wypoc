"""Cross-file resolution (`wypoc/symbol_index.py`): following an `import`
to the file and declaration it names.

Uses a throwaway module tree under tmp_path as the search root rather than
the real corelib, so the assertions describe the fixture rather than
whatever corelib happens to contain today. Module resolution itself goes
through `wyrm_modules.resolve_module_file`, the same function the
interpreter uses, so what the editor offers to jump to is what the
interpreter would actually load."""
import pytest

from wypoc.symbol_index import SymbolIndex

HELPERS = """fn compute(x: int) -> int:
    return x * 2

fn tidy(text):
    return text

class Widget:
    slot label: str = ""
"""

PACKAGE_INIT = "version := 1\n"


@pytest.fixture
def root(tmp_path):
    """A search root holding `util` (a package) with a `helpers` submodule."""
    package = tmp_path / "util"
    package.mkdir()
    (package / "__init__.wy").write_text(PACKAGE_INIT)
    (package / "helpers.wy").write_text(HELPERS)
    return tmp_path


@pytest.fixture
def index(root):
    return SymbolIndex(roots=[str(root)])


def table_for(index, source, path="/tmp/main.wy"):
    index.set_document(path, source)
    return index.table_for_path(path)


def point_of(source, text, occurrence=0):
    """(1-based line, 0-based col) of the nth occurrence of `text`."""
    seen = -1
    for lineno, line in enumerate(source.splitlines(), start=1):
        start = 0
        while (col := line.find(text, start)) != -1:
            seen += 1
            if seen == occurrence:
                return lineno, col
            start = col + 1
    raise AssertionError(f"{text!r} #{occurrence} not in source")


def only_target(index, source, text, occurrence=0, path="/tmp/main.wy"):
    table = table_for(index, source, path)
    targets = index.definitions_at(table, *point_of(source, text, occurrence))
    assert len(targets) == 1, f"expected exactly one target, got {targets}"
    return targets[0]


# ---------------------------------------------------------------------
# import statements
# ---------------------------------------------------------------------

def test_package_segment_resolves_to_its_init_file(index, root):
    target = only_target(index, "import util::helpers\n", "util")
    assert target.path == str(root / "util" / "__init__.wy")
    assert target.span == (1, 0, 1, 0), "a module jump lands at the top of the file"


def test_submodule_segment_resolves_to_the_submodule(index, root):
    """The two segments of one `import` must lead to different files -
    that's the whole reason each is tracked separately."""
    target = only_target(index, "import util::helpers\n", "helpers")
    assert target.path == str(root / "util" / "helpers.wy")


def test_import_item_resolves_to_the_declaration_inside_the_module(index, root):
    target = only_target(index, "import util::helpers::(compute)\n", "compute")
    assert target.path == str(root / "util" / "helpers.wy")
    assert target.symbol.name == "compute"
    assert target.symbol.detail == "fn compute(x: int) -> int"


def test_ambiguous_leaf_falls_back_to_a_symbol(index, root):
    """`import util::helpers::compute` has no module of that name, so the
    leaf must resolve as a symbol exported by `util::helpers` - the same
    order eval_import tries at runtime."""
    target = only_target(index, "import util::helpers::compute\n", "compute")
    assert target.symbol.name == "compute"
    assert target.path == str(root / "util" / "helpers.wy")


def test_ambiguous_leaf_prefers_a_real_module(index, root):
    """When both readings are possible, the module wins - again matching
    eval_import, which only falls back to a symbol on ImportError."""
    target = only_target(index, "import util::helpers\n", "helpers")
    assert target.symbol is None and target.span == (1, 0, 1, 0)


def test_unresolvable_import_yields_nothing(index):
    table = table_for(index, "import nope::missing\n")
    assert index.definitions_at(table, *point_of("import nope::missing\n", "nope")) == []


# ---------------------------------------------------------------------
# uses of imported names
# ---------------------------------------------------------------------

def test_use_of_an_imported_name_follows_through_to_the_definition(index, root):
    source = "import util::helpers::(compute)\n\nx := compute(2)\n"
    target = only_target(index, source, "compute", occurrence=1)
    assert target.path == str(root / "util" / "helpers.wy")
    assert target.symbol.name == "compute"


def test_alias_follows_to_the_original_declaration(index, root):
    source = "import util::helpers::(compute as calc)\n\nx := calc(2)\n"
    target = only_target(index, source, "calc", occurrence=1)
    assert target.symbol.name == "compute", "the alias points at the real name"


def test_scope_access_resolves_through_the_module_binding(index, root):
    """`helpers::compute` - the chain's root is whatever `import` bound
    here, so this has to go through the import to find the module."""
    source = "import util::helpers\n\nx := helpers::compute(2)\n"
    target = only_target(index, source, "compute")
    assert target.path == str(root / "util" / "helpers.wy")


def test_scope_access_through_a_package_root(index, root):
    source = "import util::helpers\n\nx := util::helpers::compute(2)\n"
    target = only_target(index, source, "compute")
    assert target.symbol.name == "compute"


def test_wildcard_import_resolves_bare_names(index, root):
    source = "import util::helpers::*\n\nx := compute(2)\n"
    target = only_target(index, source, "compute")
    assert target.symbol.name == "compute"


def test_wildcard_respects_except(index):
    source = "import util::helpers::* except compute\n\nx := compute(2)\n"
    table = table_for(index, source)
    assert index.definitions_at(table, *point_of(source, "compute", 1)) == []


# ---------------------------------------------------------------------
# local resolution and hover
# ---------------------------------------------------------------------

def test_local_definition_beats_a_trip_through_imports(index):
    source = "import util::helpers::(compute)\n\nfn compute(y):\n    return y\n\nz := compute(1)\n"
    target = only_target(index, source, "compute", occurrence=2)
    assert target.path == "/tmp/main.wy", "the file's own declaration wins"


def test_message_definition_lists_every_overload(index):
    source = (
        "class Canvas:\n    fn area():\n        return 1\n\n"
        "fn [Circle] area(c) -> float:\n    return 2.0\n\n"
        "fn render(shape):\n    return shape!area()\n"
    )
    table = table_for(index, source)
    targets = index.definitions_at(table, *point_of(source, "area", 2))
    assert [t.description for t in targets] == [
        "fn area()", "fn [Circle] area(c) -> float",
    ]


def test_hover_on_a_module_segment_names_the_file(index, root):
    source = "import util::helpers\n"
    table = table_for(index, source)
    markdown, span = index.hover_at(table, *point_of(source, "helpers"))
    assert "module" in markdown and "helpers.wy" in markdown
    assert span == (1, 13, 1, 20), "the hover highlights the segment, not the statement"


def test_hover_on_an_unresolved_import_says_where_it_looked(index):
    source = "import nope\n"
    table = table_for(index, source)
    markdown, _ = index.hover_at(table, *point_of(source, "nope"))
    assert "unresolved" in markdown and "Searched:" in markdown


def test_hover_on_an_imported_symbol_shows_its_signature(index):
    source = "import util::helpers::(compute)\n\nx := compute(2)\n"
    table = table_for(index, source)
    markdown, _ = index.hover_at(table, *point_of(source, "compute", 1))
    assert "fn compute(x: int) -> int" in markdown
    assert "helpers.wy" in markdown, "hover says which file it came from"


def test_hover_on_a_multi_overload_message_lists_them(index):
    source = (
        "class Canvas:\n    fn area():\n        return 1\n\n"
        "fn [Circle] area(c) -> float:\n    return 2.0\n\n"
        "fn render(shape):\n    return shape!area()\n"
    )
    table = table_for(index, source)
    markdown, _ = index.hover_at(table, *point_of(source, "area", 2))
    assert "2 overloads" in markdown
    assert "fn [Circle] area(c) -> float" in markdown


def test_hover_on_nothing_is_none(index):
    source = "x := 1\n"
    table = table_for(index, source)
    assert index.hover_at(table, 1, 3) is None  # on the `:=`


# ---------------------------------------------------------------------
# caching and robustness
# ---------------------------------------------------------------------

def test_open_document_shadows_what_is_on_disk(index, root):
    """An editor's unsaved buffer is authoritative - a jump must land on
    the declaration as currently typed, not as last saved."""
    path = str(root / "util" / "helpers.wy")
    index.set_document(path, "fn compute(a, b, c):\n    return a\n")
    target = index.module_symbol(("util", "helpers"), "compute")
    assert target.symbol.detail == "fn compute(a, b, c)"

    index.forget_document(path)
    target = index.module_symbol(("util", "helpers"), "compute")
    assert target.symbol.detail == "fn compute(x: int) -> int", "back to disk"


def test_a_document_that_does_not_parse_answers_nothing(index):
    """Half-typed files are the common case in an editor; a request over
    one must come back empty rather than raise."""
    table = table_for(index, "fn broken(:\n")
    assert table is None
    assert index.definitions_at(table, 1, 4) == []
    assert index.hover_at(table, 1, 4) is None


def test_a_broken_import_target_does_not_break_the_request(index, root):
    (root / "util" / "helpers.wy").write_text("fn (((\n")
    table = table_for(index, "import util::helpers::(compute)\n")
    source = "import util::helpers::(compute)\n"
    assert index.definitions_at(table, *point_of(source, "compute")) == []
