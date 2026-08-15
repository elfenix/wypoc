import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";

/**
 * Reading (and writing) the `wyrm.*` settings, plus turning them into the
 * concrete things the rest of the extension needs: an executable to spawn
 * and an environment to spawn it in.
 *
 * Nothing here touches the language client or the UI, so the resolution
 * rules stay in one place and both the LSP spawn and the "run this file"
 * terminal go through them.
 */

export const SECTION = "wyrm";

/** Executable basenames, `.exe`-suffixed on Windows. */
export const INTERPRETER_EXE = process.platform === "win32" ? "wyrm.exe" : "wyrm";
export const SERVER_EXE = process.platform === "win32" ? "wyrm-lsp.exe" : "wyrm-lsp";
export const DAP_EXE = process.platform === "win32" ? "wyrm-dap.exe" : "wyrm-dap";

/** `bin/` on POSIX, `Scripts/` in a Windows virtualenv. */
export const VENV_BIN = process.platform === "win32" ? "Scripts" : "bin";

export function config(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration(SECTION);
}

export function workspaceFolders(): readonly vscode.WorkspaceFolder[] {
  return vscode.workspace.workspaceFolders ?? [];
}

function firstFolderPath(): string | undefined {
  return workspaceFolders()[0]?.uri.fsPath;
}

/**
 * Expands a path written by a human into one the OS will accept:
 * `${workspaceFolder}` (VS Code only substitutes that for free in built-in
 * contexts like launch.json, never in arbitrary extension settings, so we
 * do it ourselves to keep settings portable across checkouts), a leading
 * `~`, and plain relative paths - all resolved against the first workspace
 * folder. Returns null when the value needs a workspace folder and there
 * isn't one.
 */
export function expandPath(value: string): string | null {
  let result = value.trim();
  if (!result) {
    return null;
  }

  const folder = firstFolderPath();
  if (result.includes("${workspaceFolder}")) {
    if (!folder) {
      return null;
    }
    result = result.replaceAll("${workspaceFolder}", folder);
  }
  if (result === "~" || result.startsWith("~/")) {
    result = path.join(os.homedir(), result.slice(1));
  }
  // A bare name like `wyrm` is a PATH lookup, not a relative path - only
  // something with a separator in it is meant to be resolved against the
  // workspace.
  if (!path.isAbsolute(result) && result.includes(path.sep)) {
    if (!folder) {
      return null;
    }
    result = path.resolve(folder, result);
  }
  return result;
}

/**
 * The reverse of expandPath for the folder picker: store a path inside the
 * workspace as `${workspaceFolder}/...` so the setting survives being
 * committed and opened on another machine.
 */
export function portablePath(fsPath: string): string {
  const folder = firstFolderPath();
  if (!folder) {
    return fsPath;
  }
  const relative = path.relative(folder, fsPath);
  if (relative && !relative.startsWith("..") && !path.isAbsolute(relative)) {
    return path.posix.join("${workspaceFolder}", relative.split(path.sep).join("/"));
  }
  return fsPath;
}

/**
 * Where a setting write should land: the workspace when one is open (these
 * are project settings - a WYRM_PATH is a property of the project, not of
 * the user), otherwise the user's global settings so the command still
 * does something useful in a single-file window.
 */
export function writeTarget(): vscode.ConfigurationTarget {
  return workspaceFolders().length > 0
    ? vscode.ConfigurationTarget.Workspace
    : vscode.ConfigurationTarget.Global;
}

// -- WYRM_PATH ---------------------------------------------------------

/** The raw `wyrm.modulePath` entries, unexpanded, as stored. */
export function modulePathEntries(): string[] {
  return config().get<string[]>("modulePath", []).filter((entry) => entry.trim());
}

export async function setModulePathEntries(entries: string[]): Promise<void> {
  await config().update("modulePath", entries, writeTarget());
}

/**
 * The value to hand the interpreter/server as WYRM_PATH: every configured
 * entry expanded and joined the way wyrm_modules.search_paths() splits it,
 * with the ambient WYRM_PATH appended when `wyrm.modulePathInheritEnvironment`
 * is on (so a shell-level path keeps working and the setting adds to it
 * rather than silently replacing it). Entries that need a workspace folder
 * and don't have one are dropped. Returns undefined when there is nothing
 * to set, so callers can leave the variable off entirely.
 */
export function modulePathValue(): string | undefined {
  const separator = process.platform === "win32" ? ";" : ":";
  const entries: string[] = [];
  for (const entry of modulePathEntries()) {
    const expanded = expandPath(entry);
    if (expanded) {
      entries.push(expanded);
    }
  }
  if (config().get<boolean>("modulePathInheritEnvironment", true)) {
    const inherited = process.env.WYRM_PATH;
    if (inherited) {
      entries.push(...inherited.split(separator).filter(Boolean));
    }
  }
  // Preserve first-wins order while dropping duplicates - search order is
  // meaningful, a repeated root is just noise.
  const unique = [...new Set(entries)];
  return unique.length > 0 ? unique.join(separator) : undefined;
}

/** The child-process environment for anything wyrm we spawn. */
export function environment(): NodeJS.ProcessEnv {
  const value = modulePathValue();
  return value === undefined ? { ...process.env } : { ...process.env, WYRM_PATH: value };
}

// -- executables -------------------------------------------------------

/** Candidate `.venv/<bin>/<exe>` paths across the open workspace folders. */
function venvCandidates(exe: string): string[] {
  return workspaceFolders().map((folder) =>
    path.join(folder.uri.fsPath, ".venv", VENV_BIN, exe)
  );
}

/**
 * The configured `wyrm` interpreter, or null if the setting is empty or
 * unresolvable. Only the setting - discovery of unconfigured interpreters
 * is the interpreter picker's job (interpreter.ts), deliberately not
 * something that happens implicitly behind the user's back.
 */
export function interpreterPath(): string | null {
  return expandPath(config().get<string>("interpreterPath", ""));
}

export async function setInterpreterPath(value: string): Promise<void> {
  await config().update("interpreterPath", value, writeTarget());
}

/**
 * The `wyrm-lsp` executable to spawn, in priority order:
 *   1. `wyrm.lsp.serverPath`, if set;
 *   2. the `wyrm-lsp` sitting next to the selected `wyrm.interpreterPath` -
 *      picking an interpreter out of a venv should get you that venv's
 *      server, not some other checkout's;
 *   3. `<workspaceFolder>/.venv/<bin>/wyrm-lsp` - this repo's own dev
 *      layout (`pip install -e ".[lsp]"` puts it there);
 *   4. `wyrm-lsp` on PATH.
 * Case 4 is handed to the OS to resolve, so it is always "found" here -
 * a PATH miss surfaces later as the client failing to start, since Node
 * has no cheap portable `which`.
 */
export function serverCommand(): string {
  const configured = expandPath(config().get<string>("lsp.serverPath", ""));
  if (configured) {
    return configured;
  }

  const interpreter = interpreterPath();
  if (interpreter) {
    const sibling = path.join(path.dirname(interpreter), SERVER_EXE);
    if (fs.existsSync(sibling)) {
      return sibling;
    }
  }

  for (const candidate of venvCandidates(SERVER_EXE)) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  return SERVER_EXE;
}

/**
 * The `wyrm-dap` executable to spawn for a debug session, same priority
 * order as `serverCommand()`'s `wyrm-lsp` lookup:
 *   1. `wyrm.dap.serverPath`, if set;
 *   2. the `wyrm-dap` sitting next to the selected `wyrm.interpreterPath`;
 *   3. `<workspaceFolder>/.venv/<bin>/wyrm-dap` (this repo's own dev layout);
 *   4. `wyrm-dap` on PATH.
 */
export function dapCommand(): string {
  const configured = expandPath(config().get<string>("dap.serverPath", ""));
  if (configured) {
    return configured;
  }

  const interpreter = interpreterPath();
  if (interpreter) {
    const sibling = path.join(path.dirname(interpreter), DAP_EXE);
    if (fs.existsSync(sibling)) {
      return sibling;
    }
  }

  for (const candidate of venvCandidates(DAP_EXE)) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  return DAP_EXE;
}

/**
 * The command to actually run a `.wy` file with: the selected interpreter,
 * else this workspace's venv, else `wyrm` on PATH.
 */
export function runCommand(): string {
  const configured = interpreterPath();
  if (configured) {
    return configured;
  }
  for (const candidate of venvCandidates(INTERPRETER_EXE)) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return INTERPRETER_EXE;
}

/** True when a settings change touches anything the server spawn depends on. */
export function affectsServer(event: vscode.ConfigurationChangeEvent): boolean {
  return [
    "wyrm.lsp.serverPath",
    "wyrm.lsp.enable",
    "wyrm.interpreterPath",
    "wyrm.modulePath",
    "wyrm.modulePathInheritEnvironment",
  ].some((key) => event.affectsConfiguration(key));
}
