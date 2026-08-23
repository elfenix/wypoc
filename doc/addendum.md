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
