"""D-Bus export for wyrm objects, backed by the `dbus-fast` package (an
optional dependency - see pyproject.toml's `dbus` extra). Meant to be driven
through corelib/std/dbus.wy's thin wrapper, the same way wyrm_io.py backs
corelib/std/io.wy: this module only ever deals in plain Python/wyrm values
and the handful of `__dbus_*` primitives populate_globals installs; the nice
names (`dbus::register_class`, ...) live in the .wy layer.

Workflow (see corelib/std/dbus.wy and wypoc/cli.py's --dbus-session):

    wyrm --dbus-session script.wy      # connects to the session bus before
                                        # script.wy's own code runs
    dbus::register_class("com.example.Counter", Counter)   # cls -> iface name
    obj := Counter()
    dbus::register_object("/com/example/Counter", obj)     # exports it
    dbus::run()                        # blocks, serving calls, until stopped

Only what doc/language-spec.md would call a class's own scalar state and its
own messages cross onto the bus - see _scalar_signature and _own_methods:

  * a slot whose *current* value is bool/int/float/str becomes a read/write
    D-Bus property, typed from that value (fixed at registration time - a
    POC simplification, not a live re-inspection on every access);
  * a message with exactly one overload whose receiver signature is this
    exact class (own or inherited, single-dispatch) becomes a D-Bus method.
    Its arguments and return value cross as D-Bus variants (`v`), so any
    wyrm value marshals without this module needing to know its type ahead
    of time - the tradeoff for not requiring `fn` parameters to carry D-Bus
    type annotations of their own;
  * a `signal` (own or inherited) becomes a D-Bus signal, and is wired up
    automatically at dbus::register_object time - see
    _make_dbus_signal_bridge: no separate "publish this signal" step is
    needed, an ordinary `emit name(...)` inside the wyrm class fires the
    D-Bus signal the same moment it calls any wyrm-side `connect`ed
    subscriber. Its arguments cross as variants, same as a method's.

A D-Bus method call runs the wyrm method to completion (via send_message,
the same call path `recv ! name(...)` itself uses) before replying - there
is no async/await anywhere in a wyrm method body, so "call it and wait for
the answer" is just an ordinary Python call from inside the event loop's own
callback, per dbus-fast's own sync-handler path for a non-coroutine method
(see dbus_fast.aio.message_bus.MessageBus._make_method_handler).
"""
import asyncio

from wypoc import ast_nodes as ast


class DbusError(Exception):
    """A dbus-fast/session-bus problem a wyrm script can catch the message
    of, rather than a raw traceback - e.g. no session bus available, a class
    used before it's registered, or dbus-fast not installed at all."""


_loop: "asyncio.AbstractEventLoop | None" = None
_bus = None  # dbus_fast.aio.MessageBus, once connected
_registered_classes: dict = {}  # wyrm Class -> D-Bus interface name


def _require_dbus_fast():
    try:
        import dbus_fast  # noqa: F401
    except ImportError as e:
        raise DbusError(
            "the 'dbus' extra isn't installed (pip install 'wypoc[dbus]', "
            "or: pip install dbus-fast)"
        ) from e
    return dbus_fast


def _require_loop() -> "asyncio.AbstractEventLoop":
    if _loop is None:
        raise DbusError("not connected to a bus yet - run with --dbus-session, "
                         "or call dbus::connect_session() first")
    return _loop


def _require_bus():
    if _bus is None:
        raise DbusError("not connected to a bus yet - run with --dbus-session, "
                         "or call dbus::connect_session() first")
    return _bus


def is_connected() -> bool:
    return _bus is not None


def connect_session() -> bool:
    """Connects to the session bus and blocks (via run_until_complete, not
    run_forever - this returns as soon as the connection handshake is done)
    until that's finished. Safe to call more than once; only the first call
    does anything. This is what `wyrm --dbus-session` calls before running
    the script (see cli.py) - also callable directly from wyrm code for
    workflows that don't want a whole process dedicated to one bus
    connection from the start."""
    global _loop, _bus
    if _bus is not None:
        return True
    dbus_fast = _require_dbus_fast()
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _connect():
        # MessageBus() itself calls asyncio.get_running_loop() (see
        # dbus_fast.aio.message_bus.MessageBus.__init__), so it has to be
        # constructed from inside the coroutine run_until_complete drives,
        # not handed to it already built.
        return await MessageBus(bus_type=BusType.SESSION).connect()

    try:
        bus = loop.run_until_complete(_connect())
    except Exception as e:
        raise DbusError(f"couldn't connect to the session bus: {e}") from e
    _loop = loop
    _bus = bus
    return True


def request_name(name: str) -> str:
    """Asks the bus to own `name` (a well-known bus name, e.g.
    "com.example.Counter") so clients can address this process by name
    instead of only by its unique (":1.42"-style) connection name. Answers
    the RequestNameReply's own name (e.g. "PRIMARY_OWNER")."""
    loop = _require_loop()
    bus = _require_bus()
    reply = loop.run_until_complete(bus.request_name(name))
    return reply.name


def register_class(interface_name: str, cls) -> None:
    """`dbus::register_class(interface_name, Cls)` - remembers that
    instances of `cls` should be exported under `interface_name` when
    dbus::register_object hands one to us. Doesn't touch the bus itself;
    nothing is exported (and no D-Bus type can be fixed - see the module
    docstring) until an actual instance exists."""
    from wypoc.wyrm_eval_parse_tree import Class

    if not isinstance(cls, Class):
        raise TypeError(f"dbus::register_class expects a class (got {type(cls).__name__})")
    if not interface_name or "." not in interface_name:
        raise ValueError(f"{interface_name!r} doesn't look like a D-Bus interface name "
                          "(expected dot-separated, e.g. 'com.example.Counter')")
    _registered_classes[cls] = interface_name


def _scalar_signature(value) -> "str | None":
    """The D-Bus type of `value` if it's one of the scalar types a slot may
    cross as a property (see the module docstring) - bool checked ahead of
    int since Python's bool is an int subclass."""
    if isinstance(value, bool):
        return "b"
    if isinstance(value, int):
        return "x"  # int64 - wyrm ints aren't fixed-width, so this is the roomiest fit
    if isinstance(value, float):
        return "d"
    if isinstance(value, str):
        return "s"
    return None


def _all_methods(cls) -> dict:
    """(name -> FnDef) for every method `cls` answers as a single-receiver
    message, own methods first (so a subclass's own override, walked last,
    wins the dict-key collision) - mirrors Class.all_slots's own
    base-classes-first, own-class-last layering."""
    result = {}
    for base in cls.bases:
        result.update(_all_methods(base))
    result.update(cls.methods)
    return result


def _method_arity(node: "ast.FnDef") -> int:
    return sum(1 for p in node.params if isinstance(p, ast.Param))


def _wyrm_value_to_variant(dbus_fast, value):
    from wypoc import wyrm_builtins

    sig = _scalar_signature(value)
    if sig is not None:
        return dbus_fast.Variant(sig, value)
    if value is None or value is wyrm_builtins.NIL:
        return dbus_fast.Variant("s", "nil")
    return dbus_fast.Variant("s", str(value))


def _variant_to_wyrm_value(value):
    from dbus_fast import Variant

    return value.value if isinstance(value, Variant) else value


def _build_service_interface(interface_name: str, instance, ctx: dict):
    """Builds (and returns, not yet exported) one dbus_fast ServiceInterface
    instance for `instance` - a fresh Python subclass of ServiceInterface
    per call, since dbus_fast decorators (dbus_method/dbus_property) fix a
    D-Bus type signature at class-definition time and this class's own
    property set depends on which of *this* instance's slots currently hold
    a scalar value (see the module docstring's "fixed at registration
    time")."""
    dbus_fast = _require_dbus_fast()
    from dbus_fast.service import ServiceInterface, dbus_method, dbus_property, dbus_signal
    from wypoc.wyrm_eval_parse_tree import SignalValue, send_message, unwrap

    cls = instance.cls
    namespace = {}
    used_names = set()

    def _claim(name, what):
        if name in used_names:
            raise DbusError(f"class {cls.name!r}: {what} {name!r} collides with another "
                             "property/method/signal of the same name")
        used_names.add(name)

    def _make_property(slot_name, sig):
        # A closure, not a default-arg trick: dbus_property's getter check
        # (_Property.__init__) requires exactly one parameter ("self") on
        # the function it decorates, so slot_name has to be captured by the
        # closure rather than an extra (even defaulted) parameter.
        def getter(self):
            return unwrap(self._wyrm_instance.attrs[slot_name])
        getter.__name__ = slot_name
        getter.__annotations__ = {"return": sig}
        prop = dbus_property()(getter)

        def setter(self, value):
            self._wyrm_instance.attrs[slot_name].value = value
        setter.__name__ = slot_name
        setter.__annotations__ = {"value": sig}
        return prop.setter(setter)

    def _make_signal(signal_name, arity):
        # Args and return both cross as variants, same tradeoff as a
        # method's own arguments (see the module docstring) - the return
        # annotation is what fixes the signal's own D-Bus signature
        # (dbus_fast.service._Signal reads it, not the params), so a 0-arg
        # signal gets none at all (empty signature) rather than `-> ''`.
        # A signal argument crosses as a variant exactly like a method's
        # does (see _wyrm_value_to_variant) - unlike a method's own return,
        # the marshaller reads this straight from what `_s` returns rather
        # than through a wrapping call of ours, so each value has to
        # already be a Variant by the time it gets there.
        params = ", ".join(f"v{i}: 'v'" for i in range(arity))
        if arity == 0:
            src = "def _s(self):\n    return None\n"
        elif arity == 1:
            src = f"def _s(self, {params}) -> 'v':\n    return _wrap(v0)\n"
        else:
            args = ", ".join(f"_wrap(v{i})" for i in range(arity))
            src = f"def _s(self, {params}) -> '{'v' * arity}':\n    return ({args},)\n"
        signal_ns = {}
        signal_globals = {"_wrap": lambda v: _wyrm_value_to_variant(dbus_fast, v)}
        exec(src, signal_globals, signal_ns)  # noqa: S102 - fixed-arity wrapper, no external input
        fn = signal_ns["_s"]
        return dbus_signal(name=signal_name)(fn)

    for slot_name in cls.all_slots():
        current = unwrap(instance.attrs[slot_name])
        sig = _scalar_signature(current)
        if sig is None:
            continue  # not one of the property-eligible scalar types - skip it
        _claim(slot_name, "slot")
        namespace[slot_name] = _make_property(slot_name, sig)

    for name, node in _all_methods(cls).items():
        if name.startswith("__"):
            continue  # dunder hooks (init, sexpr, ...) aren't user-facing messages
        _claim(name, "message")
        arity = _method_arity(node)
        params = "".join(f", p{i}: 'v'" for i in range(arity))
        args = ", ".join(f"p{i}" for i in range(arity))
        src = f"def _m(self{params}) -> 'v':\n    return self._dbus_call({name!r}, [{args}])\n"
        method_ns = {}
        exec(src, {}, method_ns)  # noqa: S102 - building a fixed-arity wrapper, no external input
        fn = method_ns["_m"]
        namespace[name] = dbus_method(name=name)(fn)

    for signal_name, signal_def in cls.all_signals().items():
        _claim(signal_name, "signal")
        namespace[signal_name] = _make_signal(signal_name, len(signal_def.params))

    def _dbus_call(self, name, args):
        positional = [_variant_to_wyrm_value(a) for a in args]
        result = send_message(name, [self._wyrm_instance], positional, {}, self._ctx)
        return _wyrm_value_to_variant(dbus_fast, result)

    def __init__(self, iface_name, wyrm_instance, wyrm_ctx):
        self._wyrm_instance = wyrm_instance
        self._ctx = wyrm_ctx
        ServiceInterface.__init__(self, iface_name)

    namespace["_dbus_call"] = _dbus_call
    namespace["__init__"] = __init__

    iface_cls = type(f"Wyrm_{cls.name}_Interface", (ServiceInterface,), namespace)
    iface = iface_cls(interface_name, instance, ctx)

    # Wire each signal's wyrm-side subscriber list to this interface's own
    # dbus_signal member, so an `emit` inside a wyrm method (this instance's
    # own, or any native/dbus-dispatched one - it's the same SignalValue
    # either way) also fires the D-Bus signal, with no extra step required
    # in the wyrm source itself (a script can still `connect` its own wyrm
    # callbacks to the same signal for local reactions).
    for signal_name in cls.all_signals():
        sig = unwrap(instance.attrs[signal_name])
        if isinstance(sig, SignalValue):
            sig.subscribers.append(_make_dbus_signal_bridge(iface, signal_name))

    return iface


def _make_dbus_signal_bridge(iface, signal_name: str):
    """A wyrm-callable (see call_value's plain-callable fallback) that
    forwards an `emit` straight into `iface`'s own dbus_signal-decorated
    member of the same name (calling it runs the member's body *and*
    notifies the bus - see dbus_fast.service.dbus_signal's `wrapped`).
    Positional-only: `emit`'s kwarg form isn't given a D-Bus counterpart
    (see ast_nodes.Emit/eval_stmt's Emit case - kwargs are legal syntax
    there but have nowhere sensible to go here)."""

    def bridge(*args, **kwargs):
        getattr(iface, signal_name)(*args)

    return bridge


def register_object(ctx: dict, path: str, instance) -> None:
    """`dbus::register_object(path, instance)` - exports `instance` (a
    ClassInstance of a class already passed to dbus::register_class) on the
    session bus at `path`. `ctx` is the calling scope (see ContextualBuiltin
    in wyrm_eval_parse_tree.py): a method call is dispatched back through
    *this* scope's message table, so it resolves the same overloads
    `instance ! name(...)` would from the same call site."""
    from wypoc.wyrm_eval_parse_tree import ClassInstance

    bus = _require_bus()
    if not isinstance(instance, ClassInstance):
        raise TypeError(f"dbus::register_object expects a class instance "
                         f"(got {type(instance).__name__})")
    interface_name = _registered_classes.get(instance.cls)
    if interface_name is None:
        raise DbusError(f"class {instance.cls.name!r} was never passed to "
                         "dbus::register_class")
    iface = _build_service_interface(interface_name, instance, ctx)
    bus.export(path, iface)


def run() -> None:
    """`dbus::run()` - the blocking event loop: serves incoming D-Bus calls
    (each one, per the module docstring, run to completion synchronously)
    until dbus::stop() is called from a method handler, or the process gets
    Ctrl-C'd."""
    loop = _require_loop()
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass


def stop() -> None:
    """`dbus::stop()` - ends a dbus::run() that's currently blocked, e.g.
    called from within a "quit" method's own wyrm body. call_soon_threadsafe
    rather than a bare loop.stop() so this is safe even though it's usually
    invoked from inside a callback the loop itself is already running."""
    loop = _require_loop()
    loop.call_soon_threadsafe(loop.stop)


def install(ctx: dict) -> None:
    """Seeds `ctx` with the `__dbus_*` primitives corelib/std/dbus.wy wraps
    - called from populate_globals exactly like wyrm_io.install/
    wyrm_builtins.install, so every module sees them regardless of whether
    dbus-fast is installed at all (see _require_dbus_fast: only actually
    *using* one of these needs the extra, importing wypoc doesn't)."""
    from wypoc.wyrm_eval_parse_tree import ContextualBuiltin, expose_all

    expose_all(
        ctx,
        __dbus_connect_session=connect_session,
        __dbus_connected=is_connected,
        __dbus_request_name=request_name,
        __dbus_register_class=register_class,
        __dbus_register_object=ContextualBuiltin(register_object, "__dbus_register_object"),
        __dbus_run=run,
        __dbus_stop=stop,
    )
