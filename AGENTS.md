# Agent Development Guide

A file for [guiding coding agents](https://agents.md/). This repository is worked on by
multiple agent tools (Claude, Mistral, others) — keep instructions here generic and
tool-agnostic; put tool-specific setup in that tool's own config, not here.

## What This Is

`wypoc` is a **Python proof-of-concept** for the wyrm language: a PEG grammar (via
[`pegen`](https://github.com/we-like-parsers/pegen), CPython's own PEG parser generator),
a hand-rolled tokenizer, a small typed AST, and a tree-walking interpreter, wired up as
an installable `wyrm` command plus a minimal LSP server.

It exists to validate the language design in `doc/grammar.ebnf` / `doc/language-spec.md`
against real parsing and execution, quickly and cheaply, in a language (Python) that's
fast to iterate in. **It is not wyrm's production implementation.** That's the C11 object
system/VM in the separate `wyrm` repository (separate git history) — an
embeddable QObject-equivalent (stable identity, property system, signal/slot dispatch,
introspectable type hierarchy) with a Scheme-inspired scripting surface. This repo was
split out of `wyrm` specifically so PoC/interpreter work can move fast without being
held to that repo's handcrafted / minimal LLM environment requirements.

**Practical implication:** when the language design changes, the intended flow is
PoC here first (grammar + interpreter, cheap to change) → prove it out → port the
settled design decision into wyrm design documentation. Don't treat this interpreter's
Python object model (plain `int`/`float`/`str`/`dict`/etc., closures as Python closures)
as a spec for wyrm's actual value representation — see `doc/design.md` for that
(primitive value = type tag + register-sized value; heap objects — `str`, `table`,
`fiber`, `type` — inherit from a common object type). This repo's job is syntax and
semantics validation, not memory layout or dispatch performance.

## Source of Truth for the Language

- `doc/grammar.ebnf` — the reference EBNF grammar. Authoritative for syntax, with one
  exception below.
- `doc/language-spec.md` — prose spec with worked examples (message dispatch, modules,
  classes, control flow, etc.).
- `doc/design.md` — the wider Wyrm Object Extensions design (goals, inspirations, data
  model) — the "why", shared with `wyrm/doc/design.md`.
- `wypoc/wyrm.gram` — the actual `pegen` grammar this interpreter runs on. Its header
  comment documents every place it *deliberately* deviates from `grammar.ebnf` (e.g.
  `expression` vs `assign_expr`/`expr_list` splitting to avoid a real parsing ambiguity).
  Read that header before assuming `grammar.ebnf` is 100% authoritative on an edge case.

## Project Layout

```
doc/                 language spec / grammar / design docs (see above)
editors/vscode/       syntax highlighting + LSP client extension
wypoc/
  corelib/             small wyrm-language standard library (shapes.wy, std/, wyrm/),
                        installed as package data
  wyrm_tokenizer.py    hand-rolled lexer -> tokenize.TokenInfo stream
  wyrm.gram            pegen grammar source
  parser.py            GENERATED from wyrm.gram — do not hand-edit (see Commands)
  actions.py           helpers used by wyrm.gram's grammar actions
  ast_nodes.py         typed AST node dataclasses
  parse.py             glues tokenizer + generated parser together
  wyrm_eval_parse_tree.py   the tree-walking evaluator (the interpreter proper)
  wyrm_modules.py      WYRM_PATH search-path resolution
  wyrm_io.py           POSIX-ish low-level I/O primitives exposed to wyrm
  cli.py               the `wyrm` command
  lsp.py               the `wyrm-lsp` language server (diagnostics only)
  samples/             *.wy fixtures used by test/
test/                  pytest suite (see "Commands")
```

See `wypoc/README.md` for the full pipeline walkthrough (tokenizer → pegen parser → AST →
tree-walking eval), the message-dispatch (`!`) implementation notes, and the current list
of known gaps (coroutines not drivable, no `init`-based construction with args, no
attribute assignment, slot getter/setter options parsed but unused, diamond inheritance
uses a simplified linearization, LSP is diagnostics-only).

## Commands

```bash
python -m venv .venv && .venv/bin/python -m pip install -e ".[lsp,dev]"   # setup

.venv/bin/pytest                                       # run the whole test suite
.venv/bin/pytest test/test_grammar.py                  # run one test file

.venv/bin/wyrm path/to/script.wy                       # run a .wy script
.venv/bin/python -m pegen wypoc/wyrm.gram -o wypoc/parser.py -q   # regenerate parser.py
                                                                    # after editing wyrm.gram
```

`parser.py` is generated output — edit `wypoc/wyrm.gram`, then regenerate; never
hand-edit `parser.py` directly. After any grammar change, run `test/test_grammar.py`
first (it parses every `samples/*.wy` fixture and fails loudly on syntax errors).

**`pytest` must run clean (zero failures) at all times.** Any change that breaks a test
is not done until the test suite passes again — either fix the code, or update the test
if the behavior change was intentional. Don't leave a known-broken test in place, and
don't skip/xfail a test to work around a real regression instead of fixing it.

## Coding Standards

- Standard Python (3.10+), plain stdlib data structures — no need to model wyrm's real
  value/type system faithfully, that's the point of a PoC.
- One dataclass per grammar construct in `ast_nodes.py`; `Node.__str__` is generic via
  `dataclasses.fields`, don't write per-node pretty-printers.
- Tests live under `test/` and use pytest (plain `assert`, fixtures, `pytest.mark.parametrize`);
  keep new tests in that style. Shared helpers (`eval_sample`, `SAMPLES_DIR`, ...) live in
  `test/conftest.py`.
- Keep `wypoc/corelib/`'s wyrm-language stdlib written in wyrm itself (`.wy` files), not Python.

## Issue and PR Guidelines

- Never create an issue or PR. If asked, decline and explain that this repository
  requires issues and PRs to be created by the developer directly.

## Working Across Agents

This repo is used by more than one agent tool (Claude, Mistral, etc.) — don't assume
another session's agent-specific memory, config, or conversational context. Anything an
agent needs to work here should be discoverable from this file, `doc/`, and
`wypoc/README.md` — if you find yourself relying on something else, consider whether it
belongs in one of those instead.

If you keep local, non-committed navigation/context notes for yourself (a `gen/`
scratch folder — already `.gitignore`d here), don't assume another agent or session has
seen them; they're personal scratch, not a shared doc.
