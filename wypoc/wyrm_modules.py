"""WYRM_PATH-based module search-path resolution for wyrm's import system.

Mirrors Python's own import machinery closely enough to be familiar:
  - WYRM_PATH (colon-separated, like PYTHONPATH) lists directories to search,
    checked in order, with wypoc's own corelib/ directory (installed as
    package data) as a final fallback.
  - `import std::io` looks for <root>/std/io.wy under each search root.
  - `import std` (a directory) looks for <root>/std/__init__.wy, matching
    Python's package convention (a bare directory with no __init__.wy isn't
    an importable package here - no namespace-package support).

No parsing/evaluation lives here - just figuring out which file (if any) a
`mod::sub::leaf`-style path resolves to under the configured search paths.
Actually loading a module (parsing + eval_program) lives in
wyrm_eval_parse_tree.py, which imports this module (not the other way
around, to avoid a parse/eval <-> path-resolution import cycle).
"""
import os

_WYPOC_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COREPATH = os.path.join(_WYPOC_DIR, "corelib")


_script_root: "str | None" = None
_extra_paths: list = []


def set_extra_search_paths(paths) -> list:
    """Registers directories to search *before* WYRM_PATH's own entries -
    the REPL's `path` config option (see project.py), resolved to absolute
    paths by the caller. Returns the previous list, the way
    `set_script_root` does, so a caller can restore it."""
    global _extra_paths
    previous = _extra_paths
    _extra_paths = list(paths)
    return previous


def set_script_root(path: "str | None") -> "str | None":
    """Register the directory a script is being run from, so its own
    neighbours are importable - `import static decolib` next to `main.wy`
    resolves without WYRM_PATH being set at all, the same way Python puts a
    script's directory on sys.path. Set by cli.py (and by tests running a
    sample); returns the previous value so a caller can restore it."""
    global _script_root
    previous = _script_root
    _script_root = os.path.abspath(path) if path else None
    return previous


def script_root() -> "str | None":
    """The directory currently registered by `set_script_root`, for a caller
    that wants to put it back afterwards without changing it first."""
    return _script_root


def search_paths(env: dict = None) -> list:
    """The search roots, in order: the extra paths registered by
    `set_extra_search_paths` (the REPL's `path` config option), then
    WYRM_PATH's entries (colon-separated), then the running script's own
    directory if one is registered (see set_script_root), then the default
    corelib directory as a fallback. Reads `env` (os.environ by default)
    fresh on every call - nothing here is cached at import time - so tests
    can override WYRM_PATH without needing to patch this module."""
    env = os.environ if env is None else env
    raw = env.get("WYRM_PATH", "")
    paths = list(_extra_paths) + [p for p in raw.split(":") if p]
    if _script_root is not None:
        paths.append(_script_root)
    paths.append(DEFAULT_COREPATH)
    return paths


def prelude_path() -> str:
    """corelib/prelude.wy - always-available globals (e.g. `co range(...)`)
    seeded into every scope by populate_globals (wyrm_eval_parse_tree.py),
    independent of WYRM_PATH/search_paths since it isn't reached through an
    `import` statement."""
    return os.path.join(DEFAULT_COREPATH, "prelude.wy")


def resolve_module_file(path_segments, roots=None):
    """Find the .wy file for a `mod::sub::leaf` path (path_segments a list
    of names, e.g. ["std", "io"]). Returns (file_path, is_package), or None
    if no root has it. A plain file `<root>/mod/sub/leaf.wy` is preferred
    over a package directory `<root>/mod/sub/leaf/__init__.wy` when a root
    happens to have both, mirroring Python's own module-over-package
    preference within a single search root."""
    if roots is None:
        roots = search_paths()
    for root in roots:
        candidate = os.path.join(root, *path_segments)
        file_candidate = candidate + ".wy"
        if os.path.isfile(file_candidate):
            return file_candidate, False
        init_candidate = os.path.join(candidate, "__init__.wy")
        if os.path.isfile(init_candidate):
            return init_candidate, True
    return None
