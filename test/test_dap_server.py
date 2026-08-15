"""The DAP server (wypoc/dap/server.py + protocol.py) end-to-end: real
Content-Length-framed JSON over a pair of OS pipes, `serve()` running on
its own thread exactly as it would talk to a real client's stdin/stdout.
"""
import os
import queue
import threading

import pytest

from wypoc.dap.protocol import ConnectionClosed, MessageWriter, read_message
from wypoc.dap.server import serve


class Client:
    """A minimal DAP client: sends requests, matches responses by
    `request_seq`, and buffers anything else (events) for `wait_event` to
    search - a real client interleaves both, and tests need to see both."""

    def __init__(self):
        to_server_r, to_server_w = os.pipe()
        to_client_r, to_client_w = os.pipe()
        self._out = os.fdopen(to_server_w, "wb", buffering=0)
        self._in = os.fdopen(to_client_r, "rb", buffering=0)
        server_in = os.fdopen(to_server_r, "rb", buffering=0)
        server_out = os.fdopen(to_client_w, "wb", buffering=0)

        self._writer = MessageWriter(self._out)
        self._seq = 0
        self._incoming: "queue.Queue" = queue.Queue()
        self._events: list = []

        self._server_thread = threading.Thread(
            target=serve, args=(server_in, server_out), daemon=True)
        self._server_thread.start()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self) -> None:
        while True:
            try:
                message = read_message(self._in)
            except ConnectionClosed:
                return
            self._incoming.put(message)

    def request(self, command: str, arguments: "dict | None" = None, timeout: float = 5) -> dict:
        self._seq += 1
        seq = self._seq
        message = {"seq": seq, "type": "request", "command": command}
        if arguments is not None:
            message["arguments"] = arguments
        self._writer.write(message)
        while True:
            message = self._incoming.get(timeout=timeout)
            if message.get("type") == "response" and message.get("request_seq") == seq:
                return message
            self._events.append(message)

    def wait_event(self, event: str, timeout: float = 5) -> dict:
        for i, message in enumerate(self._events):
            if message.get("type") == "event" and message.get("event") == event:
                return self._events.pop(i)
        while True:
            message = self._incoming.get(timeout=timeout)
            if message.get("type") == "event" and message.get("event") == event:
                return message
            self._events.append(message)


@pytest.fixture
def client():
    return Client()


def write_script(tmp_path, name: str, source: str) -> str:
    path = tmp_path / name
    path.write_text(source)
    return str(path)


def start_session(client: Client, program: str, breakpoints: "list[int]" = (),
                   stop_on_entry: bool = False) -> None:
    assert client.request("initialize", {})["success"]
    client.wait_event("initialized")
    assert client.request("launch", {"program": program, "stopOnEntry": stop_on_entry})["success"]
    if breakpoints:
        reply = client.request("setBreakpoints", {
            "source": {"path": program},
            "breakpoints": [{"line": line} for line in breakpoints],
        })
        assert reply["success"]
        assert all(bp["verified"] for bp in reply["body"]["breakpoints"])
    assert client.request("configurationDone", {})["success"]


def test_breakpoint_then_continue_to_termination(client, tmp_path):
    program = write_script(tmp_path, "t.wy", "var x = 1\nvar y = 2\n")
    start_session(client, program, breakpoints=[2])

    stopped = client.wait_event("stopped")
    assert stopped["body"]["reason"] == "breakpoint"
    assert stopped["body"]["threadId"] == 1

    assert client.request("continue", {"threadId": 1})["success"]
    client.wait_event("terminated")


def test_threads_reports_a_single_fixed_thread(client, tmp_path):
    program = write_script(tmp_path, "t.wy", "var x = 1\n")
    start_session(client, program)
    reply = client.request("threads", {})
    assert reply["body"]["threads"] == [{"id": 1, "name": "main"}]
    client.wait_event("terminated")


def test_stepping_over_a_call_does_not_descend_into_it(client, tmp_path):
    program = write_script(tmp_path, "t.wy", (
        "fn add(a, b):\n"
        "    var result = a + b\n"
        "    return result\n"
        "\n"
        "var total = add(1, 2)\n"
        "var after = total + 1\n"
    ))
    start_session(client, program, breakpoints=[5])
    client.wait_event("stopped")

    assert client.request("next", {"threadId": 1})["success"]
    stopped = client.wait_event("stopped")
    assert stopped["body"]["reason"] == "step"

    trace = client.request("stackTrace", {"threadId": 1})["body"]
    assert len(trace["stackFrames"]) == 1
    assert trace["stackFrames"][0]["line"] == 6

    client.request("continue", {"threadId": 1})
    client.wait_event("terminated")


def test_step_in_and_variables_round_trip(client, tmp_path):
    program = write_script(tmp_path, "t.wy", (
        "fn add(a, b):\n"
        "    var result = a + b\n"
        "    return result\n"
        "\n"
        "var total = add(1, 2)\n"
    ))
    start_session(client, program, breakpoints=[5])
    client.wait_event("stopped")

    assert client.request("stepIn", {"threadId": 1})["success"]
    stopped = client.wait_event("stopped")
    assert stopped["body"]["reason"] == "step"

    trace = client.request("stackTrace", {"threadId": 1})["body"]
    assert [f["name"] for f in trace["stackFrames"]] == ["add", "<module>"]
    assert trace["stackFrames"][0]["line"] == 2

    scopes = client.request("scopes", {"frameId": 0})["body"]["scopes"]
    names = {s["name"]: s["variablesReference"] for s in scopes}
    assert set(names) == {"Locals", "Closure", "Globals"}

    local_vars = client.request("variables", {"variablesReference": names["Locals"]})["body"]
    values = {v["name"]: v["value"] for v in local_vars["variables"]}
    assert values == {"a": "1", "b": "2"}

    global_vars = client.request("variables", {"variablesReference": names["Globals"]})["body"]
    global_names = {v["name"] for v in global_vars["variables"]}
    assert "add" in global_names

    client.request("continue", {"threadId": 1})
    client.wait_event("terminated")


def test_step_out_returns_to_the_caller(client, tmp_path):
    program = write_script(tmp_path, "t.wy", (
        "fn add(a, b):\n"
        "    var result = a + b\n"
        "    return result\n"
        "\n"
        "var total = add(1, 2)\n"
        "var after = total + 1\n"
    ))
    start_session(client, program, breakpoints=[2])
    client.wait_event("stopped")

    assert client.request("stepOut", {"threadId": 1})["success"]
    stopped = client.wait_event("stopped")
    assert stopped["body"]["reason"] == "step"

    trace = client.request("stackTrace", {"threadId": 1})["body"]
    assert len(trace["stackFrames"]) == 1
    assert trace["stackFrames"][0]["line"] == 6

    client.request("continue", {"threadId": 1})
    client.wait_event("terminated")


def test_pause_requested_before_start_stops_at_the_first_statement(client, tmp_path):
    # Requesting a pause while nothing is running yet is deterministic (no
    # race against how fast the program runs): it just leaves the debugger
    # already primed to stop at the very first eval_stmt hit, the same way
    # stopOnEntry does.
    program = write_script(tmp_path, "t.wy", "var x = 1\nvar y = 2\n")
    assert client.request("initialize", {})["success"]
    client.wait_event("initialized")
    assert client.request("launch", {"program": program})["success"]
    assert client.request("pause", {"threadId": 1})["success"]
    assert client.request("configurationDone", {})["success"]

    stopped = client.wait_event("stopped")
    assert stopped["body"]["reason"] == "pause"

    client.request("continue", {"threadId": 1})
    client.wait_event("terminated")


def test_uncaught_exception_reports_stack_and_exception_info(client, tmp_path):
    program = write_script(tmp_path, "t.wy", (
        "fn inner():\n"
        "    return undeclared_name\n"
        "\n"
        "fn outer():\n"
        "    return inner()\n"
        "\n"
        "outer()\n"
    ))
    start_session(client, program)

    stopped = client.wait_event("stopped")
    assert stopped["body"]["reason"] == "exception"

    trace = client.request("stackTrace", {"threadId": 1})["body"]
    assert [f["name"] for f in trace["stackFrames"]] == ["inner", "outer", "<module>"]

    info = client.request("exceptionInfo", {"threadId": 1})["body"]
    assert info["exceptionId"] == "NameError"

    client.request("continue", {"threadId": 1})
    client.wait_event("terminated")


def test_stop_on_entry(client, tmp_path):
    program = write_script(tmp_path, "t.wy", "var x = 1\n")
    start_session(client, program, stop_on_entry=True)
    stopped = client.wait_event("stopped")
    assert stopped["body"]["reason"] == "entry"
    client.request("continue", {"threadId": 1})
    client.wait_event("terminated")


def test_disconnect_lets_a_paused_program_finish_and_closes_the_session(client, tmp_path):
    program = write_script(tmp_path, "t.wy", "var x = 1\n")
    start_session(client, program, breakpoints=[1])
    client.wait_event("stopped")
    assert client.request("disconnect", {})["success"]
