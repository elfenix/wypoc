"""`thread <module-path>` (see ast_nodes.ThreadSpawn, wypoc/wyrm_remote.py):
spawns a wyrm module fresh on its own OS process, and the RemoteModule
value it evaluates to - blocking `remote ! name(...)` calls and
`remote.signal_name ! connect(...)` signal delivery over the two
multiprocessing.Queues wyrm_remote.py's module docstring describes.

Every test tears down whatever it spawned via wyrm_remote.terminate_all() -
otherwise a still-blocked, non-daemon child process left registered would
hang a *later* test (or the whole suite) at interpreter shutdown, the same
hazard `end()`'s docstring in cli.py describes for a real script.
"""
import os

import pytest

from wypoc import wyrm_remote
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import Scope, eval_program, populate_globals


@pytest.fixture(autouse=True)
def _clean_remote_registry():
    yield
    wyrm_remote.terminate_all()


def run(src: str, tmp_path, monkeypatch) -> dict:
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    ctx = Scope()
    populate_globals(ctx)
    eval_program(parse(src), ctx)
    return ctx


def test_spawned_module_runs_in_a_separate_process(tmp_path, monkeypatch):
    (tmp_path / "svc.wy").write_text("fn [] get():\n    return 1\n")
    ctx = run("remote := thread svc\n", tmp_path, monkeypatch)
    remote = ctx["remote"].value
    assert isinstance(remote, wyrm_remote.RemoteModule)
    assert remote.process.pid is not None
    assert remote.process.pid != os.getpid()
    assert remote.process.is_alive()


def test_spawning_does_not_touch_the_parents_module_cache(tmp_path, monkeypatch):
    from wypoc.wyrm_eval_parse_tree import _module_cache

    (tmp_path / "svc.wy").write_text("fn [] get():\n    return 1\n")
    before = dict(_module_cache)
    run("remote := thread svc\n", tmp_path, monkeypatch)
    assert dict(_module_cache) == before, (
        "a thread-spawned module runs in its own process's own cache, "
        "never the caller's _module_cache"
    )


def test_blocking_call_and_signal_delivery_end_to_end(tmp_path, monkeypatch):
    """Reproduces the original design example: connect to a remote signal,
    make a blocking call, and see the connected callback have already run
    by the time the call returns."""
    (tmp_path / "svc.wy").write_text(
        "signal a_signal(x: int)\n"
        "fn [] a_method():\n"
        "    emit a_signal(5)\n"
        "    return 42\n"
    )
    ctx = run(
        "remote := thread svc\n"
        "seen := []\n"
        "remote.a_signal ! connect(fn(v) { seen ! append(v) })\n"
        "result := remote ! a_method()\n",
        tmp_path, monkeypatch,
    )
    assert list(ctx["seen"].value) == [5]
    assert ctx["result"].value == 42


def test_a_second_call_still_works(tmp_path, monkeypatch):
    (tmp_path / "svc.wy").write_text(
        "count := 0\n"
        "fn [] bump():\n"
        "    count = count + 1\n"
        "    return count\n"
    )
    ctx = run(
        "remote := thread svc\n"
        "a := remote ! bump()\n"
        "b := remote ! bump()\n",
        tmp_path, monkeypatch,
    )
    assert ctx["a"].value == 1
    assert ctx["b"].value == 2, "state persists across calls - same process, same module scope"


def test_an_error_in_the_remote_handler_surfaces_to_the_caller(tmp_path, monkeypatch):
    (tmp_path / "svc.wy").write_text("fn [] boom():\n    return undefined_name\n")
    with pytest.raises(RuntimeError, match="NameError"):
        run("remote := thread svc\nremote ! boom()\n", tmp_path, monkeypatch)


def test_end_inside_a_handler_stops_just_that_process(tmp_path, monkeypatch):
    (tmp_path / "svc.wy").write_text("fn [] stop():\n    end()\n")
    ctx = run("remote := thread svc\nremote ! stop()\n", tmp_path, monkeypatch)
    remote = ctx["remote"].value
    remote.process.join(timeout=5)
    assert not remote.process.is_alive(), "end() inside the handler stopped this process"


def test_no_parens_remote_message_is_not_supported_yet(tmp_path, monkeypatch):
    (tmp_path / "svc.wy").write_text("fn [] get():\n    return 1\n")
    with pytest.raises(NotImplementedError):
        run("remote := thread svc\nbound := remote ! get\n", tmp_path, monkeypatch)


# --- task/resolve (see ast.TaskSpawn, Future, _dispatch_remote_message) ----
# Deliberately narrow: `task expr` only actually goes async for a
# `remote ! name(...)` call reached while evaluating `expr` - see those
# docstrings. Reproduces the original design example: `task` returns
# immediately, `resolve()` blocks until the remote call has actually
# finished.

_SLOW_SVC = (
    "fn [] slow():\n"
    "    i := 0\n"
    "    while i < 400000:\n"
    "        i = i + 1\n"
    "    return \"done\"\n"
)


def test_task_returns_immediately_and_resolve_blocks_for_the_result(tmp_path, monkeypatch):
    import time

    (tmp_path / "svc.wy").write_text(_SLOW_SVC)
    monkeypatch.setenv("WYRM_PATH", str(tmp_path))
    ctx = Scope()
    populate_globals(ctx)

    start = time.monotonic()
    eval_program(parse("remote := thread svc\nfut := task remote ! slow()\n"), ctx)
    task_elapsed = time.monotonic() - start

    start = time.monotonic()
    eval_program(parse("result := resolve(fut)\n"), ctx)
    resolve_elapsed = time.monotonic() - start

    assert ctx["result"].value == "done"
    assert resolve_elapsed > task_elapsed, (
        "task returned near-instantly; resolve() is what actually waited "
        f"(task={task_elapsed:.3f}s, resolve={resolve_elapsed:.3f}s)"
    )
    assert task_elapsed < 1.0, "task itself must not block on the remote call"


def test_resolve_reraises_an_error_from_the_remote_handler(tmp_path, monkeypatch):
    (tmp_path / "svc.wy").write_text("fn [] boom():\n    return undefined_name\n")
    ctx = run(
        "remote := thread svc\nfut := task remote ! boom()\n",
        tmp_path, monkeypatch,
    )
    from wypoc.wyrm_builtins import resolve as resolve_builtin

    with pytest.raises(RuntimeError, match="NameError"):
        resolve_builtin(ctx["fut"].value)


def test_task_on_a_non_remote_call_never_resolves_the_future(tmp_path, monkeypatch):
    """Explicitly out of scope: `task` only goes async for a remote `!`
    call - anything else in `expr` just runs synchronously and its return
    value is discarded, so the Future is left permanently pending. Checked
    via the Future's own internal state, never via a blocking resolve()
    (which would hang this test on a real regression)."""
    ctx = run("fut := task (1 + 1)\n", tmp_path, monkeypatch)
    from wypoc.wyrm_eval_parse_tree import Future

    future = ctx["fut"].value
    assert isinstance(future, Future)
    assert not future._event.is_set(), "nothing ever resolves a non-remote task's future"


def test_resolve_on_a_non_future_is_a_type_error(tmp_path, monkeypatch):
    with pytest.raises(TypeError):
        run("resolve(5)\n", tmp_path, monkeypatch)
