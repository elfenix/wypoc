"""Parses wypoc/samples/eval_classes.wy and checks that class evaluation and
`new` build the right metadata / instances. No message dispatch yet, so
area() is checked for existence as class metadata, not called.

Run with:
    PYTHONPATH=. .venv/bin/python wypoc/test_eval_classes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import Class, ClassInstance, Variable, eval_program, instantiate

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "eval_classes.wy")


def main() -> int:
    with open(SAMPLE) as f:
        src = f.read()
    tree = parse(src)
    ctx: dict = {}
    eval_program(tree, ctx)

    failures = 0

    def check(cond, msg):
        nonlocal failures
        if cond:
            print(f"OK   {msg}")
        else:
            print(f"FAIL {msg}")
            failures += 1

    Shape = ctx["Shape"].value
    Circle = ctx["Circle"].value
    Rectangle = ctx["Rectangle"].value
    Square = ctx["Square"].value

    check(isinstance(Shape, Class) and Shape.bases == [], "Shape is a base Class with no parents")
    check(isinstance(Circle, Class) and Circle.bases == [Shape], "Circle's parent is Shape")
    check(isinstance(Rectangle, Class) and Rectangle.bases == [Shape], "Rectangle's parent is Shape")
    check(isinstance(Square, Class) and Square.bases == [Rectangle], "Square's parent is Rectangle")

    check("area" in Shape.methods, "Shape stores an 'area' method (metadata only)")
    check("area" in Circle.methods, "Circle stores its own 'area' method")
    check("radius" in Circle.slots and "radius" not in Shape.slots, "radius is Circle-only")
    check(set(Square.all_slots()) == {"name", "width", "height"},
          "Square's slots (via inheritance) are name/width/height")

    c = ctx["c"].value
    r = ctx["r"].value
    s = ctx["s"].value
    plain = ctx["plain"].value

    check(isinstance(c, ClassInstance) and c.cls is Circle, "new Circle() makes a ClassInstance of Circle")
    check(isinstance(c.attrs["radius"], Variable) and c.attrs["radius"].value == 2.0,
          "Circle instance has radius default 2.0")
    check(c.attrs["name"].value == "shape", "Circle instance inherits name default from Shape")

    check(r.attrs["width"].value == 3.0 and r.attrs["height"].value == 4.0,
          "Rectangle instance has its own width/height defaults")

    check(s.cls is Square, "new Square() makes an instance of Square")
    check(s.attrs["width"].value == 5.0 and s.attrs["height"].value == 5.0,
          "Square instance's width/height override Rectangle's defaults")
    check(s.attrs["name"].value == "shape", "Square instance still inherits name from Shape")

    check(plain.cls is Shape and plain.attrs["name"].value == "shape",
          "new Shape() alone (no subclass) still works")

    # Two instances of the same class must not share slot storage.
    other_circle = instantiate(Circle, [], {})
    other_circle.attrs["radius"].value = 999.0
    check(c.attrs["radius"].value == 2.0, "separate instances don't share slot storage")

    try:
        instantiate(Circle, [5.0], {})
    except NotImplementedError:
        check(True, "new Circle(5.0) raises NotImplementedError (needs message dispatch for init)")
    else:
        check(False, "new Circle(5.0) should have raised NotImplementedError")

    if failures:
        print(f"\n{failures} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
