import asyncio
import os

import pytest

from wypoc import config, wyrm_modules
from wypoc.compiler_c import compile_module
from wypoc.compiler_py import compile_module as compile_py_module
from wypoc.compiler_py import compile_entry_module as compile_py_entry_module
from wypoc.compiler_py import compile_tree as compile_py_tree
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import eval_program

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.path.join(REPO_ROOT, "wypoc", "samples")


@pytest.fixture(autouse=True, scope="session")
def _wyrm_search_environment():
    """The module search roots every test runs against.

    Tests assume `import std::...` resolves through wypoc's own bundled
    corelib/ (see wyrm_modules.DEFAULT_COREPATH) unless a test explicitly
    opts into WYRM_PATH (test_eval_modules.py's test_wyrm_path_override sets
    it itself via monkeypatch, which takes effect on top of this). Any
    WYRM_PATH inherited from the shell is stripped so a sibling project's
    stdlib on the dev's machine can't shadow wypoc's own and make results
    depend on the environment the tests happen to run in.

    samples/ stands in for the script's own directory, which is a search
    root when a script runs (see wyrm_modules.set_script_root, and cli.py
    which sets it) - so a sample importing a neighbouring sample (as
    decorators.wy imports decolib.wy) resolves here exactly as it would from
    the command line.

    Session-scoped, not function-scoped: a module-scoped fixture elsewhere
    (test_eval_decorators.py runs its whole sample once) would otherwise set
    up *before* this and see the environment unprepared."""
    previous_path = os.environ.pop("WYRM_PATH", None)
    previous_root = wyrm_modules.set_script_root(SAMPLES_DIR)
    yield
    wyrm_modules.set_script_root(previous_root)
    if previous_path is not None:
        os.environ["WYRM_PATH"] = previous_path


@pytest.fixture(autouse=True)
def _wyrm_config(tmp_path, monkeypatch):
    """Every test gets a config file of its own (an empty, not-yet-created
    one), so a Session picks up wypoc's own defaults rather than whatever
    the developer running the suite happens to have in ~/.wyrm/config - and
    so a test writing one (`:set config`) can't overwrite theirs."""
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(tmp_path / "wyrm-config"))
    # Building a Session points the module search root at the working
    # directory (repl.Session.reset does, standing in for a script's own
    # directory) - put back what _wyrm_search_environment set, so a test
    # that starts a REPL doesn't leave later tests unable to find samples/.
    previous_root = wyrm_modules.script_root()
    yield
    wyrm_modules.set_script_root(previous_root)


def sample_path(name: str) -> str:
    return os.path.join(SAMPLES_DIR, name)


def sample_source(name: str) -> str:
    with open(sample_path(name)) as f:
        return f.read()


def eval_sample(name: str, ctx: dict | None = None) -> dict:
    if ctx is None:
        ctx = {}
    eval_program(parse(sample_source(name)), ctx)
    return ctx


def eval_sample_with_builtins(name: str) -> dict:
    """Like eval_sample, but seeds `next`/`send`/`is_error`/etc first (see
    test_eval_coroutines.py's own `ctx` fixture) - needed for any sample
    that calls a builtin directly at top level rather than only through an
    `import`ed module (which gets its own populate_globals call)."""
    from wypoc import wyrm_builtins
    ctx: dict = {}
    wyrm_builtins.install(ctx)
    return eval_sample(name, ctx)


def compile_sample(name: str) -> str:
    module_name = os.path.splitext(name)[0]
    return compile_module(parse(sample_source(name)), module_name)


def compile_py_entry_sample(name: str) -> str:
    """Like compile_sample, but for --compile-py's entry-script form
    (module.py's compile_entry_module) - returns the generated .py source,
    with the `if __name__ == "__main__":` driver appended."""
    entry_name = os.path.splitext(name)[0]
    return compile_py_entry_module(parse(sample_source(name)), entry_name)


def compile_py_sample(name: str) -> str:
    """Like compile_py_entry_sample, but without the __main__ driver - the
    plain compile_module form, for a test that wants to exec the module
    and drive do_import itself (see exec_compiled_py_module)."""
    return compile_py_module(parse(sample_source(name)))


def compile_py_decorated_sample(name: str) -> str:
    """Like compile_py_sample, but runs the decorator pre-pass first (see
    wypoc.compiler_py.decorators_pass) - needed for any sample using
    `@decorator` syntax, since compile_module itself has no handler for
    Decorated nodes (by design - codegen should never see one)."""
    from wypoc.compiler_py.decorators_pass import expand_all_decorators
    tree = expand_all_decorators(parse(sample_source(name)), ())
    return compile_py_module(tree)


def exec_compiled_py_module(source: str, module_name: str = "_wy_test_module") -> dict:
    """Execs a --compile-py-generated module's source in a fresh namespace
    and returns that namespace (so a test can reach into it for the
    top-level wy_-prefixed globals do_import populates)."""
    namespace = {"__name__": module_name}
    exec(compile(source, module_name, "exec"), namespace)
    return namespace


def compile_py_tree_sample(name: str, output_dir) -> str:
    """Compiles samples/{name} (and everything it transitively imports)
    into `output_dir` via compile_tree - the full multi-file driver, not
    just one module's source. Returns the entry script's path. Relies on
    the session-scoped _wyrm_search_environment fixture already pointing
    wyrm_modules.set_script_root at SAMPLES_DIR, so a sibling sample
    resolves as an import target exactly as it would from the command
    line."""
    compile_py_tree(sample_path(name), str(output_dir))
    entry_name = os.path.splitext(name)[0]
    return os.path.join(str(output_dir), f"{entry_name}.py")


def run_py_do_import(namespace: dict):
    """Builds a fresh engine.Machine/Context, awaits the compiled module's
    do_import(ctx) to completion, and returns the Context (so a test can
    inspect ctx.machine, or the namespace's own wy_-prefixed globals which
    do_import assigns into directly)."""
    from wypoc.compiler_py.engine import Machine

    machine = Machine()
    ctx = machine.root_context()
    asyncio.run(namespace["do_import"](ctx))
    return ctx
