"""wypoc/wyrm_socket.py: the BSD socket primitives behind corelib/std/socket.wy.

Exercised two ways: directly against the Python module (a real client
socket talks to a wyrm-driven server socket in a background thread - no
subprocess needed, since a socket crosses thread boundaries fine), and
one end-to-end test that runs corelib/std/socket.wy's own wrapper through
`wyrm -c` as a real subprocess for both tcp and udp.
"""
import os
import shutil
import socket
import subprocess
import threading
import time

import pytest

from conftest import REPO_ROOT
from wypoc import wyrm_socket
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import Scope, eval_program, populate_globals


@pytest.fixture(autouse=True)
def _clean_handles():
    # wyrm_socket keeps one process-global handle table (see wyrm_io.py's
    # own convention) - close whatever a test leaves open so handle
    # numbers (and open fds) don't leak across tests.
    yield
    for handle in list(wyrm_socket._handles):
        try:
            wyrm_socket.sock_close(handle)
        except OSError:
            pass


def test_create_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown kind"):
        wyrm_socket.sock_create("sctp")


def test_bad_handle_raises_os_error():
    with pytest.raises(OSError, match="bad socket handle"):
        wyrm_socket.sock_send(999, "x")


def test_tcp_round_trip_against_a_background_server():
    server = wyrm_socket.sock_create("tcp")
    wyrm_socket.sock_bind(server, "127.0.0.1", 0)
    port = wyrm_socket._get(server).getsockname()[1]
    wyrm_socket.sock_listen(server, 1)

    accepted = {}

    def _serve():
        h, host, peer_port = wyrm_socket.sock_accept(server)
        accepted["handle"] = h
        data = wyrm_socket.sock_recv(h, 4096)
        wyrm_socket.sock_send(h, "echo:" + data)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    client.send(b"hi")
    reply = client.recv(4096)
    client.close()
    t.join(timeout=3)

    assert reply == b"echo:hi"
    wyrm_socket.sock_close(accepted["handle"])


def test_udp_round_trip():
    server = wyrm_socket.sock_create("udp")
    wyrm_socket.sock_bind(server, "127.0.0.1", 0)
    port = wyrm_socket._get(server).getsockname()[1]

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(3)
    client.sendto(b"ping", ("127.0.0.1", port))

    data, host, from_port = wyrm_socket.sock_recvfrom(server, 4096)
    assert data == "ping"
    wyrm_socket.sock_sendto(server, "pong", host, from_port)

    reply, _ = client.recvfrom(4096)
    assert reply == b"pong"
    client.close()


def test_recv_returns_empty_string_at_eof():
    server = wyrm_socket.sock_create("tcp")
    wyrm_socket.sock_bind(server, "127.0.0.1", 0)
    port = wyrm_socket._get(server).getsockname()[1]
    wyrm_socket.sock_listen(server, 1)

    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    conn_handle, _, _ = wyrm_socket.sock_accept(server)
    client.close()  # closed with nothing sent - the accepted side sees EOF

    assert wyrm_socket.sock_recv(conn_handle, 4096) == ""
    wyrm_socket.sock_close(conn_handle)


# --- end-to-end, driving corelib/std/socket.wy through `wyrm -c` ---------

_VENV_WYRM = os.path.join(REPO_ROOT, ".venv", "bin", "wyrm")
WYRM = _VENV_WYRM if os.path.isfile(_VENV_WYRM) else shutil.which("wyrm")

pytestmark_e2e = pytest.mark.skipif(
    not WYRM or not os.path.isfile(WYRM),
    reason="wyrm console script not found - run `pip install -e .` first",
)


def _run_wyrm(src: str):
    env = {**os.environ, "WYRM_PATH": ""}
    return subprocess.Popen([WYRM, "-c", src], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)


@pytestmark_e2e
def test_tcp_end_to_end_through_the_wy_wrapper():
    port = _free_port()
    server_src = f'''
import std::socket

s := socket::create("tcp")
socket::bind(s, "127.0.0.1", {port})
socket::listen(s, 4)
conn := socket::accept(s)
data := socket::recv(conn[0], 4096)
print(data)
socket::send(conn[0], "echo:" + data)
socket::close(conn[0])
socket::close(s)
'''
    proc = _run_wyrm(server_src)
    try:
        time.sleep(0.5)
        client = socket.create_connection(("127.0.0.1", port), timeout=3)
        client.send(b"hello wyrm")
        reply = client.recv(4096)
        client.close()
        out, _ = proc.communicate(timeout=5)
        assert reply == b"echo:hello wyrm"
        assert "hello wyrm" in out
    finally:
        if proc.poll() is None:
            proc.kill()


@pytestmark_e2e
def test_udp_end_to_end_through_the_wy_wrapper():
    port = _free_port(udp=True)
    server_src = f'''
import std::socket

s := socket::create("udp")
socket::bind(s, "127.0.0.1", {port})
msg := socket::recvfrom(s, 4096)
print(msg[0])
socket::sendto(s, "reply:" + msg[0], msg[1], msg[2])
socket::close(s)
'''
    proc = _run_wyrm(server_src)
    try:
        time.sleep(0.5)
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(3)
        client.sendto(b"ping", ("127.0.0.1", port))
        data, _ = client.recvfrom(4096)
        client.close()
        out, _ = proc.communicate(timeout=5)
        assert data == b"reply:ping"
        assert "ping" in out
    finally:
        if proc.poll() is None:
            proc.kill()


def _free_port(udp: bool = False) -> int:
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
