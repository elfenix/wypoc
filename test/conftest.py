import os

import pytest

from wypoc import wyrm_modules
from wypoc.compiler_c import compile_module
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


def compile_sample(name: str) -> str:
    module_name = os.path.splitext(name)[0]
    return compile_module(parse(sample_source(name)), module_name)
