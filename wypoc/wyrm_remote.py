"""wyrm routines, process edition: `thread a::b::c` spawns `a::b::c` fresh
on its own OS process - real `multiprocessing`, not a thread. "No dynamic
scope reuse - the thread is essentially a full copied process" (the design
this implements) is what a real OS process gives for free: a fresh Python
interpreter, fresh globals, an empty `_module_cache` of its own, none of
which `wyrm_eval_parse_tree.import_module`'s cache is ever touched by or
shares with.

Wire protocol, two `multiprocessing.Queue`s per `RemoteModule`:

    call_q (parent -> child): ("call", name, positional, kwargs)
    event_q (child -> parent): ("result", value)
                              | ("error", type_name, message)
                              | ("signal", name, args)
                              | ("exit", code)

Only plain wyrm values (numbers, strings, lists/dicts, simple
ClassInstances) may cross either queue - anything `populate_globals`
installs (native builtins, sockets, open files) is not picklable and must
not appear in a `fn []`/`signal`'s arguments or return value. Not
enforced here; a `PicklingError` at the queue boundary is what a script
crossing this line gets instead, which is the accepted POC-level failure
mode.

Known gap: a signal fired by the remote module while the parent isn't
inside an active blocking `remote ! name(...)` call (nothing draining
event_q) is not observed until the next blocking call happens to drain it.
True asynchronous/unprompted delivery would need a background drain thread
in the parent - out of scope for this pass.
"""
import multiprocessing
import os
import sys

from wypoc import wyrm_modules
from wypoc.parse import parse

# "spawn", not the platform default "fork": coroutines already run
# background daemon threads (CoroutineInstance, wyrm_eval_parse_tree.py),
# and forking a multithreaded process is a known deadlock hazard (this
# repo's own test suite already carries a live DeprecationWarning about it
# from test_wyrm_term.py's pty test) - spawn re-execs Python cleanly and
# sidesteps that entirely, at the cost of a heavier startup, which matches
# the "heavyweight, unlike a coroutine" cost model `thread` already has.
_CTX = multiprocessing.get_context("spawn")

# Every RemoteModule this process has spawned, so an implicit or explicit
# exit() can tear all of them down on the way out (see terminate_all()) -
# multiprocessing otherwise joins non-daemon children at interpreter exit,
# which would hang the whole program waiting on a process blocked forever
# on its own empty call_q.
_registry: list = []


class RemoteModule:
    """The value a `thread a::b::c` expression evaluates to: a live child
    process running that module, plus the queues used to call into it and
    receive its results/signal events (see the module docstring's wire
    protocol)."""

    def __init__(self, name: str, process: "multiprocessing.process.BaseProcess",
                 call_q, event_q):
        self.name = name
        self.process = process
        self._call_q = call_q
        self._event_q = event_q
        self._signal_proxies: dict = {}

    def __repr__(self):
        return f"RemoteModule({self.name!r})"

    def signal(self, name: str):
        """The parent-side proxy SignalValue for the remote module's
        `name` signal - created lazily on first access (`remote.name`, see
        wyrm_eval_parse_tree.py's ast.Attr case). `connect`/`disconnect` on
        it are the existing generic wildcard natives - no new code needed
        there. Not validated against what the remote module actually
        declares: a name nothing ever emits just never fires, silently."""
        proxy = self._signal_proxies.get(name)
        if proxy is None:
            from wypoc.wyrm_eval_parse_tree import SignalValue

            proxy = SignalValue(name)
            self._signal_proxies[name] = proxy
        return proxy

    def call(self, name: str, positional, kwargs):
        """`remote ! name(...)` - sends the call, then blocks draining
        event_q on the caller's own thread: a ("signal", ...) event
        dispatches to this remote's own locally-held proxy subscribers
        before the loop continues, a ("result", ...) event ends it. No
        background thread needed for this - the whole round trip runs on
        whichever wyrm thread made the call, matching "all calls/slots are
        blocking, the full round trip must happen"."""
        from wypoc.wyrm_eval_parse_tree import call_value

        self._call_q.put(("call", name, positional, kwargs))
        while True:
            item = self._event_q.get()
            kind = item[0]
            if kind == "result":
                return item[1]
            if kind == "signal":
                _, sig_name, args = item
                proxy = self._signal_proxies.get(sig_name)
                if proxy is not None:
                    for callback in list(proxy.subscribers):
                        call_value(callback, list(args), {})
                continue
            if kind == "error":
                _, type_name, message = item
                raise RuntimeError(f"{self.name}: {type_name}: {message}")
            if kind == "exit":
                # `exit()` inside the remote handler - best-effort full-tree
                # teardown: stop every other spawned process, then this
                # process itself, same as main's own exit() would (see
                # cli.py). The call that triggered this never gets a
                # "result" - the whole program is going away instead.
                _, code = item
                terminate_all(exclude=self)
                sys.exit(code)
            raise RuntimeError(f"unknown remote event {item!r}")

    def terminate(self) -> None:
        """Stops this remote process - best-effort. Used by terminate_all()
        and directly, for a RemoteModule the caller is done with."""
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)
        if self in _registry:
            _registry.remove(self)


def terminate_all(exclude: "RemoteModule | None" = None) -> None:
    """Tears down every still-registered RemoteModule - what an implicit or
    explicit exit() does on its way out (see cli.py's EndSignal/ExitSignal
    handling), so a spawned process blocked on its own call_q doesn't leave
    the interpreter hanging at shutdown."""
    for remote in list(_registry):
        if remote is not exclude:
            remote.terminate()


def spawn_module_process(path_segments) -> RemoteModule:
    """`thread a::b::c` - resolves the module file in *this* (parent)
    process, via the same wyrm_modules.resolve_module_file import_module
    itself uses, but never import_module: a `thread`-spawned module must
    not touch (or be touched by) the caller's own _module_cache, since it
    gets a fully separate process, not a share of the caller's. Resolving
    here rather than in the child also sidesteps the child's fresh
    interpreter not inheriting the parent's wyrm_modules search-path setup
    (set_script_root/set_extra_search_paths are runtime state, not
    environment - only WYRM_PATH itself crosses a spawned process
    automatically)."""
    resolved = wyrm_modules.resolve_module_file(path_segments)
    if resolved is None:
        key = "::".join(path_segments)
        raise ImportError(
            f"no module named {key!r} (searched: {', '.join(wyrm_modules.search_paths())})"
        )
    file_path, _is_package = resolved

    call_q = _CTX.Queue()
    event_q = _CTX.Queue()
    process = _CTX.Process(target=_remote_main, args=(file_path, call_q, event_q), daemon=False)
    process.start()

    remote = RemoteModule("::".join(path_segments), process, call_q, event_q)
    _registry.append(remote)
    return remote


def _remote_main(file_path: str, call_q, event_q) -> None:
    """The child process's entire life: load the module once, then serve
    calls off call_q until told to stop. Runs in a fresh Python interpreter
    (the "spawn" start method) - none of the parent's in-memory state
    (its _module_cache, its own wyrm_modules search-path setup, etc.) is
    present here except what's passed in explicitly."""
    from wypoc.wyrm_eval_parse_tree import (
        EndSignal, ExitSignal, Module, Scope, SignalValue, Variable,
        eval_program, populate_globals, send_message,
    )

    wyrm_modules.set_script_root(os.path.dirname(os.path.abspath(file_path)))

    with open(file_path) as f:
        src = f.read()
    tree = parse(src)

    module_name = os.path.splitext(os.path.basename(file_path))[0]
    module_ctx = Scope()
    populate_globals(module_ctx, name=module_name)
    mod = Module(module_name, file_path, module_ctx, False, tree=tree)

    try:
        eval_program(tree, module_ctx)
    except EndSignal:
        return
    except ExitSignal as e:
        event_q.put(("exit", e.code))
        return
    except Exception as e:
        event_q.put(("error", type(e).__name__, str(e)))
        return

    # Every SignalValue the module's own top level bound (its `signal`
    # declarations - see wyrm_eval_parse_tree.py's ast.SignalDef case) gets
    # one extra subscriber: a plain Python closure forwarding any emit to
    # event_q. `emit`'s own eval loop already calls every subscriber via
    # call_value, which already falls back to a bare Python callable - so
    # this needs no interpreter changes at all, just appending to the
    # existing subscribers list the normal `connect` native would append
    # to.
    for name, var in dict.items(module_ctx):
        if not isinstance(name, str):
            continue  # message_table's own non-string sentinel key
        value = var.value if isinstance(var, Variable) else var
        if isinstance(value, SignalValue):
            value.subscribers.append(
                lambda *args, _name=name: event_q.put(("signal", _name, args))
            )

    while True:
        name, positional, kwargs = call_q.get()[1:]
        try:
            result = send_message(name, [mod], positional, kwargs, module_ctx)
        except EndSignal:
            event_q.put(("result", None))
            return
        except ExitSignal as e:
            event_q.put(("exit", e.code))
            return
        except Exception as e:
            event_q.put(("error", type(e).__name__, str(e)))
            continue
        event_q.put(("result", result))
