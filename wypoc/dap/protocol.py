"""The DAP wire transport: `Content-Length: N\\r\\n\\r\\n<json>` framing over
a pair of byte streams (stdin/stdout for a real client, an in-memory pipe
for tests). No knowledge of DAP's request/response/event *shapes* lives
here - just "read one message", "write one message" - so this file has no
dependency on wyrm or on `debugger.py` at all, and would work for any
Content-Length-framed JSON protocol (DAP or otherwise).
"""
import json
import threading


class ConnectionClosed(Exception):
    """The input stream ended (EOF) before/while reading a message - the
    ordinary way a DAP session ends (the client closes its side)."""


def read_message(stream) -> dict:
    """Reads one `Content-Length`-framed JSON message from `stream` (a
    binary file-like object - `sys.stdin.buffer`, a socket's `makefile('rb')`,
    or a test's `io.BytesIO`/pipe). Raises `ConnectionClosed` on EOF."""
    length = None
    while True:
        line = stream.readline()
        if line == b"":
            raise ConnectionClosed()
        line = line.strip()
        if not line:
            break  # blank line ends the header block
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    if length is None:
        raise ConnectionClosed()
    body = _read_exactly(stream, length)
    return json.loads(body.decode("utf-8"))


def _read_exactly(stream, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise ConnectionClosed()
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class MessageWriter:
    """Writes Content-Length-framed JSON messages to `stream`, serialized
    with a lock: a DAP session writes from more than one thread (the
    request-handling thread replying, the evaluator thread's `on_stopped`
    sending an unsolicited `stopped` event), and interleaved writes would
    corrupt the framing."""

    def __init__(self, stream):
        self.stream = stream
        self._lock = threading.Lock()

    def write(self, message: dict) -> None:
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._lock:
            self.stream.write(header)
            self.stream.write(body)
            self.stream.flush()
