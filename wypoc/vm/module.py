"""A loaded image bound to a runtime: global slots, the bind table, and the
façade that lets the tree walker treat it as an ordinary module.

This is load step 1-5's tail (doc/wyc-format.md §7.1): allocate the globals,
apply their constant defaults, allocate the bind table. Running init is the
interpreter's job, not this module's.

The façade is the interop seam. A compiled module's globals are a flat slot
array; an interpreted one's are a `Scope` of `Variable` cells keyed by name.
`exports` (§8.10) is what bridges them - it maps each name to its slot, so a
`GlobalCell` can present one slot as the `Variable`-shaped thing the walker
reads and writes. Both sides then see the same storage, and neither has to
know which kind of module it is talking to.
"""

from wypoc import wyrm_eval_parse_tree as ev

from .errors import LinkError

AmbiguousName = ev.AmbiguousName

UNBOUND = object()  # a bind-table slot nothing has resolved yet


class GlobalCell(ev.Variable):
    """One global slot, wearing the shape the tree walker expects.

    A `Variable` subclass, not merely something with a `.value`: the walker
    decides what is a binding with `isinstance(value, Variable)` - in
    `unwrap`, and in `bind`, which writes through an existing one. Anything
    else would read back as the cell itself rather than the value inside it.

    `value` is a property over the slot array rather than an attribute, so an
    interpreted module reads and writes a compiled module's globals in place
    and sees the compiled code's own writes. Neither side copies, and neither
    has to know which kind of module it is talking to.

    `Variable.__init__` is deliberately not called: it would assign through
    the property and overwrite the slot with None.
    """

    __slots__ = ("slots", "index")

    def __init__(self, slots, index):
        self.slots = slots
        self.index = index
        self.immutable = False

    @property
    def value(self):
        return self.slots[self.index]

    @value.setter
    def value(self, new):
        self.slots[self.index] = new

    def __repr__(self):
        return f"GlobalCell({self.index}, {self.value!r})"


class LoadedModule:
    """A `.wyc` bound to a runtime and ready for its init routine to run."""

    def __init__(self, image, scope=None, path=None):
        self.image = image
        self.name = image.name
        self.path = path

        # Every global starts Unset, then slot_defaults are applied
        # (spec 7.1 steps 1-2).
        self.globals = [ev.UNSET] * image.nglobals
        for slot, value in image.slot_defaults.items():
            self.globals[slot] = value

        # One bind slot per message identity, all unbound (step 4).
        self.bindings = [UNBOUND] * len(image.messages)

        # Namespaces registered by `import_star`, in registration order, each
        # with the except-list of the import that registered it (spec 7.3
        # step 3). The instruction that appends to this arrives with imports.
        self.wildcards = []

        # The fill list: name -> global slot, for names this module references
        # but does not define (§8.11), and what filled each of those slots.
        #
        # `fill_layer` is the whole precedence mechanism (doc/addendum.md).
        # Layers are ordered by strength, 1 strongest, and a fill only happens
        # when it is at least as strong as what is already there - so the
        # answer does not depend on the order the imports appear in. Ordering
        # imports instead would have made `import a::*` before `import a::x`
        # mean something different from the reverse, which is precisely the
        # accidental, positional behaviour this replaces.
        self.free = dict(image.free)
        self.fill_layer = {}
        # The slot numbers of the free names, for the read guard. A module's
        # *own* global may legitimately be Unset when it is read - `static x:
        # int` followed by `x ?= 0` probes exactly that - so only a slot that
        # was supposed to be filled from outside is a fault when it is empty.
        self.free_slots = frozenset(image.free.values())

        # Symbols are interned as the strings the runtime already uses for
        # names; the pool is the index space instructions address them by.
        self.symbols = list(image.symbols)
        self.statics = list(image.statics)

        # The module's own namespace, seeded with builtins so the tree walker
        # can run inside it and so `resolve` has somewhere to look.
        self.scope = ev.Scope() if scope is None else scope

        # Realized objects, filled in as init runs.
        self.functions = [None] * len(image.functions)
        self.classes = [None] * len(image.classes)

        # (class, name) -> module global slot, for the class statics an image
        # records (spec 8.6): module-lifetime storage a class merely names.
        self.class_statics = {}

        self._exports = dict(image.exports)
        self._cells = {}
        self._module = None  # the ev.Module façade, built once (see as_module)

    # -- the interop façade -------------------------------------------------

    def cell(self, name):
        """The `Variable`-shaped view of an exported global, or None."""
        slot = self._exports.get(name)
        if slot is None:
            return None
        cell = self._cells.get(name)
        if cell is None:
            cell = self._cells[name] = GlobalCell(self.globals, slot)
        return cell

    def get(self, name, default=None):
        """`::` lookup by name (spec 7.3), and what `getscope` reads.

        Module globals first - they are what this module defines - then
        whatever else is in its scope, which is where builtins and imported
        names live.
        """
        cell = self.cell(name)
        if cell is not None:
            return cell
        return self.scope.get(name, default)

    def __contains__(self, name):
        return name in self._exports or name in self.scope

    def as_module(self):
        """An `ev.Module` sharing this module's storage.

        The walker's `import_module` hands back one of these, and `::`
        resolution and `getscope` go through its `ctx`. Handing it a mapping
        backed by our slots is what makes a compiled module importable by
        interpreted source.

        Built once and kept: a module has one identity, and code that compares
        or caches module objects must not see two of them.
        """
        if self._module is None:
            self._module = ev.Module(
                self.name, self.path or "<image>", _Namespace(self),
                is_package=False, tree=None,
            )
        return self._module

    def namespace(self) -> dict:
        """The module's globals as a plain `name -> value` dict - what a
        caller of `run_image` gets back, and the compiled counterpart of the
        `ctx` an interpreted module leaves behind."""
        return {name: self.globals[slot] for name, slot in self._exports.items()}

    def message(self, name):
        """The message identity `name` denotes here, created if this is the
        first anyone has heard of it (spec 7.3).

        The table is the runtime's own - `message_table` over this module's
        scope - so a message a compiled class defines and one an interpreted
        module defines are the same kind of thing in the same namespace.
        """
        messages = ev.message_table(self.scope)
        method = messages.get(name)
        if not isinstance(method, ev.Method):
            method = messages[name] = ev.Method(name, owner=self.name)
        return method

    def export_names(self):
        return sorted(self._exports)

    # -- filling free names --------------------------------------------------

    LAYER_EXPLICIT = 1   # a local definition, or a named/aliased import
    LAYER_WILDCARD = 2   # `import mod::*`
    LAYER_BUILTIN = 3    # the builtins and the prelude

    def fill(self, slot, value, layer, source=None) -> bool:
        """Put `value` in a free slot on behalf of `layer`.

        Answers whether the slot now holds this layer's value. A stronger
        layer always wins; a weaker one never displaces a stronger one. Two
        *different* sources in the same layer with different values is the
        ambiguity doc/addendum.md makes an error at the point of use - the
        slot takes a marker instead of a winner, and reading it raises.
        """
        current = self.fill_layer.get(slot)
        if current is None or layer < current[0]:
            self.fill_layer[slot] = (layer, source)
            self.globals[slot] = value
            return True
        if layer > current[0]:
            return False
        held_layer, held_source = current
        if held_source == source or self.globals[slot] is value:
            return True
        if isinstance(self.globals[slot], AmbiguousName):
            return False
        name = self._slot_name(slot)
        self.globals[slot] = AmbiguousName(name, held_source, source)
        return False

    def global_fault(self, slot) -> str:
        """Why reading this global slot failed.

        Two ways it can: the name was ambiguous between wildcard imports, or
        nothing ever filled it. Both are "error at the point of use", and both
        say the same thing here that the tree walker says at its own lookup,
        so a program fails the same way whichever engine runs it.
        """
        value = self.globals[slot]
        if isinstance(value, AmbiguousName):
            return value.message()
        return f"variable {self.slot_name(slot)!r} is declared but has no value yet"

    def slot_name(self, slot):
        """How to spell a global slot in a diagnostic."""
        for name, index in self._exports.items():
            if index == slot:
                return name
        return self._slot_name(slot)

    def _slot_name(self, slot):
        for name, index in self.free.items():
            if index == slot:
                return name
        return f"g{slot}"

    # -- message identities ---------------------------------------------------

    def bound(self, index):
        """The message identity entry `index` names, for `msg`/`getmsg`/`reg_msg`.

        Only messages reach this now. Every *variable* a module references is a
        global slot filled by whatever supplies it (doc/addendum.md), so the
        relocation table is gone: section 7 holds message identities alone,
        and the import instructions carry their own paths as operands.

        Bound on first read rather than in a batch. A message name resolves in
        its own namespace and an unqualified one *creates* the identity when
        nothing has it yet, so there is no import for it to wait on and no
        ordering to get right - the first read is as good a moment as any, and
        it costs one check on a path that already does a dispatch.
        """
        value = self.bindings[index]
        if value is UNBOUND:
            from . import link

            value = link.resolve(self, self.image.messages[index])
            self.bindings[index] = value
        return value

    def bind(self, index, value):
        self.bindings[index] = value

    def is_bound(self, index):
        return self.bindings[index] is not UNBOUND

    def __repr__(self):
        return f"LoadedModule({self.name!r}, {len(self.globals)} globals)"


class _Namespace(dict):
    """A dict view over a LoadedModule: its exported globals, then its scope.

    `dict` rather than a mapping so it can stand in wherever the walker types
    a module namespace as one; the lookups it actually performs go through
    `get`/`__contains__`, which are overridden to consult the slots.
    """

    def __init__(self, module):
        super().__init__()
        self._module = module

    def get(self, name, default=None):
        return self._module.get(name, default)

    def __getitem__(self, name):
        found = self._module.get(name, _MISSING)
        if found is _MISSING:
            raise KeyError(name)
        return found

    def __contains__(self, name):
        return name in self._module

    def __setitem__(self, name, value):
        cell = self._module.cell(name)
        if cell is not None and isinstance(value, ev.Variable):
            cell.value = value.value
            return
        self._module.scope[name] = value

    def keys(self):
        return list(self._module.export_names()) + list(self._module.scope.keys())

    def __iter__(self):
        return iter(self.keys())

    def __repr__(self):
        return f"<namespace of {self._module.name}>"


_MISSING = object()
