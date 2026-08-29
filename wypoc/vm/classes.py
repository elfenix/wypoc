"""Realising a `classes` entry into a class the runtime can dispatch on
(doc/wyc-format.md §8.6).

The class this builds is an ordinary `wyrm_eval_parse_tree.Class` - the same
object an interpreted `class C:` produces - with its slots and message map
filled in from the image rather than from a syntax tree. That is what makes
the two kinds of class interchangeable: an interpreted class can inherit from
a compiled one, dispatch sees a single table, and `instantiate` needs to know
nothing about where the class came from.

Two pieces carry the whole difference:

* a slot default is a *value* out of the static pool, not an expression, so it
  is wrapped in a `ReadyValue` (which is why that type exists in the runtime);
* a method body is bytecode, so it is registered as a `NativeBody` over a
  `BytecodeMethod` - the same door builtin per-value methods already use.
"""

from wypoc import ast_nodes as ast
from wypoc import wyrm_eval_parse_tree as ev

from .errors import LinkError
from .values import BytecodeMethod


def realize(module, index) -> ev.Class:
    """Build (once) the class `classes[index]` names, and register it."""
    existing = module.classes[index]
    if existing is not None:
        return existing

    entry = module.image.classes[index]
    cls = ev.Class(
        entry.name,
        # An empty body: everything a `ClassDef` node would have told the
        # runtime is below, out of the image instead.
        ast.ClassDef(entry.name, [], []),
        module.scope,
        _bases(module, entry),
    )
    for slot in entry.slots:
        cls.slots[slot.name] = _slot(module, slot)
    module.classes[index] = cls

    # Methods register as messages against this class, exactly as an
    # in-class `fn` does when the tree walker evaluates a ClassDef: the
    # message namespace is the module's, and dispatch is by receiver class.
    if entry.init is not None:
        _register(module, cls, "init", entry.init)
    for symbol, function in entry.messages:
        _register(module, cls, module.symbols[symbol], function)

    # A class static is a module global (§8.6) - module-lifetime storage the
    # class merely names. The module records which slot, so `Class::name` can
    # resolve through the class object to it.
    for name, glob in entry.statics:
        module.class_statics[(cls, name)] = glob
    return cls


def _bases(module, entry) -> list:
    """The superclass, which is a global slot even for a class next door
    (§8.6) - a name is a name, and this module's own classes have slots like
    anything else it defines. Read at the moment the class is realised, which
    is what makes a base defined further down the same file work."""
    if entry.superclass is None:
        return []
    base = ev.unwrap(module.globals[entry.superclass])
    if not isinstance(base, ev.Class):
        raise LinkError(
            f"{module.name}: superclass {module.slot_name(entry.superclass)} of "
            f"{entry.name} is not a class (got {base!r})"
        )
    return [base]


def _slot(module, slot) -> ast.SlotDef:
    """One slot as the `SlotDef` shape the runtime expects.

    A virtual slot's accessors (`g`/`s`) are recorded but not wired: this
    POC's evaluator does not implement slot getter/setter options either
    (see wypoc/README.md's known gaps), and inventing behaviour here would
    make a compiled class behave differently from the interpreted one it is
    supposed to be indistinguishable from.
    """
    default = None
    if slot.default is not None:
        default = ev.ReadyValue(module.statics[slot.default])
    return ast.SlotDef(slot.name, None, default, None)


def _register(module, cls, name, function) -> None:
    method = module.message(name)
    method.add_overload((cls,), ev.NativeBody(BytecodeMethod(module, function)), {})
