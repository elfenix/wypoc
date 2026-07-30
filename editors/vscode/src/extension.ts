import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

let client: LanguageClient | undefined;

/**
 * Finds the wyrm-lsp executable to spawn, in priority order:
 *   1. the `wyrm.lsp.serverPath` setting, if set. `${workspaceFolder}` is
 *      substituted with the first workspace folder's path if present - VS
 *      Code does NOT do this automatically for arbitrary extension
 *      settings (only built-in contexts like launch.json/tasks.json get
 *      that for free), so this extension has to do it itself to let the
 *      setting stay portable across machines/checkouts.
 *   2. `${workspaceFolder}/.venv/bin/wyrm-lsp` - this repo's own dev layout
 *      (see wypoc/README.md: `pip install -e ".[lsp]"` creates it there);
 *   3. `wyrm-lsp` on PATH, for anyone who installed it globally/elsewhere.
 * Returns null if none of the above resolve to an existing file (case 3 is
 * handed to the OS to resolve, so it's always "found" as far as this
 * function is concerned - PATH resolution failure surfaces later, as the
 * client failing to start, since Node has no cheap portable `which`).
 */
function resolveServerCommand(): string | null {
  const folders = vscode.workspace.workspaceFolders;
  const firstFolderPath = folders && folders.length > 0 ? folders[0].uri.fsPath : undefined;

  const config = vscode.workspace.getConfiguration("wyrm");
  let configured = config.get<string>("lsp.serverPath", "").trim();
  if (configured) {
    if (configured.includes("${workspaceFolder}")) {
      if (!firstFolderPath) {
        vscode.window.showWarningMessage(
          "Wyrm: wyrm.lsp.serverPath uses ${workspaceFolder} but no folder is open."
        );
        return null;
      }
      configured = configured.replaceAll("${workspaceFolder}", firstFolderPath);
    }
    return configured;
  }

  if (folders) {
    const exeName = process.platform === "win32" ? "wyrm-lsp.exe" : "wyrm-lsp";
    const binDir = process.platform === "win32" ? "Scripts" : "bin";
    for (const folder of folders) {
      const candidate = path.join(folder.uri.fsPath, ".venv", binDir, exeName);
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    }
  }

  return "wyrm-lsp";
}

export function activate(context: vscode.ExtensionContext): void {
  const config = vscode.workspace.getConfiguration("wyrm");
  if (!config.get<boolean>("lsp.enable", true)) {
    return;
  }

  const command = resolveServerCommand();
  if (!command) {
    return;
  }

  const serverOptions: ServerOptions = {
    command,
    args: [],
  };

  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: "file", language: "wyrm" }],
  };

  client = new LanguageClient("wyrmLsp", "Wyrm Language Server", serverOptions, clientOptions);

  client.start().then(undefined, (err: unknown) => {
    vscode.window.showWarningMessage(
      `Wyrm: couldn't start wyrm-lsp (${command}). Diagnostics will be unavailable. ` +
        `Run \`pip install -e ".[lsp]"\` in the wyrm repo, or set wyrm.lsp.serverPath. (${err})`
    );
  });

  context.subscriptions.push({ dispose: () => void client?.stop() });
}

export function deactivate(): Thenable<void> | undefined {
  return client?.stop();
}
