"""The sample sweep: every `wypoc/samples/*.wy` the compiler accepts, run under
the VM and under the tree walker, compared.

M6's exit criterion (gen/bytecode-vm-plan.md). The bytecode fixtures in
test/bytecode/ were written for the compiler and are small on purpose; the
sample corpus is the interpreter's own, written with no bytecode in mind, and
running it is what says the VM handles the language rather than the fixtures.

Nothing here is allowed to be vague. A sample is in exactly one of three
lists, each with its reason, and a sample that changes category fails the
test rather than quietly moving:

* it compiles and both runs agree - the default, and no list at all;
* the compiler refuses it, for a reason named in REFUSED;
* it compiles but the VM cannot match the interpreter, for a reason named in
  DIVERGES, which points at the doc entry that owns the gap.
"""

import io
import os
import contextlib

import pytest
from conftest import SAMPLES_DIR

from wypoc import wyrm_eval_parse_tree as ev
from wypoc import wyrm_io
from wypoc import wyrm_modules
from wypoc.compiler_bc import compile_module
from wypoc.compiler_bc.errors import CompileError
from wypoc.parse import parse
from wypoc.vm import load, load_module

# Samples the compiler will not take, and why. Every one of these is a
# construct doc/llm-bytecode.md §9 or §8.5 already accounts for, or a sample
# that is a *fragment* - written to be run with names its harness supplies
# rather than as a whole program.
REFUSED = {
    "basics.wy": "`with` has been removed from the language",
    "classes.wy": "a fragment: `canvas` comes from the test harness, not the file",
    "control_flow.wy": "a fragment: `condition` comes from the test harness",
    "decorators.wy": "its decorators build trees the compiler expands at compile time",
    "eval_builtins.wy": "a fragment: `display` is a Python function the test exposes",
    "eval_classes.wy": "a fragment: `total` comes from the test harness",
    "eval_defer_with_do.wy": "`with` has been removed from the language",
    "eval_io.wy": "a fragment: `path` comes from the test harness",
    "eval_signals.wy": "`signal` in a class body is not lowered (§8.5)",
    "lexical.wy": "`with` has been removed from the language",
}

# Samples that compile but whose output the VM cannot match, with the gap that
# owns them. One entry, and it is a *lowering* gap rather than a VM one.
DIVERGES = {
    "eval_messages.wy": (
        "message promotion: the interpreter turns a plain `fn describe()` into "
        "the wildcard overload of the message `describe` becomes, so a receiver "
        "with no specific overload still dispatches. The compiler emits no "
        "`reg_msg` for the plain function, so the compiled message has only the "
        "typed arm - see doc/llm-bytecode.md §9."
    ),
}


def sample_names():
    return sorted(n for n in os.listdir(SAMPLES_DIR) if n.endswith(".wy"))


@pytest.fixture(autouse=True)
def _fresh_runtime():
    ev.clear_module_cache()
    yield
    ev.clear_module_cache()


def compile_sample(name):
    with open(os.path.join(SAMPLES_DIR, name)) as handle:
        source = handle.read()
    return compile_module(parse(source, filename=name), name[:-3], name), source


def capture(run):
    """Run something, answering what it printed.

    `println` writes to the STDOUT handle wyrm_io bound at import time, so the
    handle is repointed at the redirect for the duration - the same dance
    test_compiler_bc.py does.
    """
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        wyrm_io._reset_std_handles()
        try:
            run()
        finally:
            wyrm_io._reset_std_handles()
    return out.getvalue()


def under_the_walker(source, name):
    ctx = ev.Scope()
    ev.populate_globals(ctx)
    ev.expose(ctx, "__ARGS", ())  # what cli.py binds for every run
    return capture(lambda: ev.eval_program(parse(source, filename=name), ctx))


def under_the_vm(built):
    return capture(lambda: load_module(load(built.to_wyc())))


@pytest.mark.parametrize("name", sample_names())
def test_a_sample_runs_the_same_way_under_both(name):
    previous = wyrm_modules.set_script_root(SAMPLES_DIR)
    try:
        if name in REFUSED:
            with pytest.raises(CompileError):
                compile_sample(name)
            return

        built, source = compile_sample(name)
        if name in DIVERGES:
            # Still asserted, just from the other side: it must still fail,
            # and when it stops failing this entry comes out.
            with pytest.raises(Exception):
                under_the_vm(built)
            return

        assert under_the_vm(built) == under_the_walker(source, name)
    finally:
        wyrm_modules.set_script_root(previous)


def test_every_sample_is_accounted_for():
    """No sample is silently absent: each is compiled-and-compared, refused
    for a named reason, or diverging for one."""
    covered = set(REFUSED) | set(DIVERGES)
    assert covered <= set(sample_names())


# --------------------------------------------------------------------------
# `is`, name by name


TYPE_CHECKS = (
    ("nil", "nil"), ("bool", "true"), ("int", "7"), ("uint", "7"),
    ("float", "1.5"), ("str", '"x"'), ("sym", "'ready"), ("list", "[1]"),
    ("tuple", "(1, 2)"), ("pair", "cons(1, 2)"), ("error", 'error("e")'),
)


@pytest.mark.parametrize("type_name, literal", TYPE_CHECKS)
def test_a_primitive_type_check_answers_the_same_under_both(type_name, literal):
    """Every name in the interpreter's primitive-type table, checked against
    a value of that type and against one that is not.

    Only int/float/bool/sym used to agree: the compiler resolved the type
    name to whatever it was bound to, and `str` is a builtin, `error` a
    Class, `tuple` and `pair` constructor functions, `list`/`dict`/`uint`
    bound to nothing at all - so these answered false compiled and true
    interpreted, or refused to compile.
    """
    source = (f'println({literal} is {type_name})\n'
              f'println("no" is {type_name})\n')
    name = f"is_{type_name}.wy"
    built = compile_module(parse(source, filename=name), name[:-3], name)
    assert under_the_vm(built) == under_the_walker(source, name)


def test_a_dict_type_check_answers_the_same_under_both():
    # Split out only because a dict literal needs a statement to build it.
    source = 'd := {"k": 1}\nprintln(d is dict, 1 is dict)\n'
    built = compile_module(parse(source, filename="is_dict.wy"), "is_dict", "is_dict.wy")
    assert under_the_vm(built) == under_the_walker(source, "is_dict.wy")


def test_a_sum_type_mixing_a_class_and_a_primitive_answers_the_same():
    source = (
        "class Shape:\n"
        '    slot name: str = "shape"\n'
        "\n"
        "s := Shape()\n"
        "println(s is Shape | int, 5 is Shape | int, \"x\" is Shape | int)\n"
    )
    built = compile_module(parse(source, filename="is_sum.wy"), "is_sum", "is_sum.wy")
    assert under_the_vm(built) == under_the_walker(source, "is_sum.wy")


# --------------------------------------------------------------------------
# block scoping


SHADOWING = (
    ("a `do:` body", "fn f():\n    var a = 1\n    do:\n        var a = 2\n    println(a)\nf()\n"),
    ("an `if` body", "fn f():\n    var a = 1\n    if true:\n        var a = 2\n    println(a)\nf()\n"),
    ("an `else` body", "fn f():\n    var a = 1\n    if false:\n        pass\n    else:\n        var a = 2\n    println(a)\nf()\n"),
    ("a `while` body", "fn f():\n    var a = 1\n    var n = 0\n    while n < 2:\n        var a = 9\n        n = n + 1\n    println(a)\nf()\n"),
    ("a `for` body", "fn f():\n    var a = 1\n    for i in [1, 2]:\n        var a = 7\n    println(a)\nf()\n"),
    ("a `for` variable", "fn f():\n    var i = 99\n    for i in [1, 2]:\n        pass\n    println(i)\nf()\n"),
    ("two levels of `do:`", "fn f():\n    var a = 1\n    do:\n        var a = 2\n        do:\n            var a = 3\n        println(a)\n    println(a)\nf()\n"),
    ("the module's top level", "var a = 1\ndo:\n    var a = 2\nprintln(a)\n"),
)


@pytest.mark.parametrize("where, source", SHADOWING,
                         ids=[where for where, _ in SHADOWING])
def test_a_declaration_in_a_nested_block_shadows_rather_than_overwrites(where, source):
    """Spec: "declaring a name visible from an enclosing scope is permitted
    and shadows it for the duration of the inner scope" - so the outer name
    is untouched by the inner one, in a compiled module as in an interpreted
    one. The compiler used to give both declarations one slot, so the inner
    wrote through to the outer."""
    name = "shadow.wy"
    built = compile_module(parse(source, filename=name), "shadow", name)
    assert under_the_vm(built) == under_the_walker(source, name)


def test_an_inner_block_still_assigns_through_to_an_outer_name():
    """The other half of the rule: a plain assignment is not a declaration,
    so it reaches the enclosing binding rather than making a new one."""
    source = "fn f():\n    var a = 1\n    do:\n        a = 2\n    println(a)\nf()\n"
    built = compile_module(parse(source, filename="assign.wy"), "assign", "assign.wy")
    assert under_the_vm(built) == under_the_walker(source, "assign.wy")


def test_sibling_blocks_each_get_their_own_binding():
    source = (
        "fn f():\n"
        "    do:\n"
        "        var a = 1\n"
        "        println(a)\n"
        "    do:\n"
        "        var a = 2\n"
        "        println(a)\n"
        "f()\n"
    )
    built = compile_module(parse(source, filename="siblings.wy"), "siblings", "siblings.wy")
    assert under_the_vm(built) == under_the_walker(source, "siblings.wy")
