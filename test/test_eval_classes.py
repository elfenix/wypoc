"""Parses samples/eval_classes.wy and checks that class evaluation and `new`
build the right metadata / instances. No message dispatch yet, so area() is
checked for existence as class metadata, not called."""
import pytest

from conftest import eval_sample
from wypoc.wyrm_eval_parse_tree import Class, ClassInstance, Variable, instantiate


@pytest.fixture(scope="module")
def ctx():
    return eval_sample("eval_classes.wy")


@pytest.fixture(scope="module")
def classes(ctx):
    return {
        name: ctx[name].value for name in ("Shape", "Circle", "Rectangle", "Square")
    }


def test_class_hierarchy(classes):
    Shape, Circle, Rectangle, Square = (
        classes["Shape"], classes["Circle"], classes["Rectangle"], classes["Square"]
    )
    assert isinstance(Shape, Class) and Shape.bases == []
    assert isinstance(Circle, Class) and Circle.bases == [Shape]
    assert isinstance(Rectangle, Class) and Rectangle.bases == [Shape]
    assert isinstance(Square, Class) and Square.bases == [Rectangle]


def test_method_and_slot_metadata(classes):
    Shape, Circle, Square = classes["Shape"], classes["Circle"], classes["Square"]
    assert "area" in Shape.methods, "Shape stores an 'area' method (metadata only)"
    assert "area" in Circle.methods, "Circle stores its own 'area' method"
    assert "radius" in Circle.slots and "radius" not in Shape.slots, "radius is Circle-only"
    assert set(Square.all_slots()) == {"name", "width", "height"}


def test_circle_instance(ctx, classes):
    c = ctx["c"].value
    assert isinstance(c, ClassInstance) and c.cls is classes["Circle"]
    assert isinstance(c.attrs["radius"], Variable) and c.attrs["radius"].value == 2.0
    assert c.attrs["name"].value == "shape", "inherits name default from Shape"


def test_rectangle_instance(ctx):
    r = ctx["r"].value
    assert r.attrs["width"].value == 3.0 and r.attrs["height"].value == 4.0


def test_square_instance(ctx, classes):
    s = ctx["s"].value
    assert s.cls is classes["Square"]
    assert s.attrs["width"].value == 5.0 and s.attrs["height"].value == 5.0, (
        "Square instance's width/height override Rectangle's defaults"
    )
    assert s.attrs["name"].value == "shape", "still inherits name from Shape"


def test_plain_shape_instance(ctx, classes):
    plain = ctx["plain"].value
    assert plain.cls is classes["Shape"] and plain.attrs["name"].value == "shape"


def test_instances_dont_share_slot_storage(ctx, classes):
    c = ctx["c"].value
    other_circle = instantiate(classes["Circle"], [], {})
    other_circle.attrs["radius"].value = 999.0
    assert c.attrs["radius"].value == 2.0


def test_class_scoped_static(ctx):
    """Counter's `static total` is shared across every Counter() call's
    `init`, not per-instance - see Class.__init__'s StaticDecl handling."""
    assert ctx["counter_total"].value == 3


def test_construction_rejects_args_with_no_init(classes):
    """Circle defines no `init`, so Circle(5.0) - too many args for a
    no-init construction - is a clear error rather than silently ignored
    (see instantiate())."""
    with pytest.raises(TypeError):
        instantiate(classes["Circle"], [5.0], {})


def test_deep_constructor_chain_recursion_does_not_overflow_the_python_stack():
    """`init` recursively constructing more instances of its own class in
    tail position (`return counter(n - 1)`) is trampolined by
    instantiate()/_instantiate_gen (see wyrm_eval_parse_tree.py) exactly
    like a plain recursive fn or message send - proving instantiate() no
    longer costs one native Python frame per level of constructor-chain
    recursion, only per (bounded) call site."""
    import textwrap

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    source = textwrap.dedent("""\
        class counter:
            slot n: int = 0

        fn [counter] init(n: int):
            this.n = n
            if n > 0:
                return counter(n - 1)
            else:
                return this

        c := counter(100000)
        result := c.n
        """)
    ctx: dict = {}
    eval_program(parse(source), ctx)
    assert ctx["result"].value == 100000
