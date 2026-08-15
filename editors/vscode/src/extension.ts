import * as vscode from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

import * as config from "./config";
import * as debug from "./debug";
import * as interpreter from "./interpreter";
import * as modulePath from "./modulePath";

/**
 * Activation entry point: spawns `wyrm-lsp` and wires it up via
 * `vscode-languageclient`, registers the commands that make the project's
 * settings editable without hand-writing JSON, and keeps a status bar item
 * showing which interpreter is in effect.
 *
 * Path/environment resolution all lives in config.ts; this file is only
 * responsible for lifecycle (start, restart on a settings change, stop).
 */

let client: LanguageClient | undefined;
let status: interpreter.InterpreterStatus | undefined;
let terminal: vscode.Terminal | undefined;
let terminalEnv: string | undefined;

/**
 * Starts the language server, unless disabled. The server reads WYRM_PATH
 * from its environment (wyrm_modules.search_paths), which is how the
 * `wyrm.modulePath` setting reaches cross-file go-to-definition and
 * completion - and why changing it means restarting the process.
 */
async function startClient(): Promise<void> {
  if (!config.config().get<boolean>("lsp.enable", true)) {
    return;
  }

  const command = config.serverCommand();
  const serverOptions: ServerOptions = {
    command,
    args: [],
    options: { env: config.environment() },
  };
  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: "file", language: "wyrm" }],
  };

  client = new LanguageClient("wyrmLsp", "Wyrm Language Server", serverOptions, clientOptions);
  try {
    await client.start();
  } catch (err: unknown) {
    vscode.window.showWarningMessage(
      `Wyrm: couldn't start wyrm-lsp (${command}). Diagnostics will be unavailable. ` +
        `Run \`pip install -e ".[lsp]"\` in the wyrm repo, pick an interpreter with ` +
        `"Wyrm: Select Interpreter", or set wyrm.lsp.serverPath. (${err})`
    );
  }
}

async function stopClient(): Promise<void> {
  const running = client;
  client = undefined;
  if (running) {
    try {
      await running.stop();
    } catch {
      // Already dead (a server that failed to start, say) - nothing to do,
      // and a restart must not be blocked by the corpse of the last one.
    }
  }
}

async function restartClient(): Promise<void> {
  await stopClient();
  await startClient();
}

/** Runs the active `.wy` file in a terminal, with WYRM_PATH applied. */
async function runFile(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.languageId !== "wyrm") {
    vscode.window.showWarningMessage("Wyrm: no wyrm file is active.");
    return;
  }
  if (editor.document.isDirty) {
    await editor.document.save();
  }

  // One reused terminal, but the environment is baked in when a terminal is
  // created, so a WYRM_PATH change has to get a fresh one rather than a
  // stale process pretending the setting took effect.
  const value = config.modulePathValue();
  if (!terminal || terminal.exitStatus || terminalEnv !== value) {
    terminal?.dispose();
    terminal = vscode.window.createTerminal({
      name: "Wyrm",
      env: value === undefined ? {} : { WYRM_PATH: value },
    });
    terminalEnv = value;
  }
  terminal.show();
  terminal.sendText(
    `${quoteArg(config.runCommand())} ${quoteArg(editor.document.uri.fsPath)}`
  );
}

/** Shell-quotes a path for the terminal (spaces in paths are normal). */
function quoteArg(value: string): string {
  if (process.platform === "win32") {
    return /\s/.test(value) ? `"${value}"` : value;
  }
  return /[^\w@%+=:,./-]/.test(value) ? `'${value.replaceAll("'", `'\\''`)}'` : value;
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  status = new interpreter.InterpreterStatus();
  status.refresh();

  context.subscriptions.push(
    status,
    vscode.commands.registerCommand("wyrm.selectInterpreter", async () => {
      if (await interpreter.selectInterpreter()) {
        status?.refresh();
        await restartClient();
      }
    }),
    // The explorer context menu passes the clicked resource (and, for a
    // multi-selection, all of them); the command palette passes nothing and
    // gets the folder dialog.
    vscode.commands.registerCommand("wyrm.addModulePathFolder", async (
      clicked?: vscode.Uri,
      selection?: vscode.Uri[]
    ) => {
      if (await modulePath.addFolders(selection ?? (clicked ? [clicked] : undefined))) {
        await restartClient();
      }
    }),
    vscode.commands.registerCommand("wyrm.editModulePath", async () => {
      if (await modulePath.edit()) {
        await restartClient();
      }
    }),
    vscode.commands.registerCommand("wyrm.restartServer", restartClient),
    vscode.commands.registerCommand("wyrm.runFile", runFile),
    vscode.commands.registerCommand("wyrm.debugFile", debug.debugFile),
    vscode.debug.registerDebugConfigurationProvider(
      "wyrm",
      new debug.WyrmDebugConfigurationProvider()
    ),
    vscode.debug.registerDebugAdapterDescriptorFactory(
      "wyrm",
      new debug.WyrmDebugAdapterDescriptorFactory()
    ),
    vscode.window.onDidChangeActiveTextEditor(() => status?.refresh()),
    vscode.workspace.onDidChangeConfiguration(async (event) => {
      if (config.affectsServer(event)) {
        status?.refresh();
        await restartClient();
      }
    }),
    // Adding or removing a folder changes both `${workspaceFolder}`
    // expansion and the `.venv` fallbacks.
    vscode.workspace.onDidChangeWorkspaceFolders(async () => {
      status?.refresh();
      await restartClient();
    }),
    { dispose: () => void stopClient() }
  );

  await startClient();
}

export function deactivate(): Thenable<void> | undefined {
  return stopClient();
}
