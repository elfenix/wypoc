# wypoc

A Python proof-of-concept interpreter for the wyrm language. See `AGENTS.md`
for an overview of what this repo is and how it relates to wyrm, and
`wypoc/README.md` for the full pipeline walkthrough (tokenizer -> parser ->
AST -> evaluator).

## Setup

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[lsp,repl,dev]"
```

This installs the `wyrm` command and `wyrm-lsp` language server into
`.venv/bin/`, plus the `wyrm-lsp` dependency (`pygls`) and the REPL's
(`rich`, `textual`).

## Running a script

```bash
.venv/bin/wyrm wypoc/samples/eval_functions.wy
```

## The REPL

```bash
.venv/bin/wyrm            # readline prompt
.venv/bin/wyrm --tui      # full-screen UI (-t works too)
```

`wyrm` with no script starts an interactive session, the way plain `python`
does. Everything is evaluated in one scope, so a function, class or import
from one entry is still there for the next, and a value that isn't `nil` is
echoed back - including a binding's, so `x := 5` answers `5`. (An assignment
through an attribute or an index - `p.x = 1`, `a[0] = 1` - stays quiet: it
binds no name, and re-evaluating the target to show it could repeat a call.)

An entry that isn't finished - an unclosed bracket, or a block header like
`fn add(a, b):` - keeps prompting for more; an empty line ends a block:

```
wyrm> fn add(a, b):
 ...>     return a + b
 ...>
wyrm> add(20, 22)
42
```

`--tui` puts the same session in a full-screen layout: a scrollable log on
top, then the prompt, then a status bar. `enter` runs the entry (or adds a
line, if it's one that would still be prompting above) and `shift+enter`
always adds a line. `ctrl+o` (or `F6`) moves between the prompt and the log.

`up`/`down` move by line inside the entry being typed and step into history
at its edges - `up` from the first line lands on the *last* line of the
previous entry, so holding `up` walks back through a multi-line entry and on
into the one before it. `ctrl+up`/`ctrl+down` (or readline's `alt+p`/`alt+n`)
skip a whole entry at a time; coming back down past the newest one restores
the half-typed entry you started from.

The log is a read-only text area rather than a dumb transcript, so it has a
cursor of its own and text can be taken out of it: select with the mouse or
with shift+arrows, `ctrl+c` copies, and the wheel scrolls back through the
session. Submitted entries are drawn as an inverted band, so scanning the
log for "what did I run?" is easy. `enter` (or `escape`) in the log hands
focus back to the prompt, and `ctrl+q` quits. `:help` lists the lot.

The TUI needs a real terminal and `textual`; without either, `--tui` says so
and falls back to the readline prompt.

### Results

A result is pretty-printed, broken across lines only when its one-line form
doesn't fit the terminal: a pair list in lisp form, a dict or array in JSON
layout, and a class instance in the shape of the class definition that would
declare it.

```
wyrm> $[1, $[2, 3], 4]
(1 (2 3) 4)
wyrm> p := Point()
wyrm> p
Point:
    x: 1.0
    y: 2.0
```

`:set compact` turns that off, going back to the one-line spelling `str()`
uses (`$[1, $[2, 3], 4]`); `:unset compact` turns it back on. `:set` with no
argument lists the options and their state.

`:help` lists the REPL's own commands (`:quit`, `:clear`, `:set`, `:unset`).

## Running the tests

```bash
.venv/bin/pytest
```

The suite (`test/`) is expected to run clean; see `AGENTS.md`.

## Editor support

`editors/vscode/` has a VS Code extension (syntax highlighting +
`wyrm-lsp` diagnostics). See `editors/vscode/README.md` to build and install
it; with the venv above set up, it auto-detects
`${workspaceFolder}/.venv/bin/wyrm-lsp` as long as you open VS Code on this
`wypoc` folder directly.
