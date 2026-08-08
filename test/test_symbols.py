"""The single-module symbol table (`wypoc/symbols.py`) - what a file
declares, what it references, and how `import` statements decompose into
individually navigable pieces. No filesystem and no LSP here; crossing
files is `test_symbol_index.py`'s job."""
import pytest

from wypoc import symbols
from wypoc.parse import parse

SOURCE = """import std::io
import util::helpers::(compute, tidy as clean)

with LIMIT: int = 10

class Shape:
    slot sides: int = 3

    fn perimeter(scale):
        edge := scale * 2
        return edge

fn [Shape] area(s) -> float:
    return 1.0

co counter(start: int) -> int:
    yield start

fn main():
    total := 0
    for item in things:
        total = total + item
    return shape!area()
"""


@pytest.fixture
def table():
    return symbols.build(parse(SOURCE), "/tmp/example.wy")


def named(table, name):
    matches = [s for s in table.all_symbols() if s.name == name]
    assert matches, f"no symbol named {name!r}"
    return matches[0]


def test_module_level_declarations(table):
    top = {s.name: s.kind for s in table.module_level()}
    assert top["io"] == symbols.IMPORT
    assert top["compute"] == symbols.IMPORT
    assert top["clean"] == symbols.IMPORT, "an aliased item is bound under its alias"
    assert top["LIMIT"] == symbols.CONSTANT
    assert top["Shape"] == symbols.CLASS
    assert top["area"] == symbols.METHOD, "a bracketed fn is a message overload"
    assert top["counter"] == symbols.COROUTINE
    assert top["main"] == symbols.FUNCTION


def test_class_members_nest_under_the_class(table):
    shape = named(table, "Shape")
    assert {c.name: c.kind for c in shape.children} == {
        "sides": symbols.SLOT,
        "perimeter": symbols.METHOD,
    }
    assert named(table, "perimeter").container == "Shape"


def test_parameters_and_locals_belong_to_their_function(table):
    perimeter = named(table, "perimeter")
    assert {c.name for c in perimeter.children} == {"scale", "edge"}
    assert named(table, "scale").kind == symbols.PARAM


def test_loop_variable_scopes_to_the_loop(table):
    """`for item in ...` declares `item` fresh per iteration (see the
    evaluator's Scope handling). The loop symbol *is* that variable, and
    its range is the loop, so `item` resolves inside the body and nowhere
    else."""
    loop = named(table, "item")
    assert loop.node.__class__.__name__ == "For"

    body_line, body_col = 21, 8  # `total = total + item`, inside the loop
    assert table.resolve("item", body_line, body_col)[0] is loop
    after_line, after_col = 23, 11  # `return shape!area()`, past the loop
    assert table.resolve("item", after_line, after_col) == []


def test_detail_lines_render_signatures(table):
    assert named(table, "area").detail == "fn [Shape] area(s) -> float"
    assert named(table, "counter").detail == "co counter(start: int) -> int"
    assert named(table, "sides").detail == "slot sides: int = ..."
    assert named(table, "LIMIT").detail == "with LIMIT: int"


def test_message_overloads_are_collected(table):
    """`fn [Shape] area` and a class body's `fn perimeter` are both
    overloads of a generic function; a plain `fn main()` is not."""
    assert [s.receivers for s in table.messages_named("area")] == [("Shape",)]
    assert [s.receivers for s in table.messages_named("perimeter")] == [("Shape",)]
    assert table.messages_named("main") == []


def test_import_path_segments_are_separately_navigable(table):
    """Each `::` segment resolves to a different module, so clicking `std`
    and clicking `io` must not lead to the same place."""
    segments = [b for b in table.imports if b.symbol_name is None
                and b.node.path[0] == "std"]
    assert [(b.name, b.module_path) for b in segments] == [
        ("std", ("std",)),
        ("io", ("std", "io")),
    ]


def test_import_items_target_a_name_inside_the_module(table):
    items = [b for b in table.imports if b.symbol_name is not None]
    assert [(b.symbol_name, b.module_path, b.bound_as) for b in items] == [
        ("compute", ("util", "helpers"), "compute"),
        ("tidy", ("util", "helpers"), "clean"),
    ]


def test_only_root_and_leaf_segments_bind_a_local_name():
    """`import a::b::c` binds `a` and `c`, never the middle `b` - see
    eval_import. Every segment stays navigable regardless."""
    table = symbols.build(parse("import a::b::c\n"))
    bound = {b.name: b.binds_locally for b in table.imports}
    assert bound == {"a": True, "b": False, "c": True}


def test_bare_import_leaf_is_flagged_ambiguous():
    """`import a::b::c`'s leaf is a module if one exists and a symbol
    exported by `a::b` otherwise - syntax can't tell, so the resolver has
    to try both."""
    leaf = [b for b in symbols.build(parse("import a::b::c\n")).imports
            if b.name == "c"][0]
    assert leaf.ambiguous_leaf

    single = [b for b in symbols.build(parse("import solo\n")).imports][0]
    assert not single.ambiguous_leaf, "a one-segment path has no module to fall back to"


def test_references_are_tagged_by_how_the_name_was_used(table):
    kinds = {(r.name, r.kind) for r in table.references}
    assert ("shape", symbols.REF_NAME) in kinds
    assert ("area", symbols.REF_MESSAGE) in kinds
    assert ("Shape", symbols.REF_TYPE) in kinds, "a class target is a type reference"
    assert ("int", symbols.REF_TYPE) in kinds


def test_resolve_prefers_the_innermost_scope():
    src = "total := 1\n\nfn f(total):\n    return total\n"
    table = symbols.build(parse(src))
    line = 4  # `return total`, inside f
    col = src.splitlines()[3].index("total")
    assert table.resolve("total", line, col)[0].kind == symbols.PARAM
    # ...and at module level, the module-level declaration is what's found.
    assert table.resolve("total")[0].kind == symbols.VARIABLE


def test_position_lookups():
    src = "fn greet(name):\n    return name\n"
    table = symbols.build(parse(src))

    on_definition = table.definition_at(1, 3)  # the `greet` in `fn greet`
    assert on_definition is not None and on_definition.name == "greet"

    on_use = table.reference_at(2, 11)  # the `name` in `return name`
    assert on_use is not None and on_use.name == "name"

    assert table.reference_at(1, 0) is None, "the `fn` keyword references nothing"


def test_enclosing_chain_is_outermost_first():
    src = "class C:\n    fn m():\n        x := 1\n"
    table = symbols.build(parse(src))
    assert [s.name for s in table.enclosing(3, 13)] == ["C", "m"]  # on the `1`
    assert [s.name for s in table.enclosing(3, 8)] == ["C", "m", "x"]  # on `x` itself


def test_scope_chain_reads_a_double_colon_path():
    from wypoc import ast_nodes as ast

    table = symbols.build(parse("x := std::io::println\n"))
    scope = next(n for n in table.tree.walk()
                 if isinstance(n, ast.Scope) and n.name == "println")
    assert symbols.scope_chain(scope) == ["std", "io", "println"]


def test_anonymous_scopes_hold_locals_without_entering_the_outline():
    src = "handler := fn(event):\n    seen := event\n"
    table = symbols.build(parse(src))
    assert [s.name for s in table.module_level()] == ["handler"]
    anon = next(s for s in table.all_symbols() if s.kind == symbols.ANONYMOUS)
    assert {c.name for c in anon.children} == {"event", "seen"}
