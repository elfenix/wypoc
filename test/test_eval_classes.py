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


def test_init_with_args_not_implemented(classes):
    with pytest.raises(NotImplementedError):
        instantiate(classes["Circle"], [5.0], {})
