# wypoc

`wypoc` is a Python proof-of-concept for the wyrm language: a PEG grammar
built with [`pegen`](https://github.com/we-like-parsers/pegen) (CPython's
own PEG parser generator), a hand-rolled tokenizer, a small typed AST, and a
tree-walking interpreter, wired up as an installable `wyrm` command.

It exists to validate the grammar in `doc/grammar.ebnf` / `doc/language-spec.md`
against real parsing and execution, not to be wyrm's production
implementation (that's the C++ core elsewhere in this repo). Values are
plain Python objects (`int`, `float`, `str`, `bool`, `list`, `dict`, `tuple`)
and scoping is a plain Python `dict`, the same way `eval()`/`exec()` work -
this keeps the interpreter small enough to read in one sitting, at the cost
of not modeling wyrm's real value/type system at all.

## Layout

```
doc/grammar.ebnf         reference EBNF grammar (source of truth for syntax)
doc/language-spec.md     prose spec with worked examples
wypoc/
  corelib/                a tiny wyrm-language standard library (see below),
                           installed as package data
  wyrm_tokenizer.py       hand-rolled lexer -> tokenize.TokenInfo stream
  wyrm.gram               the PEG grammar (pegen source), derived from grammar.ebnf
  parser.py               generated from wyrm.gram - do not hand-edit, see below
  actions.py              small helpers used by wyrm.gram's grammar actions
  ast_nodes.py            typed AST node classes built by the parser actions
  parse.py                glues the tokenizer + generated parser together
  wyrm_eval_parse_tree.py the tree-walking evaluator (the interpreter proper)
  compiler_c.py           wyrm --compile: translates a module to C (see below)
  wyrm_modules.py         WYRM_PATH search-path resolution (no eval/parse dependency)
  wyrm_io.py              POSIX-ish low-level I/O primitives (__open/__read/...)
  cli.py                  the `wyrm` command (installed via pyproject.toml)
  lsp.py                  the `wyrm-lsp` language server (diagnostics only)
  samples/                *.wy fixtures used by the test suite below
test/                     pytest suite (see "Running the tests")
```

## Pipeline

```
source.wy
   |  wyrm_tokenizer.generate_tokens()
   v
tokenize.TokenInfo stream
   |  pegen.tokenizer.Tokenizer + parser.GeneratedParser (via parse.parse())
   v
ast_nodes.Program (a tree of dataclasses)
   |
   +-- wyrm_eval_parse_tree.eval_program(tree, ctx)          [default: run it]
   |      v
   |   side effects on ctx (a dict[str, Variable]) + real I/O via wyrm_io
   |
   +-- compiler_c.compile_module(tree, module_name)          [--compile: translate it]
          v
       C source text targeting the real wyrm VM calling convention
```

Every stage is independently invokable:

```bash
# tokens only
PYTHONPATH=. .venv/bin/python -c \
  "from wypoc.wyrm_tokenizer import generate_tokens; [print(t) for t in generate_tokens(open('wypoc/samples/basics.wy').read())]"

# parse -> print the AST
PYTHONPATH=. .venv/bin/python wypoc/parse.py wypoc/samples/basics.wy

# parse + run a script (after `pip install -e .`, see below)
.venv/bin/wyrm wypoc/samples/eval_functions.wy

# parse + compile a module to C instead of running it
.venv/bin/wyrm --compile wypoc/samples/compile_tail_call.wy
```

### Why a custom tokenizer

Python's stdlib `tokenize` can't be reused as-is: a bare `'` starts a string
literal in Python but is wyrm's symbol sigil (`'name`), `[` doubles as wyrm's
pair/list literal, and `$[`/`${` are the array/dict sigils; wyrm also has
operators (`::`, `?=`, `<=>`, `!`, `$`) stdlib `tokenize` has
never heard of. `wyrm_tokenizer.py` implements wyrm's own lexical rules
directly (see `doc/grammar.ebnf` section 0), including Haskell/Python-style
layout (INDENT/DEDENT) and the rule that brace-delimited blocks (`{ }`) opt
out of the layout algorithm entirely for their contents, while still
treating NEWLINE/`;` as statement separators. It still yields ordinary
`tokenize.TokenInfo` tuples, so `pegen`'s generated parser (which expects
that shape) doesn't need to know anything changed.

### Why pegen

`pegen` is CPython's own PEG parser generator (the tool that produces
CPython's real parser from `Grammar/python.gram`). `wyrm.gram` is written in
its grammar DSL; `python -m pegen wypoc/wyrm.gram -o wypoc/parser.py`
compiles it into `parser.py`, a plain recursive-descent-with-memoization
parser class. **`parser.py` is generated - edit `wyrm.gram`, then
regenerate:**

```bash
.venv/bin/python -m pegen wypoc/wyrm.gram -o wypoc/parser.py -q
```

`wyrm.gram`'s header comment documents every place it deliberately deviates
from the literal EBNF (e.g. `expression` vs `assign_expr`/`expr_list` to
avoid a real ambiguity in how the EBNF nests comma-expressions; `target`
restricting attribute assignment to a plain dotted chain rather than a full
postfix expression) - read it before assuming grammar.ebnf is 100%
authoritative on some edge case.

### The AST

`ast_nodes.py` defines one dataclass per grammar construct (`Program`,
`ClassDef`, `FnDef`, `If`, `BinOp`, `Message`, ...). `Node.__str__` is
generic and recursive (via `dataclasses.fields`), so every node - and the
whole tree - prints as a readable `TypeName(field=value, ...)` with no
per-node pretty-printer needed; that's what `wypoc/parse.py`'s CLI mode
prints.

### The interpreter

`wyrm_eval_parse_tree.py` is a single-file tree-walking evaluator:
`eval_program(tree, ctx)` / `eval_stmt(stmt, ctx)` / `eval_expr(node, ctx)`,
where `ctx` is a `Scope` (a real lexical scope, chained to its parent - see
below), used exactly like passing a dict to Python's own `exec()`. Key pieces:

- **`Variable`** - what actually lives in a `Scope`; a mutable cell holding
  a value. Plain `=`/`?=` assignment mutates an existing `Variable` in place
  (found by walking the scope chain outward - so closures see writes to
  captured names), and requires the name to already be declared. `var` (and
  its `:=` shorthand) instead always binds a *fresh* `Variable` into the
  current scope's own level, erroring if that same scope already declared
  the name - shadowing a name visible from an enclosing scope is fine. See
  doc/language-spec.md's Variables section.
- **`Scope`** - one lexical level: its own declared bindings (`dict[str,
  Variable]`) plus a `parent` Scope to fall back to on read. A fresh child
  Scope is created per function/method/coroutine call, per `if`/`elif`/
  `else` branch taken, per `while` iteration, and per `for` iteration (the
  loop variable is declared fresh in that iteration's own scope, so a
  closure created inside the loop captures that iteration's binding, and
  the name is entirely out of scope once the loop ends). Function
  parameters (and `this`) always get a fresh `Variable` in the new call
  frame via `bind_new`, regardless of what an enclosing scope already binds
  under that name.
- **`Function`** - a `fn`/lambda closing over the scope it was defined in.
- **`Class`** - a user-defined class: resolved `bases` (real `Class`
  objects, not names), its own `slots`/`methods`/`coroutines`/`init`, and
  `all_slots()` for slot inheritance/overriding (a simple base-classes-first
  linearization, not a proper C3 MRO).
- **`ClassInstance`** - the result of `new`: a `Class` reference plus its own
  `attrs` (slot name -> `Variable`).
- **`Method` / `MethodOverload` / `BoundMessage`** - the `!` message
  operator's multiple-dispatch machinery. See "Message dispatch" below.
- **`Module`** - a loaded `.wy` file's own namespace plus whatever
  submodules have been imported off of it (`import std::io` registers `io`
  on `std`, like Python's own module/submodule attributes).
- **`Coroutine`** - currently just storage (a `co` definition's node +
  closure); coroutines are *not* drivable/resumable yet - see "Known gaps".

**Message dispatch (`!`)**, in more detail, since it's the least
Python-like part: `fn [Cls1, Cls2, ...] name(...)` defines one overload of
the generic function `name`, keyed by a tuple of receiver classes (an
`Optional[Class]` per position - `None` is a wildcard/"empty type"). A class
body's `fn name(...)` is auto-registered the same way, equivalent to
`fn [ThatClass] name(...)` declared externally. A plain `fn name(...)` stays
an ordinary `Function` until a bracketed sibling (or class-body method)
shows up under the same name, at which point it's "promoted": the existing
function becomes that generic function's wildcard overload, at whichever
arity the triggering signature uses. `recv ! name(...)` (or
`(a, b) ! name(...)` for multi-dispatch) resolves the best-matching overload
by comparing `(distance_at_position_0, distance_at_position_1, ...)` tuples
(0 = exact class, N = N steps up the inheritance graph, infinity = wildcard)
- Python's own tuple comparison already implements "most specific wins,
left to right, only look further right on a tie", so `resolve_overload`
doesn't need a custom comparator. `recv ! name` with no call parens returns
a `BoundMessage` (dispatch already resolved, callable later), matching
`language-spec.md`'s "the `!` operator creates a closure... it may be
stored". Inside a single-receiver method body, slot names are also directly
in scope (not just via `this.slot`) - see `call_overload`'s docstring.

**Modules** (`wyrm_modules.py` + the `Module`/`import_module` machinery in
`wyrm_eval_parse_tree.py`): `WYRM_PATH` (colon-separated, like `PYTHONPATH`)
is searched in order, with `wypoc/corelib/` (derived at runtime from
`wypoc`'s own installed location, not hardcoded) as the final fallback.
`import std::io` looks for
`<root>/std/io.wy`; `import std` (a directory) looks for
`<root>/std/__init__.wy`, mirroring Python's package convention exactly
(including loading parent packages before children, and registering
children onto their parent's `.submodules`). Path resolution itself
(`wyrm_modules.py`) has no dependency on the parser/evaluator, specifically
to avoid an import cycle with `wyrm_eval_parse_tree.py`.

**Host interop** (`expose`/`expose_all`/`builtin` in
`wyrm_eval_parse_tree.py`): any Python value can be bound into a wyrm scope
under a name, exactly as if wyrm code had written `name = value`. Calling
falls back to Python's own `callable()` check, so an exposed Python function
is immediately callable from wyrm with no wrapping required. `wyrm_io.py`
(`__open`/`__read`/`__write`/`__lseek`/`__dup2`/`__close`/`__flush` and the
`__STDIN`/`__STDOUT`/`__STDERR` constants) is the one built-in consumer of
this - a POSIX-flavored integer-handle-to-Python-file-object table meant to
back an `io` module written in wyrm itself (`corelib/std/io.wy` is a first,
tiny example).

### The compiler (`wyrm --compile`)

`compiler_c.py` is a second, alternative backend over the same
`ast_nodes.py` tree the evaluator walks: instead of running a module, it
translates it to C targeting the real wyrm VM's calling convention
(`wyrm_exec_fn` / `wyrm_state*`, following the call-handling pattern
demonstrated in the reference implementation's worked example) - a real
step toward wyrm's stated goal of self-hosting with C as its "assembly
language" (see `doc/language-spec.md`'s "Native Code" section).

This is a deliberately narrow v1 slice, not a general compiler:

- A module must `import native` to be compile-eligible at all (this is
  also what the spec says marks a module as compile-only, not for runtime
  interpretation) and must be single-file (no `import`/`from`/`using`).
- Only `fn` bodies with `int`/`bool` params and locals, arithmetic/
  comparison/boolean expressions, `if`/`elif`/`else`, `while`,
  `break`/`continue`, and `return` are compiled. `float` is deliberately
  unsupported: the real `wyrm_type_tag` enum has no dedicated float tag
  yet, so there's no encoding to target honestly.
- Each `fn` compiles to one non-static entry point (`w_{module}_{fn}`)
  plus a `static` chunk per basic block - every `if`/`elif`/`else` and
  `while` body is its own chunk, and every non-tail call splits its
  enclosing block into a chunk before and a chunk after. A call to
  another compiled function is supported anywhere a statement may appear
  (`return f(...)`, `x = f(...)`, or a bare `f(...)`) - just not nested
  inside a larger expression (`y = f(x) + 1` still raises `CompileError`).
  Locals live in fixed slots on the VM's value stack (not C variables) for
  a function's whole lifetime, so they survive both kinds of transition
  between chunks: a same-activation jump (`wyrm_state_set_pending`, used
  for branch dispatch and loop iteration - it never touches the value
  stack, so looping this way costs nothing per iteration) and a real call
  (`wyrm_state_call_continue`, used for an actual call into another
  compiled `fn` - its return value is copied into the target's slot and
  the stack immediately truncated back with `wyrm_state_pop_to_value_count`,
  keeping a function's footprint constant even across many calls or a
  call inside a loop). `return f(...)` is the one exception, compiling to
  `wyrm_state_call_continue` plus a small shared forwarding continuation
  (`__wyrm_forward_result`) that relays the callee's result on up
  unchanged, rather than the zero-overhead `WYRM_EXEC_TAIL_CALL` +
  `wyrm_stack_replace_frame_f` path `fiber.c`'s trampoline supports - that
  path has no worked example to verify frame/stack mechanics against yet.
  See `compiler_c.py`'s module docstring for the full chunk-naming and
  jump/call scheme.
- `native::block('PORTION, [inputs...], [outputs...], R"tag(...)tag")`
  (see the language spec) is supported both at module top level (spliced
  into the matching `HEADER`/`TYPES`/`CONSTANTS`/`PROTOS`/`FUNCTIONS`
  output section) and as a function-body statement (spliced inline,
  wrapped in a private nested C scope that copies inputs in and outputs
  out by value - avoiding the same-name self-init hazard a literal
  reading of the spec's own worked example would hit, since `{ int a =
  a; }` is undefined behavior in C when an outer `a` already exists).
- `for`, classes/`!`/methods, coroutines, collections, `new`, lambdas,
  and multi-module compilation are all unimplemented - each raises
  `CompileError` with a specific message, the same "fail loud, not
  silently wrong" convention as the interpreter's own known gaps below.

```bash
.venv/bin/wyrm --compile module.wy            # C source to stdout
.venv/bin/wyrm --compile -o module.c module.wy  # ...or to a file
```

`test/test_compiler_c.py` covers the supported fixtures
(`wypoc/samples/compile_*.wy`) and one `CompileError` case per documented
scope cut; when the sibling `wyrm` repo and `gcc` are both available, it
also shells out to `gcc -fsyntax-only` against the real headers to catch
type/signature mismatches plain string assertions would miss.

### `corelib/` and the `wyrm` command

`wypoc/corelib/` is a small standard library written in wyrm, demonstrating
the module system: `shapes.wy` (single-file module), `std/__init__.wy`
(package marker), `std/io.wy` (a submodule, using the `!`-less plain-`fn`
form - `println`). It's declared as package data
(`[tool.setuptools.package-data] wypoc = ["corelib/**/*.wy"]` in
`pyproject.toml`), so it's included in both editable installs and built
wheels. The `wyrm` command (installed via this repo's top-level
`pyproject.toml`, `[project.scripts] wyrm = "wypoc.cli:main"`) is wyrm's
equivalent of the `python` binary:

```bash
.venv/bin/python -m pip install -e .        # from the repo root - installs the `wyrm` command
.venv/bin/wyrm script.wy arg1 arg2          # like `python script.py arg1 arg2`
# (or: source .venv/bin/activate, then just `wyrm script.wy arg1 arg2`)
```

Script arguments (everything after the script path, dashes and all) are
packed into `__ARGS`, a tuple of strings visible to the script - wyrm's
`sys.argv[1:]`. `wyrm -h` prints usage; syntax errors are reported with the
real file/line/caret (exit 1); a missing script file or missing script
argument exits 2; a runtime error is reported as `wyrm: ErrorType: message`
(exit 1) rather than a raw Python traceback.

Note: `DEFAULT_COREPATH`'s discovery is computed from `wypoc`'s own
installed location at runtime (`wypoc/corelib`, a sibling of
`wyrm_modules.py`), so it resolves correctly for both an editable install
and a built wheel, since `corelib/` now lives inside the `wypoc` package
and ships as real package data.

### `wyrm-lsp` and the VS Code extension

`wypoc/lsp.py` is a minimal [`pygls`](https://github.com/openlawlibrary/pygls)-based
language server: on every `didOpen`/`didChange`/`didSave` it re-parses the
document with the exact same `wypoc.parse.parse()` everything else in this
package uses, and publishes any `SyntaxError` as an LSP diagnostic. That's
it - no hover, go-to-definition, or completion (see "Known gaps"). The
document-to-diagnostics logic is factored out as a plain function,
`diagnostics_for_source(text) -> list[Diagnostic]`, specifically so it's
unit-testable (`test/test_lsp.py`) without spinning up a real JSON-RPC/stdio
server.

```bash
.venv/bin/python -m pip install -e ".[lsp]"   # pulls in pygls
.venv/bin/wyrm-lsp                             # talks LSP over stdio - not meant to be run interactively
```

`editors/vscode/` is a small VS Code extension: the existing TextMate
grammar (syntax highlighting) plus a `vscode-languageclient` wrapper
(`src/extension.ts`) that spawns `wyrm-lsp` and wires it up for live
diagnostics. See `editors/vscode/README.md` for how to build and try it
locally.

## Running the tests

The suite lives under `test/` and uses [pytest](https://docs.pytest.org/); shared
helpers (`eval_sample`, `SAMPLES_DIR`, ...) live in `test/conftest.py`. Run the whole
suite from the repo root:

```bash
.venv/bin/pytest
```

Or a single file:

```bash
.venv/bin/pytest test/test_grammar.py
```

`pytest` is expected to run clean (zero failures) at all times - see `AGENTS.md`.

| File | Covers |
| --- | --- |
| `test_grammar.py` | Parses every `samples/*.wy` fixture and fails loudly on any syntax error - the grammar's own regression suite. Run this after any `wyrm.gram` change (post-regeneration). |
| `test_eval.py` | Basic single/multi-target assignment, literals, string escaping/raw/multiline decoding, arithmetic. |
| `test_eval_functions.py` | Calls, default args, `*args`, lambdas, `if`/`elif`/`else` as return-bearing control flow. |
| `test_eval_control_flow.py` | `while` + `break`/`continue`, `for`/`else` (Python-style: `else` runs only if the loop wasn't broken out of), early `return` from inside a loop. |
| `test_eval_classes.py` | Class hierarchy metadata (`bases`, `slots`, `methods`), slot inheritance/overriding via `all_slots()`, per-instance slot storage via `new`, and that `new` with constructor args is a clear `NotImplementedError` (needs `init` dispatch). |
| `test_eval_messages.py` | The full `!` story: class-body method auto-registration, plain-fn-to-Method promotion, single- vs. tuple-receiver `this` binding, bare-slot-name access in method bodies, left-to-right most-specific-wins resolution, `BoundMessage`. |
| `test_eval_builtins.py` | `expose`/`expose_all`/`builtin` - handing Python callables/values to wyrm code. |
| `test_eval_modules.py` | `WYRM_PATH` resolution (default + override), package `__init__.wy` loading, `::` module/submodule access, `from ... import`, `using` (bulk and aliased-single-name forms). |
| `test_eval_io.py` | `wyrm_io.py`'s primitives: write/read round-trip, `lseek`, `dup2` handle aliasing (shared file position), `close`/`flush`, and that a closed handle raises. |
| `test_cli.py` | The *installed* `wyrm` console script via `subprocess` - arg packing (including args that look like flags), all four exit-code/error paths. Skipped if the `wyrm` console script isn't installed. |
| `test_lsp.py` | `diagnostics_for_source()` directly (no JSON-RPC/server involved): clean source -> no diagnostics, a syntax error -> exactly one diagnostic on the right (0-indexed) line, and every bundled sample fixture is confirmed diagnostic-free. |
| `test_compiler_c.py` | `wyrm --compile` (`compiler_c.py`): structural checks on generated C for leaf functions, tail and non-tail calls (including calls inside `if`/`while` bodies), and `native::block` splices; one `CompileError` per documented v1 scope cut; an optional `gcc -fsyntax-only` check against the sibling `wyrm` repo's real headers when both are available. |

## Known gaps

Things that are deliberately unimplemented rather than silently wrong (each
raises `NotImplementedError`/`TypeError` with a clear message at the point
it'd be needed):

- **Coroutines** (`co`) are stored (`Coroutine`) but not drivable - no
  `yield`-as-suspend, no resumption/`send`.
- **`init`-based construction** - `new Cls(args)` only works with zero args
  (slots are filled from their declared defaults); passing constructor args
  requires running `init()` with `this` bound, which is method-body
  execution triggered by construction rather than by `!` - not wired up.
- **Attribute access** (`.`) only reads a `ClassInstance`'s slots
  (`this.x` / `obj.x`); there's no assignment (`this.x = v`) and no `.` on
  `Module`/`Class` (use `::` for those, as the spec does).
- **Slot `setter`/`getter` options** are parsed (`SlotOption`) but not
  consulted - direct slot access never runs a custom setter/getter.
- Diamond inheritance uses a simple base-classes-first linearization for
  both `all_slots()` and message-dispatch distance, not a real C3 MRO.
- **`wyrm-lsp` is diagnostics-only** - no hover, go-to-definition, or
  completion. Those need source positions on AST nodes (currently only a
  handful of leaf nodes carry a `pos`, and nothing propagates it through
  most rules) and a real symbol table; the tree-walking evaluator doesn't
  build one since it just runs code directly against a `dict` scope.

Anything not listed above that you'd expect to work and doesn't is a real
gap worth filing, not an intentional omission - `eval_expr`/`eval_stmt`'s
final fallback (`cannot evaluate <NodeType>`) is the tell.

`wyrm --compile` (`compiler_c.py`) has its own, much narrower, set of
deliberate v1 scope cuts (calls nested inside larger expressions, `float`,
`for`, classes, coroutines, collections, multi-module compilation) - see
"The compiler" above rather than this list, since they're compile-time-only
and don't affect the interpreter.
