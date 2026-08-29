"""Running a whole module image (wypoc/vm/run.py, and the call and linking
half of wypoc/vm/interp.py).

M2 of gen/bytecode-vm-plan.md, whose gate is `hello.wy` running with the
output the interpreter produces. The milestone's real subject is not the
opcodes it adds but the two rules underneath them:

* **Output equivalence.** A fixture's compiled image must print what the tree
  walker prints for the same source. Where doc/wyc-format.md says it cannot -
  the multi-value gap - the expected output is checked in as a file and the
  difference is asserted, so a new divergence is a failure rather than a
  shrug.
* **Fail loud.** A fixture this milestone cannot run yet must stop with a
  named opcode, never print something plausible. That is what the second
  parametrized test is for, and moving a fixture from one list to the other
  is how a later milestone records what it finished.
"""

import os

import pytest
from conftest import BYTECODE_DIR
from conftest import bytecode_fixture_names as fixture_names
from conftest import compile_bytecode_fixture as compile_fixture
from conftest import compile_bytecode_source as compile_source

from wypoc import wyrm_builtins
from wypoc import wyrm_eval_parse_tree as ev
from wypoc import wyrm_io
from wypoc import wyrm_modules
from wypoc.compiler_bc import opcodes
from wypoc.compiler_bc.image import ModuleImage
from wypoc.vm.errors import TrapError
from wypoc.parse import parse
from wypoc.vm import imports as vm_imports
from wypoc.vm import (
    BytecodeFunction,
    LoadedModule,
    TrapError,
    call_function,
    execute,
    for_init,
    load,
    load_module,
    run_file,
    run_image,
)

L = opcodes.L


@pytest.fixture(autouse=True)
def _fresh_runtime():
    """Two pieces of process-wide state every test here touches.

    Running an image publishes it in the walker's module cache (§7.1 step 6),
    so that is cleared around each test - one test's `hello` must not become
    another's import. And wyrm's `__STDOUT` was bound to `sys.stdout` when
    wyrm_io was imported, before pytest swapped it out, so it is repointed at
    the current one: `println` writes through that handle, not through
    `print`, and a stale handle is a closed file by the next test. A test
    that wants to *read* what was printed repoints it again from inside its
    own body, where `capsys`'s stdout is the one installed.
    """
    wyrm_io._reset_std_handles()
    ev.clear_module_cache()
    yield
    ev.clear_module_cache()


def run_under_vm(name):
    """Run a fixture's image; answer the module it left behind.

    The search root is the fixture's own directory for the run as well as the
    compile: a module's neighbours are importable from the directory it lives
    in, whichever kind of module is doing the importing.
    """
    wyrm_io._reset_std_handles()  # inside the test body: see _fresh_runtime
    image = load(compile_fixture(name).to_wyc())
    previous = wyrm_modules.set_script_root(
        os.path.dirname(os.path.join(BYTECODE_DIR, name))
    )
    try:
        return load_module(image)
    finally:
        wyrm_modules.set_script_root(previous)


def run_under_interpreter(name):
    """Run the same fixture's source; answer the scope it left behind."""
    wyrm_io._reset_std_handles()
    path = os.path.join(BYTECODE_DIR, name)
    with open(path) as handle:
        source = handle.read()
    ctx = ev.Scope()
    ev.populate_globals(ctx)
    previous = wyrm_modules.set_script_root(os.path.dirname(path))
    try:
        ev.eval_program(parse(source, filename=name), ctx)
    finally:
        wyrm_modules.set_script_root(previous)
    return ctx


# Fixtures this milestone can run end to end. Later milestones move names up
# from the list below as they implement what those fixtures need.
RUNNABLE = [
    "arith.wy",
    "classes.wy",
    "closures.wy",
    "collections.wy",
    "control_flow.wy",
    "coroutines.wy",
    "errors.wy",
    "decorators/declib.wy",   # a library of decorators: defines, runs nothing
    "decorators/decorated.wy",
    "hello.wy",
    "hello_1.wy",
    "hello_2.wy",
    "hello_3.wy",
    "messages.wy",
    "multiret.wy",
    "two_module/geometry.wy",
    "two_module/report.wy",
    "wildcard/palette.wy",
    "wildcard/paint.wy",
]

# Where output equivalence must *not* hold, with the reason and the checked-in
# expected output (`<name>.vm.out` beside the fixture).
MULTI_VALUE_GAP = (
    "the interpreter models `return a, b` as a single tuple value, so "
    "`println(f())` prints the whole pair. A VM implementing spec 1.3 honestly "
    "drops every result past the first, because the call asked for one. This "
    "is doc/llm-bytecode.md 9's multi-value gap, and it is the only divergence "
    "any fixture has shown so far."
)

DIVERGENCES = {
    "arith.wy": MULTI_VALUE_GAP,
    "collections.wy": MULTI_VALUE_GAP,
    "multiret.wy": MULTI_VALUE_GAP,
}


# Globals a compiled module has that its interpreted twin does not, with why.
# Kept per (fixture, name) rather than waved past, so a *new* extra global is
# a failure rather than a shrug - the same discipline DIVERGENCES applies to
# output.
EXTRA_GLOBALS = {
    ("wildcard/paint.wy", "palette"): (
        "a compiled `import a::*` binds the module under its root name as well "
        "as registering its namespace for the wildcard search, where the "
        "interpreter's wildcard import binds only the names it pulls in. The "
        "extra binding is invisible unless the source names `palette` itself."
    ),
}


NOT_RUNNABLE = [name for name in fixture_names() if name not in RUNNABLE]


def divergence_path(name):
    return os.path.join(BYTECODE_DIR, os.path.splitext(name)[0] + ".vm.out")


@pytest.mark.parametrize("name", RUNNABLE)
def test_a_compiled_fixture_prints_what_the_interpreter_prints(name, capsys):
    module = run_under_vm(name)
    got = capsys.readouterr().out
    ctx = run_under_interpreter(name)
    walker = capsys.readouterr().out

    if name in DIVERGENCES:
        with open(divergence_path(name)) as handle:
            assert got == handle.read()
        # Asserted in both directions: if the two ever agree again, this
        # fixture no longer belongs in DIVERGENCES.
        assert got != walker
    else:
        assert got == walker

    # A fixture that prints nothing - a library module - still has to agree
    # about what it left behind, or equivalence would mean "neither crashed".
    for defined, value in module.namespace().items():
        if (name, defined) in EXTRA_GLOBALS:
            assert defined not in ctx, f"{defined!r} is no longer an extra global"
            continue
        assert comparable(value) == comparable(ev.unwrap(ctx[defined])), \
            f"global {defined!r} differs"


def comparable(value):
    """A global reduced to something the two runs can be compared on.

    A function, a class and an instance are all identity-valued - two runs
    build two objects and no `==` relates them - so each is reduced to what a
    reader would actually check: that both sides have a function there, that
    the classes have the same name, and that the instances are of the same
    class with the same slot values.
    """
    if isinstance(value, (BytecodeFunction, ev.Function, ev.Coroutine)) or callable(value):
        return "<callable>"
    if isinstance(value, ev.BoundMessage):
        # `recv ! name` without the call: same message, same receivers.
        return ("bound", value.overload.signature[0].name if value.overload.signature else None,
                [comparable(r) for r in value.receivers])
    if isinstance(value, ev.Class):
        return ("class", value.name)
    if isinstance(value, ev.CoroutineInstance):
        # Identity again, and a live one: both sides have driven it the same
        # number of times, so its name and state are what can be compared.
        return ("coroutine", value.node.name, repr(value).split(", ")[-1])
    if isinstance(value, ev.ClassInstance):
        slots = {name: comparable(ev.unwrap(v)) for name, v in value.attrs.items()}
        return ("instance", value.cls.name, slots)
    return value


def test_the_runnable_list_covers_the_corpus():
    """Every fixture is either gated for output equivalence above or listed
    as one the VM cannot run yet - never quietly absent from both."""
    assert sorted(RUNNABLE) + NOT_RUNNABLE == fixture_names()


def test_a_fixture_the_vm_cannot_run_stops_by_name(capsys):
    """Never a silent wrong answer: whatever is missing is named. The list is
    empty today - every fixture runs - and this is what keeps a new one from
    being added in the "prints nothing much" way instead."""
    for name in NOT_RUNNABLE:
        image = load(compile_fixture(name).to_wyc())
        wyrm_io._reset_std_handles()
        with pytest.raises(TrapError) as caught:
            run_image(image)
        assert "not implemented yet" in str(caught.value)


# --------------------------------------------------------------------------
# calls and the backfill rule (§1.3)


def two_results():
    """A module whose `pair()` returns two values, and whose init calls it."""
    return compile_source(
        "fn pair():\n    return 1, 2\n",
        name="pairs",
    )


def test_a_callee_returns_its_whole_window():
    module = LoadedModule(load(two_results().to_wyc()))
    assert call_function(module, 0) == [1, 2]


@pytest.mark.parametrize(
    "nres, want",
    [
        (0, []),
        (1, [1]),                       # the extra result is discarded
        (2, [1, 2]),
        (4, [1, 2, wyrm_builtins.NIL, wyrm_builtins.NIL]),  # the missing ones are nil
    ],
)
def test_a_call_pads_and_truncates_to_the_results_it_asked_for(nres, want):
    """§1.3's backfill rule, which every later milestone leans on: multi-value
    return, single-value return and "no value at all" are one mechanism."""
    built = two_results()
    # Init: closure the function into L0, call it, return the window.
    base = 4
    built.init_nlocals = base + max(nres, 1) + 1
    built.code = []
    built.emit(opcodes.pack("closure", a0=L(base), a1=0, f=0))
    built.emit(opcodes.pack("call", a0=L(base), f=0, a1=nres))
    built.emit(opcodes.pack("return", a0=L(base), f=nres))
    built.functions[0].code_offset = len(built.code)
    built.emit(opcodes.pack_pairable("i8", L(0), 1))
    built.emit(opcodes.pack_pairable("i8", L(1), 2))
    built.emit(opcodes.pack("return", a0=L(0), f=2))

    module = LoadedModule(load(built.to_wyc()))
    assert execute(module, for_init(module.image, module)) == want


def test_a_compiled_function_is_callable_from_interpreted_code():
    """Interop from day one: `call_value` reaches a compiled function through
    its `callable(func)` branch, so nothing in the evaluator knows about the
    VM at all."""
    module = load_module(load(compile_fixture("hello.wy").to_wyc()))
    greet = module.namespace()["greet"]
    assert isinstance(greet, BytecodeFunction)
    assert ev.call_value(greet, ["World"], {}) == "Hello World"


def test_a_compiled_body_calls_back_into_the_runtime():
    module = load_module(load(compile_source("fn f(a):\n    return str(a)\n").to_wyc()))
    assert call_function(module, 0, [3]) == ["3"]


# --------------------------------------------------------------------------
# globals and the interop façade


def test_init_leaves_its_globals_in_the_module_s_slots():
    module = load_module(load(compile_source("var a = 1\nvar b = a + 41\n").to_wyc()))
    assert module.namespace() == {"a": 1, "b": 42}


def test_interpreted_code_reads_and_writes_a_compiled_module_s_globals():
    module = load_module(load(compile_source("var a = 1\n").to_wyc()))
    cell = module.as_module().ctx["a"]
    assert ev.unwrap(cell) == 1
    cell.value = 7
    assert module.namespace()["a"] == 7  # one storage, seen from both sides


def test_a_module_is_published_before_its_init_runs():
    """§7.1 step 6. The early publish used to be what made an import cycle
    observable rather than divergent; cycles are illegal now
    (doc/addendum.md), so it is what lets `::` navigation into a package work
    while that package initialises - and what puts the module somewhere a
    re-entry can be *caught*, rather than somewhere it can be tolerated."""
    module = load_module(load(compile_source("var a = 1\n", name="pub").to_wyc()))
    published = ev.import_module.__globals__["_module_cache"]["pub"]
    assert published is module.as_module()
    assert ev.unwrap(published.ctx["a"]) == 1


# --------------------------------------------------------------------------
# linking (§7.2, §7.3)


def test_a_referenced_builtin_is_filled_into_its_global_slot():
    """`println` is a free name: a global slot the load-time builtin pass
    fills, read afterwards by an ordinary `gget` (doc/addendum.md). No
    relocation, no bind table, and the value is the builtin the walker's own
    scope holds."""
    module = load_module(load(compile_fixture("hello.wy").to_wyc()))
    slot = module.image.free["println"]
    assert module.fill_layer[slot][0] == module.LAYER_BUILTIN
    assert module.globals[slot] is ev.unwrap(module.scope.get("println"))


def test_a_name_that_nothing_fills_faults_when_it_is_read():
    """A name nothing supplies is a free slot that stays Unset, and reading it
    fails the way any declared-but-unassigned variable does - the bespoke
    NotFound error value is gone (doc/addendum.md).

    The compiler will not emit a reference it cannot place, so this image is
    hand-assembled; it is the shape a wildcard import produces when the
    wildcard turns out not to supply the name."""
    image = ModuleImage("orphan")
    slot = image.add_free_global("nosuchname")
    image.add_global("found")
    image.init_nlocals = 1
    image.emit(opcodes.pack_pairable("gget", slot, L(0)))
    image.emit(opcodes.pack_pairable("gset", image.global_index("found"), L(0)))
    image.emit(opcodes.pack("return", f=0))

    with pytest.raises(TrapError, match="nosuchname"):
        load_module(load(image.to_wyc()))


def test_a_name_a_function_body_reads_is_filled_before_init_runs():
    """There is nothing left to defer to a function's first call.

    A body's external names are global slots like init's, filled by the same
    passes at the same moments - the builtins before init starts, each import
    as it runs. Deferring existed to survive import cycles, and those are
    illegal now (doc/addendum.md), so the function-level referenced-name set
    is gone with it."""
    module = load_module(load(compile_source("fn f():\n    return str(1)\n").to_wyc()))
    slot = module.image.free["str"]
    assert module.globals[slot] is not ev.UNSET, "filled at load, not at call"
    assert call_function(module, 0) == ["1"]


def test_a_compiled_frame_and_an_interpreted_one_interleave():
    """The integration the plan calls out as the real design risk: compiled
    init calls an *interpreted* function, which calls back into a compiled
    one, and both stacks unwind correctly.

    The walker drives calls through a generator trampoline and the VM through
    its own explicit frame stack. Here one is suspended inside the other,
    twice over, and the answer still comes back.

    Hand-assembled because a compiler will not emit a reference to a name it
    cannot place - reaching an interpreted module by name is what `import`
    is for, and that arrives with M5.
    """
    ctx = ev.Scope()
    ev.populate_globals(ctx)
    ev.eval_program(parse("fn apply(f, x):\n    return f(x)\n"), ctx)

    image = ModuleImage("interleave")
    image.add_global("answer")
    apply_slot = image.add_free_global("apply")
    double = image.add_function("double", params=["n"], nlocals=1)
    image.init_nlocals = 3
    image.emit(opcodes.pack_pairable("gget", apply_slot, L(0)))
    image.emit(opcodes.pack("closure", a0=L(1), a1=double, f=0))
    image.emit(opcodes.pack_pairable("i8", L(2), 21))
    image.emit(opcodes.pack("call", a0=L(0), f=2, a1=1))
    image.emit(opcodes.pack_pairable("gset", 0, L(0)))
    image.emit(opcodes.pack("return", f=0))
    image.functions[double].code_offset = len(image.code)
    image.emit(opcodes.pack("add", a0=L(0), a1=opcodes.P(0), a2=opcodes.P(0)))
    image.emit(opcodes.pack("return", a0=L(0), f=1))

    module = load_module(load(image.to_wyc()), scope=ctx)
    assert module.namespace()["answer"] == 42


# --------------------------------------------------------------------------
# the object language across the seam (§8.6)


MIXED = '''
class Shape:
    slot name: str = "shape"

    fn describe():
        return this.name

fn tell(what):
    return what ! describe()
'''


def function_named(module, name):
    return next(i for i, fn in enumerate(module.image.functions) if fn.name.rstrip("!") == name)


def test_an_interpreted_class_subclasses_a_compiled_one_and_dispatch_works_both_ways():
    """The milestone's real gate. A class realised from an image and one
    evaluated from source are the same kind of object in the same message
    table, so inheritance and override work across the seam in both
    directions - which is only true because the VM registers its methods as
    overloads rather than keeping a dispatch table of its own.
    """
    module = load_module(load(compile_source(MIXED, name="mixed").to_wyc()))
    shape = module.namespace()["Shape"]
    assert isinstance(shape, ev.Class)

    # Binding the compiled class by name is what an `import` will do for
    # itself in M5; here it is the one line that stands in for one.
    ev.bind_new("Shape", shape, module.scope)
    ev.eval_program(
        parse(
            "class Loud(Shape):\n"
            "    fn describe():\n"
            "        return \"LOUD\"\n"
            "\n"
            "    fn twice():\n"
            "        return this ! describe() + this ! describe()\n"
        ),
        module.scope,
    )
    loud = ev.unwrap(module.scope["Loud"])
    assert ev._class_distance(loud, shape) == 1  # it really is a subclass

    plain = ev.instantiate(shape, [], {})
    shouty = ev.instantiate(loud, [], {})

    # A compiled method reached through an interpreted instance's inherited
    # slot, and an interpreted override winning over the compiled one.
    assert ev.send_message("describe", [plain], [], {}, module.scope) == "shape"
    assert ev.send_message("describe", [shouty], [], {}, module.scope) == "LOUD"
    assert ev.send_message("twice", [shouty], [], {}, module.scope) == "LOUDLOUD"

    # And compiled code sending the message: the override is what it reaches.
    tell = function_named(module, "tell")
    assert call_function(module, tell, [plain]) == ["shape"]
    assert call_function(module, tell, [shouty]) == ["LOUD"]


def test_a_compiled_class_instantiates_through_the_runtime_s_own_construction():
    """`Cls(...)` is an ordinary call on the class value (§8.6): the runtime
    builds the instance, applies the image's slot defaults, and dispatches
    `init` like any other message."""
    module = load_module(load(compile_source(
        "class Point:\n"
        "    slot x: int = 1\n"
        "    slot y: int = 2\n"
        "\n"
        "    fn init(a):\n"
        "        this.x = a\n",
        name="pt",
    ).to_wyc()))
    point = module.namespace()["Point"]

    made = ev.call_value(point, [9], {})
    assert (ev.unwrap(made.attrs["x"]), ev.unwrap(made.attrs["y"])) == (9, 2)

    # Uninitialised construction leaves every slot at its image default.
    bare = ev.new_instance(point)
    assert (ev.unwrap(bare.attrs["x"]), ev.unwrap(bare.attrs["y"])) == (1, 2)


# --------------------------------------------------------------------------
# trees a compiled module rebuilds (§ doc/llm-bytecode.md, `foo::$ast`)


AST_SOURCE = (
    "fn greet(name):\n"
    "    return name\n"
    "\n"
    "tree := greet::$ast\n"
)


def test_a_compiled_module_rebuilds_the_same_tree_the_interpreter_hands_out():
    """A compiled module carries no ASTs: `foo::$ast` is emitted as the code
    that rebuilds its s-expression. What that code builds must be what the
    walker's own `sexpr()` answers for the same definition - and `sexpr()` is
    idempotent on an s-expression, so both sides can be asked the same way.

    This is the test that catches a mis-built tree. The compiler cannot check
    itself here: its golden listing is just as happy with instructions that
    build the wrong value, which is exactly what it did before a VM ran them.
    """
    module = run_under_vm_source(AST_SOURCE, "asts")
    rebuilt = module.namespace()["tree"]

    ctx = ev.Scope()
    ev.populate_globals(ctx)
    ev.eval_program(parse(AST_SOURCE), ctx)

    assert ev.sexpr_value(module.scope, rebuilt) == ev.sexpr_value(ctx, ev.unwrap(ctx["tree"]))


def run_under_vm_source(source, name):
    wyrm_io._reset_std_handles()
    return load_module(load(compile_source(source, name=name).to_wyc()))


# --------------------------------------------------------------------------
# across the module boundary (§7.1, §7.3)


def build_images(directory, *names):
    """Compile fixtures into `directory` as real `.wyc` files on disk."""
    for name in names:
        built = compile_fixture(name)
        target = os.path.join(directory, os.path.basename(name)[:-3] + ".wyc")
        with open(target, "wb") as handle:
            handle.write(built.to_wyc())
    return directory


def test_a_compiled_module_imports_a_compiled_one(tmp_path, capsys):
    """The milestone's gate: `report.wyc` importing `geometry.wyc`, with no
    source anywhere in the picture."""
    build_images(str(tmp_path), "two_module/geometry.wy", "two_module/report.wy")
    previous = wyrm_modules.set_script_root(str(tmp_path))
    try:
        wyrm_io._reset_std_handles()
        run_file(str(tmp_path / "report.wyc"))
    finally:
        wyrm_modules.set_script_root(previous)

    assert capsys.readouterr().out == "6cm\ncm\n"
    # And it really was the image that answered, not a source file elsewhere.
    assert ev.module_cache()["geometry"].path.endswith("geometry.wyc")


def test_a_compiled_module_imports_an_interpreted_one(capsys):
    """The same importer, reaching a dependency that is still source: the
    fixture directory has `geometry.wy` and no image, so `import geometry`
    lands in the tree walker and its module comes back through the same
    relocations."""
    run_under_vm("two_module/report.wy")
    assert capsys.readouterr().out == "6cm\ncm\n"
    assert ev.module_cache()["geometry"].path.endswith("geometry.wy")


def test_an_interpreted_module_imports_a_compiled_one(tmp_path, capsys):
    """The reverse direction. Source wins wherever it exists, so this proves
    the fallback the other way: an image is a module too, and an interpreted
    `import` reaches it when it is the only thing there."""
    build_images(str(tmp_path), "two_module/geometry.wy")
    with open(os.path.join(BYTECODE_DIR, "two_module", "report.wy")) as handle:
        source = handle.read()

    previous = wyrm_modules.set_script_root(str(tmp_path))
    try:
        wyrm_io._reset_std_handles()
        ctx = ev.Scope()
        ev.populate_globals(ctx)
        ev.eval_program(parse(source, filename="report.wy"), ctx)
    finally:
        wyrm_modules.set_script_root(previous)

    assert capsys.readouterr().out == "6cm\ncm\n"
    assert ev.module_cache()["geometry"].path.endswith("geometry.wyc")


def test_a_module_loads_once_however_it_is_reached(tmp_path):
    """One process, one instance: a compiled importer and an interpreted one
    must end up looking at the same module object, or a global written through
    one would be invisible through the other."""
    build_images(str(tmp_path), "two_module/geometry.wy")
    previous = wyrm_modules.set_script_root(str(tmp_path))
    try:
        first = ev.import_module(["geometry"])
        second = ev.import_module(["geometry"])
        third = vm_imports.import_path(["geometry"])
    finally:
        wyrm_modules.set_script_root(previous)
    assert first is second is third


# --------------------------------------------------------------------------
# parameter binding: defaults and the collecting parameters (§8.5)


def compiled(source, name="params", **options):
    wyrm_io._reset_std_handles()
    return load_module(load(compile_source(source, name=name, **options).to_wyc()))


def test_a_constant_default_comes_from_the_static_pool():
    module = compiled("fn greet(name, greeting = \"hi\"):\n    return greeting + \" \" + name\n")
    index = function_named(module, "greet")
    assert call_function(module, index, ["you"]) == ["hi you"]
    assert call_function(module, index, ["you", "yo"]) == ["yo you"]


def test_varargs_and_kwargs_arrive_pre_collected():
    """§8.5: a collecting parameter is an ordinary P slot; only the function's
    flags say it collects. The binding rules are the interpreter's, so a
    compiled call and an interpreted one accept the same arguments."""
    module = compiled(
        "fn tally(first, *rest, **named):\n"
        "    return first, rest, named\n",
        name="collect",
    )
    index = function_named(module, "tally")
    assert call_function(module, index, [1, 2, 3]) == [1, (2, 3), {}]
    assert call_function(module, index, [1], kwargs={"tag": "x"}) == [1, (), {"tag": "x"}]


def test_a_missing_argument_names_the_parameter():
    module = compiled("fn need(a, b):\n    return a\n", name="need")
    with pytest.raises(TrapError, match="missing required argument: 'b'"):
        call_function(module, function_named(module, "need"), [1])


def test_an_unexpected_keyword_is_refused():
    module = compiled("fn plain(a):\n    return a\n", name="plain")
    with pytest.raises(TrapError, match="unexpected keyword"):
        call_function(module, function_named(module, "plain"), [1], kwargs={"b": 2})


# --------------------------------------------------------------------------
# coroutines (§6.3)


def test_calling_a_compiled_coroutine_constructs_it_rather_than_running_it():
    """The `co` flag: the body does not run until something drives it, and
    what comes back is the runtime's own coroutine, so `next`/`send`/`for`
    treat it exactly as they treat an interpreted one."""
    module = compiled("co counting(limit):\n    var i = 0\n    while i < limit:\n        yield i\n        i = i + 1\n    return \"done\"\n", name="co1")
    coroutine = call_function(module, function_named(module, "counting"), [2])[0]
    assert isinstance(coroutine, ev.CoroutineInstance)
    assert ev.next_(coroutine) == 0
    assert ev.next_(coroutine) == 1
    assert wyrm_builtins.is_error(ev.next_(coroutine))  # StopIteration, then finished
    assert coroutine._result == "done"


def test_a_compiled_coroutine_iterates_like_any_other():
    module = compiled("co two():\n    yield 1\n    yield 2\n", name="co2")
    coroutine = call_function(module, function_named(module, "two"), [])[0]
    assert list(ev._iter_values(coroutine, module.scope)) == [1, 2]


def test_a_value_sent_into_a_compiled_coroutine_lands_where_yield_left_off():
    module = compiled("co echo():\n    var heard = yield \"ready\"\n    return heard\n", name="co3")
    coroutine = call_function(module, function_named(module, "echo"), [])[0]
    assert ev.next_(coroutine) == "ready"
    assert wyrm_builtins.is_error(ev.send_(coroutine, "hello"))
    assert coroutine._result == "hello"


# --------------------------------------------------------------------------
# the debug section (§8.9)


def test_a_trap_names_the_module_and_the_instruction():
    """A stub body traps when called, and says which function could not be
    compiled rather than only that something trapped."""
    module = compiled("fn template():\n    return this\n", name="stubby", stub_unlowered=True)
    with pytest.raises(TrapError) as caught:
        call_function(module, function_named(module, "template"))
    assert "could not lower" in str(caught.value)
    assert "stubby" in str(caught.value)


def test_a_code_offset_maps_back_to_a_source_line():
    """§8.9's line table, which is what turns a fault into an answer."""
    image = load(compile_fixture("hello.wy").to_wyc())
    assert image.source_location(12) == "hello.wy:3"   # greet's body
    assert image.source_location(0) is None            # before the first entry


def test_a_stripped_image_still_faults_cleanly():
    """A VM must ignore `debug` entirely: without it a trap says less, and
    nothing else changes."""
    stripped = compiled(
        "fn template():\n    return this\n",
        name="nodebug", stub_unlowered=True, debug=False,
    )
    assert not stripped.image.debug
    with pytest.raises(TrapError) as caught:
        call_function(stripped, function_named(stripped, "template"))
    assert ":" not in str(caught.value).split("(")[-1].split(",")[0]
