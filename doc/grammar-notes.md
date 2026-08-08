# Grammar design notes

Rationale for the less-obvious choices in `doc/grammar.ebnf`, kept out of
that file so it stays terse enough to actually hold in your head. Sections
below match the EBNF's own numbering. Concrete usage examples live in
`language-spec.md` (in the `wyrm` project); this file is *why*, not *how
to write wyrm code*.

## 2. Blocks and statements

**Everything is an expression.** The spec states this outright: "Every
statement produces a result... The statement rule is valid for all
special statements: `fn`, `class`, `do`, `with`, `import`," and
`if`/`while`/`for` are documented to "produce the value of the last
statement executed" (nil if none ran) - the same rule `do` gets. Most of
this grammar's shape follows from taking that literally.

- **No `simple_stmt`/`compound_stmt` split.** They used to be separate
  productions, unioned by a `statement` nonterminal that had no behavior
  of its own. `stmt_line` inlines that union directly. A real
  recursive-descent implementation still cares about the underlying
  distinction - a block-shaped alternative (`if_stmt`, `fn_def`, ...) ends
  in its own terminator (DEDENT/`}`), so nothing follows it within one
  `stmt_list` slot, while the others need an explicit `stmt_sep` after
  them (see `wyrm.gram`'s `stmt_item` vs `simple_stmt_line`) - but that's
  a parsing concern, not something the grammar needs to encode.

- **`block`'s one-liner arm takes `stmt_line`, not just a "simple"
  statement.** So `if foo: if bar: if baz:` `NEWLINE` `INDENT ...` is
  ordinary recursion, not a special case: after `:`, NEWLINE routes to
  the indented-suite arm and anything else routes to the one-liner arm,
  whatever that one statement turns out to be.

- **`decorator` is `{ decorator } , X`, not self-recursion**, so stacked
  decorators are just repetitions, applied innermost-first:
  `@outer() @inner() fn f(): pass` applies `inner` first, then `outer` to
  the result (same as Python). `decorator` is the one production in this
  grammar that consumes an optional trailing NEWLINE itself, rather than
  leaving it to `stmt_list`'s `stmt_sep` - because decorator + target is
  one production (one `stmt_list` slot), not two statements joined by a
  separator, so nothing else is positioned to eat that newline.

- **`return`/`yield`/`break`/`continue`/`pass` are `primary` alternatives,
  not dedicated `*_stmt` forms.** Each is just a keyword (plus, for
  return/yield, an optional operand) with nothing else to it, so there's
  no separate grammar left to write once `expr_stmt` already covers "a
  bare expression as a statement." This also makes them reachable
  wherever `try`/`catch` reach in the precedence chain, for the same
  reason `try` itself is expression-shaped: a control-transfer form can
  fire from arbitrarily deep inside an expression, so it should be usable
  arbitrarily deep (wrapped in parens as needed), not only at statement
  position. Precedent: Rust's `return`/`break`/`continue` are expressions
  for the same reason. (INFERRED - the spec's prose only shows these at
  statement position, or `return` inside `catch`, but never forbids
  further nesting.)

- **`if`/`while`/`for` are `primary` alternatives** - directly backed by
  the spec's "produce the value of the last statement executed" rule,
  the same treatment `do_expr` already got.

- **`fn_def`/`co_def`/`class_def` stay statement-only.** A name is
  mandatory for each, so using one is inherently a binding declaration
  (same category as `var`/`assignment`/`static`/`import`/`with_block`),
  not a value production - unlike a bare keyword, embedding a *named*
  declaration at arbitrary expression depth means resolving
  target/binding-vs-expression ambiguity at every nesting level, not just
  adding one more `primary` alternative. Their *anonymous* forms have no
  such LHS - nothing to bind, pure values - so those do get primaries:
  `lambda_expr`/`co_lambda_expr` (`fn(...){...}`/`co(...){...}`, no name)
  and `class_expr` (`class{...}`/`class(Base){...}`, no name), each
  structurally identical to its named counterpart minus the identifier.

- **`var_stmt`/`assignment_stmt`/`static_stmt`/`with_stmt_simple`/
  `with_block`/`import_stmt`/`defer_stmt` stay statement-only** for the
  same LHS-isn't-one-expression-slot reason, and because the spec never
  documents a value for them (contrast: it explicitly does for literals,
  collections, `if`/`while`/`for`, and `do`). The existing grammar
  already draws this line elsewhere - `?=` was deliberately given
  expression-position access (`set_if_unset_expr`), while plain `=`/`:=`
  were not.

- **`slot_def` is a `stmt_line` alternative**, the same way `static_stmt`
  already was despite being fn/class-body-specific - so a class body is
  just `block`, no separate `class_block`/`class_member` productions.
  (This also fixed a real bug: the old `class_block`'s indented arm never
  consumed a separator between members, unlike its own brace arm and
  unlike `slot_options` right below it - `block` inherits correct
  separator handling for free.)

- **`nil` was missing entirely** - not in `literal`, not in `primary`, not
  in the keyword list - despite being a documented fundamental type and
  the spec's own Literals/Atoms grammar listing `literal_nil` alongside
  bool/string/symbol/number. Added `nil_literal` next to `bool_literal`.

- **`from_import_stmt` was dead code** - defined, never referenced.
  `import_stmt`'s own header note confirms `from` was folded into the
  `import` forms during an earlier consolidation; this was leftover
  cruft from before that. Deleted.

## 3. Modules and imports

`module_path` and `qualified_name` were the same production
(`identifier , { "::" , identifier }`) under two names - one for import
paths, one for decorator names. Merged into `module_path`, used by both.

`type_expr` looks identical to `module_path` today but is being kept
separate deliberately - it's expected to grow into its own small DSL
(richer type expressions beyond a bare `::`-path), so collapsing it into
`module_path` now would just have to be undone later.

## 4-5. Functions and coroutines

`co_param_list`'s leading `"<-" , type_constraint` is the type of
whatever value is sent in through each `yield` expression - not a
bindable parameter (no name, never supplied by the caller by position).

## 6. Classes

No dedicated constructor syntax: the constructor is an ordinary method
named `init`. Slots without an explicit default are zero-valued (nil ref
/ 0 / false, per type) before `init` runs.

## 7. Expressions

No `new` keyword: `MyClass(args)` is ordinary `call_op` on a class value
(a class is constructed by calling it like any other callable, binding
`this` to a fresh zero-valued instance; an error returned from `init`
overrides the constructed instance - RAII). Falls out of `postfix_expr`
applied to an identifier naming a class; no dedicated `primary` form
needed - same reasoning that keeps `defined(...)` *out* of this grammar
(it isn't in the canonical spec at all; `x is Unset` is how definedness
is actually checked).

`defer_stmt`'s `[ "on" , type_constraint ]` - not a fixed `"on" "error"`
literal - because the spec shows `defer on error | nil:` as legal:
`error` there is just the ordinary built-in `error` type flowing through
the normal `type_constraint` rule, not a second reserved word.

`catch_expr`'s handler is a plain `or_expr`, not a special-cased
`"return" , [ expression ] | or_expr` - `return` reaches through there
like any other primary, so `EXPR catch return OTHER` needs no dedicated
grammar.

`target`'s trailing `{ "[" , expression , "]" }` suffixes are how
`arr[i] = x` / `grid[i][j] = x` mutate in place rather than only ever
rebuilding; only a plain identifier target is legal with `:=` (it
declares a fresh name, so there's nothing to index into yet).
