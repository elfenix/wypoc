"""BSD socket primitives for wyrm code, over Python's own `socket` module.

Handles are small integers, exactly like wyrm_io.py's file handles (a
separate namespace from those - a socket handle and a file handle of the
same integer value name different things), mapping to real Python socket
objects; the real I/O goes through Python's socket API directly. Meant to
back an `io::socket` module written in wyrm itself (see corelib/), not to
be a full BSD sockets layer - IPv4 TCP/UDP only, blocking calls throughout
(matching this interpreter's synchronous evaluation model - there's no
async/await anywhere in a wyrm method body, so a blocking recv is exactly
what "call it and wait" already means here), no options/flags beyond what
each primitive's own parameters cover.
"""
import socket

_handles: dict = {}
_next_handle = 1

_KINDS = {
    "tcp": (socket.AF_INET, socket.SOCK_STREAM),
    "udp": (socket.AF_INET, socket.SOCK_DGRAM),
}


def _get(handle: int) -> socket.socket:
    try:
        return _handles[handle]
    except KeyError:
        raise OSError(f"bad socket handle: {handle}")


def _put(sock: socket.socket) -> int:
    global _next_handle
    handle = _next_handle
    _next_handle += 1
    _handles[handle] = sock
    return handle


def sock_create(kind: str) -> int:
    """__sock_create(kind) -> handle, for kind "tcp" (a stream socket) or
    "udp" (a datagram socket) - both IPv4 (AF_INET)."""
    spec = _KINDS.get(kind)
    if spec is None:
        raise ValueError(f"__sock_create: unknown kind {kind!r} (expected 'tcp' or 'udp')")
    family, type_ = spec
    return _put(socket.socket(family, type_))


def sock_bind(handle: int, host: str, port: int) -> None:
    """__sock_bind(handle, host, port) -> binds `handle` to (host, port) -
    "" (or "0.0.0.0") for host means "every local interface", matching
    Python's/BSD's own convention. Sets SO_REUSEADDR first, since a POC
    server restarted right after a previous run would otherwise often hit
    "address already in use" waiting out TIME_WAIT for no good reason."""
    sock = _get(handle)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))


def sock_listen(handle: int, backlog: int = 16) -> None:
    """__sock_listen(handle, backlog=16) -> marks `handle` (already bound)
    as a listening socket, per the same POSIX listen(2) call this wraps."""
    _get(handle).listen(backlog)


def sock_accept(handle: int) -> list:
    """__sock_accept(handle) -> [new_handle, peer_host, peer_port], blocking
    until a client connects. `handle` itself keeps listening; `new_handle`
    is the accepted connection's own socket, closed independently."""
    conn, addr = _get(handle).accept()
    return [_put(conn), addr[0], addr[1]]


def sock_connect(handle: int, host: str, port: int) -> None:
    """__sock_connect(handle, host, port) -> blocks until connected (tcp)
    or, for udp, just remembers (host, port) as this socket's peer for
    later sock_send/sock_recv calls (the same "connected UDP socket"
    convenience BSD sockets already offer)."""
    _get(handle).connect((host, port))


def sock_send(handle: int, data: str) -> int:
    """__sock_send(handle, data) -> bytes sent, encoding `data` (a wyrm
    str) as UTF-8 first - text in, text out is this module's only mode,
    same simplification wyrm_io.py's own read/write make."""
    return _get(handle).send(data.encode("utf-8"))


def sock_recv(handle: int, size: int = 4096) -> str:
    """__sock_recv(handle, size=4096) -> up to `size` bytes read and
    decoded as UTF-8, or "" at end-of-stream (the peer closed) - matching
    Python's own socket.recv's "empty bytes means EOF" convention."""
    data = _get(handle).recv(size)
    return data.decode("utf-8", errors="replace")


def sock_sendto(handle: int, data: str, host: str, port: int) -> int:
    """__sock_sendto(handle, data, host, port) -> bytes sent, for an
    unconnected (or unconnected-so-far) udp socket - the datagram
    counterpart to sock_send, addressing each call independently rather
    than relying on a prior sock_connect."""
    return _get(handle).sendto(data.encode("utf-8"), (host, port))


def sock_recvfrom(handle: int, size: int = 4096) -> list:
    """__sock_recvfrom(handle, size=4096) -> [data, from_host, from_port],
    blocking until one datagram arrives - the udp counterpart to
    sock_accept in the sense that it's the call that hands back who's on
    the other end, since udp has no separate accept() of its own."""
    data, addr = _get(handle).recvfrom(size)
    return [data.decode("utf-8", errors="replace"), addr[0], addr[1]]


def sock_close(handle: int) -> None:
    """__sock_close(handle) -> closes and forgets `handle`; closing an
    already-closed (or never-valid) handle raises, same as a bad handle
    anywhere else in this module."""
    _get(handle).close()
    del _handles[handle]


def install(ctx: dict) -> None:
    from wypoc.wyrm_eval_parse_tree import expose_all

    expose_all(
        ctx,
        __sock_create=sock_create,
        __sock_bind=sock_bind,
        __sock_listen=sock_listen,
        __sock_accept=sock_accept,
        __sock_connect=sock_connect,
        __sock_send=sock_send,
        __sock_recv=sock_recv,
        __sock_sendto=sock_sendto,
        __sock_recvfrom=sock_recvfrom,
        __sock_close=sock_close,
    )
