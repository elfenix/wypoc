"""Bytecode-cache-style persistence for parsed wyrm ASTs: skip re-parsing a
script whose source hasn't changed since last run, the way Python's
`__pycache__` does for `.pyc` files - except here the cache holds the
pegen-parsed `ast.Program` tree itself, pickled, rather than bytecode.

Where the cache lives, per script:

    1. the `global_cache` directory in `~/.wyrm/config`'s `[wyrm]` section
       (config.py), when set - opt-in, and takes priority over the local
       directory below when it's set. Cache file named by the sha256 of
       the script's absolute path, so scripts from many directories can
       share one cache directory without colliding: `<hexdigest>.wyc`.
    2. otherwise, `<script_dir>/__wycache__/` - automatic, the same way
       Python creates `__pycache__/` next to a module it imports, no
       opt-in needed. Cache file named after the script itself: `foo.wy`
       -> `__wycache__/foo.wyc`.

Either directory is created on demand if it doesn't exist yet. If that
creation (or any other part of writing/reading the cache) fails - a
read-only filesystem, a permissions error, anything - caching is simply
skipped for that run: nothing here ever raises into the caller, so a
broken or inaccessible cache directory costs you the cache, not the run.

Each cache file is a pickled dict: `{"source_path", "mtime", "tree"}`. A
hit requires `source_path` and `mtime` to match the script being loaded
exactly; anything else - missing file, corrupt pickle, a dataclass shape
that's drifted since a wypoc upgrade, a header that doesn't match - is
just a miss, and `save` overwrites the stale entry, same as a first run.

Proof-of-concept only: pickle, no format versioning, no security
hardening, no cross-interpreter guarantees.
"""
import hashlib
import os
import pickle

from wypoc import config as config_mod

CACHE_DIR_NAME = "__wycache__"
CACHE_EXT = ".wyc"


def _local_cache_dir(abs_script_path: str) -> str:
    return os.path.join(os.path.dirname(abs_script_path), CACHE_DIR_NAME)


def cache_file_for(script_path: str) -> str:
    """Where `script_path`'s cache file lives: the configured
    `global_cache` directory if one is set, otherwise the automatic local
    `<script_dir>/__wycache__/` - see the module docstring. Always returns
    a path; whether that path is actually usable (directory creatable,
    file readable/writable) is for `load`/`save` to find out and fall back
    from, not this."""
    abs_path = os.path.abspath(script_path)
    global_dir = config_mod.load().get("global_cache")
    if global_dir:
        global_dir = os.path.abspath(os.path.expanduser(global_dir))
        digest = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()
        return os.path.join(global_dir, digest + CACHE_EXT)

    local_dir = _local_cache_dir(abs_path)
    name = os.path.splitext(os.path.basename(abs_path))[0] + CACHE_EXT
    return os.path.join(local_dir, name)


def load(script_path: str):
    """The cached `ast.Program` for `script_path`, or None on a miss - no
    cache file yet, or its header/pickle no longer matching what's on disk
    (source edited or moved, or a corrupt/stale-shape pickle left by a
    different wypoc version), or the cache file simply unreadable."""
    cache_path = cache_file_for(script_path)
    try:
        with open(cache_path, "rb") as f:
            entry = pickle.load(f)
        if (entry["source_path"] != os.path.abspath(script_path)
                or entry["mtime"] != os.path.getmtime(script_path)):
            return None
        return entry["tree"]
    except Exception:
        # Any failure - missing file, permissions, truncated/corrupt
        # pickle, a header that doesn't parse as expected - is just a
        # miss; the caller reparses and `save` below overwrites this
        # entry (or, if the directory isn't writable either, silently
        # doesn't - see `save`).
        return None


def save(script_path: str, tree) -> None:
    """Writes `tree` to `script_path`'s cache file, creating its directory
    on demand. Best-effort: a failure anywhere along the way (read-only
    filesystem, permissions, whatever) just means this run - and the
    next - go uncached, not a run-stopping error."""
    cache_path = cache_file_for(script_path)
    entry = {
        "source_path": os.path.abspath(script_path),
        "mtime": os.path.getmtime(script_path),
        "tree": tree,
    }
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp_path = f"{cache_path}.tmp.{os.getpid()}"
        with open(tmp_path, "wb") as f:
            pickle.dump(entry, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp_path, cache_path)
    except OSError:
        pass
