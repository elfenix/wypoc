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


def run(*args, stdin: str = ""):
    # Strip WYRM_PATH so a shell-level override (e.g. pointing at a
    # different project's stdlib) can't change what these scripts import -
    # mirrors conftest.py's _clean_wyrm_path fixture for in-process tests.
    # stdin is always fed (empty by default) rather than inherited: with no
    # script argument `wyrm` is an interactive REPL, and one reading the
    # test runner's own stdin would hang instead of seeing end of input.
    env = {**os.environ, "WYRM_PATH": ""}
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
