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

WYRM = shutil.which("wyrm") or os.path.join(REPO_ROOT, ".venv", "bin", "wyrm")
SAMPLE = sample_path("eval_args.wy")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(WYRM),
    reason=f"wyrm console script not found at {WYRM!r} - run `pip install -e .` first",
)


def run(*args):
    return subprocess.run([WYRM, *args], capture_output=True, text=True)


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


def test_no_script_path():
    r = run()
    assert r.returncode == 2 and "Usage" in r.stderr


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
