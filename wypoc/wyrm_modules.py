"""WYRM_PATH-based module search-path resolution for wyrm's import system.

Mirrors Python's own import machinery closely enough to be familiar:
  - WYRM_PATH (colon-separated, like PYTHONPATH) lists directories to search,
    checked in order, with the repo's corelib/ directory as a final fallback.
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
_REPO_ROOT = os.path.dirname(_WYPOC_DIR)
DEFAULT_COREPATH = os.path.join(_REPO_ROOT, "corelib")


def search_paths(env: dict = None) -> list:
    """WYRM_PATH entries (colon-separated), in search order, followed by the
    default corelib directory as a fallback. Reads `env` (os.environ by
    default) fresh on every call - nothing here is cached at import time -
    so tests can override WYRM_PATH without needing to patch this module."""
    env = os.environ if env is None else env
    raw = env.get("WYRM_PATH", "")
    paths = [p for p in raw.split(":") if p]
    paths.append(DEFAULT_COREPATH)
    return paths


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
