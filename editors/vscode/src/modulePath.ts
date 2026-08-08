import * as vscode from "vscode";

import * as config from "./config";

/**
 * Editing `wyrm.modulePath` - the project's WYRM_PATH - without typing JSON
 * into settings.json.
 *
 * Two commands: one that goes straight to a folder dialog and appends what
 * you pick, and one that lists the current entries so you can add, remove
 * or reorder them. Both write workspace settings when a folder is open (a
 * search path is a property of the project) and return true if anything
 * changed, so the caller can restart the language server - the server reads
 * WYRM_PATH from its environment at spawn time, so a change only takes
 * effect on restart.
 */

/**
 * Append folders to `wyrm.modulePath`: the ones passed in (the explorer's
 * context menu hands us the folder that was right-clicked), or whatever a
 * folder dialog returns when called with none. Duplicates are skipped
 * rather than appended again; order is preserved because search order is
 * meaningful.
 */
export async function addFolders(given?: vscode.Uri[]): Promise<boolean> {
  const uris = given?.length
    ? given
    : await vscode.window.showOpenDialog({
        title: "Add folders to WYRM_PATH",
        openLabel: "Add to WYRM_PATH",
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: true,
        defaultUri: config.workspaceFolders()[0]?.uri,
      });
  if (!uris || uris.length === 0) {
    return false;
  }

  const entries = config.modulePathEntries();
  const added: string[] = [];
  for (const uri of uris) {
    const entry = config.portablePath(uri.fsPath);
    if (!entries.includes(entry)) {
      entries.push(entry);
      added.push(entry);
    }
  }
  if (added.length === 0) {
    vscode.window.showInformationMessage("Wyrm: already on WYRM_PATH.");
    return false;
  }

  await config.setModulePathEntries(entries);
  vscode.window.showInformationMessage(`Wyrm: added ${added.join(", ")} to WYRM_PATH.`);
  return true;
}

interface EntryItem extends vscode.QuickPickItem {
  entry?: string;
  /** "reopen" is synthetic: a button was pressed, re-render the list. */
  action?: "add" | "settings" | "reopen";
}

/**
 * The list view: every configured entry with its resolved location, plus
 * per-entry buttons to remove it or move it up (search order), and rows for
 * adding a folder or dropping into the raw settings UI.
 */
export async function edit(): Promise<boolean> {
  const remove: vscode.QuickInputButton = {
    iconPath: new vscode.ThemeIcon("trash"),
    tooltip: "Remove from WYRM_PATH",
  };
  const moveUp: vscode.QuickInputButton = {
    iconPath: new vscode.ThemeIcon("arrow-up"),
    tooltip: "Search this entry earlier",
  };

  let entries = config.modulePathEntries();
  let changed = false;

  // Loop rather than a single showQuickPick: removing an entry should
  // leave the list open on the updated list, the way a list editor behaves.
  for (;;) {
    const picked = await pickOne(entries, remove, moveUp, async (item, button) => {
      const at = entries.indexOf(item.entry!);
      if (at < 0) {
        return;
      }
      if (button === remove) {
        entries = entries.filter((_, i) => i !== at);
      } else if (button === moveUp && at > 0) {
        entries = [...entries];
        [entries[at - 1], entries[at]] = [entries[at], entries[at - 1]];
      }
      await config.setModulePathEntries(entries);
      changed = true;
    });

    if (!picked) {
      return changed;
    }
    if (picked.action === "reopen") {
      entries = config.modulePathEntries();
      continue;
    }
    if (picked.action === "add") {
      changed = (await addFolders()) || changed;
      entries = config.modulePathEntries();
      continue;
    }
    if (picked.action === "settings") {
      await vscode.commands.executeCommand(
        "workbench.action.openSettings",
        "wyrm.modulePath"
      );
      return changed;
    }
    // Picking an entry itself reveals it, which is the useful thing to do
    // with a path you're looking at in a list.
    const resolved = config.expandPath(picked.entry!);
    if (resolved) {
      await vscode.commands.executeCommand("revealFileInOS", vscode.Uri.file(resolved));
    }
    return changed;
  }
}

/**
 * One pass of the list: resolves with the selected item, or undefined if
 * dismissed. Button presses are handled by `onButton` and keep the list
 * open (the caller re-renders it with the new entries).
 */
function pickOne(
  entries: string[],
  remove: vscode.QuickInputButton,
  moveUp: vscode.QuickInputButton,
  onButton: (item: EntryItem, button: vscode.QuickInputButton) => Promise<void>
): Promise<EntryItem | undefined> {
  const items: EntryItem[] = entries.map((entry, i) => {
    const resolved = config.expandPath(entry);
    return {
      label: `${i + 1}. ${entry}`,
      description: resolved === entry ? "" : (resolved ?? "unresolved (no workspace folder)"),
      entry,
      buttons: i > 0 ? [moveUp, remove] : [remove],
    };
  });
  if (items.length === 0) {
    items.push({
      label: "$(info) WYRM_PATH is empty",
      description: "only the running file's directory and corelib are searched",
    });
  }
  items.push(
    { label: "", kind: vscode.QuickPickItemKind.Separator },
    { label: "$(add) Add folder...", description: "pick a directory to search", action: "add" },
    {
      label: "$(settings-gear) Open in Settings",
      description: "edit wyrm.modulePath as JSON",
      action: "settings",
    }
  );

  const quickPick = vscode.window.createQuickPick<EntryItem>();
  quickPick.title = "Wyrm module search path (WYRM_PATH)";
  quickPick.placeholder = "Searched in order, before the script's own directory and corelib";
  quickPick.items = items;

  return new Promise<EntryItem | undefined>((resolve) => {
    let result: EntryItem | undefined;
    quickPick.onDidTriggerItemButton(async (event) => {
      await onButton(event.item, event.button);
      result = { label: "", action: "reopen" };
      quickPick.hide(); // the caller re-opens with the updated entries
    });
    quickPick.onDidAccept(() => {
      result = quickPick.selectedItems[0];
      quickPick.hide();
    });
    quickPick.onDidHide(() => {
      quickPick.dispose();
      resolve(result);
    });
    quickPick.show();
  });
}
