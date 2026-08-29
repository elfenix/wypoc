# Addendum: wypoc-specific extensions to the base design

The canonical language spec is `doc/language-spec.md` in the `wyrm` reference
implementation, and changes to *that* design belong there, not here. This
file tracks where wypoc's own implementation adds behavior the base spec
doesn't define, or resolves something the base spec leaves open, so the two
don't quietly drift apart without a record of why.

Each section: what wypoc adds, and why it isn't (yet, or ever) in the base
spec.

## Threading (`thread`)

`thread a::b::c` spawns module `a::b::c` in a fresh child **OS process**
(`multiprocessing`, "spawn" start method - not a real thread despite the
keyword), with no dynamic scope reuse: a fresh interpreter, fresh globals,
its own empty module cache. Calls into it (`remote ! name(...)`) are
synchronous over a pair of queues and block the calling side for the full
round trip; a `signal` the child emits is delivered as a proxy `SignalValue`
on the parent, drained opportunistically during the next blocking call (no
background thread drains it eagerly - a signal fired while the parent isn't
inside a blocking call isn't observed until the next one happens).

Only plain values (numbers, strings, lists/dicts, simple class instances)
may cross the queue boundary; anything holding a native resource (open
files, sockets) is not picklable and isn't allowed to appear in a remote
call's arguments or return value. Not enforced - crossing this line fails
with a raw `PicklingError` at the queue, the accepted POC-level failure
mode.

Implementation: `wypoc/wyrm_remote.py` (`RemoteModule`, `spawn_module_process`).

## Tasks (`task`)

`task expr` runs `expr` and evaluates to a `Future` - a placeholder for a
result that some other thread resolves later via `resolve_with`/
`fail_with`. `resolve(fut)` (a builtin) blocks the caller until the future
settles. In this implementation, the only thing that ever actually resolves
a `Future` asynchronously is a `remote ! name(...)` call made from inside a
`task` block; `task` isn't (yet) a general-purpose green-thread/promise
primitive elsewhere in the language.

Implementation: `wypoc/wyrm_eval_parse_tree.py` (`Future`, `_task_stack`,
`ast.TaskSpawn`'s eval case).

## Signals

A class (or a module, for the module-scope case) may declare
`signal name(...)`. Each instance gets its own `SignalValue` - a
subscriber list reached as an ordinary attribute (`obj.name`, or bare
`name`/`this.name` inside a method, same lookup path a slot gets) - seeded
alongside slots at construction time. `obj.name ! connect(cb)` /
`! disconnect(cb)` subscribe/unsubscribe; `emit name(...)` fires it. This
is a Qt-style signal/slot mechanism grafted onto the class system as "a
message on a value with no real class behind it" (the same trick `str`'s
`substr`/`list`'s `append` use), not something the base spec's class model
describes.

A `thread`-spawned module's signals are proxied transparently: the parent
gets its own local `SignalValue` per remote signal name, fed from
`("signal", name, args)` events on the wire (see Threading above).

Implementation: `wypoc/wyrm_eval_parse_tree.py` (`SignalValue`,
`ast.Emit`, `ast.SignalDef`), `wypoc/wyrm_remote.py` (`RemoteModule.signal`).

## Message identity across modules (design decided, not yet implemented)

**Status: agreed design, not yet built** - tracked so the intent survives
until the implementation lands.

Base spec problem: today, each module keeps its own `message_table`
(`name -> Method`), and only `import mod::*` touches it at all - it copies
entries across with a plain overwrite. A plain `import mod` / `import mod
as alias` binds a `Module` value but never makes that module's messages
reachable, qualified or not; two wildcard-imported modules that both extend
a same-named message silently clobber one another instead of merging. None
of this is namespacing on purpose - it's an import path that was never
built for messages, only for plain variables.

Decided replacement, modeled on Common Lisp's package/generic-function
split (symbols carry a home-package identity; `defmethod` always extends
whatever generic function a symbol's value currently is, never creates a
second one, because there is only ever one object per symbol):

- **A message name has one canonical owner.** The first `fn name(...)` /
  `fn [Cls] name(...)` to define a given name, anywhere, creates the
  `Method` object. It's owned by the module that defined it.
- **Importing brings in a reference, not a copy.** `import std::io::*`,
  and `import std::io as io` reached via `io::msg`, both make the *same*
  canonical `Method` object visible under a name - not a merged/copied
  table entry. Since it's the same object, extending it from an importing
  module and extending it from the owning module are the same operation.
- **Extend vs. create is decided by local visibility.** `fn [Cls] name(...)`
  extends the canonical message if `name` already resolves to one in the
  current module's visible scope (declared locally already, or imported).
  If `name` isn't visible yet, this `fn` creates a *new*, locally-owned
  message - even if some unrelated module has a same-named one that was
  never imported. Two libraries can each own an unrelated `draw` message.
- **Name collisions are a hard import-time error.** If two distinct
  canonical messages both resolve to the same unqualified name in one
  module's scope (e.g. two wildcard imports that collide), that name is
  unusable unqualified until disambiguated with `mod::name` at the call
  site - no last-import-wins, no silent merge.

`mod::name` as a message selector (`instance ! rnd::msg()`) is new syntax
needed to make the disambiguation case usable: it resolves `msg` directly
against `rnd`'s canonical message, bypassing whatever's (or isn't) visible
unqualified in the local scope.

## Import cycles are illegal

**Status: implemented.** This is a change to the base design rather than a
wypoc extension - it still belongs in `doc/language-spec.md` in the reference
implementation, and this section is the record until it lands there.

Implementation: `cli.check_imports` (the build-time walk), and
`wyrm_eval_parse_tree.check_import_cycle` over the one import stack both
engines share.

The base spec doesn't say what a cyclic import means, and the two engines
answered differently by accident. Both publish a module in the cache before
running its body (`wyrm_eval_parse_tree.py`'s "cache before eval so circular
imports don't infinite-loop", and §7.1 step 6 of `doc/wyc-format.md`), so a
module reached mid-cycle is observable in a half-initialized state: its
globals hold whatever its body has assigned so far, and which of them that is
depends on statement order in a file the reader may never have opened.

**The import graph must be acyclic.** A cycle is a diagnostic, reported
against the import statement that closes it and naming the whole path, in
Go's shape:

    import cycle not allowed:
        paint imports palette
        palette imports paint

This buys three things:

- **A total initialization order.** A module's body runs after every module
  it imports has finished its own, and exactly once. That is now a guarantee
  the language makes, not an emergent property of who imported whom first.
- **Every name is bindable at import time.** With no cycles there is no
  moment at which a dependency is visible but incomplete, so an importing
  module can bind everything it needs from a dependency the instant that
  dependency's body has run. This is what lets the bytecode VM drop deferred
  binding entirely (see below).
- **Publish-before-init changes job.** The mechanism stays, but it stops
  being a way to *survive* cycles and becomes the way to *detect* them: a
  module found in the cache in the initializing state proves a cycle, and
  raises rather than being handed back half-built.

The cost, which is Go's cost too: mutually recursive definitions cannot span
a module boundary. `class A(foo::B)` in one module and `class B(bar::A)` in
another is now a compile error. The remedies are Go's - merge the two
modules, or hoist the shared piece into a third that both import.

Cycle detection lives in the build driver, not in the bytecode compiler:
`compile_module` sees one parsed program and never the graph, while
`cli.check_imports` already walks every import transitively to resolve it to
a file. Detection is that walk with a visiting-stack instead of a flat
visited set, so `--check` and `--build-bc` share one implementation and the
tree walker enforces the same rule at `import` time.

## Wildcard ambiguity is an error at the point of use

**Status: implemented**, except that the diagnostic is raised when the name is
read rather than refused at compile time - see the note at the end of this
section. Resolves what `doc/wyc-format.md` §10 listed as undecided ("Wildcard
shadowing").

Implementation: `wyrm_eval_parse_tree._merge_wildcard_name` and `AmbiguousName`
for the tree walker; `vm/link.fill_from_wildcard` and `LoadedModule.fill` for
the bytecode VM.

The base spec permits `import a::*` and `import b::*` together but never says
what happens when both export `mix`. The two engines disagree, and neither
answer was chosen: the tree walker copies names into the importing scope as
it goes, so the **last** import wins; the VM walks a registered search list
and returns on the first hit, so the **first** one wins. Both are artifacts
of a data structure - assignment into a dict, versus early return from a loop
- rather than semantics anyone picked.

Neither is right. Silent shadowing by import order means adding an export to
a library can change the meaning of a downstream module that never mentioned
it. Every language with a wildcard import and a compiler that can see through
it - Java, C++, C#, Rust, Haskell - treats the collision as an error at the
point of use, and the outlier that picks a winner silently (Python's
`from m import *`, last wins) is the one its own ecosystem lints against.

**Precedence is layered.** A name resolves through, in order:

1. the module's own definitions, and its named or aliased imports,
2. wildcard imports,
3. builtins.

A collision *across* layers is not an error - the earlier layer wins, so an
explicit import always beats a wildcard and a local definition always beats
both. This is the escape hatch as well as the rule: an ambiguous wildcard
name is disambiguated by naming it explicitly, `import palette::mix`, which
lifts it into layer 1.

**A collision within layer 2 is a diagnostic**, naming both sources:

    ambiguous name 'mix': supplied by both 'palette::*' and 'pigment::*'
        disambiguate with an explicit import, or qualify at the use site

**The error is at use, not at import.** Two wildcard imports that overlap on
names the module never mentions are legal, and must be - otherwise
`import std::*` alongside any second wildcard becomes unusable the moment the
two share a single name. Only a name the module actually references can be
ambiguous.

This matches the rule already recorded above for messages ("Name collisions
are a hard import-time error"), so the two namespaces now fail the same way
for the same reason.

Because import cycles are illegal, the whole dependency graph is known before
any module in it is compiled, so this *could* be a compile-time diagnostic:
the compiler would know each dependency's export set and could refuse the
ambiguous program outright.

**It is not one yet.** `compile_module` takes a single parsed program and never
loads a dependency - there is no whole-program build for it to hang off - so
both engines detect the collision when the name is filled and report it when
the name is read. The observable rule is the one this section describes either
way; what moves, when a whole-program build exists, is only how early the
error arrives.

## Name binding without relocations (bytecode VM)

**Status: implemented.** This one is a `doc/wyc-format.md` change rather than
a language change; it is recorded here because it is a consequence of the two
rules above. The format spec now describes the result: §7.2, §7.3, the new
§8.11 `free` section, and Appendix A.

The `.wyc` format reached names through a `relocations` table: a symbol table
of every external name, with a parallel bind table filled in batches (a
module's set at init's `resolve`, a function's set at its first call) so that
`rget`/`rset`/`msg`/`getmsg` could be bind-check-free table reads.

The table was doing three unrelated jobs at once - naming symbols to bind,
holding path literals for `import` and `import_star` (both of which read
`reloc.path` directly and never touch the bind table), and registering a
wildcard namespace in a search list - and of its six kinds only *message* was
ever branched on, the other five resolving identically.

With cycles illegal, the deferred-binding machinery that motivated the split
has no remaining job, and the design collapses: **a module's global slots are
its bind table.** The compiler already resolves names lexically for globals,
including allocating a distinct slot for a block-local `var` that shadows a
module name (`add_shadow_global`); relocations were the one place it gave up
and handed the question to a runtime ladder.

- A name the module references but does not define is a global slot that
  starts Unset, exactly like a declared-but-unassigned variable. The bespoke
  NotFound error value disappears; an unresolved name is Unset.
- `import` binds its slots directly. A wildcard import fills still-Unset
  *free-name* slots - iterating the importer's free names and asking the
  dependency, not iterating the dependency's exports, so the cost is
  proportional to what this module actually uses and a dependency's export
  set is not part of its ABI. Builtins fill whatever remains, last, which is
  what implements the layered precedence above.
- `foo::bar::baz` is an ordinary global slot bound during the import that
  makes it reachable. The prefix-import behavior stays, because
  `import a::b::c` remains ambiguous between a submodule and a member.
- `rget`/`rset` fold into `gget`/`gset`; `resolve` (`0x07`) and both `u` sets
  are removed.

Message identities keep a table of their own. They resolve in a namespace
separate from variables - a module may have an unrelated `describe` variable
and `describe` message - and an unqualified message name *creates* the
identity when nothing has it yet, neither of which folds into a global slot.
That table is single-purpose, which is the point: what remains is one table
that means one thing.

**Imports need no table at all.** A path is a constant string, which the
statics pool already holds and which the `free` list already uses as its key;
a wildcard's except-list is a window of interned symbols, read the way
`tuple`/`list`/`dict` already read theirs. So `import` takes a static, and
`import_star` takes a static plus a base and a count:

    lsym L0 <- sym#File
    lsym L1 <- sym#StreamReader
    import_star static#3 except L0, 2 names

The test of whether something deserves a table is whether it caches a binding.
A message identity does, and keeps one. An import path does not - it is read
once, split, and handed to the loader - so it became operands instead. That
also disposes of the last oddity in the old scheme: a wildcard entry could
never be deduplicated with anything else, because each carried its own
except-list. Nothing to deduplicate now.
