"""Exercises the installed `wyrm` console script (wypoc/cli.py, wired up via
pyproject.toml's [project.scripts]): argument packing into __ARGS, exit
codes, and error reporting.

Requires `pip install -e .` to have been run first (so the `wyrm` command
exists in this venv).
"""
import os
import shutil
import subprocess

import pytest

from conftest import REPO_ROOT, sample_path
from wypoc import cache as cache_mod

# Prefer this repo's own venv script over whatever `wyrm` a bare `which`
# would find on PATH - a dev machine may have an unrelated `wyrm` binary
# (e.g. a different project's own script engine) earlier on PATH, which
# would make these tests exercise the wrong program entirely.
_VENV_WYRM = os.path.join(REPO_ROOT, ".venv", "bin", "wyrm")
WYRM = _VENV_WYRM if os.path.isfile(_VENV_WYRM) else shutil.which("wyrm")
SAMPLE = sample_path("eval_args.wy")

pytestmark = pytest.mark.skipif(
    not WYRM or not os.path.isfile(WYRM),
    reason=f"wyrm console script not found at {WYRM!r} - run `pip install -e .` first",
)


def run(*args, stdin: str = "", wyrm_path: str = ""):
    # Strip WYRM_PATH so a shell-level override (e.g. pointing at a
    # different project's stdlib) can't change what these scripts import -
    # mirrors conftest.py's _clean_wyrm_path fixture for in-process tests.
    # stdin is always fed (empty by default) rather than inherited: with no
    # script argument `wyrm` is an interactive REPL, and one reading the
    # test runner's own stdin would hang instead of seeing end of input.
    env = {**os.environ, "WYRM_PATH": wyrm_path}
    return subprocess.run([WYRM, *args], capture_output=True, text=True, env=env,
                          input=stdin)


def test_script_args_packed_into_args_in_order():
    r = run(SAMPLE, "foo", "bar", "--baz", "3")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert r.stdout == "foo\nbar\n--baz\n3\n"


def test_no_script_args_means_empty_args():
    r = run(SAMPLE)
    assert r.returncode == 0 and r.stdout == ""


def test_help_flag():
    r = run("-h")
    assert r.returncode == 0 and "Usage" in r.stdout


def test_no_script_path_starts_the_repl():
    r = run(stdin="1 + 2\n:quit\n")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "3" in r.stdout


def test_repl_keeps_reading_while_an_entry_is_unfinished():
    # The `fn` body only runs once the empty line closes the block, and the
    # function it defines is still there for the entry after it.
    r = run(stdin="fn double(a):\n    return a * 2\n\ndouble(21)\n:quit\n")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "42" in r.stdout


def test_tui_off_a_terminal_falls_back_to_the_readline_repl():
    r = run("--tui", stdin="1 + 2\n:quit\n")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "--tui unavailable" in r.stderr
    assert "3" in r.stdout


def test_tui_with_a_script_is_a_usage_error():
    r = run("--tui", SAMPLE)
    assert r.returncode == 2 and "takes no script" in r.stderr


def test_missing_script_file():
    r = run("/no/such/file.wy")
    assert r.returncode == 2 and "can't open file" in r.stderr


# --- -m: run a module found via search-path resolution, as the entry script

def test_dash_m_runs_a_resolved_module_with_dunder_main_as_its_name(tmp_path):
    (tmp_path / "leaf.wy").write_text('println(__name__)\n')
    r = run("-m", "leaf", wyrm_path=str(tmp_path))
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert r.stdout == "__main__\n", (
        "run directly via -m, not imported - __name__ is \"__main__\", "
        "not the module's own fully-qualified name"
    )


def test_dash_m_resolves_a_double_colon_path(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "leaf.wy").write_text('println(__name__)\n')
    r = run("-m", "pkg::leaf", wyrm_path=str(tmp_path))
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert r.stdout == "__main__\n"


def test_dash_m_packs_remaining_args_into_dunder_args(tmp_path):
    (tmp_path / "leaf.wy").write_text('println(__ARGS)\n')
    r = run("-m", "leaf", "foo", "bar", wyrm_path=str(tmp_path))
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "foo" in r.stdout and "bar" in r.stdout


def test_dash_m_missing_module_is_a_usage_error():
    r = run("-m", "no::such::module", wyrm_path="")
    assert r.returncode == 2 and "no module named" in r.stderr


def test_syntax_error_exits_1(tmp_path):
    bad = tmp_path / "bad.wy"
    bad.write_text("x = 1 +\n")
    r = run(str(bad))
    assert r.returncode == 1
    assert str(bad) in r.stderr and "SyntaxError" in r.stderr, (
        "a syntax error's message names the actual script file"
    )


def test_runtime_error_exits_1(tmp_path):
    boom = tmp_path / "boom.wy"
    boom.write_text("x = undefined_name\n")
    r = run(str(boom))
    assert r.returncode == 1 and "NameError" in r.stderr


# --------------------------------------------------------------------------
# end()/exit() builtins (see EndSignal/ExitSignal, wyrm_eval_parse_tree.py).
# --------------------------------------------------------------------------

def test_exit_stops_the_script_immediately_with_its_code(tmp_path):
    script = tmp_path / "quit.wy"
    script.write_text('print("before")\nexit(3)\nprint("after")\n')
    r = run(str(script))
    assert r.returncode == 3
    assert r.stdout == "before", "nothing after exit() ran"


def test_exit_with_no_argument_uses_code_zero(tmp_path):
    script = tmp_path / "quit.wy"
    script.write_text("exit()\n")
    r = run(str(script))
    assert r.returncode == 0


def test_end_stops_cleanly_like_falling_off_the_end(tmp_path):
    script = tmp_path / "stop.wy"
    script.write_text('print("before")\nend()\nprint("after")\n')
    r = run(str(script))
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert r.stdout == "before", "nothing after end() ran"


def test_falling_off_the_end_with_no_end_or_exit_call_is_code_zero(tmp_path):
    script = tmp_path / "plain.wy"
    script.write_text('print("hi")\n')
    r = run(str(script))
    assert r.returncode == 0 and r.stdout == "hi"


def test_falling_off_the_end_tears_down_a_spawned_thread_without_hanging(tmp_path):
    """Regression test: an implicit exit() (falling off the end, with no
    explicit end()/exit() call) must terminate any `thread`-spawned
    process, or the interpreter hangs at shutdown joining a non-daemon
    child blocked forever on its own empty queue (see wyrm_remote.py's
    terminate_all). Runs under a timeout of its own so a real regression
    fails the test instead of hanging the whole suite."""
    (tmp_path / "svc.wy").write_text("fn [] get():\n    return 1\n")
    script = tmp_path / "main.wy"
    script.write_text('remote := thread svc\nresult := remote ! get()\nprint(result)\n')
    try:
        r = subprocess.run([WYRM, str(script)], capture_output=True, text=True,
                            env={**os.environ, "WYRM_PATH": ""}, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail("interpreter hung at shutdown instead of tearing down the spawned thread")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert r.stdout == "1"


# --------------------------------------------------------------------------
# --dump-wys and running a .wys file directly (see wypoc/wys.py).
# --------------------------------------------------------------------------

def test_dump_wys_writes_a_compiled_unit(tmp_path):
    src = tmp_path / "hello.wy"
    src.write_text('fn greet(name) {\n    print("hi " + name)\n}\n\ngreet("wyrm")\n')
    out = tmp_path / "hello.wys"
    r = run("--dump-wys", "-o", str(out), str(src))
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    text = out.read_text()
    assert text.startswith("$['program, [")
    assert '"hi "' in text  # a real string literal, not the REPL's 'hi ' form


def test_dump_wys_to_stdout_without_o():
    r = run("--dump-wys", SAMPLE)
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert r.stdout.startswith("$['program, [")


def test_running_a_wys_file_matches_running_its_source(tmp_path):
    src = tmp_path / "hello.wy"
    src.write_text('print(1 + 2)\nprint("ok")\n')
    out = tmp_path / "hello.wys"
    dumped = run("--dump-wys", "-o", str(out), str(src))
    assert dumped.returncode == 0, f"stderr={dumped.stderr!r}"

    from_source = run(str(src))
    from_wys = run(str(out))
    assert from_wys.returncode == 0, f"stderr={from_wys.stderr!r}"
    assert from_wys.stdout == from_source.stdout == "3ok"


def test_dump_wys_expands_decorators(tmp_path):
    """--dump-wys runs expand_decorators itself, so a source file with a
    decorator still dumps cleanly - `@__identity` answers the tree it was
    given, unchanged, so the output has no `decorat` substring left in it."""
    src = tmp_path / "deco.wy"
    src.write_text("@__identity fn f() { return 1 }\n")
    r = run("--dump-wys", str(src))
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "decorat" not in r.stdout


# --------------------------------------------------------------------------
# --config: set an option in ~/.wyrm/config and exit (see wypoc/config.py).
# conftest's autouse fixture points WYRM_CONFIG at a per-test file, which
# `run` passes through in the environment - so these never touch the real one.
# --------------------------------------------------------------------------

def config_file() -> str:
    return os.environ["WYRM_CONFIG"]


def test_config_sets_an_option_and_exits_without_running_anything():
    r = run("--config", "compact=true")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "compact = true" in r.stdout
    with open(config_file()) as f:
        assert f.read() == "[wyrm]\ncompact = true\n"


def test_config_takes_several_assignments():
    r = run("--config", "compact=true", "--config", "tui=off")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    with open(config_file()) as f:
        text = f.read()
    assert "compact = true" in text and "tui = false" in text


def test_bare_config_lists_the_options():
    r = run("--config")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert config_file() in r.stdout
    assert "compact = false" in r.stdout and "tui = false" in r.stdout


def test_a_bad_config_assignment_is_a_usage_error():
    r = run("--config", "colour=true")
    assert r.returncode == 2 and "unknown option 'colour'" in r.stderr
    assert not os.path.exists(config_file()), "and nothing was written"


def test_config_with_a_script_is_a_usage_error():
    r = run("--config", "compact=true", SAMPLE)
    assert r.returncode == 2 and "sets options and exits" in r.stderr


def test_config_tui_starts_the_repl_in_the_full_screen_ui():
    # Off a terminal the TUI can't actually run, so what's observable is that
    # `wyrm` with no arguments *tried* to - the same fallback --tui takes.
    assert run("--config", "tui=true").returncode == 0
    r = run(stdin="1 + 2\n:quit\n")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "--tui unavailable" in r.stderr and "3" in r.stdout


# --- AST cache (wypoc/cache.py) -------------------------------------------
# End-to-end through the real `wyrm` command, complementing test_cache.py's
# unit-level coverage of the cache module itself.

def test_running_a_script_creates_wycache_automatically(tmp_path):
    script = tmp_path / "hello.wy"
    script.write_text('print("hi")\n')
    assert not (tmp_path / cache_mod.CACHE_DIR_NAME).exists()

    r = run(str(script))
    assert r.returncode == 0 and r.stdout == "hi"
    cache_file = tmp_path / cache_mod.CACHE_DIR_NAME / "hello.wyc"
    assert cache_file.is_file()

    # The cache is now a hit; the script still runs the same either way.
    r = run(str(script))
    assert r.returncode == 0 and r.stdout == "hi"


def test_global_cache_option_is_used_instead_of_local_wycache(tmp_path):
    global_dir = tmp_path / "shared-cache"
    assert run("--config", f"global_cache={global_dir}").returncode == 0

    script = tmp_path / "hello.wy"
    script.write_text('print("hi")\n')
    r = run(str(script))
    assert r.returncode == 0 and r.stdout == "hi"

    assert not (tmp_path / cache_mod.CACHE_DIR_NAME).exists()
    assert global_dir.is_dir() and list(global_dir.iterdir()), \
        "the script's cache file went into global_cache instead"


def test_a_corrupt_cache_file_does_not_stop_the_script_from_running(tmp_path):
    script = tmp_path / "hello.wy"
    script.write_text('print("hi")\n')
    cache_dir = tmp_path / cache_mod.CACHE_DIR_NAME
    cache_dir.mkdir()
    (cache_dir / "hello.wyc").write_bytes(b"not a pickle")

    r = run(str(script))
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert r.stdout == "hi"


def test_no_tui_overrides_the_configured_default():
    assert run("--config", "tui=true").returncode == 0
    r = run("--no-tui", stdin="1 + 2\n:quit\n")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "--tui unavailable" not in r.stderr and "3" in r.stdout


def test_configured_compact_reaches_the_repl():
    assert run("--config", "compact=true").returncode == 0
    r = run(stdin="$[1, 2, 3]\n:quit\n")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    # Commas mean `$[1, 2, 3]` (compact) rather than the pretty `(1 2 3)`;
    # the brackets themselves are wrapped in colour escapes, so the string
    # isn't there contiguously to look for.
    assert "1, 2, 3" in r.stdout, "the config file's compact took effect"
