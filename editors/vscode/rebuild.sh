#!/usr/bin/env bash
# Rebuilds the wyrm-lang VS Code extension (TypeScript compile + vsix
# package) and reinstalls it, so a syntaxes/wyrm.tmLanguage.json or
# src/extension.ts edit actually takes effect.
#
# The packaged .vsix is a point-in-time snapshot - editing the grammar or
# extension source does NOT update an already-installed extension on its
# own, only an unpacked Extension Development Host window (F5) re-reads
# those files live. Run this after any such edit if you're testing against
# a normal, installed VS Code window instead.
#
# Usage:
#   ./rebuild.sh              compile, package, install, done
#   ./rebuild.sh --no-install compile and package only, skip `code --install-extension`
#
# After it finishes, reload any VS Code window that already had the
# extension loaded (Ctrl+Shift+P -> "Developer: Reload Window") - installing
# a new version doesn't hot-swap it into windows opened before the install.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

do_install=1
if [[ "${1:-}" == "--no-install" ]]; then
    do_install=0
fi

if ! command -v npx >/dev/null 2>&1; then
    echo "rebuild.sh: npx not found (need Node/npm on PATH)" >&2
    exit 1
fi

if [[ ! -d node_modules ]]; then
    echo "==> node_modules missing, running npm ci"
    npm ci
fi

# vsce looks for a LICENSE file next to package.json; pull the repo's
# root license in rather than duplicating it (this copy is gitignored).
cp ../../LICENSE ./LICENSE

# NOT --no-dependencies: `out/extension.js` requires vscode-languageclient at
# load time, so it has to ship inside the .vsix. Without it the extension
# fails to activate with MODULE_NOT_FOUND and every command it contributes
# reports "command not found" - the manifest lists them, but the module that
# registers them never loaded.
echo "==> packaging (npm run compile + vsce package)"
npx --yes @vscode/vsce package --allow-missing-repository

vsix=$(ls -t wyrm-lang-*.vsix | head -1)
echo "==> built $vsix"

if [[ "$do_install" -eq 1 ]]; then
    installed_any=0

    if command -v code >/dev/null 2>&1; then
        echo "==> installing $vsix (code)"
        code --install-extension "$vsix"
        installed_any=1
    else
        echo "rebuild.sh: 'code' CLI not found, skipping VS Code install" >&2
    fi

    if command -v codium >/dev/null 2>&1; then
        echo "==> installing $vsix (codium)"
        codium --install-extension "$vsix"
        installed_any=1
    else
        echo "rebuild.sh: 'codium' CLI not found, skipping VSCodium install" >&2
    fi

    if [[ "$installed_any" -eq 0 ]]; then
        echo "rebuild.sh: neither 'code' nor 'codium' CLI found, skipping install (pass --no-install to silence this)" >&2
        exit 1
    fi

    echo "==> done - reload any already-open VS Code/VSCodium window to pick it up"
else
    echo "==> --no-install passed, skipping install"
fi
