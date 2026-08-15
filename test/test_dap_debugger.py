"""The debugger core (wypoc/dap/debugger.py), driven directly - no DAP wire
protocol involved. Each test runs a small wyrm source snippet under a
`Debugger` on its own thread and synchronizes with it through `on_stopped`/
`on_terminated`, exactly the way a DAP server (untested here) eventually
will.
"""
import queue

from wypoc.dap.debugger import Debugger


class Harness:
    """Runs `source` under a `Debugger`, funneling `on_stopped`/
    `on_terminated` into one queue so a test can `next_event()` without
    caring which one fires next."""

    def __init__(self, source: str, filename: str = "test.wy"):
        self.debugger = Debugger()
        self.debugger.load(source, filename)
        self.events: "queue.Queue" = queue.Queue()
        self.debugger.on_stopped = lambda info: self.events.put(("stopped", info))
        self.debugger.on_terminated = lambda: self.events.put(("terminated", None))

    def start(self, **kwargs):
        self.debugger.start(**kwargs)
        return self.next_event()

    def next_event(self, timeout: float = 5):
        return self.events.get(timeout=timeout)


def test_breakpoint_pauses_with_expected_stack_and_locals():
    source = "var x = 1\nvar y = 2\nvar z = x + y\n"
    h = Harness(source)
    h.debugger.set_breakpoints("test.wy", {3})

    kind, info = h.start()
    assert (kind, info.reason) == ("stopped", "breakpoint")
    assert len(h.debugger.stack) == 1
    frame = h.debugger.stack[0]
    assert frame.current_pos[0] == 3
    locs = h.debugger.locals_of(frame)
    assert locs["x"].value == 1 and locs["y"].value == 2
    assert "z" not in locs, "the breakpointed statement hasn't run yet"

    h.debugger.resume()
    assert h.next_event() == ("terminated", None)


def test_no_breakpoint_means_it_just_runs_to_completion():
    h = Harness("var x = 1\n")
    assert h.start() == ("terminated", None)


def test_step_over_does_not_descend_into_a_call():
    source = (
        "fn add(a, b):\n"
        "    var result = a + b\n"
        "    return result\n"
        "\n"
        "var total = add(1, 2)\n"
        "var after = total + 1\n"
    )
    h = Harness(source)
    h.debugger.set_breakpoints("test.wy", {5})

    kind, info = h.start()
    assert (kind, info.reason) == ("stopped", "breakpoint")
    assert len(h.debugger.stack) == 1

    h.debugger.step_over()
    kind, info = h.next_event()
    assert (kind, info.reason) == ("stopped", "step")
    assert len(h.debugger.stack) == 1, "should not have descended into add()"
    assert h.debugger.stack[0].current_pos[0] == 6

    h.debugger.resume()
    assert h.next_event() == ("terminated", None)


def test_step_in_descends_into_the_call():
    source = (
        "fn add(a, b):\n"
        "    var result = a + b\n"
        "    return result\n"
        "\n"
        "var total = add(1, 2)\n"
    )
    h = Harness(source)
    h.debugger.set_breakpoints("test.wy", {5})

    kind, info = h.start()
    assert (kind, info.reason) == ("stopped", "breakpoint")

    h.debugger.step_in()
    kind, info = h.next_event()
    assert (kind, info.reason) == ("stopped", "step")
    assert len(h.debugger.stack) == 2
    frame = h.debugger.stack[-1]
    assert frame.current_pos[0] == 2
    locs = h.debugger.locals_of(frame)
    assert locs["a"].value == 1 and locs["b"].value == 2

    h.debugger.resume()
    assert h.next_event() == ("terminated", None)


def test_step_out_returns_to_the_caller():
    source = (
        "fn add(a, b):\n"
        "    var result = a + b\n"
        "    return result\n"
        "\n"
        "var total = add(1, 2)\n"
        "var after = total + 1\n"
    )
    h = Harness(source)
    h.debugger.set_breakpoints("test.wy", {2})

    kind, info = h.start()
    assert (kind, info.reason) == ("stopped", "breakpoint")
    assert len(h.debugger.stack) == 2

    h.debugger.step_out()
    kind, info = h.next_event()
    assert (kind, info.reason) == ("stopped", "step")
    assert len(h.debugger.stack) == 1
    assert h.debugger.stack[0].current_pos[0] == 6

    h.debugger.resume()
    assert h.next_event() == ("terminated", None)


def test_loop_body_breakpoint_fires_every_iteration():
    source = "var i = 0\nwhile i < 3:\n    i = i + 1\n"
    h = Harness(source)
    h.debugger.set_breakpoints("test.wy", {3})

    kind, info = h.start()
    hits = 0
    while kind == "stopped":
        assert info.reason == "breakpoint"
        hits += 1
        h.debugger.resume()
        kind, info = h.next_event()
    assert kind == "terminated"
    assert hits == 3


def test_stop_on_entry_pauses_before_the_first_statement():
    h = Harness("var x = 1\nvar y = 2\n")
    kind, info = h.start(stop_on_entry=True)
    assert (kind, info.reason) == ("stopped", "entry")
    assert h.debugger.stack[0].current_pos[0] == 1

    h.debugger.resume()
    assert h.next_event() == ("terminated", None)


def test_closure_variables_are_distinguished_from_locals_and_globals():
    source = (
        "fn make_adder(n):\n"
        "    fn adder(x):\n"
        "        return x + n\n"
        "    return adder\n"
        "\n"
        "var top = 100\n"
        "var add5 = make_adder(5)\n"
        "var result = add5(10)\n"
    )
    h = Harness(source)
    h.debugger.set_breakpoints("test.wy", {3})

    kind, info = h.start()
    assert (kind, info.reason) == ("stopped", "breakpoint")
    frame = h.debugger.stack[-1]

    locs = h.debugger.locals_of(frame)
    assert set(locs) == {"x"}
    assert locs["x"].value == 10

    closure = h.debugger.closure_of(frame)
    assert "n" in closure and closure["n"].value == 5
    assert "n" not in locs
    assert "top" not in closure, "module-level names are globals, not closure"

    globs = h.debugger.globals_of()
    assert "top" in globs and globs["top"].value == 100

    h.debugger.resume()
    assert h.next_event() == ("terminated", None)


def test_uncaught_exception_pauses_with_the_stack_intact():
    source = (
        "fn inner():\n"
        "    return undeclared_name\n"
        "\n"
        "fn outer():\n"
        "    return inner()\n"
        "\n"
        "outer()\n"
    )
    h = Harness(source)

    kind, info = h.start()
    assert kind == "stopped"
    assert info.reason == "exception"
    assert info.exception is not None
    names = [frame.name for frame in info.exception_stack]
    assert names == ["<module>", "outer", "inner"], \
        "the stack should still show every frame, though the real Python " \
        "stack has already unwound by the time this hook runs"

    h.debugger.resume()
    kind, info = h.next_event()
    assert kind == "terminated"


def test_a_breakpoint_never_touched_does_nothing():
    h = Harness("var x = 1\n")
    h.debugger.set_breakpoints("test.wy", {99})
    assert h.start() == ("terminated", None)
