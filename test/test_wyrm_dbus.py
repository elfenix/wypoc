"""wypoc/wyrm_dbus.py: the D-Bus export layer behind corelib/std/dbus.wy.

Two tiers:

  * pure unit tests against the introspection helpers (_scalar_signature,
    _all_methods, _method_arity) and the validation register_class/
    register_object do before ever touching a bus - no session bus needed,
    dbus-fast still needs to be importable (it's in pyproject.toml's `dev`
    extra, same as pytest itself);
  * one end-to-end test that actually runs `wyrm --dbus-session` as a
    subprocess and drives it over the real session bus with `busctl` - the
    same tool this feature was hand-verified with. Skipped if there's no
    session bus, no busctl, or dbus-fast isn't installed, since none of
    those are things this test suite should require.
"""
import os
import shutil
import subprocess
import time
import uuid

import pytest

from conftest import REPO_ROOT
from wypoc.parse import parse
from wypoc.wyrm_eval_parse_tree import Scope, eval_program, lookup, populate_globals

dbus_fast = pytest.importorskip("dbus_fast", reason="the 'dbus' extra isn't installed")

from wypoc import wyrm_dbus  # noqa: E402


def _class_from(src: str, name: str):
    ctx = Scope()
    populate_globals(ctx)
    eval_program(parse(src), ctx)
    return lookup(name, ctx), ctx


def test_scalar_signature_covers_bool_int_float_str_only():
    assert wyrm_dbus._scalar_signature(True) == "b"
    assert wyrm_dbus._scalar_signature(3) == "x"
    assert wyrm_dbus._scalar_signature(3.5) == "d"
    assert wyrm_dbus._scalar_signature("hi") == "s"
    assert wyrm_dbus._scalar_signature([1, 2]) is None
    assert wyrm_dbus._scalar_signature(None) is None


def test_register_class_requires_a_class():
    with pytest.raises(TypeError):
        wyrm_dbus.register_class("com.example.X", object())


def test_register_class_requires_a_dotted_interface_name():
    cls, _ = _class_from("class Foo:\n    slot x: int = 0\n", "Foo")
    with pytest.raises(ValueError):
        wyrm_dbus.register_class("NotDotted", cls)


def test_register_object_before_connect_raises_dbus_error():
    cls, ctx = _class_from("class Foo:\n    slot x: int = 0\n", "Foo")
    wyrm_dbus.register_class("com.example.Foo", cls)
    wyrm_dbus._bus = None  # in case an earlier test in this process connected
    inst = lookup("Foo", ctx)  # not actually a ClassInstance, but never gets that far
    with pytest.raises(wyrm_dbus.DbusError):
        wyrm_dbus.register_object(ctx, "/foo", inst)


def test_all_methods_includes_inherited_and_own():
    cls, _ = _class_from(
        "class Base:\n"
        "    fn greet() -> str:\n"
        "        return \"hi\"\n"
        "class Sub(Base):\n"
        "    fn bye() -> str:\n"
        "        return \"bye\"\n",
        "Sub",
    )
    methods = wyrm_dbus._all_methods(cls)
    assert set(methods) == {"greet", "bye"}


def test_all_methods_own_override_wins():
    cls, _ = _class_from(
        "class Base:\n"
        "    fn greet() -> str:\n"
        "        return \"base\"\n"
        "class Sub(Base):\n"
        "    fn greet() -> str:\n"
        "        return \"sub\"\n",
        "Sub",
    )
    methods = wyrm_dbus._all_methods(cls)
    assert methods["greet"] is cls.methods["greet"]


def test_method_arity_counts_plain_params_only():
    cls, _ = _class_from(
        "class Foo:\n"
        "    fn two(a, b):\n"
        "        pass\n"
        "    fn none():\n"
        "        pass\n",
        "Foo",
    )
    assert wyrm_dbus._method_arity(cls.methods["two"]) == 2
    assert wyrm_dbus._method_arity(cls.methods["none"]) == 0


def _instance_from(src: str, instance_name: str):
    ctx = Scope()
    populate_globals(ctx)
    eval_program(parse(src), ctx)
    return lookup(instance_name, ctx), ctx


def test_build_service_interface_exposes_a_dbus_signal_member():
    instance, ctx = _instance_from(
        "class Counter:\n"
        "    slot count: int = 0\n"
        "    signal changed(old: int, new: int)\n"
        "obj := Counter()\n",
        "obj",
    )
    wyrm_dbus.register_class("com.example.Counter", instance.cls)
    iface = wyrm_dbus._build_service_interface("com.example.Counter", instance, ctx)
    member = type(iface).__dict__["changed"]
    assert "__DBUS_SIGNAL" in member.__dict__
    assert member.__dict__["__DBUS_SIGNAL"].signature == "vv"


def test_build_service_interface_rejects_name_collision():
    # A slot and a message of the same name are legal wyrm (they occupy
    # separate namespaces - see message_table's docstring), but this
    # module's dynamic ServiceInterface subclass has to hang both off one
    # Python attribute name, which is where the collision actually is.
    instance, ctx = _instance_from(
        "class Bad:\n"
        "    slot count: int = 0\n"
        "    fn count() -> int:\n"
        "        return this.count\n"
        "obj := Bad()\n",
        "obj",
    )
    wyrm_dbus.register_class("com.example.Bad", instance.cls)
    with pytest.raises(wyrm_dbus.DbusError, match="collides"):
        wyrm_dbus._build_service_interface("com.example.Bad", instance, ctx)


def test_class_with_a_slot_and_signal_of_the_same_name_is_rejected():
    with pytest.raises(TypeError, match="both a slot and a signal"):
        _instance_from(
            "class Bad:\n"
            "    slot changed: int = 0\n"
            "    signal changed()\n"
            "obj := Bad()\n",
            "obj",
        )


def test_emit_reaches_a_dbus_signal_bridge_without_a_real_bus():
    """The bridge connect happens at _build_service_interface time (see
    _make_dbus_signal_bridge), independent of an actual bus connection -
    exercised directly here so this doesn't need a session bus."""
    instance, ctx = _instance_from(
        "class Counter:\n"
        "    signal changed(v: int)\n"
        "    fn bump(v):\n"
        "        emit changed(v)\n"
        "obj := Counter()\n",
        "obj",
    )
    wyrm_dbus.register_class("com.example.Counter", instance.cls)
    iface = wyrm_dbus._build_service_interface("com.example.Counter", instance, ctx)

    seen = []
    orig = type(iface).__dict__["changed"]
    # Swap in a plain recorder in place of the real dbus_signal member so
    # this doesn't need ServiceInterface._get_buses to have anything in it.
    type(iface).changed = lambda self, v: seen.append(v)
    try:
        from wypoc.wyrm_eval_parse_tree import send_message
        send_message("bump", [instance], [9], {}, ctx)
    finally:
        type(iface).changed = orig
    assert seen == [9]


# --- end-to-end, over the real session bus -------------------------------

_VENV_WYRM = os.path.join(REPO_ROOT, ".venv", "bin", "wyrm")
WYRM = _VENV_WYRM if os.path.isfile(_VENV_WYRM) else shutil.which("wyrm")
BUSCTL = shutil.which("busctl")

pytestmark_e2e = pytest.mark.skipif(
    not WYRM or not os.path.isfile(WYRM) or not BUSCTL
    or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
    reason="needs the wyrm console script, busctl, and a session bus",
)


def _busctl(*args):
    return subprocess.run(["busctl", "--user", *args], capture_output=True,
                           text=True, timeout=10)


def _wait_for_name(name: str, timeout: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _busctl("status", name).returncode == 0:
            return
        time.sleep(0.1)
    raise TimeoutError(f"{name!r} never appeared on the session bus")


@pytestmark_e2e
def test_dbus_server_end_to_end(tmp_path):
    iface = f"org.wypoc.test{uuid.uuid4().hex}"
    script = tmp_path / "server.wy"
    script.write_text(f'''
import std::dbus

class Counter:
    slot count: int = 0
    slot label: str = "hi"

    fn increment(by) -> int:
        this.count = this.count + by
        return this.count

dbus::register_class("{iface}", Counter)
c := Counter()
dbus::register_object("/obj", c)
dbus::request_name("{iface}")
dbus::run()
''')
    env = {**os.environ, "WYRM_PATH": ""}
    proc = subprocess.Popen([WYRM, "--dbus-session", str(script)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_for_name(iface)

        r = _busctl("call", iface, "/obj", iface, "increment", "v", "x", "7")
        assert r.returncode == 0, r.stdout
        assert "7" in r.stdout

        r = _busctl("get-property", iface, "/obj", iface, "count")
        assert r.returncode == 0 and "7" in r.stdout

        r = _busctl("set-property", iface, "/obj", iface, "label", "s", "changed")
        assert r.returncode == 0

        r = _busctl("get-property", iface, "/obj", iface, "label")
        assert r.returncode == 0 and "changed" in r.stdout
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytestmark_e2e
def test_dbus_signal_end_to_end(tmp_path):
    """An `emit` inside the registered class's own method fires the D-Bus
    signal wired up by dbus::register_object (see wyrm_dbus.py's
    _make_dbus_signal_bridge) - caught here with `busctl monitor`, the same
    way the feature was hand-verified against a real session bus."""
    iface = f"org.wypoc.test{uuid.uuid4().hex}"
    script = tmp_path / "signal_server.wy"
    script.write_text(f'''
import std::dbus

class Counter:
    slot count: int = 0
    signal changed(old: int, new: int)

    fn increment(by) -> int:
        old := this.count
        this.count = this.count + by
        emit changed(old, this.count)
        return this.count

dbus::register_class("{iface}", Counter)
c := Counter()
dbus::register_object("/obj", c)
dbus::request_name("{iface}")
dbus::run()
''')
    env = {**os.environ, "WYRM_PATH": ""}
    proc = subprocess.Popen([WYRM, "--dbus-session", str(script)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    monitor = None
    try:
        _wait_for_name(iface)
        monitor = subprocess.Popen(["busctl", "--user", "monitor", iface],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(0.5)

        r = _busctl("call", iface, "/obj", iface, "increment", "v", "x", "4")
        assert r.returncode == 0, r.stdout
        time.sleep(0.5)

        monitor.terminate()
        try:
            out, _ = monitor.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            monitor.kill()
            out, _ = monitor.communicate()
        assert "Member=changed" in out, out
        assert 'INT64 0' in out and 'INT64 4' in out, out
    finally:
        if monitor is not None and monitor.poll() is None:
            monitor.kill()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
