"""Parses samples/eval_functions.wy and checks function calls + if/elif/else
control flow evaluated correctly."""
import pytest

from conftest import eval_sample
from wypoc.wyrm_eval_parse_tree import Function, Variable

EXPECTED = {
    "sum_direct": 5,
    "greeting_default": "Hello, Wyrm",
    "greeting_custom": "Hey, Wyrm",
    "classify_neg": "negative",
    "classify_zero": "zero",
    "classify_pos": "positive",
    "packed": (1, 2, 3),
    "lambda_result": 9,
    "call_count_1": 1,
    "call_count_2": 2,
    "call_count_3": 3,
    "call_count_declared_1": 1,
    "call_count_declared_2": 2,
}


@pytest.fixture(scope="module")
def ctx():
    return eval_sample("eval_functions.wy")


@pytest.mark.parametrize("name", ["add", "greet", "classify", "pack"])
def test_defines_function(ctx, name):
    var = ctx.get(name)
    assert isinstance(var, Variable) and isinstance(var.value, Function), (
        f"{name}: expected a Function, got {var!r}"
    )


@pytest.mark.parametrize("name,expected", EXPECTED.items(), ids=list(EXPECTED))
def test_result(ctx, name, expected):
    var = ctx.get(name)
    assert isinstance(var, Variable), f"{name} missing from context (got {var!r})"
    assert var.value == expected


def test_deep_tail_recursive_call_does_not_overflow_the_python_stack():
    """A `return f(...)` whose call target is a plain user Function is
    trampolined by _run_driver (see wyrm_eval_parse_tree.py, call_function)
    instead of recursing natively - so a self-recursive call chain like
    this one grows a Python list, not the C stack, and can run far past
    what native recursion allows (~60-70 deep for a similarly shaped
    function - see test_eval_coroutines.py's own recursion test for that
    baseline)."""
    import textwrap

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    source = textwrap.dedent("""\
        fn depth(n: int, acc: int) -> int:
            if n <= 0:
                return acc
            else:
                return depth(n - 1, acc + 1)

        result := depth(100000, 0)
        """)
    ctx: dict = {}
    eval_program(parse(source), ctx)
    assert ctx["result"].value == 100000


def test_deep_non_tail_recursive_call_does_not_overflow_the_python_stack():
    """`depth(n - 1) + 1` - the call wrapped in a BinOp, not itself the
    whole tail expression - used to fall back to native recursion (the
    original, narrower scope of the trampoline). _eval_expr_gen (see
    wyrm_eval_parse_tree.py) now covers a call anywhere in an expression
    tree, not just bare tail position, so this "accumulate on the way back
    up" style - the much more common way recursive functions are actually
    written - is trampolined too."""
    import textwrap

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    source = textwrap.dedent("""\
        fn depth(n: int) -> int:
            if n <= 0:
                return 0
            else:
                return depth(n - 1) + 1

        result := depth(100000)
        """)
    ctx: dict = {}
    eval_program(parse(source), ctx)
    assert ctx["result"].value == 100000


def test_calls_nested_in_argument_position_and_loop_conditions_trampoline():
    """A call as another call's argument (`add(depth(n), depth(n))`) and a
    call driving a `while`/`if` condition both go through _eval_expr_gen's
    general Call handling, not just a statement's own top-level value -
    proving the trampoline isn't limited to a fixed handful of statement
    shapes."""
    import textwrap

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    source = textwrap.dedent("""\
        fn depth_is_zero(n: int) -> bool:
            return n <= 0

        fn depth(n: int) -> int:
            if depth_is_zero(n):
                return 0
            else:
                return depth(n - 1) + 1

        fn add(a: int, b: int) -> int:
            return a + b

        result := add(depth(20000), depth(20000))
        """)
    ctx: dict = {}
    eval_program(parse(source), ctx)
    assert ctx["result"].value == 40000


def test_genuinely_infinite_tail_recursion_raises_a_catchable_recursion_error():
    """_run_driver's explicit stack is heap-bounded, not C-stack-bounded, so
    a genuinely infinite tail-recursive loop (a bug, not deep-but-finite
    recursion) would otherwise just grow memory forever instead of ever
    raising anything - _MAX_DRIVER_DEPTH (see wyrm_eval_parse_tree.py) caps
    it with a plain, catchable-by-Python-except RecursionError instead.
    Patches the module's own limit down so the test doesn't have to
    actually grow a million-entry stack to observe it."""
    import textwrap

    import wypoc.wyrm_eval_parse_tree as wept
    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    source = textwrap.dedent("""\
        fn loop(n: int) -> int:
            return loop(n + 1)

        result := loop(0)
        """)
    original_limit = wept._MAX_DRIVER_DEPTH
    wept._MAX_DRIVER_DEPTH = 100
    try:
        with pytest.raises(RecursionError, match="wyrm-level call depth exceeded"):
            eval_program(parse(source), {})
    finally:
        wept._MAX_DRIVER_DEPTH = original_limit


def test_mutual_tail_recursion_does_not_overflow_the_python_stack():
    """Two functions ping-ponging a tail call between each other - proves
    the driver's shared explicit stack handles a call chain that isn't
    self-recursion, not just a tight self-loop."""
    import textwrap

    from wypoc.parse import parse
    from wypoc.wyrm_eval_parse_tree import eval_program

    source = textwrap.dedent("""\
        fn is_even(n: int) -> bool:
            if n <= 0:
                return true
            else:
                return is_odd(n - 1)

        fn is_odd(n: int) -> bool:
            if n <= 0:
                return false
            else:
                return is_even(n - 1)

        result := is_even(100001)
        """)
    ctx: dict = {}
    eval_program(parse(source), ctx)
    assert ctx["result"].value is False
