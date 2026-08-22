"""Project-local REPL setup: a `.wyrm/` directory found by walking up from
the working directory (or pinned by the `project_root` config option - see
config.py), and the two things it may hold:

    .wyrm/config      more `[wyrm]` options, overlaid on ~/.wyrm/config's -
                       most useful for `path`, a module search directory
                       list scoped to this project (see wyrm_modules.py)
    .wyrm/preamble.wy  wyrm source the REPL runs once at startup, before the
                       first prompt - imports, aliases, whatever a session
                       started in this directory should already have

The goal is `cd project && wyrm` picking up a bit of that project's own
setup automatically, the way a `.vimrc`-per-directory or a shell's
`.envrc` does. Nothing here is read except by the REPL's own startup
(cli.py's `run_repl`); a plain `wyrm script.wy` doesn't scan for this.
"""
import os
import sys

from wypoc import config as config_mod

PROJECT_DIR = ".wyrm"
PREAMBLE_FILE = "preamble.wy"
HISTORY_FILE = "history"

# A `path` entry containing this is expanded to the project root before
# being resolved - see resolve_search_paths. Lets a *global* ~/.wyrm/config
# name a directory inside whatever project is currently found (`path =
# "${project_root}/wy"`) rather than only being able to add paths that mean
# the same thing in every project.
PROJECT_ROOT_TOKEN = "${project_root}"


def find_project_root(start: "str | None" = None,
                       configured: "str | None" = None) -> "str | None":
    """The project's root directory. `configured` (the `project_root`
    config option) is taken as-is and ends the search right there - no
    scanning happens once a root is pinned. Otherwise this walks upward
    from `start` (the working directory by default) through every parent,
    stopping at the first one holding a `.wyrm/` directory; None if the
    walk reaches the filesystem root without finding one."""
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    directory = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(directory, PROJECT_DIR)):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def project_config_path(root: "str | None") -> "str | None":
    return os.path.join(root, PROJECT_DIR, config_mod.CONFIG_FILE) if root else None


def preamble_path(root: "str | None") -> "str | None":
    path = os.path.join(root, PROJECT_DIR, PREAMBLE_FILE) if root else None
    return path if path and os.path.isfile(path) else None


def history_path(root: "str | None") -> str:
    """Where a session's entry history is kept: a project's own
    `.wyrm/history` when a project root was found (see find_project_root),
    next to the user's own config file otherwise (config.config_path,
    which WYRM_CONFIG can redirect - so a session run against a config of
    its own, as tests do, doesn't also inherit the developer's real
    history). The same "project setup wins when there is one" rule
    `preamble_path` and `project_config_path` follow."""
    if root:
        return os.path.join(root, PROJECT_DIR, HISTORY_FILE)
    return os.path.join(os.path.dirname(config_mod.config_path()), HISTORY_FILE)


def load_options(start: "str | None" = None, warn=None) -> tuple:
    """The options a REPL session starts with, and the project root that
    supplied the project-specific ones (for `load_preamble` and for
    resolving `path`): `~/.wyrm/config` first, then the project's own
    `.wyrm/config` overlaid on top of it - closer to the work wins."""
    options = config_mod.load(warn=warn)
    if config_mod.is_disabled():
        # --no-config: no file, global or project, is read - and with none
        # read, there is nothing to scan upward looking for either (see
        # config_mod.set_disabled's docstring).
        return options, None
    root = find_project_root(start, configured=options.get("project_root") or None)
    path = project_config_path(root)
    if path:
        options.update(config_mod.load_overrides(path, warn=warn))
    return options, root


def load_preamble(root: "str | None", warn=None) -> "str | None":
    """`.wyrm/preamble.wy`'s text, or None when there's no project root or
    no preamble file - never an error the REPL has to handle specially, the
    same "missing customisation costs you the customisation" spirit as a
    missing config file."""
    path = preamble_path(root)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        (warn or _warn)(f"can't read preamble {path}: {e.strerror}")
        return None


def resolve_search_paths(path_option: str, root: "str | None") -> list:
    """The `path` config option's colon-separated entries as absolute
    directories. Two ways an entry names something relative to the project
    root:

    * plainly relative (`"lib"`, `"../vendor/wy"`) - resolved against the
      project root, or the working directory when there isn't one, the way
      a relative WYRM_PATH entry would already be resolved by whatever
      started the process from that directory;
    * `${project_root}` written explicitly (`"${project_root}/wy"`) -
      expanded to the same root before resolving. Only worth spelling out
      over a plain relative entry when the root needs to appear somewhere
      other than the front (`"/opt/shared:${project_root}/wy"`), or the
      option is set in ~/.wyrm/config and should still mean "this project's
      own wy/", not a fixed path.

    `${project_root}` with no project root found expands to the working
    directory, same as a plain relative entry falls back to it."""
    base = root or os.getcwd()
    paths = []
    for entry in path_option.split(":"):
        entry = entry.strip()
        if not entry:
            continue
        entry = entry.replace(PROJECT_ROOT_TOKEN, base)
        paths.append(os.path.normpath(entry if os.path.isabs(entry)
                                      else os.path.join(base, entry)))
    return paths


def _warn(message: str) -> None:
    print(f"wyrm: {message}", file=sys.stderr)
