# wypoc

A Python proof-of-concept interpreter for the wyrm language. See `AGENTS.md`
for an overview of what this repo is and how it relates to wyrm, and
`wypoc/README.md` for the full pipeline walkthrough (tokenizer -> parser ->
AST -> evaluator).

## Setup

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[lsp,dev]"
```

This installs the `wyrm` command and `wyrm-lsp` language server into
`.venv/bin/`, plus the `wyrm-lsp` dependency (`pygls`).

## Running a script

```bash
.venv/bin/wyrm wypoc/samples/eval_functions.wy
```

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
