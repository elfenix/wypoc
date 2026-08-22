"""wypoc/wyrm_term.py: raw terminal primitives behind corelib/std/term.wy.

Two tiers: unit tests against a plain pipe (no real terminal needed - a
pipe answers os.read/select fine, it just isn't a tty, which is exactly
what a couple of these tests want to exercise), and one end-to-end test
over a real pseudo-terminal (pty), verifying isatty/size/raw_enable/
read_key together the same way this feature was hand-verified.
"""
import os
import struct
import sys

import pytest

from wypoc import wyrm_term

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only (termios/tty)")


@pytest.fixture(autouse=True)
def _restore_raw_state():
    # wyrm_term keeps one process-global "saved termios state" (see
    # raw_enable's idempotence note) - make sure a test that enters raw
    # mode on some fd always leaves it cleared afterward, regardless of
    # how the test ends, so later tests see raw_enable as available again.
    yield
    wyrm_term._saved_state = None


def test_isatty_false_on_a_plain_pipe():
    r, w = os.pipe()
    try:
        assert wyrm_term.isatty(r) is False
    finally:
        os.close(r)
        os.close(w)


def test_size_falls_back_when_not_a_terminal(monkeypatch):
    def _raise():
        raise OSError("not a terminal")
    monkeypatch.setattr(os, "get_terminal_size", _raise)
    assert wyrm_term.size() == [24, 80]


def test_size_reads_rows_and_cols(monkeypatch):
    monkeypatch.setattr(os, "get_terminal_size", lambda: os.terminal_size((120, 40)))
    assert wyrm_term.size() == [40, 120]


def test_raw_enable_on_a_non_tty_raises_term_error():
    r, w = os.pipe()
    try:
        with pytest.raises(wyrm_term.TermError, match="isn't a terminal"):
            wyrm_term.raw_enable(r)
    finally:
        os.close(r)
        os.close(w)


def test_raw_enable_is_idempotent_and_disable_is_a_no_op_when_never_entered():
    wyrm_term.raw_disable(0)  # never entered - must not raise


def test_read_key_returns_minus_one_on_immediate_timeout():
    r, w = os.pipe()
    try:
        assert wyrm_term.read_key(timeout=0, handle=r) == -1
    finally:
        os.close(r)
        os.close(w)


def test_read_key_reads_one_byte_at_a_time():
    r, w = os.pipe()
    try:
        os.write(w, b"AB")
        assert wyrm_term.read_key(timeout=1, handle=r) == ord("A")
        assert wyrm_term.read_key(timeout=1, handle=r) == ord("B")
    finally:
        os.close(r)
        os.close(w)


def test_read_key_returns_minus_one_at_eof():
    r, w = os.pipe()
    os.close(w)  # write end closed with nothing written - read() sees EOF
    try:
        assert wyrm_term.read_key(timeout=1, handle=r) == -1
    finally:
        os.close(r)


# --- end-to-end, over a real pseudo-terminal ------------------------------

pty = pytest.importorskip("pty", reason="pty module unavailable")


def test_term_primitives_over_a_real_pty():
    import fcntl
    import termios
    import time

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    pid = os.fork()
    if pid == 0:  # child: runs wyrm directly, attached to the pty's slave side
        os.close(master)
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        os.execv(sys.executable, [
            sys.executable, "-c",
            "import sys; sys.path.insert(0, %r)\n"
            "from wypoc.parse import parse\n"
            "from wypoc.wyrm_eval_parse_tree import Scope, eval_program, populate_globals\n"
            "src = '''\n"
            "import std::term\n"
            "print(term::isatty())\n"
            "sz := term::size()\n"
            "print(sz)\n"
            "term::raw_enable()\n"
            "k1 := term::read_key(-1)\n"
            "k2 := term::read_key(-1)\n"
            "term::raw_disable()\n"
            "print(k1)\n"
            "print(k2)\n"
            "'''\n"
            "ctx = Scope(); populate_globals(ctx)\n"
            "eval_program(parse(src), ctx)\n"
            % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),),
        ])
        os._exit(1)  # pragma: no cover - execv never returns on success

    os.close(slave)
    try:
        time.sleep(0.6)
        os.write(master, b"AB")
        deadline = time.time() + 5
        output = b""
        while time.time() < deadline:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if output.count(b"\n") >= 4 or b"66" in output:
                break
        text = output.decode(errors="replace")
        assert "true" in text
        assert "[40, 120]" in text
        assert "65" in text  # 'A'
        assert "66" in text  # 'B'
    finally:
        os.waitpid(pid, 0)
