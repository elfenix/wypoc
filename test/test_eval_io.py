"""Exercises the wyrm_io primitives (__open/__read/__write/__lseek/__dup2/
__close/__flush, __STDIN/__STDOUT/__STDERR) exposed into wyrm via
wyrm_io.install()."""
import io
import sys

import pytest

from conftest import eval_sample
from wypoc import wyrm_io
from wypoc.wyrm_eval_parse_tree import expose


def test_std_handle_constants():
    assert wyrm_io.STDIN == 0 and wyrm_io.STDOUT == 1 and wyrm_io.STDERR == 2


@pytest.fixture
def fake_stdout():
    fake = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = fake
    wyrm_io._reset_std_handles()  # repoint handle 1 at the fake stdout
    try:
        yield fake
    finally:
        sys.stdout = old_stdout
        wyrm_io._reset_std_handles()


def test_io_primitives(tmp_path, fake_stdout):
    path = tmp_path / "io_test.txt"

    ctx: dict = {}
    wyrm_io.install(ctx)
    expose(ctx, "path", str(path))

    eval_sample("eval_io.wy", ctx)

    assert ctx["written"].value == len("hello wyrm io"), "__write returns the byte/char count written"
    assert ctx["content"].value == "hello wyrm io", "__read(fd2) reads the full file back"
    assert ctx["pos"].value == 2, "__lseek(fd2, 2, 0) returns the new position"
    assert ctx["partial"].value == "llo wyrm io", "__read after seeking to 2 gets the tail"
    assert ctx["new_handle"].value == 42, "__dup2(fd2, 42) returns the new handle"
    assert ctx["remaining_via_dup"].value == "", (
        "dup2'd handle shares fd2's file position (already at EOF)"
    )

    assert ctx["to_stdout"].value == len("printed via __STDOUT\n")
    assert fake_stdout.getvalue() == "printed via __STDOUT\n", (
        "__write(__STDOUT, ...) actually reached Python's stdout"
    )

    assert path.read_text() == "hello wyrm io", "the file on disk matches what __write wrote"

    assert ctx["flushed"].value == 0, "__flush(fd) returns 0"
    assert ctx["closed"].value == 0, "__close(fd2) returns 0"
    assert ctx["close_again"].value == 0, (
        "__close on new_handle (dup2'd, already-closed file) is still a clean 0"
    )

    with pytest.raises(OSError):
        wyrm_io.wyrm_read(ctx["fd2"].value)
