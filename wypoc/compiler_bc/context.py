"""Compile-time state: the module being built, and one frame per function.

`ModuleContext` owns the image and everything module-wide - the global slots
by name, the builtin namespace, and the pending function bodies waiting to be
laid out after module init.  `FnContext` owns one function's frame: its named
local slots, the expression temp stack above them, the emit buffer, its
labels, and the set of message identities its body references (the `u`
list the VM resolves at the function's first call, spec 6.2).

Register allocation is deliberately simple and deterministic (spec 8.3):
named locals take fixed L slots in declaration order, and expression
temporaries are a stack pushed above them.  `nlocals` is the high-water mark.
"""

from . import opcodes
from .errors import CompileError
from .image import ModuleImage

# Names the *host* seeds into an entry script's scope rather than
# populate_globals: the command line packs `__ARGS` (see cli.py and repl.py,
# which wyrm_sys.__argv answers from).  They are part of the runtime's global
# namespace, so a compiled module reaches them by name like any other
# builtin - `interpreter_names()` just cannot see them, since nothing has run.
HOST_GLOBALS = frozenset({"__ARGS"})


class Label:
    """A jump target, patched once its address is known (spec 2.2)."""

    __slots__ = ("target", "sites")

    def __init__(self):
        self.target = None  # word offset within the owning function's buffer
        self.sites = []  # (word offset of the instruction, slot to patch)


class FnContext:
    """One function's frame and emit buffer."""

    def __init__(self, module, name, params=(), captures=(), this_count=0,
                 is_module_init=False, this_class=None):
        self.module = module
        self.name = name
        # Module init is a function like any other, except that the names it
        # declares are module globals rather than frame locals (spec 7.2).
        self.is_module_init = is_module_init
        self.loops = []  # (continue label, break label) per enclosing loop
        self.max_results = 1  # the widest `return` in this body, the `r` field
        # Names whose register holds a one-element cell rather than the value
        # itself, because a nested scope both captures and assigns them
        # (spec 8.3).  Reads and writes go through the cell; a capture copies
        # the cell, so both frames see the same box.
        self.cells = set()
        self._zero = None  # register holding the constant 0 cell index
        self.code = []  # u32 words, offsets relative to this body
        self.uses = []  # reloc idxs this body references, in first-use order
        self.labels = []
        self.line = None  # source line the next instruction belongs to
        self.lines = {}  # word offset within this body -> source line
        self._emitted_line = None  # last line the table recorded
        self.locals = {}  # name -> L slot
        self.nnamed = 0  # named locals occupy L0 .. L(nnamed-1)
        self.top = 0  # next free temp slot
        self.high = 0  # nlocals high-water mark
        # P frame layout is fixed at compile time: this values, then declared
        # parameters, then captures (spec 1).
        self.params = {}
        self.captures = {}
        self.this_count = this_count
        # A message's receiver arrives in P0 (spec 1); a plain function has
        # no `this` at all.
        self.this_reg = opcodes.P(0) if this_count else None
        # The class this method belongs to, when it is one - what the
        # getslot/setslot check needs to know whose slots `this` has.
        self.this_class = this_class
        self.is_coroutine = False
        # `static` names in this body: module globals, not frame slots.
        self.statics = {}
        # Nested block scopes (spec: an inner declaration shadows an outer
        # name for the duration of its block). `block_slots` is filled by the
        # declaration pass, keyed by the statement list - or the `for` node -
        # the scope belongs to; `scopes` is the stack the emitter pushes as it
        # enters each one. Both hold `storage()`-shaped bindings.
        self.block_slots = {}
        self.scopes = []
        for offset, name_ in enumerate(params):
            self.params[name_] = opcodes.P(this_count + offset)
        for offset, name_ in enumerate(captures):
            self.captures[name_] = opcodes.P(this_count + len(params) + offset)

    # -- named locals -----------------------------------------------------

    def declare(self, name, pos=None):
        """Introduce a name in this scope: a module global at the top level,
        a frame local anywhere else."""
        if self.is_module_init:
            return self.module.declare_global(name)
        return self.declare_local(name, pos)

    def declare_block(self, name, pos=None):
        """Storage for a name declared inside a nested block: a slot of its
        own, so that it shadows an outer name of the same spelling for the
        block's duration rather than writing through to it."""
        if self.is_module_init:
            return ("global", self.module.declare_shadow_global(name))
        return ("reg", opcodes.L(self.reserve_local(name, pos)))

    def frame_storage(self, name, pos=None):
        """The frame-level binding for `name`, declared if it has none yet.

        The one case a block-scoped name falls back to: a captured-and-
        assigned variable lives in a cell, and there is exactly one cell per
        name per frame (spec 8.3), so it cannot also have a per-block slot.
        """
        if self.is_module_init:
            return ("global", self.module.declare_global(name))
        slot = self.locals.get(name)
        if slot is None:
            slot = self.declare_local(name, pos)
        return ("reg", opcodes.L(slot))

    def allocate_block_scopes(self, body, visible) -> None:
        """Give every nested block scope in `body` its own storage.

        Part of the declaration pass, not the emission: slots have to be
        allotted before the first instruction, because temps live directly
        above the named ones. So each scope's bindings are worked out here
        and parked in `block_slots`, and `push_block` installs them when the
        emitter reaches that scope.

        `visible` is what an enclosing scope already binds, which is only
        needed to tell a shadowing declaration from a plain one: a captured-
        and-assigned name lives in one cell per frame (spec 8.3) and so
        cannot take a per-block slot, which is fine while it shadows nothing
        and a refusal when it does - wrong bytes being the alternative.
        """
        from .analysis import block_scopes

        for owner, names, enclosing in block_scopes(body, frozenset(visible)):
            bindings = {}
            for name in names:
                if name in self.cells:
                    if name in enclosing:
                        raise CompileError(
                            f"{name!r} is declared in a nested block, shadows "
                            f"an enclosing {name!r}, and is captured by a "
                            f"closure: one cell per name per frame cannot "
                            f"hold both (spec 8.3)"
                        )
                    bindings[name] = self.frame_storage(name)
                else:
                    bindings[name] = self.declare_block(name)
            self.block_slots[id(owner)] = (owner, bindings)

    def push_block(self, owner) -> bool:
        """Enter the scope `owner` names, if it declared anything. Answers
        whether a scope was actually pushed, which is what pop_block needs."""
        entry = self.block_slots.get(id(owner))
        if entry is None:
            return False
        self.scopes.append(entry[1])
        return True

    def pop_block(self) -> None:
        self.scopes.pop()

    def block_binding(self, name):
        """`name`'s binding from the innermost block scope that has one."""
        for scope in reversed(self.scopes):
            binding = scope.get(name)
            if binding is not None:
                return binding
        return None

    def storage(self, name):
        """Where `name` lives: `("reg", reg)` for a local, parameter or
        capture, `("slot", symbol index)` for a slot of `this`, `("global",
        index)` for a module global, or None when it is not this module's.

        Inside a method a bare name may be a slot - `radius` means
        `this.radius` - and the interpreter seeds slots into the method's
        scope beneath its parameters, so that is where they sit here too:
        after locals and parameters, ahead of module globals.

        A class whose chain leaves this module has slots the compiler cannot
        enumerate, so inside *its* methods an otherwise-unresolvable name is
        taken to be one of them rather than refused - that is the only reading
        left, and refusing would make an inherited slot unusable.
        """
        binding = self.block_binding(name)
        if binding is not None:
            return binding
        reg = self.lookup(name)
        if reg is not None:
            return ("reg", reg)
        static = self.statics.get(name)
        if static is not None:
            return ("global", static)
        if self.this_class is not None:
            if self.module.is_own_slot(self.this_class, name):
                return ("slot", self.module.image.add_symbol(name))
            # A class static is a module global, and its bare name is visible
            # inside the class's own methods (spec 4.7).
            static = self.module.class_statics.get((self.this_class, name))
            if static is not None:
                return ("global", static)
        index = self.module.global_index(name)
        if index is not None:
            return ("global", index)
        if (
            self.this_class is not None
            and name not in self.module.builtins
            and self.module.chain_reaches_outside(self.this_class)
        ):
            return ("slot", self.module.image.add_symbol(name))
        return None

    def declare_local(self, name, pos=None) -> int:
        """Reserve an L slot for a named local and bind `name` to it.

        Every local is declared before any expression is compiled - the
        two-pass shape (collect, then emit) - because temps live directly
        above the named locals and a late declaration would land on one.
        """
        slot = self.reserve_local(name, pos)
        self.locals[name] = slot
        return slot

    def reserve_local(self, name, pos=None) -> int:
        """An L slot with no frame-level binding attached.

        What a nested block's declaration takes: it gets a slot of its own -
        shadowing never reuses the shadowed slot - but binding the name here
        would make the *outer* name resolve to it once the block is over,
        which is the write-through this whole mechanism exists to stop. The
        binding lives in the block's own scope instead (see push_block).
        """
        if self.top > self.nnamed:
            raise CompileError(
                f"internal: local {name!r} declared after temps were allocated", pos
            )
        slot = self.nnamed
        self.nnamed += 1
        self.top = self.nnamed
        self._raise_high()
        return slot

    # -- the temp stack ---------------------------------------------------

    def mark(self) -> int:
        """The current top of the temp stack, to release back to later."""
        return self.top

    def push(self) -> int:
        """Allocate the next temp slot and return it as an L register ref."""
        slot = self.top
        self.top += 1
        self._raise_high()
        return opcodes.L(slot)

    def free_to(self, mark: int) -> None:
        """Release temps down to `mark` (which a caller got from `mark()`)."""
        if mark > self.top:
            raise CompileError(f"internal: temp stack rewound forward in {self.name}")
        self.top = mark

    def _raise_high(self):
        if self.top > self.high:
            self.high = self.top
            if self.high > 32767:
                raise CompileError(
                    f"{self.name} needs {self.high} locals, over the 32767-local frame limit"
                )

    def reserve_zero(self) -> int:
        """Reserve the slot holding the constant 0 that indexes into cells.

        `getidx`/`setidx` take their index in a register, and every cell
        access uses the same one, so it is loaded once at function entry
        rather than before each access.
        """
        if self._zero is None:
            self._zero = opcodes.L(self.declare_local("<cell index>"))
        return self._zero

    @property
    def zero(self) -> int:
        if self._zero is None:
            raise CompileError(f"internal: {self.name} has no cell index register")
        return self._zero

    # -- name lookup ------------------------------------------------------

    def lookup(self, name):
        """The register holding `name` in this frame, or None if it is not one
        of its locals, parameters or captures.

        An enclosing block scope wins over the frame's own binding: that is
        what makes an inner declaration shadow rather than overwrite.
        """
        binding = self.block_binding(name)
        if binding is not None:
            return binding[1] if binding[0] == "reg" else None
        slot = self.locals.get(name)
        if slot is not None:
            return opcodes.L(slot)
        reg = self.params.get(name)
        if reg is not None:
            return reg
        return self.captures.get(name)

    # -- emitting ---------------------------------------------------------

    def here(self) -> int:
        return len(self.code)

    def emit(self, words) -> int:
        offset = len(self.code)
        # One entry per run of instructions on the same line, not per
        # instruction: the table is a lookup, not a log.
        if self.line is not None and self.line != self._emitted_line:
            self.lines[offset] = self.line
            self._emitted_line = self.line
        self.code.extend(words)
        return offset

    def emit_return(self, reg, count=1) -> None:
        """Emit `return`, keeping the value window in the L frame.

        `return` reads its values from consecutive L slots (spec 3.1), so a
        single value that happens to live in a parameter or capture register
        is copied down into a temp first.
        """
        if count == 1 and opcodes.is_p(reg):
            out = self.push()
            self.emit(opcodes.pack_pairable("move", out, reg))
            reg = out
        self.emit(opcodes.pack("return", a0=reg, f=count))

    def reference(self, path, pos=None) -> int:
        """Add (or reuse) a `messages` entry and record it in this scope's
        use set.

        Only messages reach here. Every *name* a body reads is a global slot
        (doc/addendum.md), and the import instructions carry their own path,
        so what used to be "every external reference passes through here" is
        now "every message send does".
        """
        index = self.module.image.add_message(path)
        if index not in self.uses:
            self.uses.append(index)
        return index

    # -- labels -----------------------------------------------------------

    def new_label(self) -> Label:
        label = Label()
        self.labels.append(label)
        return label

    def mark_label(self, label: Label) -> None:
        if label.target is not None:
            raise CompileError(f"internal: label marked twice in {self.name}")
        label.target = len(self.code)

    def patch_labels(self) -> None:
        """Resolve every jump site once the whole body is emitted.

        Offsets are signed word counts relative to the address of the next
        instruction (spec 2.2), so the site records where its own instruction
        ends.
        """
        for label in self.labels:
            if label.target is None:
                raise CompileError(f"internal: unmarked label in {self.name}")
            for after, word_index, half in label.sites:
                delta = label.target - after
                if half == "w1":
                    self.code[word_index] = delta & 0xFFFFFFFF
                    continue
                if not opcodes.fits_i16(delta):
                    raise CompileError(
                        f"jump in {self.name} spans {delta} words, over the "
                        "±32K reach of a compact jump"
                    )
                if half == "a0":
                    self.code[word_index] = (self.code[word_index] & 0x0000FFFF) | (
                        (delta & 0xFFFF) << 16
                    )
                else:  # "a2": the low half of the second word
                    self.code[word_index] = (self.code[word_index] & 0xFFFF0000) | (
                        delta & 0xFFFF
                    )

    def emit_jump(self, name, label: Label, cond=None) -> None:
        """Emit a jump to `label`, recording the site for later patching.

        The offset is not known yet, so the encoding has to be chosen now:
        the compact form is used, and a body that outgrows its ±32K reach is
        a CompileError at patch time rather than a silently wrong jump.
        """
        words = (
            opcodes.pack_pairable(name, 0)
            if cond is None
            else opcodes.pack_pairable(name, cond, 0)
        )
        offset = self.emit(words)
        self.patch_site(label, offset + len(words), offset, "a0")

    def patch_site(self, label: Label, after: int, word_index: int, half: str) -> None:
        """Record a jump offset to fill in once `label`'s address is known."""
        label.sites.append((after, word_index, half))


class ModuleContext:
    """The module being compiled: its image, globals, and pending functions."""

    def __init__(self, module_name: str, source_file=None):
        self.image = ModuleImage(module_name)
        self.source_file = source_file
        self.globals = {}  # name -> global slot index
        self.classes = {}  # class name -> class table index, for this module
        self.class_statics = {}  # (class name, static name) -> global index
        self.slot_names = {}  # class name -> its own slot names, in order
        self.superclasses = {}  # class name -> base class name, or None
        # spec 7.1: `getslot`/`setslot` is an optimization the compiler may
        # only make when the receiver's whole inheritance chain is
        # module-local.  The check is implemented; emitting it is off until a
        # VM exists to run it against.
        self.slot_optimization = False
        self.static_imports = set()  # names bound by `import static`
        # (global index, initializer, position) for every `static` a body
        # declared, run in init where its owner is created (spec 7.2).
        self.pending_statics = []
        self.has_wildcard_import = False
        self.emit_debug = True
        # Every fn/co/class this module defines, by name - what `foo::$ast`
        # reads.  Built before lowering, from the tree decorators left behind.
        self.definitions = {}
        # Whether a function body that will not lower becomes a trapping stub
        # rather than failing the whole module (see functions.compile_callable).
        self.stub_unlowered = True
        self.init = FnContext(self, "<init>", is_module_init=True)
        self.pending = []  # (function index, FnContext) laid out after init
        self._builtins = None

    # -- module globals ---------------------------------------------------

    def declare_global(self, name: str) -> int:
        index = self.globals.get(name)
        if index is None:
            index = self.image.add_global(name)
            self.globals[name] = index
        return index

    def declare_shadow_global(self, name: str) -> int:
        """A global slot for a top-level *block*'s declaration.

        It is storage, not a module member: the name it shadows is the one
        `mod::name` and the exports table answer with, and the block's own
        binding disappears with the block. So the slot is anonymous - never
        interned by name, never exported (see image.add_shadow_global).
        """
        return self.image.add_shadow_global(name)

    def declare_free_global(self, name: str) -> int:
        """A slot for a name this module references but does not define.

        Reached only after `storage` has failed and `_declare_module_names`
        has already claimed a slot for everything the module does define, so
        arriving here means the name has to come from somewhere outside - a
        wildcard import, an explicit one, or the builtins. Which of those it
        was is decided when the slot is filled, not here (doc/addendum.md).
        """
        return self.image.add_free_global(name)

    def name_slot(self, name: str) -> int:
        """The global slot a bare name reads, whoever defines it.

        This module's own if it declares one - `_declare_module_names` has
        already claimed a slot for everything the top level introduces - and a
        free slot otherwise. It is the same question `resolve_name` asks for an
        expression, asked from a table that holds a slot index rather than
        emitting an instruction: a class's superclass and a method's dispatch
        types are operands of the `classes` and `functions` entries, not of any
        opcode.
        """
        index = self.globals.get(name)
        if index is not None:
            return index
        return self.image.add_free_global(name)

    def global_index(self, name):
        return self.globals.get(name)

    def is_local_class(self, name) -> bool:
        return name in self.classes

    def is_own_slot(self, class_name, attr) -> bool:
        """True when `attr` is a slot declared by `class_name` or by one of
        its module-local ancestors."""
        name = class_name
        while name in self.slot_names:
            if attr in self.slot_names[name]:
                return True
            name = self.superclasses.get(name)
        return False

    def chain_reaches_outside(self, class_name) -> bool:
        """True when some class in `class_name`'s chain is not this module's,
        so the compiler cannot know the full slot set."""
        name = class_name
        while name is not None:
            if name not in self.slot_names:
                return True
            name = self.superclasses.get(name)
        return False

    def slot_index(self, class_name, attr):
        """The fixed slot number of `attr` on `class_name`, or None.

        None means the optimization does not apply: some class in the chain
        comes from another module, so its slot layout is that module's to
        decide at run time and no offset here would survive (spec 7.1).
        Slots are numbered base-first, which is the order an instance lays
        them out.
        """
        chain = []
        name = class_name
        while name is not None:
            if name not in self.slot_names:
                return None
            chain.append(name)
            name = self.superclasses.get(name)
        index = 0
        for owner in reversed(chain):
            for slot in self.slot_names[owner]:
                if slot == attr:
                    return index
                index += 1
        return None

    def class_static(self, parts):
        """The module global holding `Class::name`, when `Class` is one of
        this module's own (spec 4.7)."""
        if len(parts) != 2:
            return None
        return self.class_statics.get((parts[0], parts[1]))

    # -- the builtin namespace --------------------------------------------

    @property
    def builtins(self) -> frozenset:
        """The names resolution falls through to (spec 6.2 step 2).

        Asked of the interpreter rather than kept as a second list here, so a
        builtin added to `wyrm_builtins` or `corelib/prelude.wy` needs no
        change in this package - the same reasoning `completion.py` uses, and
        the same cached helper.
        """
        if self._builtins is None:
            from wypoc.completion import interpreter_names

            names, _messages = interpreter_names()
            self._builtins = frozenset(names) | HOST_GLOBALS
        return self._builtins

    # -- functions ---------------------------------------------------------

    def add_pending(self, index: int, fn: FnContext) -> None:
        self.pending.append((index, fn))

    def emit_return(self, reg, count=1) -> None:
        """Emit `return`, keeping the value window in the L frame.

        `return` reads its values from consecutive L slots (spec 3.1), so a
        single value that happens to live in a parameter or capture register
        is copied down into a temp first.
        """
        if count == 1 and opcodes.is_p(reg):
            out = self.push()
            self.emit(opcodes.pack_pairable("move", out, reg))
            reg = out
        self.emit(opcodes.pack("return", a0=reg, f=count))

    def reference(self, path, pos=None) -> int:
        """A module-init-level message reference."""
        return self.init.reference(path, pos)


__all__ = ["FnContext", "Label", "ModuleContext"]
