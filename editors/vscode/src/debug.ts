import * as vscode from "vscode";

import * as config from "./config";

/**
 * The `wyrm` debug type: spawns `wyrm-dap` (wypoc/dap/server.py) as a
 * DAP-over-stdio adapter, the same way the language client spawns
 * `wyrm-lsp` (see extension.ts's startClient) - VS Code owns the protocol
 * once the process exists, so there's no client library to wire up here,
 * just "what to run" (this file) and "what to run it with" (config.ts's
 * dapCommand/environment, shared with the LSP spawn and "Run Current File").
 *
 * Two pieces:
 *   - WyrmDebugConfigurationProvider fills in a launch config that's
 *     missing pieces (F5 with an empty/no launch.json debugs the active
 *     file, exactly like the `wyrm.debugFile` command does explicitly) and
 *     supplies the "Wyrm: Debug Current File" entry launch.json's
 *     "Add Configuration..." offers.
 *   - WyrmDebugAdapterDescriptorFactory says how to start the adapter
 *     process for a resolved session.
 */

export class WyrmDebugConfigurationProvider implements vscode.DebugConfigurationProvider {
  provideDebugConfigurations(
    _folder: vscode.WorkspaceFolder | undefined
  ): vscode.ProviderResult<vscode.DebugConfiguration[]> {
    return [
      {
        type: "wyrm",
        request: "launch",
        name: "Wyrm: Debug Current File",
        program: "${file}",
        stopOnEntry: false,
      },
    ];
  }

  async resolveDebugConfiguration(
    _folder: vscode.WorkspaceFolder | undefined,
    debugConfiguration: vscode.DebugConfiguration
  ): Promise<vscode.DebugConfiguration | null | undefined> {
    // F5 with nothing configured yet (no launch.json, or an empty one):
    // VS Code hands back an object with none of its own fields set. Fall
    // back to the active file, the same target `wyrm.debugFile` uses.
    if (!debugConfiguration.type && !debugConfiguration.request && !debugConfiguration.program) {
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.document.languageId !== "wyrm") {
        vscode.window.showWarningMessage("Wyrm: no wyrm file is active.");
        return undefined;
      }
      debugConfiguration.type = "wyrm";
      debugConfiguration.request = "launch";
      debugConfiguration.name = "Wyrm: Debug Current File";
      debugConfiguration.program = editor.document.uri.fsPath;
    }

    if (!debugConfiguration.program) {
      vscode.window.showErrorMessage("Wyrm: launch configuration needs a 'program'.");
      return undefined;
    }

    // A breakpoint set in an unsaved file isn't at the line the debugger
    // (reading the file fresh off disk) will think it's at.
    const document = vscode.workspace.textDocuments.find(
      (doc) => doc.uri.fsPath === debugConfiguration.program
    );
    if (document?.isDirty) {
      await document.save();
    }

    return debugConfiguration;
  }
}

/** `vscode.DebugAdapterExecutable`'s `env` wants `string`-only values; `process.env` (what
 * config.environment() is built from) types each entry as `string | undefined`. */
function stringEnv(env: NodeJS.ProcessEnv): { [key: string]: string } {
  const result: { [key: string]: string } = {};
  for (const [key, value] of Object.entries(env)) {
    if (value !== undefined) {
      result[key] = value;
    }
  }
  return result;
}

export class WyrmDebugAdapterDescriptorFactory implements vscode.DebugAdapterDescriptorFactory {
  createDebugAdapterDescriptor(
    _session: vscode.DebugSession,
    _executable: vscode.DebugAdapterExecutable | undefined
  ): vscode.ProviderResult<vscode.DebugAdapterDescriptor> {
    return new vscode.DebugAdapterExecutable(config.dapCommand(), [], {
      env: stringEnv(config.environment()),
    });
  }
}

/** The "Wyrm: Debug Current File" command / ▷▷ toolbar button. */
export async function debugFile(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.languageId !== "wyrm") {
    vscode.window.showWarningMessage("Wyrm: no wyrm file is active.");
    return;
  }
  if (editor.document.isDirty) {
    await editor.document.save();
  }

  await vscode.debug.startDebugging(vscode.workspace.getWorkspaceFolder(editor.document.uri), {
    type: "wyrm",
    request: "launch",
    name: "Wyrm: Debug Current File",
    program: editor.document.uri.fsPath,
  });
}
