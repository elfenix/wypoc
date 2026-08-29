"""Class compilation (spec 7.3): the `classes` section and its methods.

A `class C(Base):` becomes one entry in the classes table - its slots with
their constant defaults, its virtual slots' accessor functions, its `init`,
its message map, and the module globals its statics were allotted - plus one
compiled function per method.  Module init realizes it with `class`, which
registers the message map, and stores the class object in the global named
after it.

Methods are messages: they carry the message flag, dispatch on `[C]`, and
receive the instance in P0.  Slot access inside them stays symbolic
(`getattr`/`setattr` on `this`) because an external superclass makes absolute
slot offsets unknowable at compile time; `getslot`/`setslot` is available as
an optimization only when the whole inheritance chain is module-local (§7.1),
and ships behind a flag.
"""

from wypoc import ast_nodes as ast

from .errors import CompileError
from .functions import compile_callable
from .handlers import construct_name
from .image import FN_COROUTINE, FN_MESSAGE, Slot

MESSAGE_MAP_LIMIT = 16  # spec 1.1: fixed-size scan/cache on an MCU


def compile_class(node, module) -> int:
    """Compile a class definition, returning its index in the classes table."""
    if len(node.bases) > 1:
        raise CompileError(
            f"class {node.name} has {len(node.bases)} bases; the image format "
            "records one superclass",
            node.pos,
        )
    superclass = _superclass_slot(node, module)

    # The dispatch relocation is created on first use, not up front: a class
    # with no methods names nothing, and an unused entry in the module's
    # referenced-name set would have the VM resolve - and report failures for -
    # a name nothing mentions.
    dispatch = _LazyDispatch(node, module)

    # Recorded before the body is walked, so that a method compiled here can
    # already ask whether `this`'s slot layout is this module's to know
    # (spec 7.1) - the answer must not depend on how far the walk has got.
    module.classes[node.name] = len(module.image.classes)
    module.superclasses[node.name] = _superclass_name(node)
    module.slot_names[node.name] = [
        member.name for member in node.body if isinstance(member, ast.SlotDef)
    ]

    slots = []
    messages = []
    statics = []
    init = None

    for member in node.body:
        if isinstance(member, ast.SlotDef):
            slots.append(_slot(member, node, module, dispatch))
        elif isinstance(member, (ast.FnDef, ast.CoDef)):
            index = _method(member, node, module, dispatch)
            if member.name == "init":
                init = index
            else:
                symbol = module.image.add_symbol(member.name)
                messages.append((symbol, index))
        elif isinstance(member, ast.StaticDecl):
            statics.append(_static(member, node, module))
        elif isinstance(member, ast.Pass):
            continue
        else:
            raise CompileError(
                f"the bytecode compiler does not support {construct_name(member)} "
                f"in a class body yet",
                getattr(member, "pos", node.pos),
            )

    if len(messages) > MESSAGE_MAP_LIMIT:
        raise CompileError(
            f"class {node.name} defines {len(messages)} methods, over the "
            f"{MESSAGE_MAP_LIMIT}-entry message map limit",
            node.pos,
        )

    index = module.image.add_class(
        node.name,
        superclass=superclass,
        slots=slots,
        init=init,
        messages=messages,
        statics=statics,
    )
    module.classes[node.name] = index
    return index


class _LazyDispatch(list):
    """The `[C]` a method dispatches on, materialized the first time one asks."""

    def __init__(self, node, module):
        super().__init__()
        self._node = node
        self._module = module

    def resolve(self):
        if not self:
            self.append(self._module.name_slot(self._node.name))
        return list(self)


def _superclass_name(node):
    if not node.bases:
        return None
    base = node.bases[0]
    return base.id if isinstance(base, ast.Name) else None


def _superclass_slot(node, module):
    """A superclass is named, never resolved at compile time - the base may
    live in another module whose interface at run time is what counts
    (spec 6.3). It is a global slot like any other name: this module's own if
    it declares the class, and a free slot filled by whatever import supplies
    it otherwise."""
    if not node.bases:
        return None
    base = node.bases[0]
    if not isinstance(base, ast.Name):
        raise CompileError(
            f"class {node.name}: a base class must be a name", node.pos
        )
    return module.name_slot(base.id)


def _slot(member, node, module, dispatch):
    """One slot, with its constant default and any virtual accessors."""
    default = None
    if member.default is not None:
        default = _constant(member.default, module, member)
    getter = setter = None
    for option in member.options or []:
        accessor = _accessor(option, member, node, module, dispatch)
        if option.kind == "getter":
            getter = accessor
        else:
            setter = accessor
    return Slot(member.name, default=default, getter=getter, setter=setter)


def _accessor(option, member, node, module, dispatch):
    """A virtual slot's getter/setter, compiled as a method on this class.

    `getter = undefined` means the slot has no accessor of that kind, which
    the format says by leaving the key out (D7 - there are no sentinels).

    Otherwise only a literal function body can become one: the format records
    a function *index*, so a value computed at run time has nowhere to live.
    """
    if option.value == "undefined":
        return None
    if not isinstance(option.value, ast.Lambda):
        raise CompileError(
            f"slot {member.name}: a {option.kind} must be written as a "
            "function literal",
            option.pos,
        )
    return compile_callable(
        module,
        f"{node.name}.{member.name}.{option.kind}",
        option.value.params,
        option.value.body,
        option.pos,
        flags=FN_MESSAGE,
        dispatch=dispatch.resolve(),
        this_class=node.name,
    )[0]


def _method(member, node, module, dispatch):
    if member.class_target:
        raise CompileError(
            f"method {member.name} may not also name a class target", member.pos
        )
    flags = FN_MESSAGE
    if isinstance(member, ast.CoDef):
        # A `co` method is an ordinary message carrying the coroutine flag;
        # calling it constructs the coroutine (spec 7.2).
        flags |= FN_COROUTINE
    index, _captures = compile_callable(
        module,
        f"{member.name}!",  # the "!" suffix marks a message (spec 4.6)
        member.params,
        member.body,
        member.pos,
        flags=flags,
        dispatch=dispatch.resolve(),
        this_class=node.name,
    )
    return index


def _static(member, node, module):
    """A class static is a module global: it is module-lifetime, and
    `Class::name` addressing resolves through the class object to the
    recorded slot (spec 4.7)."""
    key = f"{node.name}::{member.name}"
    global_index = module.declare_global(key)
    module.class_statics[(node.name, member.name)] = global_index
    return (member.name, global_index)


def _constant(node, module, member):
    """The static-pool index of a constant initializer.

    Non-constant slot defaults are a v1 refusal: evaluating them at
    class-body time can be added later as init code, without a format change
    (spec 7.3).
    """
    from .expressions import NOT_CONSTANT, constant_value

    value = constant_value(node)
    if value is NOT_CONSTANT:
        raise CompileError(
            f"slot {member.name}: a default must be a constant",
            getattr(node, "pos", member.pos),
        )
    return module.image.add_static(value)
