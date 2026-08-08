"""Source spans on the AST (`ast_nodes.Span`): every node carries one, the
identifier-level `name_pos`/`<field>_pos` extras line up with the names
they describe, and the text a span selects is the text you'd expect.

These are what a jump-to-definition lands on, so a span that's merely
present but off by a token is as broken as a missing one - hence the
`text_at` helper: assertions here compare the *source text a span selects*
rather than raw line/column numbers, which are unreadable in a failure
message and would need rewriting every time a fixture gains a line."""
import glob
import os

import pytest

from conftest import SAMPLES_DIR
from wypoc.parse import parse


def text_at(src: str, span) -> str:
    """The source `span` covers. Spans are 1-based line, 0-based column
    (see ast_nodes.Span)."""
    assert span is not None, "expected a span, got None"
    line, col, end_line, end_col = span
    lines = src.splitlines()
    if line == end_line:
        return lines[line - 1][col:end_col]
    body = [lines[line - 1][col:]]
    body += lines[line:end_line - 1]
    body.append(lines[end_line - 1][:end_col])
    return "\n".join(body)


def parse_and_find(src: str, node_type, index: int = 0):
    matches = [n for n in parse(src).walk() if isinstance(n, node_type)]
    assert len(matches) > index, f"no {node_type.__name__} #{index} in tree"
    return matches[index]


SAMPLE_PATHS = sorted(glob.glob(os.path.join(SAMPLES_DIR, "*.wy")))


@pytest.mark.parametrize(
    "path", SAMPLE_PATHS, ids=[os.path.basename(p) for p in SAMPLE_PATHS]
)
def test_every_node_in_every_sample_has_a_span(path):
    """The whole point of the exercise: no node type may quietly go
    unpositioned. A new grammar rule whose action forgets LOCATIONS fails
    here, on whichever sample first exercises it."""
    with open(path) as f:
        src = f.read()
    unpositioned = sorted({type(n).__name__ for n in parse(src).walk() if n.pos is None})
    assert unpositioned == [], f"nodes with no pos: {unpositioned}"


@pytest.mark.parametrize(
    "path", SAMPLE_PATHS, ids=[os.path.basename(p) for p in SAMPLE_PATHS]
)
def test_spans_are_well_formed(path):
    """Start never after end, and no span pointing past the file."""
    with open(path) as f:
        src = f.read()
    line_count = len(src.splitlines())
    for node in parse(src).walk():
        line, col, end_line, end_col = node.pos
        assert (line, col) <= (end_line, end_col), f"{node} has an inverted span"
        assert 1 <= line <= line_count, f"{node} starts outside the file"
        assert end_line <= line_count, f"{node} ends outside the file"


def test_parallel_name_span_lists_line_up():
    """`Import.path_pos`, `FnDef.class_target_pos`, `TypeExpr.parts_pos`
    and friends are positionally aligned with the name lists they
    annotate - a consumer indexes them together."""
    src = "import std::io\nfn [Canvas, Shape] draw(c):\n    var x: mod::Type = 1\n"
    tree = parse(src)
    imp, fn = tree.body[0], tree.body[1]
    type_expr = fn.body[0].targets[0].type

    assert [text_at(src, s) for s in imp.path_pos] == imp.path == ["std", "io"]
    assert ([text_at(src, s) for s in fn.class_target_pos]
            == fn.class_target == ["Canvas", "Shape"])
    assert ([text_at(src, s) for s in type_expr.parts_pos]
            == type_expr.parts == ["mod", "Type"])


def test_definition_name_span_covers_only_the_name():
    """A definition's `pos` spans the whole construct (that's the fold/hover
    range) while `name_pos` is just the identifier (that's where a go-to
    -definition jump should land)."""
    from wypoc.ast_nodes import ClassDef, FnDef, SlotDef

    src = "class Point:\n    slot x: int = 0\n\n    fn shift(self, dx):\n        pass\n"
    cls = parse_and_find(src, ClassDef)
    assert text_at(src, cls.name_pos) == "Point"
    assert text_at(src, cls.pos).startswith("class Point:")
    assert text_at(src, cls.pos).rstrip().endswith("pass"), "class span covers its body"

    assert text_at(src, parse_and_find(src, SlotDef).name_pos) == "x"

    fn = parse_and_find(src, FnDef)
    assert text_at(src, fn.name_pos) == "shift"
    assert text_at(src, fn.pos).startswith("fn shift")


def test_message_and_attribute_spans():
    """The postfix chain is built by folding, so each link has to widen its
    own span back to where the whole expression started (see
    actions.fold_postfix) while keeping `name_pos` on the message/attribute
    name itself - the two things a `!`-dispatch jump needs."""
    from wypoc.ast_nodes import Attr, Message, Scope

    src = "x := shape!area()\ny := this.origin.x\nz := std::io::println\n"

    msg = parse_and_find(src, Message)
    assert text_at(src, msg.pos) == "shape!area()"
    assert text_at(src, msg.name_pos) == "area"

    # walk() is pre-order, so index 0 is the outermost link of the chain
    # and index 1 the one it wraps.
    outer_attr = parse_and_find(src, Attr)
    assert text_at(src, outer_attr.pos) == "this.origin.x"
    assert text_at(src, outer_attr.name_pos) == "x"
    assert text_at(src, parse_and_find(src, Attr, index=1).pos) == "this.origin"

    scope = parse_and_find(src, Scope)
    assert text_at(src, scope.pos) == "std::io::println"
    assert text_at(src, scope.name_pos) == "println"
    assert text_at(src, parse_and_find(src, Scope, index=1).pos) == "std::io"


def test_binary_operator_span_covers_both_operands():
    """fold_left builds `a + b + c` one BinOp at a time; each has to end up
    spanning everything to its left, not just the operator's own rule."""
    from wypoc.ast_nodes import BinOp

    src = "total := a + b * c\n"
    outer = parse_and_find(src, BinOp)
    assert text_at(src, outer.pos) == "a + b * c"
    assert text_at(src, outer.right.pos) == "b * c"


def test_declaration_spans():
    from wypoc.ast_nodes import For, Param, VarTarget

    src = "fn f(count: int = 0):\n    for item in items:\n        v := item\n"
    assert text_at(src, parse_and_find(src, Param).name_pos) == "count"

    loop = parse_and_find(src, For)
    assert text_at(src, loop.var_pos) == "item"
    assert text_at(src, loop.pos).startswith("for item in items:")

    # `:=` desugars to a VarDecl (see actions.make_assignment_stmt); the
    # synthesized VarTarget has to inherit the original target's spans
    # rather than losing them in the rewrite.
    assert text_at(src, parse_and_find(src, VarTarget).name_pos) == "v"


def test_hand_built_nodes_have_no_span():
    """Spans are always optional - nothing may require them to be present."""
    from wypoc.ast_nodes import Name

    node = Name("x")
    assert node.pos is None
    assert str(node) == "Name(id='x')", "__str__ hides position fields"
