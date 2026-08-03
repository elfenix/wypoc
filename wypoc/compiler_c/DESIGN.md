# `wypoc/compiler_c` design

`wyrm --compile` translates a parsed Wyrm module into C targeting the real
wyrm VM's calling convention. This is a narrow v1 slice, not a general
compiler — see each section below for exactly what's supported today, and
`wypoc/README.md`'s "Known gaps" for how this fits the project's "fail loud,
not silently wrong" convention (unsupported input raises `CompileError`,
never silently-wrong C).

This document tracks *design state*, not API reference — read the module
docstrings for that. Update the relevant section here when a submodule's
scope changes, especially when closing a gap against
`wyrm_eval_parse_tree.py` (the POC interpreter) toward feature parity.

## Package layout

| File | Owns | Depends on |
|---|---|---|
| `errors.py` | `CompileError`, the `err()` raise helper | nothing |
| `wtypes.py` | supported-type table (`TYPES`), operator table (`BINOPS`), `ctype()`, `c_ident()`, `is_float_literal()` | `errors` |
| `handlers.py` | dispatch registries (`STATEMENT_HANDLERS`, `EXPR_HANDLERS`, `LOCAL_COLLECT_HANDLERS`, `TOPLEVEL_HANDLERS`) | nothing at runtime (only `TYPE_CHECKING` imports) |
| `context.py` | `FnContext` — per-`fn` compiler state + chunk/local bookkeeping | `errors`, `wtypes` |
| `expressions.py` | `compile_expr()` + one `EXPR_HANDLERS` entry per expression kind | `context`, `errors`, `handlers`, `wtypes` |
| `native_blocks.py` | `native::block(...)` detection/parsing/splicing (top-level and in-body) | `context`, `errors`, `wtypes` |
| `calls.py` | callee resolution, args-array construction, non-tail call split, tail-call forwarding | `context`, `errors`, `expressions`, `native_blocks`, `wtypes` |
| `statements.py` | the chunk-compilation engine (`run_stmts`), `if`/`while`/`return`/`break`/`continue`, plus `STATEMENT_HANDLERS`/`LOCAL_COLLECT_HANDLERS` entries for `Assign`/`ExprStmt` | `calls`, `context`, `errors`, `expressions`, `handlers`, `native_blocks`, `wtypes` |
| `functions.py` | `compile_fn()` — pass 1 (collect locals) + pass 2 (emit) for one `fn` | `context`, `errors`, `statements`, `wtypes` |
| `classes.py` | `compile_class()` — one `class` def to its `wyrm_class*` builder function | `errors`, `wtypes` |
| `module.py` | `compile_module()` — top-level statement walk + final C-source assembly | `calls`, `classes`, `errors`, `functions`, `native_blocks`, `wtypes` |
| `__init__.py` | re-exports `CompileError`, `compile_module` | `errors`, `module` |

The dependency column is deliberately close to a DAG rooted at `errors.py`
and `wtypes.py` (pure data/helpers) and `handlers.py` (pure dispatch
mechanism, zero runtime deps) — nothing imports "sideways" except where
noted below.

## Dispatch

Four registries live in `handlers.py`, keyed by concrete `wypoc.ast_nodes`
type:

- `EXPR_HANDLERS` (`expressions.py` owns lookup) — every supported
  expression kind. This is where feature-parity work will concentrate:
  each new literal/operator the interpreter supports (strings, arrays,
  dicts, attribute access, ...) is one more registered handler here, with
  no change needed to any caller of `compile_expr()`.
- `LOCAL_COLLECT_HANDLERS` (`statements.py` owns lookup, via
  `collect_locals_stmt`) — pass-1 "does this statement introduce a typed
  local" check, for statement kinds that aren't structural control flow.
- `STATEMENT_HANDLERS` (`statements.py` owns lookup, via `run_stmts`) —
  pass-2 "plain" statement emission (no chunk-splitting needed).
- `TOPLEVEL_HANDLERS` — reserved for module-level statement kinds beyond
  the four `module.py` currently special-cases inline (`import`, `fn`,
  `class`, top-level `native::block()`). Not populated yet; a future
  top-level form (e.g. `using`) can self-register here instead of growing
  `module.py`'s dispatch by hand.

**Not** dispatch-based, on purpose: `if`/`while`/`return`/`break`/
`continue`, and non-tail call splitting, are all handled directly inside
`statements.py`'s `run_stmts`/`collect_locals_stmt`. Each needs plumbing a
plain `(ctx, node)` handler signature can't carry — `block_path`,
`remaining_stmts`, `fallthrough` — and this set of control-flow forms is
expected to stay small and central, unlike expression/statement kinds
which will grow a lot chasing interpreter parity. Forcing them through the
registry would mean widening every handler's signature for a case that
five functions need and the rest never will.

**Breaking the import DAG on purpose:** `calls.compile_call_split` needs to
recurse back into `statements.run_stmts` once its call-split chunk is
closed, but `statements.py` is the one that calls *into* `calls.py` (to
compile a call it finds mid-block) — a direct import either way would be a
cycle. `run_stmts` is passed in as a plain parameter instead. This is the
one place the compiler leans on "pass the function you need" rather than
"import the module you need"; if a second case like this shows up, it's a
signal `calls.py`'s and `statements.py`'s responsibilities should be
redrawn rather than adding a second such parameter.

## Chunk model (`context.py`, `statements.py`, `functions.py`)

Every `fn` body is compiled as a graph of small `wyrm_exec_fn` "chunks"
rather than one C function - one non-static entry point, `w_{module}_
{fn}`, plus a `static` chunk per basic block: every `if`/`elif`/`else` and
`while` body is its own chunk, and every non-tail call site splits its
enclosing block into a chunk before the call and one after. Chunks are
named `{module}_{fn}_chunk_b{p0}_b{p1}..._{n}`, where the `b*` segments are
a tree-coordinate path down through nested blocks (empty for the
function's own top-level block) and `n` is a sequence number among chunks
sharing that path. This is intentionally not optimized - every block gets
a real C function and a real state-machine transition, even ones with no
call in them - in exchange for one uniform code path that already
generalizes to arbitrary nesting.

Two kinds of transition connect chunks, both ending in `return
WYRM_EXEC_CONTINUE;`:

- A **same-activation jump** (`wyrm_state_set_pending`) - used for
  `if`/`elif`/`else` branch dispatch, entering/looping a `while`, and
  `break`/`continue`. It only swaps which `wyrm_exec_fn` runs next; it
  never touches the value stack, so looping this way costs nothing per
  iteration (this is *not* `WYRM_EXEC_TAIL_CALL` + `wyrm_stack_
  replace_frame_f`, the "proper" trampoline `fiber.c` supports - there's
  still no worked example of that path to verify frame/stack mechanics
  against; `set_pending` gets the same "no stack growth" property for
  same-function control flow via the demonstrated API surface instead).
- A **real call** (`wyrm_state_call_continue`) - used for an actual call
  into another compiled `fn`. It pushes a fresh frame for the callee, so
  the caller's locals must already live *on* the value stack (not in C
  variables) to survive it: every local gets one fixed slot, established
  once by the entry chunk and addressed via `wyrm_state_value_n(state,
  slot)` from every chunk of the function for its entire lifetime,
  restored to exactly that many slots (`wyrm_state_pop_to_value_count`)
  right after each call's return value is copied out.

Tail calls (`return f(...)`) compile to `wyrm_state_call_continue` plus a
shared forwarding continuation (`__wyrm_forward_result`, `calls.py`) that
relays the callee's result stack-for-stack back to whatever called *this*
function, so a tail call doesn't grow the caller's own footprint the way
an ordinary call's "read result, pop back down" sequence does.

`FnContext` (`context.py`) is the mutable state this whole model shares —
locals/types, the current chunk's buffered lines/indent, chunk-name
bookkeeping, and the current loop's break/continue targets — threaded as
an explicit first argument through every handler instead of living on a
compiler object's `self`.

## Classes (`classes.py`)

`class` defs compile to a plain C function (not a `wyrm_exec_fn`, no chunk
model involved — construction is one-shot, not called from the VM's
dispatch loop): `w_{module}_{class}(wyrm_context* context, wyrm_class**
out)`, following the same `w_{module}_{name}` naming `fn` entries use.
Body is a straight-line `wyrm_class_new` + one `wyrm_machine_insert_symbol`
+ `wyrm_class_add_slot_f` pair per slot.

Supported today: bare typed slots (`int`/`bool`, same `wtypes.TYPES` table
`fn` locals use — both map to `WYRM_TYPE_TAG_WORD`, since the underlying
`wyrm_type_tag` enum has no separate bool tag either). **Not** supported
(raises `CompileError`): base classes/inheritance, slot defaults, slot
options (setter/getter/etc.), `init` (constructors), and methods declared
inside the class body (`fn [ClassName] ...` methods are a separate
top-level construct already rejected by `functions.compile_fn`'s
`class_target` check — they aren't part of a class's own body syntax).

Closing this gap toward interpreter parity means: constructors (an actual
`w_{module}_{class}_new` that populates slot values, presumably taking the
slot values as params once defaults are figured out), inheritance (walking
`super` to inherit slots, matching `wyrm_class`'s `super` field), and slot
defaults/options (need an evaluated-at-construction-time story, since
`--compile` today only evaluates expressions inside a `fn` body's
execution, not at class-definition time).

## `native::block(...)` (`native_blocks.py`)

The escape hatch from `doc/language-spec.md`'s "Native Code" section:
`native::block(portion, inputs, outputs, code)` where all four arguments
must be literals. Two call sites, both routed through
`parse_native_block_args`:

- **Top-level** (`module.py`): appends `code` verbatim into one of the
  module's `HEADER`/`TYPES`/`CONSTANTS`/`PROTOS`/`FUNCTIONS` sections.
  Must have empty `inputs`/`outputs` lists.
- **In a `fn` body** (`compile_native_block`, dispatched from
  `statements._compile_expr_stmt`): splices `code` into the current chunk
  inside a private nested C scope, copying named `inputs` in and named
  `outputs` back out to their slots so the spliced code can use bare
  symbol names.

## Known gaps / where parity work lands

Tracking against `wyrm_eval_parse_tree.py` (the POC interpreter), in
roughly the order a "closes the most ground" heuristic would suggest:

- **Expressions** (`expressions.py`): only `Num`/`Bool`/`Name`/`UnaryOp`/
  `BinOp` are registered. Missing: `Str`, `Char`, `Symbol`, `Array`,
  `Pair`, `Tuple`, `Dict`, `Attr`, `Index`, `Message`, `Scope`,
  `Defined`, `Lambda`, `SetIfUnset`, float literals (blocked on the VM
  having no `WYRM_TYPE_TAG_FLOAT` yet, not a compiler gap). Each is one
  `EXPR_HANDLERS.register(...)` entry away.
- **Statements**: `For` loops, `Using`, `With`/`WithBlock` are entirely
  unhandled (`run_stmts`/`collect_locals_stmt` raise `CompileError` on
  anything without a case). `For` in particular will need an iteration
  protocol decision before it can lower to the chunk model.
- **Types**: only `int`/`bool` (`wtypes.TYPES`). `str` support depends on
  `wyrm_string`/GC-object handling in generated C, which the chunk model's
  "locals live in fixed value-stack slots" scheme hasn't been extended to
  yet (`wyrm_value` slots today only carry a `wyrm_word`).
- **Classes** (`classes.py`): see above — constructors, inheritance, slot
  defaults/options.
- **Calls** (`calls.py`): no support for calling into an *uncompiled*
  function (e.g. a corelib/interpreted one), keyword/spread args, or
  multi-value returns.
- **Modules**: `--compile` only ever compiles a single file (`import
  native` opt-in, no other imports); the interpreter's multi-file
  `mod::sub::leaf` resolution (`wyrm_modules.py`) has no compiled
  counterpart.
