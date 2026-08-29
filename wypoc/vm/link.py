"""Filling names, and resolving message identities: doc/wyc-format.md §7.2, §7.3.

Two jobs, and neither is a relocation table any more.

Most of this file **fills free global slots** - the names a module references
but does not define (§8.11). Three layers do the filling, each knowing its own
layer rather than depending on when it runs: the builtins at load, an `import`
as it runs, an `import_star` as it runs. `LoadedModule.fill` arbitrates.

The rest resolves a **message identity**, which is the one thing left with a
table and a bind slot behind it. A message resolves in a namespace of its own -
a module may have an unrelated `describe` variable and `describe` message - and
an unqualified one *creates* the identity when nothing has it yet, which is how
a module's own messages come into being before `reg_msg` populates them.

A name that resolves to nothing is not an error here and no longer produces a
value either: the slot simply stays Unset, and reading it faults the way any
declared-but-unassigned variable does.
"""

from wypoc import wyrm_builtins
from wypoc import wyrm_eval_parse_tree as ev

MISSING = _MISSING = object()


def resolve(module, entry):
    """The message identity one `messages` entry names (§7.3)."""
    return _message(module, [module.symbols[i] for i in entry.path])


def _message(module, path):
    """A message, which resolves in the *message* namespace.

    Messages and variables are separate namespaces in wyrm - a module may have
    both a `describe` variable and a `describe` message, and they are
    unrelated - so a message entry never walks the variable ladder §7.3
    describes for everything else. A qualified `mod::name` asks that module's
    own table (§7.3's "message identity across modules"); an unqualified one
    asks this module's, *creating* the identity if this is the first anyone
    has heard of it, which is how a module's own messages come into being
    before `reg_msg` or a class's message map fills them.
    """
    if len(path) == 1:
        return module.message(path[0])
    owner = _first_component(module, path[0])
    for name in path[1:-1]:
        if owner is _MISSING:
            break
        owner = _scope_lookup(owner, name)
    owner = ev.unwrap(owner) if owner is not _MISSING else owner
    ctx = getattr(owner, "ctx", None) if owner is not _MISSING else None
    if ctx is None:
        return not_found(module, "::".join(path))
    found = ev.message_table(ctx).get(path[-1])
    return found if found is not None else not_found(module, "::".join(path))


def not_found(module, spelling):
    """§7.3: failure binds an error value rather than aborting anything."""
    return wyrm_builtins.error(f"undefined name {spelling!r} in {module.name}")


def _first_component(module, name):
    """The first component of a path, through the three layers
    doc/addendum.md orders: this module's own definitions and named imports,
    then wildcard imports, then builtins.

    Wildcards ahead of builtins is the correction. §7.3 had them the other way
    round, which meant a wildcard import could not supply a name the prelude
    already defined - the import silently did nothing rather than shadowing,
    and only for the subset of names that happen to be builtins. Layer 2 beats
    layer 3 now, so what a wildcard supplies is what the module sees.

    An import lands in the global named after it (§6.2's hoisted import
    sequence `import` + `gset`), so this module's *globals* are its import and
    alias table - there is no second place for one to live.
    """
    cell = module.cell(name)
    if cell is not None:
        # The cell, not its value: this module's own globals are written by
        # init, and a name may well be resolved before the instruction that
        # fills it has run (a class is the everyday case - §8.6 makes even a
        # local superclass a global slot). Binding the place rather than the
        # contents is what makes that correct without resolving twice.
        return cell

    from_wildcards = _from_wildcards(module, name)
    if from_wildcards is not _MISSING:
        return from_wildcards

    found = module.scope.get(name)
    if found is not None:
        return found

    return _MISSING


def _from_wildcards(module, name):
    """Layer 2: the namespaces `import_star` registered.

    Every one of them is consulted, not just up to the first hit. Returning
    early was what made "first registered wins" the VM's answer while the tree
    walker's copy-as-you-go made it "last registered wins" - two different
    answers to a question neither engine was actually deciding. Ambiguity is
    now the answer (doc/addendum.md), so the whole layer has to be searched
    before it is known.

    Two wildcards reaching the *same* object is not a collision - that is one
    canonical thing seen twice, the case `_merge_wildcard_name` treats as a
    no-op on the walker side.
    """
    found = _MISSING
    source = None
    for namespace, excepts in module.wildcards:
        if name in excepts:
            continue
        offered = ev.wildcard_exports(namespace)
        if offered is not None and name not in offered:
            # Layer 3 reached through a layer 2 namespace. Every module's scope
            # holds the builtins and the prelude, so without this every wildcard
            # import supplies all of them - and two of them collide over every
            # builtin there is. See ev.wildcard_exports.
            continue
        value = _scope_lookup(namespace, name)
        if value is _MISSING:
            continue
        if found is _MISSING:
            found, source = value, _namespace_name(namespace)
            continue
        if ev.unwrap(value) is ev.unwrap(found):
            continue
        return wyrm_builtins.error(
            f"ambiguous name {name!r}: supplied by both '{source}::*' and "
            f"'{_namespace_name(namespace)}::*'; disambiguate with an explicit "
            "import, or qualify at the use site"
        )
    return found


def _namespace_name(namespace) -> str:
    return getattr(namespace, "name", None) or "<wildcard>"


# --------------------------------------------------------------------------
# filling free names (doc/addendum.md)
#
# A name the module references but does not define is a global slot that
# starts Unset. Three things fill those slots, and each one knows its own
# layer, so nothing has to run in a particular order to get the precedence
# right - see LoadedModule.fill.


def fill_from_builtins(module) -> None:
    """Layer 3, at load, before init runs.

    Only bare names: a qualified path cannot be a builtin, and the import that
    makes it reachable has not run yet.
    """
    for name, slot in module.free.items():
        if "::" in name:
            continue
        found = module.scope.get(name)
        if found is not None:
            module.fill(slot, ev.unwrap(found), module.LAYER_BUILTIN, "<builtins>")


def fill_from_import(module, path, value) -> None:
    """Layer 1, as each `import` runs.

    Fills the slot named by the import path itself, and every slot naming a
    path *through* it - `import foo` is what makes `foo::bar::baz` reachable,
    so it is what binds that slot too (doc/addendum.md). The remainder of the
    path is walked with the same `::` step `getscope` uses.
    """
    spelling = "::".join(path)
    prefix = spelling + "::"
    for name, slot in module.free.items():
        if name == spelling:
            module.fill(slot, ev.unwrap(value), module.LAYER_EXPLICIT, spelling)
            continue
        if not name.startswith(prefix):
            continue
        reached = value
        for step in name[len(prefix):].split("::"):
            reached = _scope_lookup(reached, step)
            if reached is _MISSING:
                break
        if reached is not _MISSING:
            module.fill(slot, ev.unwrap(reached), module.LAYER_EXPLICIT, spelling)


def fill_from_wildcard(module, namespace, excepts, source) -> None:
    """Layer 2, as each `import_star` runs.

    Offers only what the target module declared - never the builtins and
    prelude its scope also holds, which every scope has of its own and which
    would otherwise make every wildcard supply every builtin (see
    ev.wildcard_exports).
    """
    offered = ev.wildcard_exports(namespace)
    for name, slot in module.free.items():
        if "::" in name or name in excepts:
            continue
        if offered is not None and name not in offered:
            continue
        value = _scope_lookup(namespace, name)
        if value is not _MISSING:
            module.fill(slot, ev.unwrap(value), module.LAYER_WILDCARD, source)


def scope_member(module, value, name):
    """`value::name` at execution time - `getscope`/`setscope` (§7.3).

    Answers the *binding* where there is one, so `setscope` can write through
    it, and `MISSING` when the namespace has no such member. A class is a
    namespace too: its statics live in module global slots (§8.6), and the
    class object is what names them.
    """
    value = ev.unwrap(value)
    if isinstance(value, ev.Class):
        slot = module.class_statics.get((value, name))
        if slot is not None:
            return _GlobalSlot(module, slot)
    return _scope_lookup(value, name)


class _GlobalSlot(ev.Variable):
    """A class static, as the binding `getscope`/`setscope` expect.

    `Variable.__init__` is deliberately not called - see module.GlobalCell,
    which does the same thing for a module's own globals.
    """

    __slots__ = ("module", "slot")

    def __init__(self, module, slot):
        self.module = module
        self.slot = slot
        self.immutable = False

    @property
    def value(self):
        return self.module.globals[self.slot]

    @value.setter
    def value(self, new):
        self.module.globals[self.slot] = new


def _scope_lookup(value, name):
    """One `::` step through whatever the previous component produced."""
    from .module import LoadedModule

    value = ev.unwrap(value)
    if isinstance(value, LoadedModule):
        return value.get(name, _MISSING)
    if isinstance(value, ev.Module):
        if name in value.submodules:
            return value.submodules[name]
        found = value.ctx.get(name)
        return _MISSING if found is None else found
    return _MISSING
