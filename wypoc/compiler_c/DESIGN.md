# `wypoc/compiler_c` design

`wyrm --compile` translates a parsed wyrm module into C. This document tracks
*design state*, not API reference — read the module docstrings for that.
Update the relevant section here when a submodule's scope changes, especially
when closing a gap against `wyrm_eval_parse_tree.py` (the PoC interpreter)
toward feature parity.

It is a narrow slice, not a general compiler. Unsupported input raises
`CompileError` naming the construct — never silently-wrong C. That's the same
"fail loud, not silently wrong" convention the interpreter's own known gaps
follow (see `wypoc/README.md`).

## Calling convention

A compiled `fn` is **one ordinary C function** in the target interpreter's
native calling convention — the same shape its own builtins have:

```c
bool w_{module}_{fn}(wyrm_lang_vm* vm, wyrm_value* args,
                     wyrm_uword argc, wyrm_value* out);
```

`false` out means it failed, with the error already recorded on the `vm`;
otherwise the result goes through `*out`. A module ends with a
`{MODULE}_BUILTINS[]` table naming each compiled function with its arity, so a
host installs the module by walking one array rather than keeping a list of
names in step by hand.

Inside a function body a wyrm local is an **ordinary C variable** of its
declared type. Boxing into `wyrm_value` happens only at the three boundaries —
parameters in, call arguments out, the result — so arithmetic compiles to
arithmetic. Parameters arrive dynamically typed, which is the one place
compiled code meets values it did not produce, so the prologue checks the
argument count and each argument's type tag before unboxing.

### What this replaced, and what it bought

The previous target was the raw object-system VM's *resumable* convention
(`wyrm_exec_fn` + `wyrm_state*`), where a function could not hold a C stack
frame across a call. Everything expensive about the old design followed from
that one constraint:

| Old (resumable `wyrm_exec_fn`) | Now |
|---|---|
| a graph of `static` chunks per `fn`, one per basic block, with a tree-coordinate naming scheme | one C function |
| locals in fixed value-stack slots, addressed by index from every chunk | plain C variables |
| `if`/`while` as `wyrm_state_set_pending` jumps between chunks | C's own `if`/`while` |
| a non-tail call splits its block in two; a tail call needs a shared `__wyrm_forward_result` continuation | an ordinary C call |
| a call only where a whole statement or a bare `return` could go | a call anywhere in an expression |
| no `float` (the old target had no float tag) | `float` alongside `int`/`uint`/`bool` |
| slot defaults impossible (the old slot API took a type, not a value) | a slot carries a constant default |

The generated C is also readable, which matters more than it sounds: a
compiler whose output nobody can follow is a compiler whose bugs nobody
finds. `test_compiler_c.py` runs every fixture through a C compiler with
`-Wall -Werror` for the same reason.

## Package layout

| File | Owns | Depends on |
|---|---|---|
| `errors.py` | `CompileError`, the `err()` raise helper | nothing |
| `wtypes.py` | the supported-type table (`TYPES`, each a `WType` knowing its C type, tag, payload field, boxing constructor and zero), the operator tables, `wtype()`, `c_ident()` | `errors` |
| `handlers.py` | dispatch registries (`STATEMENT_HANDLERS`, `EXPR_HANDLERS`, `LOCAL_COLLECT_HANDLERS`, `TOPLEVEL_HANDLERS`) | nothing at runtime |
| `context.py` | `FnContext` — one `fn`'s locals, emit buffer, and temp counter | `errors`, `wtypes` |
| `expressions.py` | `compile_expr()` → a `Value` (C text + wyrm type), one `EXPR_HANDLERS` entry per expression kind, and the short-circuit lowering | `context`, `errors`, `handlers`, `wtypes` |
| `native_blocks.py` | `native::block(...)` detection/parsing/splicing | `context`, `errors` |
| `calls.py` | callee resolution and the one call shape (registers the `Call` expression handler) | `context`, `errors`, `expressions`, `handlers`, `native_blocks`, `wtypes` |
| `statements.py` | the two passes (`collect_locals`, `run_stmts`), control flow, and the `Assign`/`VarDecl`/`ExprStmt` handlers | `calls`, `context`, `errors`, `expressions`, `handlers`, `native_blocks` |
| `functions.py` | `compile_fn()` — one `fn`'s prologue plus body | `context`, `errors`, `statements`, `wtypes` |
| `classes.py` | `compile_class()` — one `class` to its builder function | `errors`, `expressions`, `wtypes`, `context` |
| `module.py` | `compile_module()` — top-level walk, assembly, registration table | `classes`, `errors`, `functions`, `native_blocks`, `wtypes` |
| `__init__.py` | re-exports `CompileError`, `compile_module` | `errors`, `module` |

The dependency column is a DAG rooted at `errors.py`/`wtypes.py` (pure
data/helpers) and `handlers.py` (pure dispatch, zero runtime deps). **Nothing
imports sideways.** The old design had one deliberate cycle-break —
`calls.py` needed to recurse back into `statements.run_stmts` to continue a
block after a call split, and got it passed in as a parameter. Ordinary C
calls removed the need, so that exception is gone; if a second one appears,
it's a signal to redraw responsibilities rather than to reintroduce the
pattern.

## Dispatch

Four registries live in `handlers.py`, keyed by concrete `wypoc.ast_nodes`
type:

- `EXPR_HANDLERS` (`expressions.py` owns lookup) — every supported expression
  kind. This is where feature-parity work concentrates: each new
  literal/operator is one more registered handler, with no change to any
  caller of `compile_expr()`. `calls.py` registers `Call` here, which is what
  keeps expressions.py from having to know about calls at all.
- `LOCAL_COLLECT_HANDLERS` (`statements.py`, via `collect_locals_stmt`) —
  pass-1 "does this statement introduce a typed local".
- `STATEMENT_HANDLERS` (`statements.py`, via `run_stmts`) — pass-2 emission
  for statements that don't recurse into a nested block.
- `TOPLEVEL_HANDLERS` — reserved for a module-level statement kind beyond the
  three `module.py` handles inline (`import`, `fn`/`class`, top-level
  `native::block()`). Not populated yet.

**Not** dispatch-based, on purpose: `if`/`while`/`return`/`break`/`continue`
stay inside `run_stmts`. Each recurses into `run_stmts` for its own body,
which a plain `(ctx, node)` handler signature cannot express, and this set is
expected to stay small and central unlike the expression and statement kinds
that grow chasing interpreter parity.

## Two passes, and why there are still two

`collect_locals` walks a body before anything is emitted, so every local's C
declaration lands at the top of the function. C wants a declaration before
use, and a `var` inside a loop body must not be redeclared on each iteration.

The pass exists only because `--compile` does no type inference: a local needs
a declared type to have a C representation at all, which is why `x := 1` is
refused and `var x: int = 1` is not. Inference would let the declaration be
emitted where the wyrm code puts it, and would retire the pass.

## Statement-hoisting, and the two places it needs care

`compile_expr` may **emit statements before** returning its expression. That's
what lets a call appear anywhere: the call becomes a statement assigning a
temporary, and the temporary is the expression. Two constructs are not
indifferent to when those statements run:

- **Short-circuiting.** `a and f(b)` must not call `f` when `a` is false.
  `expressions._logical_op` compiles the right operand, notices whether
  anything got hoisted, and if so rewrites the whole thing as a `bool`
  temporary plus an `if` — recovering the short circuit that C's `&&` would
  have given for free on a pure operand.
- **Loop conditions.** A `while` condition is re-evaluated every iteration, so
  anything hoisted out of it has to be re-run every iteration:
  `statements.compile_while_stmt` emits `for (;;) { <hoisted> if (!cond)
  break; ... }` when there is a hoist, and the plain `while (cond)` when there
  isn't.

An `elif` is a third case, handled structurally: the chain nests as `else { if
(...) }` rather than flattening to `else if`, so a later condition's hoisted
statements can't run before the earlier branch was ruled out. That's also
exactly what an `elif` means.

## Classes (`classes.py`)

A `class` compiles to a plain builder function (not a compiled `fn` — class
construction is one-shot):

```c
bool w_{module}_{class}(wyrm_lang_vm* vm, wyrm_patch_class** out);
```

Straight-line: allocate the class, intern its name, give it a method
dictionary, then one interned symbol plus `add_slot` per `slot`. The slot API
takes a default **value**, so a declared default compiles; a slot with none
gets its type's zero value, matching the interpreter's own `_zero_value`.

A default must be a *constant*. It is evaluated when the class is built, with
no function body around it, so there is nowhere for a name or a call to
resolve — both are refused by name.

**Not** supported: base classes/inheritance, slot options (setter/getter), and
methods declared in the class body. A method is a message, and message
definitions (`fn [Cls] ...`) are a separate gap.

## `native::block(...)` (`native_blocks.py`)

The escape hatch from `doc/language-spec.md`'s "Native Code" section:
`native::block(portion, inputs, outputs, code)`, all four arguments literals.
Two call sites:

- **Top-level** (`module.py`): appends `code` verbatim into one of the
  module's `HEADER`/`TYPES`/`CONSTANTS`/`PROTOS`/`FUNCTIONS` sections. Must
  have empty `inputs`/`outputs`.
- **In a `fn` body** (`compile_native_block`): splices `code` into the body
  inside a scope of its own. Under this convention a wyrm local *is* a C local
  of the same name, so there is no copy-in/copy-out marshalling — the declared
  `inputs`/`outputs` are checked (naming an unknown local is an error rather
  than C the compiler would reject far from its cause) and then emitted as a
  comment recording what the block touches.

## Known gaps / where parity work lands

Tracking against `wyrm_eval_parse_tree.py`, roughly in "closes the most
ground" order:

- **Installation needs one hook on the other side.** The generated
  `{MODULE}_BUILTINS[]` table is the interpreter's own builtin-row type, but
  the interpreter has no public way to *append* to its builtin table — that's
  a change in its repository, not this one. It is the only part of the output
  that needs anything the interpreter doesn't already expose; everything else
  compiles against what is there.
- **Expressions**: `Num` (int and float), `Bool`, `Name`, `UnaryOp`, `BinOp`,
  `Call` are registered. Missing: `Str`, `Char`, `Symbol`, `Array`, `Pair`,
  `Tuple`, `Dict`, `Attr`, `Index`, `Message`, `Scope`, `Defined`, `Lambda`,
  `SetIfUnset`. The collection and string kinds all need the same thing first:
  a story for GC-managed values in generated code, since a `wyrm_value`
  holding a heap object has to be reachable by the collector. The scalar-only
  types sidestep that entirely, which is why they came first.
- **Statements**: `For`, `WithBlock`/`WithSimple`, `Defer`, `Try`/`Catch`, and
  `StaticDecl` are unhandled. `For` needs an iteration-protocol decision;
  `Defer` maps naturally onto a C cleanup label, `Try`/`Catch` onto the
  `false`-returning convention this backend already has, so those two are the
  cheapest of the set.
- **Types**: `int`/`uint`/`bool`/`float`. `str` needs the GC story above.
- **`$` in a name rides on a compiler extension.** `$` is an ordinary wyrm
  identifier character (`reg$0`), and locals/parameters are emitted as C
  identifiers verbatim, so such a name reaches the C compiler as-is. GCC and
  Clang both accept `$` in an identifier (even under `-std=c11 -pedantic`);
  a toolchain that doesn't - MSVC, or GCC with `-fno-dollars-in-identifiers`
  - would need a mangling pass here first.
- **Calls**: only to another compiled function in the same module. Calling
  *into* the interpreter (an uncompiled or corelib function) would need a
  by-name lookup at call time; keyword/spread arguments and multi-value
  returns are separate gaps.
- **Messages**: `fn [Cls] name(...)` is rejected outright. A message is a
  generic function with overload resolution, so compiling one means either
  emitting the resolution or registering an overload with the interpreter's
  message table — a design decision, not just more handlers.
- **Modules**: one file at a time (`import native` only). The interpreter's
  `mod::sub::leaf` resolution has no compiled counterpart.
