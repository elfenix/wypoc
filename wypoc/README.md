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
tools/generate_parser.py  regenerates parser.py from wyrm.gram
  actions.py              small helpers used by wyrm.gram's grammar actions
  ast_nodes.py            typed AST node classes built by the parser actions
  parse.py                glues the tokenizer + generated parser together
  wyrm_eval_parse_tree.py the tree-walking evaluator (the interpreter proper)
  sexpr.py                the s-expression wire format a tree crosses in/out of
                           wyrm code (what a decorator sees) - see below
  compiler_c/             wyrm --compile: translates a module to C (see below)
  wyrm_modules.py         WYRM_PATH search-path resolution (no eval/parse dependency)
  wyrm_io.py              POSIX-ish low-level I/O primitives (__open/__read/...)
  symbols.py              static symbol table for one parsed module
  symbol_index.py         cross-file symbol lookup (following imports)
  completion.py           completion candidates for a position (see below)
  cli.py                  the `wyrm` command (installed via pyproject.toml)
  config.py               ~/.wyrm/config: the TOML `[wyrm]` section holding the
                           defaults for the REPL's options (read at startup,
                           written by `--config` / `:set config`)
  repl.py                 the interactive REPL: session, options, "is this
                           entry finished?", and the readline front end
  pretty.py               multi-line renderings of a result (lisp pairs, JSON
                           dicts/arrays, class-definition-shaped instances)
  repl_tui.py             `wyrm --tui`: the same REPL as a Textual full-screen
                           UI - log (selectable/copyable), prompt, status bar
  lsp.py                  the `wyrm-lsp` language server
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
   |      v
   |   C source text targeting the real wyrm VM calling convention
   |
   +-- symbols.build(tree)                                   [wyrm-lsp: analyze it]
          v
       a SymbolTable of declarations/references, which symbol_index.py
       joins across files (following imports) for the editor
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
array literal, `{` doubles as wyrm's dict literal, and `$[` is the cons-list
(pair-list) sigil; wyrm also has operators (`::`, `?=`, `<=>`, `!`, `$`) stdlib `tokenize` has
never heard of, and `$` is one of its identifier characters (`$ast`, `reg$0`)
on top of being an operator - a `$` starts a name only when a letter follows
it, which is what keeps `$[1, 2]` the pair-list sigil it always was. `wyrm_tokenizer.py` implements wyrm's own lexical rules
directly (see `doc/grammar.ebnf` section 0), including Haskell/Python-style
layout (INDENT/DEDENT) and the rule that brace-delimited blocks (`{ }`) opt
out of the layout algorithm entirely for their contents, while still
treating NEWLINE/`;` as statement separators. It still yields ordinary
`tokenize.TokenInfo` tuples, so `pegen`'s generated parser (which expects
that shape) doesn't need to know anything changed.

### Why pegen

`pegen` is CPython's own PEG parser generator (the tool that produces
CPython's real parser from `Grammar/python.gram`). `wyrm.gram` is written in
its grammar DSL, and compiles into `parser.py`, a plain
recursive-descent-with-memoization parser class. **`parser.py` is generated -
edit `wyrm.gram`, then regenerate:**

```bash
.venv/bin/python tools/generate_parser.py
```

Note this is `tools/generate_parser.py`, not plain `python -m pegen`. The
grammar annotates its actions with pegen's `LOCATIONS` magic to attach
source spans to AST nodes, and what `LOCATIONS` expands to is set by the
generator's `location_formatting` - which pegen's own CLI hardcodes to
CPython's four-keyword `lineno=/col_offset=/end_lineno=/end_col_offset=`
shape. wyrm nodes take a single `pos` 4-tuple instead, so the format has to
be overridden, and that's only reachable through the generator API. A parser
built with plain `python -m pegen` raises `TypeError` from every action
rather than silently losing positions.

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
prints. `Node.children()`/`Node.walk()` are generic in the same way, so a
consumer can traverse the tree without a visitor method per node type.

**Source positions.** Every node carries a `pos` span - a `(line, col,
end_line, end_col)` 4-tuple, 1-based lines and 0-based columns, the
convention `tokenize.TokenInfo` already uses. Nodes whose identifier is a
bare `str` rather than a child node also carry a `name_pos` for just that
identifier: `FnDef.pos` spans the whole function including its body, while
`FnDef.name_pos` is the few columns a "go to definition" jump should land
on. Where a node holds a *list* of bare names (`Import.path`,
`TypeExpr.parts`, `FnDef.class_target`) there's a parallel `<field>_pos`
list, one span per name, indexed in step with it.

Spans come from the grammar actions: `LOCATIONS` (pegen's own magic name)
for a rule's whole extent, `tok_pos(n)` for one NAME token. Rules that
collect several names therefore yield raw tokens, which their consumers
split with `names()`/`spans()` from `actions.py`. Left-folded constructs
(`a + b + c`, `obj.a.b`, `x!m()`) are built one link at a time, so
`fold_left`/`fold_postfix` widen each link's span back to where the chain
started rather than leaving it covering only the last operator.

Spans are always optional - a node built by hand has `pos=None`, and
consumers must tolerate that - but the parser fills in every one, which
`test/test_positions.py` enforces across every sample. `__str__` hides all
position fields, so printed trees stay readable.

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

### Decorators and the s-expression bridge

`@dec(args) X` hands `X`'s syntax tree to `dec` and evaluates whatever tree
`dec` answers in `X`'s place. This is the language's macro system, and it
leans on the AST being homoiconic: a decorator is ordinary wyrm code that
reads and builds trees.

Three pieces make it work.

- **`sexpr.py`** — the wire format, both directions, from one table (`ROWS`).
  A node is a pair list whose head is a symbol naming its kind:
  `1 + 2` is `$['binop, '+, $['int, 1], $['int, 2]]`. The format has no
  boolean fields on purpose — a distinction in what a node *is* becomes a
  kind of its own (`'defer`/`'defer_on`, `'catch`/`'catch_return`,
  `'true`/`'false`), and a distinction in what role a child *plays* becomes a
  position (a `*rest` parameter). Where this AST spells such a distinction as
  a field, a row carries the value that field takes, which is the one place
  the two spellings meet. A construct the format doesn't carry fails by name
  (`a coroutine cannot cross into a decorator yet`) rather than crossing
  half-translated. The module docstring lists where this AST's shape forces a
  difference from the reference implementation's.
- **`TreeBase` and `sexpr(x)`** (`wyrm_eval_parse_tree.py`) — a decorator is
  `fn [TreeBase] name(...)`, so the tree it receives is boxed as a real class
  instance and found by ordinary message dispatch. `sexpr(x)` is three cases
  in order: a class that answers `__sexpr` is asked first and its answer
  taken, a `TreeBase` unwraps, and anything else passes through — so it is
  the identity on an s-expression that already is one. That ordering is what
  lets one decorator's source run against either representation of a tree.
  It's an unqualified builtin rather than a module member deliberately: a
  decorator's source shouldn't differ by an import between the two worlds.
- **`expand_decorated`** — the rewrite itself, done once per `Decorated` AST
  node and cached on it. That's this interpreter's stand-in for the reference
  implementation's "decorators run at compile time": a decorated definition
  nested in a function is rewritten on the first call and reused thereafter,
  so what the decorator answered is fixed for the life of the parsed tree.

Reaching a decorator written in wyrm needs `import static m`, which is an
ordinary import plus adopting `m`'s messages — a decorator name is a
*selector*, never a path (`@traced`, not `@m::traced`), so a plain import
leaves it unreachable no matter that the module is loaded. The `static`
constraint itself (no closures over a live environment, no coroutines) is
recorded, not enforced: this interpreter runs a module's top level on import
either way.

`foo::$ast` is a definition's own tree, boxed the same way, which is what
lets a template be written as ordinary wyrm rather than assembled node by
node — the template is a real function and `...` marks where the decorated
body lands. It resolves through the *binding*, so it describes the definition
after decoration.

`@__dump X` (prints the s-expression, compiles `X` unchanged) and
`@__identity X` (rebuilds `X` from its s-expression, so every use is a full
round trip) are native, and need no `import static`.

`samples/decorators.wy` runs every node kind through `@__identity` and then
through the wyrm-written decorators in `samples/decolib.wy`;
`test/test_sexpr.py` covers the bridge on its own, including each kind's
documented shape and each failure mode.

### The compiler (`wyrm --compile`)

`compiler_c/` is a second, alternative backend over the same `ast_nodes.py`
tree the evaluator walks: instead of running a module, it translates it to C -
a real step toward wyrm's stated goal of self-hosting with C as its "assembly
language" (see `doc/language-spec.md`'s "Native Code" section).

It targets the **native calling convention of the wyrm-language interpreter**,
which is the same shape that interpreter's own builtins have:

```c
bool w_{module}_{fn}(wyrm_lang_vm* vm, wyrm_value* args,
                     wyrm_uword argc, wyrm_value* out);
```

`false` out means it failed, with the error already recorded; otherwise the
result goes through `*out`. Inside the body a wyrm local is an **ordinary C
variable** of its declared type, so arithmetic compiles to arithmetic and
boxing happens only at the boundaries - parameters in, call arguments out, the
result. Each module ends with a `{MODULE}_BUILTINS[]` table naming its
functions, so a host installs the whole module by walking one array.

This convention can hold a C stack frame across a call, and that one fact is
what makes the generated code look like the wyrm it came from: `if` is `if`,
`while` is `while`, `break` is `break`, and a call is a call - anywhere in an
expression. An earlier version of this backend targeted the object-system VM's
*resumable* convention instead, where a function could not, and paid for it
with a graph of `static` chunks per function, locals living in fixed
value-stack slots, branches as pending-function jumps, and a restriction that
a call could only appear as a whole statement or a bare `return`. All of that
is gone; `compiler_c/DESIGN.md` has the before/after table and the reasoning.

Still a narrow slice, not a general compiler:

- A module must `import native` to be compile-eligible at all (this is also
  what the spec says marks a module as compile-only, not for runtime
  interpretation) and must be single-file (no `import`/`from`).
- Types are `int`/`uint`/`bool`/`float`. `str` and the collection types need a
  story for GC-managed values in generated code first - a `wyrm_value` holding
  a heap object has to be reachable by the collector, which the scalar types
  sidestep entirely.
- `fn` bodies get arithmetic/comparison/boolean expressions,
  `if`/`elif`/`else`, `while`, `break`/`continue`, `return`, and calls to
  other compiled functions in the same module. There is no type inference, so
  a local needs a declared type: `var x: int = 1`, not `x := 1`.
- A call may appear anywhere in an expression. It compiles to a statement
  assigning a temporary, which two constructs care about: `and`/`or` rebuild
  their short circuit around a `bool` temporary when the right operand hoists
  anything (C's `&&` would have run it unconditionally), and a `while` whose
  condition hoists becomes `for (;;) { <hoisted> if (!cond) break; ... }` so
  the condition is still re-evaluated each iteration.
- `class` defs compile to a builder function returning the interpreter's class
  object: typed slots, each with a constant default (or its type's zero
  value). Inheritance, slot options, and class-body methods are not supported.
- `native::block('PORTION, $[inputs...], $[outputs...], R"tag(...)tag")` works
  at module top level (spliced into the matching `HEADER`/`TYPES`/
  `CONSTANTS`/`PROTOS`/`FUNCTIONS` output section) and as a function-body
  statement. In a body it needs no marshalling at all - a wyrm local is
  already a C local of the same name - so the declared input/output lists are
  checked and then recorded as a comment.
- `for`, messages (`fn [Cls] ...`), coroutines, `defer`, `try`/`catch`,
  `static` locals, collections, lambdas, and multi-module compilation are
  unimplemented - each raises `CompileError` with a specific message, the same
  "fail loud, not silently wrong" convention as the interpreter's own known
  gaps below.

One thing the output needs that the target interpreter doesn't yet offer: a
way to *append* to its builtin table. The generated registration table is that
interpreter's own row type, so installing a compiled module is a small hook on
its side - and the only part of the output that isn't already compilable
against what it exposes.

```bash
.venv/bin/wyrm --compile module.wy              # C source to stdout
.venv/bin/wyrm --compile -o module.c module.wy  # ...or to a file
```

`test/test_compiler_c.py` covers the supported fixtures
(`wypoc/samples/compile_*.wy`) and one `CompileError` case per documented
scope cut, and then hands every fixture to a real C compiler with
`-Wall -Werror` against `test/native/lang_internal.h` - a stub of exactly the
interpreter surface the output is allowed to depend on. That catches what
string assertions miss (a mismatched brace, a value read through the wrong
union field, a call with the wrong arity) and keeps the dependency explicit:
widening what the compiler emits means widening that header first. It is
deliberately a stub rather than the real header from a sibling checkout, so
the check can't quietly stop running for whoever doesn't have one.

### `corelib/` and the `wyrm` command

`wypoc/corelib/` is a small standard library written in wyrm, demonstrating
the module system: `shapes.wy` (single-file module), `std/__init__.wy`
(package marker), `std/io.wy` (a submodule, using the `!`-less plain-`fn`
form - `println`). `prelude.wy` is the one exception to "reached via
`import`" - it's parsed once and run into every fresh scope directly by
`populate_globals` (`wyrm_eval_parse_tree.py`), the same way the
Python-level builtins (`car`/`cdr`/`substr`/...) are, so its definitions
(e.g. `co range(begin, end) -> int`) are available with no import, despite
being real wyrm source rather than Python. It's declared as package data
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

`wypoc/lsp.py` is a [`pygls`](https://github.com/openlawlibrary/pygls)-based
language server. On every `didOpen`/`didChange`/`didSave` it re-parses the
document with the exact same `wypoc.parse.parse()` everything else in this
package uses, publishes any `SyntaxError` as a diagnostic, and hands the
resulting AST to the symbol layer below. Five features today - diagnostics,
`documentSymbol` (outline), `definition` (including across files), `hover`,
and `completion`; no references or rename yet (see "Known gaps").

`lsp.py` itself is only a protocol adapter. The analysis lives in three
modules that know nothing about LSP, so all three are testable without a
server:

- **`symbols.py`** - a pure AST pass over one parsed module. It records
  what the file *declares* (`Symbol`: nested, so a class owns its slots and
  methods, with a `name_pos` to jump to and a rendered `detail` line for
  hover), what it *references* (`Reference`, tagged by how the name was
  used - plain name, `!` message, `::` scope, type annotation), and how
  each `import` decomposes into individually navigable pieces
  (`ImportBinding`). The evaluator can't be reused for this: a `Scope` only
  holds what execution has reached, whereas an editor needs every
  declaration in a file that may not even run yet.
- **`symbol_index.py`** - the part that needs a filesystem. It caches symbol
  tables per file (open documents take priority over what's on disk, since
  an unsaved buffer is what the user is looking at) and resolves imports
  through `wyrm_modules.resolve_module_file`, the same function the
  interpreter uses - so a jump the editor offers is a jump the interpreter
  would make.
- **`completion.py`** - candidates for a position. See below.

Import navigation is per *piece*, not per statement: in
`import std::io::(println as p)`, `std` goes to `std/__init__.wy`, `io` to
`std/io.wy`, and `println` to the `fn println` declaration inside it. A use
of `p` later in the file follows the alias through to that same
declaration, and so does `io::println`. A bare `import a::b::c` is
ambiguous - the leaf is a module if one exists and a symbol exported by
`a::b` otherwise - and resolves in the same order `eval_import` tries at
runtime.

Go-to-definition on a `!` message returns *every* overload rather than one
location (`fn [Canvas] draw` and a `Canvas` class body's `fn draw` are both
overloads of one generic function), which is what an editor's
pick-a-definition list is for. Hover on such a message lists them all.

**Completion** is the one feature that has to answer about source that does
not parse, and it is built in two halves for exactly that reason. The
*context* - which trigger the cursor sits after and what has been typed since
- is read from the **raw text**, scanning backwards from the cursor; text
always parses. The *candidates* come from a symbol table, which needs a
parse, so they come from the last version of the document that parsed
(`SymbolIndex.last_good_table`). A document being edited was almost always
valid a keystroke ago, and a moment ago's declarations are the right answer
while the current word is half-typed. For a document that has *never* parsed
in the session - a freshly opened file with a dangling `obj.` - the dangling
fragment is dropped and the parse retried, then the line replaced with
`pass`, then dropped entirely.

What each trigger offers:

| after | candidates |
|---|---|
| `!` or `@` | message selectors: every `fn [Cls] name` overload and class-body method in this file and its imports, plus the native ones. `@` because a decorator's name *is* a selector |
| `::` | the members of whatever module the `::` chain names, resolved through this file's imports the way go-to-definition resolves the same chain |
| `.` | slot names, from this file and the modules it imports |
| nothing | names in scope innermost-first, then module level, then imported names, then the interpreter's own globals, then keywords |

Two deliberate choices worth knowing. `.` offers *every* known slot rather
than one class's: there's no type inference here, so narrowing `obj.` would
mean guessing - a superset the editor filters by prefix is honest, and each
candidate's `detail` names the class it came from so a wrong one is visible.
And the interpreter's globals are derived by asking `populate_globals` what
it installs rather than from a second list, so a builtin added to
`wyrm_builtins.py` or `corelib/prelude.wy` appears in completion with no
change to `completion.py`.

A diagnostic covers the whole token the parse tripped over and names it
(`unexpected ':'`, `unexpected end of line`) rather than marking a single
column as "invalid syntax" - see `wypoc/parse.py`'s `syntax_error`, which
builds a SyntaxError with `end_lineno`/`end_offset` from the furthest token
the parser reached. Errors raised by the tokenizer rather than the parser
(unterminated string, bad dedent) carry only a point, and fall back to a
one-character range. There is still at most **one** diagnostic per parse:
pegen has no error recovery, so the parse stops at the first problem.

The
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
| `test_grammar.py` | Parses every `samples/*.wy` fixture and fails loudly on any syntax error - the grammar's own regression suite. Run this after any `wyrm.gram` change (post-regeneration). Plus `is not` (built as the `not` of a check, so `a is not T` and `not a is T` are the same tree) and `$` as an identifier character: names that contain one, `$[` still lexing as the pair-list sigil, `foo::$ast` as a tree reference against `foo::$line` as a plain lookup, and `$ast` refused as an ordinary name everywhere else. |
| `test_eval.py` | Basic single/multi-target assignment, literals, string escaping/raw/multiline decoding, arithmetic, and the bitwise family - unary `~`/`+`, the `<<`/`>>` shifts, how tightly each binds, and a negative shift count answering an error value rather than raising. |
| `test_eval_functions.py` | Calls, default args, `*args`, lambdas, `if`/`elif`/`else` as return-bearing control flow. |
| `test_eval_control_flow.py` | `while` + `break`/`continue`, `for`/`else` (Python-style: `else` runs only if the loop wasn't broken out of), early `return` from inside a loop. |
| `test_eval_classes.py` | Class hierarchy metadata (`bases`, `slots`, `methods`), slot inheritance/overriding via `all_slots()`, per-instance slot storage via `new`, and that `new` with constructor args is a clear `NotImplementedError` (needs `init` dispatch). |
| `test_eval_messages.py` | The full `!` story: class-body method auto-registration, plain-fn-to-Method promotion, single- vs. tuple-receiver `this` binding, bare-slot-name access in method bodies, left-to-right most-specific-wins resolution, `BoundMessage`. |
| `test_eval_builtins.py` | `expose`/`expose_all`/`builtin` - handing Python callables/values to wyrm code. |
| `test_eval_modules.py` | `WYRM_PATH` resolution (default + override), package `__init__.wy` loading, `::` module/submodule access, `from ... import`, `import`'s wildcard and aliased-single-name forms. |
| `test_eval_io.py` | `wyrm_io.py`'s primitives: write/read round-trip, `lseek`, `dup2` handle aliasing (shared file position), `close`/`flush`, and that a closed handle raises. |
| `test_cli.py` | The *installed* `wyrm` console script via `subprocess` - arg packing (including args that look like flags), every exit-code/error path, and the bare-`wyrm` REPL driven over a pipe (including `--tui`'s fallback off a terminal), plus `--config` as a mode of its own (writing options, listing them, refusing to be combined with something to run) and a configured `tui`/`compact` reaching the REPL. Skipped if the `wyrm` console script isn't installed. |
| `test_pretty.py` | `pretty.py`: a pair list as lisp (including an improper tail), dicts/arrays in JSON layout, a class instance always rendering as a block in its definition's shape (and a container holding one having to break), where the breaks and indentation land at a given width, and the fallback to the one-line spelling for everything else. |
| `test_config.py` | `config.py`: the defaults `[wyrm]` in `~/.wyrm/config` supplies a session, an unknown option / wrong-typed value / unparseable file each being reported and skipped rather than fatal, how `name=value` assignments are read, `set_option` creating the directory and file and round-tripping the rest of the document (comments and all) while refusing to overwrite a file that isn't TOML, and `:set config NAME` writing as well as setting. |
| `test_repl.py` | `repl.py`: which entries are still being typed (unclosed bracket/string, a trailing `:`, an unfinished block) versus ready to run, and `Session.evaluate` - values echoed (and nil not), a binding answering with what it bound, bindings/functions/classes/imports surviving from one entry to the next, captured output, every error kind coming back as a `Result` rather than an exception, and the `:set`/`:unset` options (`compact` off by default, bare `:set` listing state, an unknown name reported rather than ignored), with defaults coming from `config.py`. |
| `test_repl_tui.py` | `repl_tui.py` through Textual's headless test pilot: `enter` running a finished entry and extending an unfinished one, `shift+enter` always inserting a newline, `ctrl+o`/`F6` moving between prompt and log (each keeping a shadow cursor when the other has focus, and the log highlighting its cursor line only while focused), multi-line-aware history recall on `up`/`down` and `ctrl+up`/`ctrl+down`, multi-line entries collapsed to `...` in the log, output/errors/status, `:quit`, and the top-to-bottom widget order. Plus the log as a place to read from: mouse drag selection and `ctrl+c` copy, click-to-focus with `enter` handing focus back, the wheel scrolling the backlog (and new entries returning to the tail), and the submitted entry rendering as an inverted band. Skipped without `textual`. |
| `test_lsp.py` | `lsp.py`'s feature functions directly (no JSON-RPC/server involved). Diagnostics: clean source -> no diagnostics, a syntax error -> exactly one diagnostic on the right (0-indexed) line, covering the whole offending token and naming it, a tokenizer-level error still getting a visible range, and every bundled sample fixture confirmed diagnostic-free. Navigation: outline nesting and parameter filtering, definition into another file through an `import` (against the real corelib), hover content, and 0-based LSP position conversion. |
| `test_symbols.py` | `symbols.py` over one module: which declarations are found and how they nest, signature rendering, message-overload collection, how each `import` form decomposes into navigable bindings (and which of them actually bind a local name), reference tagging, and innermost-scope name resolution. |
| `test_symbol_index.py` | `symbol_index.py` across files, against a throwaway module tree under `tmp_path`: every `import` form resolving to the right file/declaration, aliases and `::` chains followed through to the original, wildcard `except`, open documents shadowing disk, and unparseable files answering empty instead of raising. |
| `test_positions.py` | Source spans on the AST: every node of every sample carries a well-formed `pos`, `name_pos` covers just the identifier while `pos` covers the whole construct, parallel `<field>_pos` lists line up with their name lists, and folded chains (`a + b * c`, `this.origin.x`, `shape!area()`) span the whole expression. Run this after any `wyrm.gram` change - a new rule that forgets `LOCATIONS` fails here. |
| `test_completion.py` | `completion.py`: reading the trigger/prefix out of raw text (including a decimal point that isn't an attribute access), what each trigger offers, the scope ordering, a loop variable's range, and that a document which doesn't parse - or never has - still answers. Plus the `lsp.py` adapter's replacement range and kind mapping. |
| `test_sexpr.py` | `sexpr.py` on its own: each node kind's documented s-expression shape, that every kind round-trips, the irregular cases (`elif` as a nested `if`, the `*rest` position, qualified types, a union collapsing), and each failure mode - a construct the format lacks, and a malformed s-expression coming back. |
| `test_eval_decorators.py` | `samples/decorators.wy` end to end: every kind through `@__identity`, definitions rebuilt and still binding, decorators written in wyrm (`samples/decolib.wy`) rewriting bodies and reading signatures, templates and `$ast`, the `__sexpr` hook - plus the failure modes a sample can't reach (an unreachable decorator, a non-tree answer, a qualified name, the once-per-node rewrite). |
| `test_compiler_c.py` | `wyrm --compile` (`compiler_c/`): structural checks on the generated C - the calling convention, argument checking, control flow, nested calls, short-circuit lowering, floats, class builders, `native::block` splices, the registration table; one `CompileError` per documented scope cut; and every fixture run through a real C compiler with `-Wall -Werror` against `test/native/lang_internal.h`. |

## Known gaps

Things that are deliberately unimplemented (or only partially implemented)
rather than silently wrong (each raises `NotImplementedError`/`TypeError`
with a clear message at the point it'd be needed, or is called out below):

- **`super()`** doesn't evaluate - single-dispatch "call up the inheritance
  tree" isn't wired up yet, unlike the rest of message dispatch.
- **Multi-value unpack from a single multi-valued expression** isn't
  supported - `a, b := f()` (or `a, b = f()`) requires `len(targets) ==
  len(values)`; it can't unpack one call that itself returns a tuple. This
  affects a few of the spec's own worked examples (e.g.
  `greeting, name := arguments`).
- **`do`/`defer`/`with` are basic-use implementations**, not fully
  conforming (see wyrm_eval_parse_tree.py's `run_scoped_block`/
  `Scope.defers`): `do:`'s value is only threaded through when its last
  statement is a bare expression (a block ending in `if`/`while`/`for`
  evaluates to `nil`, since those don't yet propagate a value themselves -
  see below); `defer` is tied to whichever block Scope it's lexically
  written in (an `if`/`while`/`for` body, not only the enclosing function
  call), so a defer inside a loop body fires once per iteration rather than
  once per call; `with` declares an immutable binding but doesn't do any
  of the type-checking the spec's type constraints imply elsewhere either.
- **`if`/`while`/`for` don't produce a value** the way the spec's "Like
  other statements... produce the value of the last statement executed"
  note describes - `eval_stmt` returns a value only for a bare expression
  statement, not for compound statements. This is what limits `do:`'s value
  threading above.
- **Slot `setter`/`getter` options** are parsed (`SlotOption`) but not
  consulted - direct slot access never runs a custom setter/getter.
- **Decorated classes** aren't supported: `@dec class Foo:` parses, but a
  class has no kind in the s-expression format (slots and methods need more
  than a name and a body before they can cross), so it fails at the crossing
  rather than at the parse. Coroutines are the same - `'co` is unbuilt, so a
  `co` does not cross in either direction.
- **The `static` in `import static` is recorded, not enforced** - nothing
  checks that a statically imported module could not create a dynamic frame.
  See "Decorators" above for why that constraint buys less here than in a
  compiled implementation.
- **Properties and messages don't yet occupy separate namespaces** - the
  spec says `.` should error on a name that's only a message, but
  `wyrm_eval_parse_tree.py`'s attribute lookup doesn't distinguish the two.
- Diamond inheritance uses a simple base-classes-first linearization for
  both `all_slots()` and message-dispatch distance, not a real C3 MRO.
- **`wyrm-lsp` has no references or rename** - diagnostics, outline,
  definition, hover, and completion work (see above). References and rename
  need the reverse index (which files use a name), which means walking the
  workspace rather than only the files reached through imports.
- **Name resolution is span containment, not a real scope chain** - see
  `symbols.py`'s "Known simplifications". A name resolves against the
  declarations of each construct whose source range encloses the reference,
  innermost first, then module level. That's right for parameters, locals,
  and module-level definitions, but it doesn't model declaration *order*
  within a block (a name declared later in the same block still matches)
  and has no notion of a name being unbound at the point of use.
- **Attribute and slot references resolve by name only** - there's no type
  inference, so `a.x` can't be narrowed to one class's `x`. `.`-access
  currently resolves to nothing rather than guessing.
- **Only one syntax error is reported per parse** - pegen has no error
  recovery, so the parse stops at the first problem rather than resyncing
  to report several. Multiple simultaneous squiggles would most naturally
  come from a semantic pass (undefined name, redeclaration, unresolvable
  import) once the symbol table above exists, since that pass has no reason
  to stop at the first finding.

Anything not listed above that you'd expect to work and doesn't is a real
gap worth filing, not an intentional omission - `eval_expr`/`eval_stmt`'s
final fallback (`cannot evaluate <NodeType>`) is the tell. Coroutines are
fully drivable (`next`/`send`/`yield from`), `init`-based construction
with real constructor arguments works, and attribute assignment
(`this.x = v` / `obj.x = v`) works - none of those are gaps, despite what
older notes here may have implied.

`wyrm --compile` (`compiler_c/`) has its own, much narrower, set of
deliberate scope cuts (`str` and the collection types, `for`, messages,
coroutines, `defer`/`try`, multi-module compilation) - see "The compiler"
above rather than this list, since they're compile-time-only and don't affect
the interpreter.
