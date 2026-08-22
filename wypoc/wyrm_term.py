"""Raw terminal control primitives for building TUIs in wyrm, over Linux's
(POSIX's) termios/tty - no ncurses, no line-discipline abstraction beyond
what stdlib termios/tty already give: enter/exit raw mode, ask the terminal
its size, and read one keypress at a time. Everything else a TUI needs
(cursor movement, clearing, colors, the alternate screen buffer) is plain
ANSI escape sequences with no native code behind them at all - see
corelib/std/term.wy, which builds those directly over __write/__STDOUT
(wyrm_io.py), the same way this module's own primitives get their nice
names there.

Meant to be driven through corelib/std/term.wy, the same way wyrm_io.py
backs corelib/std/io.wy: this module only deals in plain Python/wyrm
values and the handful of `__term_*` primitives populate_globals installs.

## Raw mode

A TUI's main loop brackets itself with raw_enable()/raw_disable(): in raw
mode, read_key() sees each keypress the instant it arrives (no waiting for
Enter, no local echo, no Ctrl-C/Ctrl-Z signal generation) instead of a
buffered, line-editing terminal driver getting in the way first - the same
tradeoff curses' cbreak()+noecho() makes. raw_disable() is also registered
via `atexit`, as a safety net: a script that raw_enable()s and then crashes
(or is interrupted) before calling raw_disable() itself would otherwise
leave the user's shell looking broken (no echo, no newline translation)
after the process exits.
"""
import atexit
import os
import select
import termios
import tty


class TermError(Exception):
    """A terminal operation failed - typically "not a tty" (stdin/stdout
    piped or redirected, as a test runner's usually are)."""


_saved_state = None  # termios attrs from before raw_enable(), for restoring


def isatty(handle: int = 0) -> bool:
    """__term_isatty(handle=0) -> whether `handle` is connected to a real
    terminal - worth checking before raw_enable(), which needs one."""
    return os.isatty(handle)


def raw_enable(handle: int = 0) -> None:
    """__term_raw_enable(handle=0) -> puts `handle`'s terminal into raw
    mode (see the module docstring). Idempotent: a second call while
    already raw is a no-op, so a script doesn't need to track whether it's
    already called this."""
    global _saved_state
    if _saved_state is not None:
        return
    try:
        state = termios.tcgetattr(handle)
    except termios.error as e:
        raise TermError(f"fd {handle} isn't a terminal: {e}") from e
    tty.setraw(handle)
    _saved_state = state


def raw_disable(handle: int = 0) -> None:
    """__term_raw_disable(handle=0) -> restores whatever terminal mode
    raw_enable() saved. A no-op if raw mode was never entered (or was
    already exited) - see raw_enable's idempotence note."""
    global _saved_state
    if _saved_state is None:
        return
    termios.tcsetattr(handle, termios.TCSADRAIN, _saved_state)
    _saved_state = None


atexit.register(raw_disable)


def size() -> list:
    """__term_size() -> [rows, cols] of the controlling terminal, or the
    conventional [24, 80] fallback os.get_terminal_size() itself falls
    back to (COLUMNS/LINES env vars, then that default) when stdout isn't
    actually attached to one."""
    try:
        cols, rows = os.get_terminal_size()
    except OSError:
        rows, cols = 24, 80
    return [rows, cols]


def read_key(timeout: float = -1, handle: int = 0) -> int:
    """__term_read_key(timeout=-1, handle=0) -> the next byte read from
    `handle` as an integer codepoint (0-255), or -1 if `timeout` (seconds)
    elapses first without one arriving, or at end of input. `timeout < 0`
    (the default) blocks indefinitely, matching os.read's own blocking
    behavior. Reads one *byte*, not one character/escape sequence - a
    multi-byte UTF-8 character or an ANSI escape sequence (arrow keys,
    etc.) arrives as several calls' worth; assembling those is corelib's
    job (or the script's own), same as it would be reading raw bytes off
    any other handle."""
    if timeout is not None and timeout >= 0:
        ready, _, _ = select.select([handle], [], [], timeout)
        if not ready:
            return -1
    data = os.read(handle, 1)
    return data[0] if data else -1


def install(ctx: dict) -> None:
    from wypoc.wyrm_eval_parse_tree import expose_all

    expose_all(
        ctx,
        __term_isatty=isatty,
        __term_raw_enable=raw_enable,
        __term_raw_disable=raw_disable,
        __term_size=size,
        __term_read_key=read_key,
    )
