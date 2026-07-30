# Wyrm VS Code extension

TextMate grammar (syntax highlighting) for `.wy` / `.wyrm` files, derived
from `doc/language-spec.md` (formal grammar tracked in `doc/grammar.ebnf`),
plus an LSP client that spawns `wyrm-lsp` (from `wypoc/`, this repo's
Python proof-of-concept - see `wypoc/README.md`) for live syntax
diagnostics: parse errors are reported as red squiggles as you type, using
wypoc's own tokenizer/parser as the sole source of truth.

## Try it locally

Recent VS Code versions (this was tested against 1.130.0) only load "user"
extensions listed in `extensions.json` — they no longer auto-discover an
arbitrary symlinked folder dropped into `~/.vscode/extensions`, so that
older trick silently does nothing (no error, it just won't appear in
`code --list-extensions` or the Extensions view). Package and install it
for real instead:

```sh
# 1. Build the LSP server's dependencies (from the repo root):
.venv/bin/python -m pip install -e ".[lsp]"

# 2. Build and package the extension:
cd editors/vscode
npm install
npm run compile
npx --yes @vscode/vsce package    # -> wyrm-lang-<version>.vsix

# 3. Install it:
code --install-extension wyrm-lang-<version>.vsix
```

Fully quit and restart VS Code (not just "Reload Window" — the extension
list is read at startup) and open a `.wy` file. Diagnostics should appear
automatically; if they don't, check the "Wyrm Language Server" output
channel (Output panel) and see "Language server" below.

Confirm it's actually installed with `code --list-extensions` — you should
see `amarach.wyrm-lang`.

**Iterating on the extension**: after editing `src/extension.ts` or the
grammar, re-run `npm run compile` (or `npm run watch` in the background),
repackage with `vsce package`, and reinstall with
`code --install-extension --force wyrm-lang-<version>.vsix` (`--force` lets
you reinstall the same version without bumping it each time). There's no
live-reload path for a normal (non-debug) window — for fast iteration, use
VS Code's actual extension-development flow instead: open this folder in
VS Code and press F5 to launch an Extension Development Host with the
current `src/` live (no packaging needed there).

## Files

- `package.json` — extension manifest: registers the `wyrm` language, the
  `wyrm-lang` grammar, the `wyrm.lsp.*` settings, and the
  `vscode-languageclient` dependency.
- `language-configuration.json` — comments, brackets, auto-closing pairs.
- `syntaxes/wyrm.tmLanguage.json` — TextMate grammar (tokenization rules).
- `src/extension.ts` — activation entry point: resolves and spawns
  `wyrm-lsp`, wires it up via `vscode-languageclient`.
- `tsconfig.json` — compiles `src/*.ts` to `out/*.js` (the `main` package.json
  points at); both `node_modules/` and `out/` are gitignored, build them
  locally per "Try it locally" above.

## Language server

`src/extension.ts` looks for the `wyrm-lsp` executable in this order:

1. the `wyrm.lsp.serverPath` setting, if set;
2. `${workspaceFolder}/.venv/bin/wyrm-lsp` — this repo's own dev layout
   (what `pip install -e ".[lsp]"` from the repo root produces);
3. `wyrm-lsp` on `PATH`.

Set `wyrm.lsp.enable` to `false` to disable the language server entirely
(syntax highlighting still works via the grammar alone). The server itself
(`wypoc/lsp.py`) is diagnostics-only right now: no hover, go-to-definition,
or completion — see `wypoc/README.md`'s "Known gaps" for why (mainly:
`ast_nodes.py` doesn't track source positions on most nodes yet, so there's
no symbol table to answer those questions from).

## Known gaps

The spec itself is still in flux in a few places (constructor syntax,
`elif`/`else` are not shown explicitly — see `doc/grammar.ebnf` for
what's inferred vs. documented). The grammar highlights on a best-effort
basis and does not attempt indentation-aware layout tokenization —
TextMate grammars are regex/line based and cannot reproduce the
Haskell-style layout rule; block structure highlighting relies on the
brace form and on `language-configuration.json`'s indent heuristics.
