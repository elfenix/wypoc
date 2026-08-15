"""The debugger core: an explicit call stack, a breakpoint table, and a
pause/resume/step state machine, all driven by hooking
`wypoc.wyrm_eval_parse_tree`'s `eval_stmt` - the one place every wyrm
statement everywhere passes through (module top level, fn/co bodies,
if/while/for bodies).

No JSON, no sockets, no DAP wire format here - `wypoc/dap/server.py` is the
only thing that imports this module and knows about the protocol. Kept
separate so this half (the part that actually understands wyrm) is
importable and testable headlessly, the way `wypoc/symbol_index.py` is
usable without `wypoc/lsp.py`.

One `Debugger` per running script. It owns the evaluator thread: `start()`
parses and runs the program on a dedicated thread (so a caller - eventually
the DAP server's stdio loop - stays responsive whether the evaluator is
running free or blocked at a breakpoint), and installs itself into
`wyrm_eval_parse_tree`'s module-level hooks for the duration.

Threading model: the evaluator thread runs wyrm code and blocks on
`self._resume_event` whenever paused; a debugger driver (a DAP server's
request-handling thread, or a test) calls `Debugger`'s control methods
(`resume`/`step_*`/`pause`/`set_breakpoints`) from any other thread. Those
methods only ever mutate small bits of state and set an Event - they never
block - so the driver is never stuck waiting on a paused evaluator. This is
the same shape `wyrm_eval_parse_tree.CoroutineInstance` already uses
internally (a dedicated thread blocking on a `threading.Event`, driven by
whoever calls its control methods), reused here for the same reason: the
GIL means only one of {evaluator thread, whoever's calling in} truly runs
at a time, so nothing here needs finer-grained locking than "wait on an
Event, then go"."""
import os
import threading

from wypoc import wyrm_eval_parse_tree as wyrm_eval
from wypoc import wyrm_builtins
from wypoc import wyrm_modules
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import (
    Frame, Scope, Variable, eval_program, expose, populate_globals,
)

# Run-mode values `Debugger._run_mode` can hold. "running" means "don't
# stop except for a breakpoint or an explicit pause request"; the step_*
# modes additionally stop once the call stack returns to (or below, for
# step_out) the depth captured when the step was requested.
RUNNING = "running"
STEP_IN = "step_in"
STEP_OVER = "step_over"
STEP_OUT = "step_out"
PAUSE_REQUESTED = "pause_requested"
PAUSED = "paused"


class StoppedInfo:
    """What a driver needs when the evaluator thread pauses: why, and (for
    an exception) what was raised and the stack as of the moment it was
    first seen - captured separately from `Debugger.stack` because by the
    time an exception reaches the top, the live stack has already unwound
    back through every `finally` it passed through on the way up."""

    def __init__(self, reason: str, exception: "BaseException | None" = None,
                 exception_stack: "list | None" = None):
        self.reason = reason
        self.exception = exception
        self.exception_stack = exception_stack


class Debugger:
    def __init__(self):
        self.stack: "list[Frame]" = []
        # filename -> set of 1-based line numbers, checked against
        # stmt.pos[0]. Not resolved against any particular path convention
        # here - the caller (server.py) decides what "filename" means and
        # is consistent between set_breakpoints and the pos it's comparing.
        self.breakpoints: "dict[str, set[int]]" = {}
        self.filename: "str | None" = None

        self._run_mode = RUNNING
        self._step_frame_depth = 0
        self._stop_on_entry = False
        self._resume_event = threading.Event()
        self._thread: "threading.Thread | None" = None

        # Set by on_stopped's caller (server.py) or a test; called from the
        # evaluator thread itself, right before it blocks, so the callback
        # sees a fully-consistent `self.stack`/`self.paused_at`.
        self.on_stopped = lambda info: None
        self.on_terminated = lambda: None
        self.paused_at = None  # (stmt, ctx) while paused, else None

    # -- starting a script ---------------------------------------------

    def load(self, source: str, filename: str) -> None:
        """Parses `source` and remembers it for `start()` - split out so a
        caller (server.py's `launch` handler) can report a parse error
        immediately rather than only on `configurationDone`."""
        self.filename = filename
        self._tree = parse(source, filename=filename)

    def start(self, args: "tuple[str, ...]" = (), stop_on_entry: bool = False) -> None:
        """Runs the loaded program on a dedicated thread. `stop_on_entry`
        pauses at the very first statement, the same way a breakpoint
        would, before any wyrm code runs."""
        self._stop_on_entry = stop_on_entry
        if stop_on_entry:
            self._run_mode = PAUSE_REQUESTED
        scope = Scope()
        populate_globals(scope)
        expose(scope, "__ARGS", tuple(args))
        wyrm_modules.set_script_root(os.path.dirname(os.path.abspath(self.filename)))
        self._thread = threading.Thread(target=self._run, args=(scope,), daemon=True)
        self._thread.start()

    def _run(self, scope: Scope) -> None:
        wyrm_eval._call_stack = self.stack
        wyrm_eval._stmt_hook = self._on_stmt
        wyrm_eval._exception_hook = self._on_exception
        try:
            eval_program(self._tree, scope)
        except BaseException:  # noqa: BLE001 - _on_exception already ran
            pass
        finally:
            wyrm_eval._call_stack = None
            wyrm_eval._stmt_hook = None
            wyrm_eval._exception_hook = None
            self.on_terminated()

    # -- breakpoints ------------------------------------------------------

    def set_breakpoints(self, filename: str, lines: "set[int]") -> None:
        """Replaces the breakpoint set for `filename` wholesale - DAP
        clients always resend the full set for a file on every
        `setBreakpoints` request, never a diff."""
        self.breakpoints[filename] = set(lines)

    # -- control (called from any thread other than the evaluator's) ------

    def resume(self) -> None:
        self._run_mode = RUNNING
        self._resume_event.set()

    def step_in(self) -> None:
        self._run_mode = STEP_IN
        self._resume_event.set()

    def step_over(self) -> None:
        self._run_mode = STEP_OVER
        self._step_frame_depth = len(self.stack)
        self._resume_event.set()

    def step_out(self) -> None:
        self._run_mode = STEP_OUT
        self._step_frame_depth = len(self.stack)
        self._resume_event.set()

    def pause(self) -> None:
        # No signal-based interruption needed: every statement, everywhere,
        # passes through _on_stmt imminently - the request is just a flag
        # the next call notices.
        if self._run_mode != PAUSED:
            self._run_mode = PAUSE_REQUESTED

    # -- the eval_stmt hook (runs on the evaluator thread) -----------------

    def _on_stmt(self, stmt, ctx) -> None:
        if self.stack:
            self.stack[-1].current_pos = stmt.pos
        if self._should_pause(stmt):
            self._enter_paused(stmt, ctx, StoppedInfo(self._pause_reason(stmt)))

    def _pause_reason(self, stmt) -> str:
        if self._run_mode == PAUSE_REQUESTED:
            if self._stop_on_entry:
                self._stop_on_entry = False
                return "entry"
            return "pause"
        if self._at_breakpoint(stmt):
            return "breakpoint"
        return "step"

    def _should_pause(self, stmt) -> bool:
        if self._run_mode == PAUSE_REQUESTED:
            return True
        if self._at_breakpoint(stmt):
            return True
        depth = len(self.stack)
        if self._run_mode == STEP_IN:
            return True
        if self._run_mode == STEP_OVER:
            return depth <= self._step_frame_depth
        if self._run_mode == STEP_OUT:
            return depth < self._step_frame_depth
        return False

    def _at_breakpoint(self, stmt) -> bool:
        if stmt.pos is None or self.filename is None:
            return False
        return stmt.pos[0] in self.breakpoints.get(self.filename, ())

    def _enter_paused(self, stmt, ctx, info: StoppedInfo) -> None:
        self.paused_at = (stmt, ctx)
        self._run_mode = PAUSED
        self.on_stopped(info)
        self._resume_event.wait()
        self._resume_event.clear()
        self.paused_at = None

    # -- the exception hook (runs on the evaluator thread) ------------------

    def _on_exception(self, exc: BaseException, stack_snapshot: list, pos) -> None:
        # `pos` (the raising statement's own position) is already on
        # stack_snapshot[-1].current_pos, set by _on_stmt just before the
        # statement ran - nothing more to attach it to.
        self._run_mode = PAUSED
        info = StoppedInfo("exception", exception=exc, exception_stack=stack_snapshot)
        self.on_stopped(info)
        self._resume_event.wait()
        self._resume_event.clear()

    # -- variable inspection ------------------------------------------------

    def locals_of(self, frame: Frame) -> dict:
        """Names declared directly in `frame`'s own scope - not walking to
        an enclosing one. `Scope` *is* a dict of its own level's bindings
        (see wyrm_eval_parse_tree.Scope) - `dict.items` (not the overridden
        instance methods, which walk `.parent`) reads just that level, and
        a plain dict copy of it is what callers actually want (a
        `dict_items` view isn't subscriptable, and Scope itself isn't
        hashable to put in a `set()`). Filtered to actual `Variable` cells:
        a call's scope also carries a couple of raw (non-Variable) internal
        bookkeeping entries - e.g. `__statics__`, `_bind_params_and_run`'s
        store for `static` locals - that aren't wyrm-level bindings and
        would break any caller expecting `.value` on every entry."""
        return {name: cell for name, cell in dict.items(frame.scope)
                if isinstance(cell, Variable)}

    def closure_of(self, frame: Frame) -> dict:
        """Names visible from an enclosing scope but not declared in
        `frame` itself - walking `frame.scope.parent` up to (but not
        including) the module scope, innermost binding wins on a shadowed
        name."""
        result: dict = {}
        scope = frame.scope.parent
        module_scope = self.stack[0].scope if self.stack else None
        while scope is not None and scope is not module_scope:
            for name, cell in dict.items(scope):
                if isinstance(cell, Variable):
                    result.setdefault(name, cell)
            scope = scope.parent
        return result

    def globals_of(self) -> dict:
        if not self.stack:
            return {}
        return {name: cell for name, cell in dict.items(self.stack[0].scope)
                if isinstance(cell, Variable)}

    @staticmethod
    def display_value(value) -> str:
        return wyrm_builtins.display(value)
