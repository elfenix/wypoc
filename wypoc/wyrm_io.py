"""POSIX-style low-level I/O primitives exposed to wyrm code.

wyrm code only ever deals with small integer handles (like real POSIX file
descriptors: 0/1/2 conventionally mean stdin/stdout/stderr); this module
maps those handles to actual Python file objects and does the real I/O
through Python's own io layer. Meant to back an `io` module written in wyrm
itself (see corelib/), not to be a full POSIX layer - no O_* flags, no
permissions modeling, no binary-vs-text distinction beyond what Python's own
open() mode string already gives you.
"""
import sys

STDIN = 0
STDOUT = 1
STDERR = 2

_handles: dict = {}
_next_handle = 3


def _reset_std_handles() -> None:
    """(Re)point handles 0/1/2 at the *current* sys.stdin/stdout/stderr -
    call this after swapping sys.stdout/etc. (e.g. in tests that capture
    output) so wyrm's __STDOUT keeps following it."""
    _handles[STDIN] = sys.stdin
    _handles[STDOUT] = sys.stdout
    _handles[STDERR] = sys.stderr


_reset_std_handles()


def _get(handle: int):
    try:
        return _handles[handle]
    except KeyError:
        raise OSError(f"bad file handle: {handle}")


def wyrm_open(path: str, mode: str = "r") -> int:
    """__open(path, mode="r") -> handle. `mode` is a Python open()-style
    mode string ("r", "w", "a", "rb", ...), not POSIX O_* flags."""
    global _next_handle
    f = open(path, mode)
    handle = _next_handle
    _next_handle += 1
    _handles[handle] = f
    return handle


def wyrm_read(handle: int, size: int = -1):
    """__read(handle, size=-1) -> up to `size` chars/bytes, or everything
    remaining if size is negative (mirrors Python's file.read())."""
    return _get(handle).read(size)


def wyrm_write(handle: int, data) -> int:
    """__write(handle, data) -> number of chars/bytes written. Flushes
    immediately - wyrm has no __close/__flush yet, so without this, a
    second handle opened on the same path wouldn't see what was just
    written until Python's own buffering happened to flush it."""
    f = _get(handle)
    n = f.write(data)
    f.flush()
    return n


def wyrm_lseek(handle: int, offset: int, whence: int = 0) -> int:
    """__lseek(handle, offset, whence=0) -> new absolute position.
    whence: 0 = SEEK_SET, 1 = SEEK_CUR, 2 = SEEK_END (same as os.SEEK_*)."""
    f = _get(handle)
    f.seek(offset, whence)
    return f.tell()


def wyrm_dup2(old_handle: int, new_handle: int) -> int:
    """__dup2(old, new) -> new. Makes `new` refer to the same underlying
    file object as `old` (so they share one file position, like real
    dup2), closing whatever `new` previously pointed to, if anything and
    if it isn't one of the standard handles."""
    target = _get(old_handle)
    existing = _handles.get(new_handle)
    if existing is not None and existing is not target and new_handle not in (STDIN, STDOUT, STDERR):
        existing.close()
    _handles[new_handle] = target
    return new_handle


def wyrm_close(handle: int) -> int:
    """__close(handle) -> 0. Closes the underlying file and forgets the
    handle, so a later __read/__write/__lseek/__close on it raises (a bad
    file handle), same as POSIX close() on an already-closed fd."""
    f = _get(handle)
    f.close()
    del _handles[handle]
    return 0


def wyrm_flush(handle: int) -> int:
    """__flush(handle) -> 0. Forces any buffered writes out."""
    _get(handle).flush()
    return 0


def install(ctx: dict) -> None:
    """Expose __open/__read/__write/__lseek/__dup2/__close/__flush and the
    __STDIN/__STDOUT/__STDERR constants into a wyrm scope."""
    from wypoc.wyrm_eval_parse_tree import expose_all

    expose_all(
        ctx,
        __open=wyrm_open,
        __read=wyrm_read,
        __write=wyrm_write,
        __lseek=wyrm_lseek,
        __dup2=wyrm_dup2,
        __close=wyrm_close,
        __flush=wyrm_flush,
        __STDIN=STDIN,
        __STDOUT=STDOUT,
        __STDERR=STDERR,
    )
