"""Shows Python objects/functions being exposed into the wyrm ecosystem via
expose()/expose_all()/builtin(), then called from wyrm source
(samples/eval_builtins.wy)."""
import math

from conftest import eval_sample
from wypoc.wyrm_eval_parse_tree import Variable, builtin, expose, expose_all


def test_builtins():
    ctx: dict = {}
    captured = []

    # expose(): register a single Python callable under a wyrm name.
    expose(ctx, "display", captured.append)

    # builtin(): decorator form - keeps `add_one` usable as normal Python too.
    @builtin(ctx, "add_one")
    def _add_one(n):
        return n + 1

    def double(a, b):
        return (a * 2, b * 2)

    # expose_all(): register several names (functions or plain values) at once.
    expose_all(ctx, pi=math.pi, double=double)

    eval_sample("eval_builtins.wy", ctx)

    assert captured == ["hello", 3], "display() calls reached Python"
    assert isinstance(ctx["r"], Variable) and ctx["r"].value == 42, "add_one(41) == 42"
    assert isinstance(ctx["p"], Variable) and ctx["p"].value == math.pi
    assert isinstance(ctx["c"], Variable) and ctx["c"].value == (6, 8)
