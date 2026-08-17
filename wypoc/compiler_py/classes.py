"""ClassDef -> `@dataclass` + constructor-wrapper + message-table
registration codegen.

A wyrm class compiles to two separate top-level Python names (see the
implementation plan's "Classes" section for the full rationale): an
internal `@dataclass class _wy_<Name>_fields` (never referenced by user
code - constructing a class is a *call*, and `wy_<Name>` can't be both the
dataclass type and a distinct async constructor function of the same
name), and `async def wy_<Name>(...)`, the actual constructor user code
calls, which zero-fills the dataclass, resolves+calls an `init` overload
if one is registered (short-circuiting on error, matching the
interpreter's RAII-flavored construction contract), and returns the
instance.

Single inheritance only (matches the language spec's documented intent);
`__wy_bases__` is set explicitly as a ClassVar tuple for message-dispatch
distance ranking, kept separate from Python's own dataclass-subclassing
MRO (which this module *does* use, for field inheritance only - see
naming.fields_class_name).

Class-body methods (an internal `fn area()`/`fn init()` inside the class
body) and external `fn [Cls] name(...)` methods compile through the same
path, `functions.compile_method` - there is no real Python method on the
dataclass; every message overload is a standalone function registered into
the module's `_TABLE`.
"""
from wypoc import ast_nodes as ast

from .coroutines import compile_co_method
from .errors import err
from .expressions import compile_expr
from .functions import compile_method
from .naming import fields_class_name, message_fn_name, py_ident


def _base_fields_name(modctx, classdef):
    if not classdef.bases:
        return None
    if len(classdef.bases) > 1:
        err(f"class '{classdef.name}': --compile-py only supports single "
            "inheritance", classdef)
    base = classdef.bases[0]
    if not isinstance(base, ast.Name):
        err(f"class '{classdef.name}': base class must be a plain name", classdef)
    if base.id not in modctx.classes:
        err(f"class '{classdef.name}': unknown base class '{base.id}' "
            "(base classes must be defined earlier in the same module)", classdef)
    return modctx.classes[base.id]


# A slot's implicit default when it has none declared - bool -> False,
# int/uint -> 0, float -> 0.0, everything else -> None/nil. Matches the
# interpreter's _zero_value exactly (see wyrm_eval_parse_tree.py).
_ZERO_VALUES = {"bool": "False", "int": "0", "uint": "0", "float": "0.0"}


def _zero_value(slot: ast.SlotDef) -> str:
    if slot.type is not None and len(slot.type.parts) == 1:
        return _ZERO_VALUES.get(slot.type.parts[0], "None")
    return "None"


def _default_expr(modctx, slot: ast.SlotDef) -> str:
    if slot.default is None:
        return _zero_value(slot)
    from .context import FnCtx
    fnctx = FnCtx(modctx=modctx)
    value = compile_expr(fnctx, slot.default)
    if fnctx.lines:
        # A default needing statements of its own (a hoisted Catch/Try/
        # Message call) can't be spelled as a single Python expression
        # inside a dataclass field default or a one-line factory lambda -
        # not needed by any current fixture, so it's a clean stopping
        # point rather than emitting something subtly wrong.
        err(f"slot '{slot.name}': this default is too complex for "
            "--compile-py yet (needs multiple statements to evaluate)", slot)
    if isinstance(slot.default, (ast.Call, ast.Message, ast.MessageTupleExpr)):
        # Evaluated at class-construction time, not Python class-definition
        # (module-import) time - e.g. `slot id: int = next_id()`.
        return f"field(default_factory=lambda: ({value}))"
    return value


def compile_class(modctx, classdef: ast.ClassDef) -> str:
    base_info = _base_fields_name(modctx, classdef)
    fields_name = fields_class_name(classdef.name)
    ctor_name = py_ident(classdef.name)

    from .module import _is_macro_only

    slots = [m for m in classdef.body if isinstance(m, ast.SlotDef)]
    # A `$`-named (or AstRef-using) class-body method is a macro-template,
    # not a real message - see module.py's _is_macro_only/_walk_toplevel
    # for the same skip at module scope.
    methods = [m for m in classdef.body
               if isinstance(m, (ast.FnDef, ast.CoDef)) and not _is_macro_only(m)]
    unsupported = [m for m in classdef.body
                   if not isinstance(m, (ast.SlotDef, ast.FnDef, ast.CoDef, ast.Pass))]
    if unsupported:
        err(f"class '{classdef.name}': unsupported class-body member", unsupported[0])

    own_slot_names = {s.name for s in slots}
    base_slot_names = base_info[3] if base_info else set()
    all_slot_names = base_slot_names | own_slot_names

    lines = []
    base_py = base_info[0] if base_info else None
    lines.append(f"@dataclass")
    lines.append(f"class {fields_name}({base_py}):" if base_py else f"class {fields_name}:")
    bases_tuple = f"({base_py},)" if base_py else "()"
    lines.append(f"    __wy_bases__: ClassVar[tuple] = {bases_tuple}")
    if not slots:
        lines.append("    pass")
    for slot in slots:
        lines.append(f"    {py_ident(slot.name)}: Any = {_default_expr(modctx, slot)}")

    registrations = []
    for m in methods:
        if m.class_target is not None:
            err(f"class '{classdef.name}': a class-body fn/co may not also "
                "declare [Cls] targets", m)
        py_name = message_fn_name(m.name, [classdef.name])
        compiler = compile_co_method if isinstance(m, ast.CoDef) else compile_method
        lines.append("")
        lines.append(compiler(modctx, m, py_name, slot_names=all_slot_names))
        registrations.append((m.name, (fields_name,), py_name))

    modctx.classes[classdef.name] = (fields_name, ctor_name, base_py, all_slot_names)
    modctx.messages.extend(registrations)

    lines.append("")
    lines.append(_compile_ctor(ctor_name, fields_name))
    return "\n".join(lines)


def _compile_ctor(ctor_name: str, fields_name: str) -> str:
    return "\n".join([
        f"async def {ctor_name}(*args, **kwargs):",
        f"    _wy_inst = {fields_name}()",
        f'    _wy_ov = engine.try_resolve_overload(_TABLE, "init", [_wy_inst])',
        f"    if _wy_ov is not None:",
        f"        _wy_result = await _wy_ov.fn(_wy_inst, *args, **kwargs)",
        f"        if is_error(_wy_result):",
        f"            return _wy_result",
        f"    elif args or kwargs:",
        f'        return WyrmError("{ctor_name}(...) takes no arguments")',
        f"    return _wy_inst",
    ])


def _external_registration(modctx, node, compiler):
    """`fn [Cls, ...] name(...)` / `co [Cls, ...] name(...)` at module top
    level - compiles the same way as a class-body method/coroutine, but
    for one or more class targets (a tuple receiver for multi-dispatch)
    and with slot-name aliasing only when there's exactly one target (see
    functions.compile_method/coroutines.compile_co_method)."""
    class_targets = node.class_target
    fields_names = []
    for cname in class_targets:
        if cname not in modctx.classes:
            err(f"[{cname}] '{node.name}': unknown class '{cname}'", node)
        fields_names.append(modctx.classes[cname][0])
    py_name = message_fn_name(node.name, class_targets)
    slot_names = modctx.classes[class_targets[0]][3] if len(class_targets) == 1 else None
    source = compiler(modctx, node, py_name, slot_names=slot_names)
    modctx.messages.append((node.name, tuple(fields_names), py_name))
    return source


def external_method_registrations(modctx, fndef: ast.FnDef):
    return _external_registration(modctx, fndef, compile_method)


def external_co_registrations(modctx, codef: ast.CoDef):
    return _external_registration(modctx, codef, compile_co_method)
