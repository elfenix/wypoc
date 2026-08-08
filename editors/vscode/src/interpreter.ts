import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

import * as config from "./config";

/**
 * Choosing which `wyrm` runs your code: discovery of the interpreters on
 * this machine, the quick-pick that selects one, and the status bar item
 * that shows the current choice while a wyrm file is open.
 *
 * Discovery only ever *offers* interpreters - nothing here writes a setting
 * unless the user picks something. An unset `wyrm.interpreterPath` stays
 * unset, and `config.runCommand()` falls back to the workspace venv/PATH.
 */

interface Candidate {
  /** Absolute path to a `wyrm` executable. */
  fsPath: string;
  /** Where it came from, shown as the quick-pick item's description. */
  origin: string;
}

function isExecutableFile(fsPath: string): boolean {
  try {
    const stat = fs.statSync(fsPath);
    if (!stat.isFile()) {
      return false;
    }
  } catch {
    return false;
  }
  if (process.platform === "win32") {
    return true; // no execute bit to check; the extension match is the test
  }
  try {
    fs.accessSync(fsPath, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * Every `wyrm` we can find, best guess first: the current setting, each
 * workspace folder's `.venv`, the venv VS Code itself was launched from
 * (`VIRTUAL_ENV`), then every directory on PATH. Deduplicated by real path
 * so a symlinked venv doesn't show up twice.
 */
export function discover(): Candidate[] {
  const found: Candidate[] = [];
  const seen = new Set<string>();

  const add = (fsPath: string | null, origin: string): void => {
    if (!fsPath || !isExecutableFile(fsPath)) {
      return;
    }
    let key = fsPath;
    try {
      key = fs.realpathSync(fsPath);
    } catch {
      // Unreadable link - fall back to the literal path as the identity.
    }
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    found.push({ fsPath, origin });
  };

  add(config.interpreterPath(), "current setting");

  for (const folder of config.workspaceFolders()) {
    add(
      path.join(folder.uri.fsPath, ".venv", config.VENV_BIN, config.INTERPRETER_EXE),
      `${folder.name}/.venv`
    );
  }

  const activeVenv = process.env.VIRTUAL_ENV;
  if (activeVenv) {
    add(path.join(activeVenv, config.VENV_BIN, config.INTERPRETER_EXE), "$VIRTUAL_ENV");
  }

  const pathDirs = (process.env.PATH ?? "").split(path.delimiter).filter(Boolean);
  for (const dir of pathDirs) {
    add(path.join(dir, config.INTERPRETER_EXE), "PATH");
  }

  return found;
}

interface InterpreterItem extends vscode.QuickPickItem {
  /** The value to store, or null for the "browse" and "clear" entries. */
  fsPath?: string;
  action?: "browse" | "clear";
}

/**
 * The "Wyrm: Select Interpreter" command. Shows what discovery found, plus
 * an explicit file-dialog escape hatch and a way to go back to
 * auto-detection. Writes `wyrm.interpreterPath` (workspace scope when a
 * folder is open) and returns true if the setting changed, so the caller
 * can restart the language server.
 */
export async function selectInterpreter(): Promise<boolean> {
  const current = config.interpreterPath();
  const items: InterpreterItem[] = discover().map((candidate) => ({
    label: candidate.fsPath === current ? `$(check) ${candidate.fsPath}` : candidate.fsPath,
    description: candidate.origin,
    fsPath: candidate.fsPath,
  }));

  items.push({
    label: "$(folder-opened) Browse...",
    description: "pick a wyrm executable from disk",
    action: "browse",
  });
  if (current) {
    items.push({
      label: "$(clear-all) Use auto-detection",
      description: "clear wyrm.interpreterPath (workspace .venv, then PATH)",
      action: "clear",
    });
  }

  const picked = await vscode.window.showQuickPick(items, {
    title: "Select Wyrm Interpreter",
    placeHolder: current ? `Current: ${current}` : "No interpreter set (auto-detecting)",
    matchOnDescription: true,
  });
  if (!picked) {
    return false;
  }

  if (picked.action === "clear") {
    await config.setInterpreterPath("");
    vscode.window.showInformationMessage("Wyrm: interpreter reset to auto-detection.");
    return true;
  }

  let chosen = picked.fsPath;
  if (picked.action === "browse") {
    const uris = await vscode.window.showOpenDialog({
      title: "Select the wyrm executable",
      openLabel: "Select interpreter",
      canSelectFiles: true,
      canSelectFolders: false,
      canSelectMany: false,
      defaultUri: current ? vscode.Uri.file(path.dirname(current)) : undefined,
    });
    chosen = uris?.[0]?.fsPath;
  }
  if (!chosen) {
    return false;
  }

  // Store venv interpreters portably: a `.venv` inside the project is the
  // common case, and an absolute path there would break for everyone else
  // who opens the repo.
  await config.setInterpreterPath(config.portablePath(chosen));
  return true;
}

/**
 * What to call an interpreter in the status bar, where there's room for a
 * word rather than a path: the environment's name for one living in a venv
 * (`.../myproj/.venv/bin/wyrm` -> `.venv`), the bare executable name
 * otherwise.
 */
function shortLabel(command: string): string {
  const parent = path.dirname(command);
  if (path.basename(parent) === config.VENV_BIN) {
    return path.basename(path.dirname(parent));
  }
  return path.basename(command);
}

/**
 * A status bar entry showing the interpreter in effect, visible only while
 * a wyrm document is in the active editor. Clicking it opens the picker.
 */
export class InterpreterStatus {
  private readonly item: vscode.StatusBarItem;

  constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    this.item.command = "wyrm.selectInterpreter";
  }

  refresh(): void {
    const editor = vscode.window.activeTextEditor;
    if (editor?.document.languageId !== "wyrm") {
      this.item.hide();
      return;
    }

    const configured = config.interpreterPath();
    const command = config.runCommand();
    this.item.text = `$(zap) Wyrm: ${shortLabel(command)}`;
    this.item.tooltip = new vscode.MarkdownString(
      [
        `**Wyrm interpreter**: \`${command}\``,
        configured ? "" : "_(auto-detected - click to choose one)_",
        "",
        `**WYRM_PATH**: \`${config.modulePathValue() ?? "(unset)"}\``,
      ].join("\n\n")
    );
    this.item.show();
  }

  dispose(): void {
    this.item.dispose();
  }
}
