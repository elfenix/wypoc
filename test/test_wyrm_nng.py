"""wypoc/wyrm_nng.py: the nng (pynng) bindings behind corelib/std/nng.wy.

Two tiers: pure unit/validation tests against the Python module directly
(no network needed for the error paths), and one end-to-end pub/sub test
driving corelib/std/nng.wy's own wrapper through `wyrm -c` as a real
subprocess against a genuine pynng publisher.
"""
import os
import shutil
import subprocess
import time

import pytest

from conftest import REPO_ROOT

pynng = pytest.importorskip("pynng", reason="the 'nng' extra isn't installed")

from wypoc import wyrm_nng  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_handles():
    yield
    for handle in list(wyrm_nng._handles):
        try:
            wyrm_nng.nng_close(handle)
        except wyrm_nng.NngError:
            pass


def test_open_unknown_protocol_raises():
    with pytest.raises(wyrm_nng.NngError, match="unknown nng protocol"):
        wyrm_nng.nng_open("xyz0")


def test_bad_handle_raises():
    with pytest.raises(wyrm_nng.NngError, match="bad nng socket handle"):
        wyrm_nng.nng_send(999, "x")


def test_subscribe_on_a_non_sub_socket_raises():
    h = wyrm_nng.nng_open("pub0")
    with pytest.raises(wyrm_nng.NngError, match="not a sub0 socket"):
        wyrm_nng.nng_subscribe(h, "")


def _free_port() -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_pair_round_trip_within_one_process():
    a = wyrm_nng.nng_open("pair0")
    b = wyrm_nng.nng_open("pair0")
    url = f"tcp://127.0.0.1:{_free_port()}"
    wyrm_nng.nng_listen(a, url)
    wyrm_nng.nng_dial(b, url)
    time.sleep(0.3)

    wyrm_nng.nng_send(a, "ping")
    assert wyrm_nng.nng_recv(b, 3000) == "ping"

    wyrm_nng.nng_send(b, "pong")
    assert wyrm_nng.nng_recv(a, 3000) == "pong"


def test_recv_returns_none_on_timeout():
    a = wyrm_nng.nng_open("pair0")
    wyrm_nng.nng_listen(a, f"tcp://127.0.0.1:{_free_port()}")
    assert wyrm_nng.nng_recv(a, 200) is None


def test_pub_sub_topic_filtering():
    pub = wyrm_nng.nng_open("pub0")
    url = f"tcp://127.0.0.1:{_free_port()}"
    wyrm_nng.nng_listen(pub, url)

    sub = wyrm_nng.nng_open("sub0")
    wyrm_nng.nng_dial(sub, url)
    wyrm_nng.nng_subscribe(sub, "topic-a:")
    time.sleep(0.3)

    wyrm_nng.nng_send(pub, "topic-b:ignored")
    wyrm_nng.nng_send(pub, "topic-a:hello")
    assert wyrm_nng.nng_recv(sub, 3000) == "topic-a:hello"


# --- end-to-end, through the .wy wrapper as a real subprocess ------------

_VENV_WYRM = os.path.join(REPO_ROOT, ".venv", "bin", "wyrm")
WYRM = _VENV_WYRM if os.path.isfile(_VENV_WYRM) else shutil.which("wyrm")

pytestmark_e2e = pytest.mark.skipif(
    not WYRM or not os.path.isfile(WYRM),
    reason="wyrm console script not found - run `pip install -e .` first",
)


@pytestmark_e2e
def test_pub_sub_end_to_end_through_the_wy_wrapper(free_tcp_port):
    port = free_tcp_port
    pub = pynng.Pub0(listen=f"tcp://127.0.0.1:{port}")
    time.sleep(0.3)

    sub_src = f'''
import std::nng

sub := nng::open("sub0")
nng::dial(sub, "tcp://127.0.0.1:{port}")
nng::subscribe(sub, "")
msg := nng::recv(sub, 5000)
print(msg)
nng::close(sub)
'''
    env = {**os.environ, "WYRM_PATH": ""}
    proc = subprocess.Popen([WYRM, "-c", sub_src], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(0.6)
        pub.send(b"hello from python pub")
        out, _ = proc.communicate(timeout=5)
        assert out.strip() == "hello from python pub"
    finally:
        pub.close()
        if proc.poll() is None:
            proc.kill()


@pytest.fixture
def free_tcp_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
