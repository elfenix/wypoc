"""The DAP request/response/event layer: glues `protocol.py`'s stdio
transport to `debugger.py`'s `Debugger`. One `Session` per connection
(equivalently, per debuggee - v1 supports exactly one `launch` per
session, no multi-session/attach).

Threading: `serve()`'s loop (the "IO thread") reads and dispatches DAP
requests synchronously - every `_cmd_*` handler below only mutates small
bits of `Debugger` state and returns immediately, never blocking on the
evaluator, so this thread is always free to answer the next request (e.g.
`stackTrace` while paused). The evaluator itself runs on the dedicated
thread `Debugger.start()` spawns; its `on_stopped`/`on_terminated`
callbacks (`Session._on_stopped`/`_on_terminated`, wired in `_cmd_launch`)
run *on that thread* and write DAP events directly - safe because
`protocol.MessageWriter` serializes writes with its own lock, so nothing
here needs a queue between the two threads."""
import sys
import threading

from wypoc.dap.debugger import Debugger, StoppedInfo
from wypoc.dap.protocol import ConnectionClosed, MessageWriter, read_message

# DAP's own default indexing - lines and columns both 1-based unless a
# client's `initialize` arguments say otherwise.
_DEFAULT_STARTS_AT_1 = True


class Session:
    def __init__(self, writer: MessageWriter):
        self.writer = writer
        self._seq = 0
        self.debugger: "Debugger | None" = None
        self._program_path: "str | None" = None
        self._launch_args: tuple = ()
        self._stop_on_entry = False
        self._lines_start_at_1 = _DEFAULT_STARTS_AT_1
        self._cols_start_at_1 = _DEFAULT_STARTS_AT_1
        # Rebuilt on every `stopped` event - DAP explicitly allows frame
        # ids and variablesReference handles to become invalid once
        # execution moves on, so there's no need for a longer-lived cache.
        self._current_frames: list = []
        self._var_refs: dict = {}
        self._next_var_ref = 1
        self._last_stopped: "StoppedInfo | None" = None
        self.done = threading.Event()

    # -- request dispatch ---------------------------------------------------

    def handle(self, message: dict) -> None:
        if message.get("type") != "request":
            return
        command = message["command"]
        request_seq = message["seq"]
        arguments = message.get("arguments") or {}
        handler = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            self._respond(request_seq, command, False,
                          message=f"unsupported request: {command!r}")
            return
        try:
            body = handler(arguments)
        except Exception as e:  # noqa: BLE001 - report to the client, don't crash the session
            self._respond(request_seq, command, False, message=str(e))
            return
        self._respond(request_seq, command, True, body=body)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _respond(self, request_seq: int, command: str, success: bool,
                 body: "dict | None" = None, message: "str | None" = None) -> None:
        msg = {"type": "response", "seq": self._next_seq(), "request_seq": request_seq,
               "success": success, "command": command}
        if body is not None:
            msg["body"] = body
        if message is not None:
            msg["message"] = message
        self.writer.write(msg)

    def _event(self, event: str, body: "dict | None" = None) -> None:
        msg = {"type": "event", "seq": self._next_seq(), "event": event}
        if body is not None:
            msg["body"] = body
        self.writer.write(msg)

    # -- handshake ------------------------------------------------------

    def _cmd_initialize(self, args: dict) -> dict:
        self._lines_start_at_1 = args.get("linesStartAt1", _DEFAULT_STARTS_AT_1)
        self._cols_start_at_1 = args.get("columnsStartAt1", _DEFAULT_STARTS_AT_1)
        self._event("initialized")
        return {
            "supportsConfigurationDoneRequest": True,
            "supportsExceptionInfoRequest": True,
        }

    def _cmd_launch(self, args: dict) -> dict:
        program = args["program"]
        with open(program, encoding="utf-8") as f:
            source = f.read()
        self.debugger = Debugger()
        self.debugger.on_stopped = self._on_stopped
        self.debugger.on_terminated = self._on_terminated
        self.debugger.load(source, program)
        self._program_path = program
        self._launch_args = tuple(args.get("args", ()))
        self._stop_on_entry = bool(args.get("stopOnEntry", False))
        return {}

    def _cmd_setBreakpoints(self, args: dict) -> dict:
        path = args["source"]["path"]
        requested = args.get("breakpoints")
        lines = [bp["line"] for bp in requested] if requested is not None \
            else list(args.get("lines", ()))
        self.debugger.set_breakpoints(path, set(lines))
        return {"breakpoints": [{"verified": True, "line": line} for line in lines]}

    def _cmd_configurationDone(self, args: dict) -> dict:
        self.debugger.start(args=self._launch_args, stop_on_entry=self._stop_on_entry)
        return {}

    def _cmd_threads(self, args: dict) -> dict:
        # A wyrm program is one DAP thread in v1, even where it uses `co`
        # internally - see the plan's "out of scope" notes.
        return {"threads": [{"id": 1, "name": "main"}]}

    # -- run control ------------------------------------------------------

    def _cmd_continue(self, args: dict) -> dict:
        self.debugger.resume()
        return {"allThreadsContinued": True}

    def _cmd_next(self, args: dict) -> dict:
        self.debugger.step_over()
        return {}

    def _cmd_stepIn(self, args: dict) -> dict:
        self.debugger.step_in()
        return {}

    def _cmd_stepOut(self, args: dict) -> dict:
        self.debugger.step_out()
        return {}

    def _cmd_pause(self, args: dict) -> dict:
        self.debugger.pause()
        return {}

    # -- inspection (only meaningful while paused) -------------------------

    def _cmd_stackTrace(self, args: dict) -> dict:
        stack_frames = []
        for i, frame in enumerate(self._current_frames):
            line, column = self._pos_to_dap(frame.current_pos)
            stack_frames.append({
                "id": i,
                "name": frame.name,
                "source": {"path": self._program_path},
                "line": line,
                "column": column,
            })
        return {"stackFrames": stack_frames, "totalFrames": len(stack_frames)}

    def _cmd_scopes(self, args: dict) -> dict:
        frame = self._current_frames[args["frameId"]]
        scopes = [
            {"name": name, "variablesReference": self._alloc_var_ref((kind, frame)),
             "expensive": False}
            for name, kind in (("Locals", "locals"), ("Closure", "closure"),
                                ("Globals", "globals"))
        ]
        return {"scopes": scopes}

    def _cmd_variables(self, args: dict) -> dict:
        kind, frame = self._var_refs[args["variablesReference"]]
        if kind == "locals":
            values = self.debugger.locals_of(frame)
        elif kind == "closure":
            values = self.debugger.closure_of(frame)
        else:
            values = self.debugger.globals_of()
        variables = [
            {"name": name, "value": Debugger.display_value(cell.value),
             "variablesReference": 0}
            for name, cell in sorted(values.items())
        ]
        return {"variables": variables}

    def _cmd_exceptionInfo(self, args: dict) -> dict:
        exc = self._last_stopped.exception if self._last_stopped else None
        return {
            "exceptionId": type(exc).__name__ if exc is not None else "Error",
            "description": str(exc) if exc is not None else "",
            "breakMode": "unhandled",
        }

    # -- teardown -----------------------------------------------------------

    def _cmd_disconnect(self, args: dict) -> dict:
        if self.debugger is not None:
            self.debugger.resume()  # don't leave a paused evaluator thread hanging
        self.done.set()
        return {}

    def _cmd_terminate(self, args: dict) -> dict:
        return self._cmd_disconnect(args)

    # -- helpers -----------------------------------------------------------

    def _pos_to_dap(self, pos) -> "tuple[int, int]":
        """ast_nodes.Span is `(line, col, end_line, end_col)`: 1-based
        line, 0-based col. Line needs no conversion unless the client
        opted into 0-based lines (`linesStartAt1: false`); col needs +1
        since DAP's own default is 1-based columns, unless the client
        opted into 0-based ones."""
        if pos is None:
            return 1, (1 if self._cols_start_at_1 else 0)
        line, col = pos[0], pos[1]
        if not self._lines_start_at_1:
            line -= 1
        if self._cols_start_at_1:
            col += 1
        return line, col

    def _alloc_var_ref(self, value) -> int:
        ref = self._next_var_ref
        self._next_var_ref += 1
        self._var_refs[ref] = value
        return ref

    # -- evaluator-thread callbacks -------------------------------------

    def _on_stopped(self, info: StoppedInfo) -> None:
        self._last_stopped = info
        self._var_refs = {}
        self._next_var_ref = 1
        self._current_frames = list(reversed(
            info.exception_stack if info.exception_stack is not None else self.debugger.stack))
        body = {"reason": info.reason, "threadId": 1, "allThreadsStopped": True}
        if info.exception is not None:
            body["description"] = str(info.exception)
            body["text"] = f"{type(info.exception).__name__}: {info.exception}"
        self._event("stopped", body)

    def _on_terminated(self) -> None:
        self._event("terminated")


def serve(input_stream, output_stream) -> None:
    """Runs one DAP session to completion: reads requests from
    `input_stream` and dispatches them until the client disconnects (or
    closes its side of the connection) - the ordinary shape of a DAP
    adapter's process lifetime."""
    session = Session(MessageWriter(output_stream))
    while not session.done.is_set():
        try:
            message = read_message(input_stream)
        except ConnectionClosed:
            break
        session.handle(message)


def main() -> int:
    serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
