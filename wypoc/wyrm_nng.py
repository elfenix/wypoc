"""Minimal nng (nanomsg-next-gen) bindings for wyrm, over the `pynng`
package (an optional dependency - see pyproject.toml's `nng` extra, same
optional-and-lazily-imported shape as wyrm_dbus.py's `dbus-fast`).

Handles are small integers (the same convention wyrm_io.py's file handles
and wyrm_socket.py's socket handles use - a separate namespace from
either), mapping to real pynng.Socket objects. Enough protocols to open,
connect, and actually publish/send and receive on a socket - not a full
nng binding: no contexts, no TLS, no raw mode, no async.

    pub := nng::open("pub0")
    nng::listen(pub, "tcp://127.0.0.1:5555")
    nng::send(pub, "hello")

    sub := nng::open("sub0")
    nng::dial(sub, "tcp://127.0.0.1:5555")
    nng::subscribe(sub, "")             # sub0-only: "" means every topic
    msg := nng::recv(sub, -1)           # blocks; see recv's own docstring
"""
_handles: dict = {}
_next_handle = 1


class NngError(Exception):
    """An nng operation failed - protocol unknown, or pynng isn't
    installed (see _require_pynng)."""


def _require_pynng():
    try:
        import pynng
    except ImportError as e:
        raise NngError(
            "the 'nng' extra isn't installed (pip install 'wypoc[nng]', "
            "or: pip install pynng)"
        ) from e
    return pynng


_PROTOCOLS = (
    "pair0", "pair1", "pub0", "sub0", "push0", "pull0", "req0", "rep0", "bus0",
)


def _get(handle: int):
    try:
        return _handles[handle]
    except KeyError:
        raise NngError(f"bad nng socket handle: {handle}")


def nng_open(protocol: str) -> int:
    """__nng_open(protocol) -> handle, for one of "pair0"/"pair1" (one-to-
    one), "pub0"/"sub0" (publish/subscribe), "push0"/"pull0" (pipeline),
    "req0"/"rep0" (request/reply), or "bus0" (many-to-many). Not yet
    listening or dialing anywhere - see nng_listen/nng_dial."""
    pynng = _require_pynng()
    if protocol not in _PROTOCOLS:
        raise NngError(f"unknown nng protocol {protocol!r} (expected one of {_PROTOCOLS})")
    cls = getattr(pynng, protocol[0].upper() + protocol[1:])
    global _next_handle
    handle = _next_handle
    _next_handle += 1
    _handles[handle] = cls()
    return handle


def nng_listen(handle: int, url: str) -> None:
    """__nng_listen(handle, url) -> starts `handle` listening at `url`
    (e.g. "tcp://127.0.0.1:5555", "ipc:///tmp/x.sock") - the "server" side
    of a connection, in nng's own sense (either side may send or receive,
    depending on the protocol)."""
    _get(handle).listen(url)


def nng_dial(handle: int, url: str) -> None:
    """__nng_dial(handle, url) -> connects `handle` to a listening socket
    at `url` - the "client" side."""
    _get(handle).dial(url)


def nng_send(handle: int, data: str) -> None:
    """__nng_send(handle, data) -> sends `data` (a wyrm str, encoded as
    UTF-8) as one nng message. Blocks if the protocol/pipe applies
    backpressure (e.g. a push0 with a full outbound queue)."""
    _get(handle).send(data.encode("utf-8"))


def nng_recv(handle: int, timeout_ms: float = -1) -> "str | None":
    """__nng_recv(handle, timeout_ms=-1) -> the next message received, as
    UTF-8 text, or None (nil) if `timeout_ms` elapses first with nothing
    arriving. A negative timeout (the default) blocks indefinitely - same
    "negative means forever" convention wyrm_term.py's read_key uses."""
    pynng = _require_pynng()
    sock = _get(handle)
    if timeout_ms is not None and timeout_ms >= 0:
        sock.recv_timeout = int(timeout_ms)
    else:
        sock.recv_timeout = -1
    try:
        data = sock.recv()
    except pynng.Timeout:
        return None
    return data.decode("utf-8", errors="replace")


def nng_subscribe(handle: int, topic: str) -> None:
    """__nng_subscribe(handle, topic) -> a sub0 socket only: subscribes to
    messages whose leading bytes match `topic` (a plain UTF-8 prefix, per
    nng's own pub/sub topic convention); "" subscribes to every message,
    since every message's leading bytes trivially match the empty prefix.
    A sub0 socket receives nothing at all until at least one subscribe
    call - there is no "subscribed to everything" default."""
    sock = _get(handle)
    if not hasattr(sock, "subscribe"):
        raise NngError("__nng_subscribe: not a sub0 socket")
    sock.subscribe(topic.encode("utf-8"))


def nng_close(handle: int) -> None:
    """__nng_close(handle) -> closes and forgets `handle`."""
    _get(handle).close()
    del _handles[handle]


def install(ctx: dict) -> None:
    from wypoc.wyrm_eval_parse_tree import expose_all

    expose_all(
        ctx,
        __nng_open=nng_open,
        __nng_listen=nng_listen,
        __nng_dial=nng_dial,
        __nng_send=nng_send,
        __nng_recv=nng_recv,
        __nng_subscribe=nng_subscribe,
        __nng_close=nng_close,
    )
