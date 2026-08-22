"""wypoc/cache.py: the AST cache that lets a script skip re-parsing when its
source hasn't changed - a local `__wycache__/`, created automatically next
to the script the way Python creates `__pycache__/`, or the opt-in
`global_cache` config option instead (test_config.py covers `~/.wyrm/config`
itself; this file is about cache.py's own logic).
"""
import hashlib
import os

from wypoc import cache, config
from wypoc.ast_nodes import ExprStmt, Num, Program


def make_tree(n: int) -> Program:
    return Program(body=[ExprStmt(value=Num(value=n))])


def write_script(path, text: str = "1 + 1\n") -> str:
    with open(path, "w") as f:
        f.write(text)
    return str(path)


def test_local_wycache_dir_is_created_automatically(tmp_path):
    script = write_script(tmp_path / "a.wy")
    cache_dir = tmp_path / cache.CACHE_DIR_NAME
    assert not cache_dir.exists()
    assert cache.cache_file_for(script) == str(cache_dir / "a.wyc")

    tree = make_tree(1)
    cache.save(script, tree)
    assert cache_dir.is_dir()
    assert cache.load(script) == tree


def test_save_then_load_round_trips_the_tree(tmp_path):
    script = write_script(tmp_path / "a.wy")
    tree = make_tree(42)
    cache.save(script, tree)
    assert os.path.isfile(tmp_path / cache.CACHE_DIR_NAME / "a.wyc")
    assert cache.load(script) == tree


def test_editing_the_source_invalidates_the_cache(tmp_path):
    script = write_script(tmp_path / "a.wy")
    cache.save(script, make_tree(1))
    assert cache.load(script) == make_tree(1)

    # Bump the mtime the way a real edit would - past whatever resolution
    # the filesystem's clock has, so it's guaranteed to differ.
    write_script(script, "2 + 2\n")
    os.utime(script, (os.path.getmtime(script) + 5, os.path.getmtime(script) + 5))
    assert cache.load(script) is None


def test_a_corrupt_cache_file_is_a_miss_not_a_crash(tmp_path):
    script = write_script(tmp_path / "a.wy")
    cache_dir = tmp_path / cache.CACHE_DIR_NAME
    cache_dir.mkdir()
    with open(cache_dir / "a.wyc", "wb") as f:
        f.write(b"not a pickle")
    assert cache.load(script) is None


def test_a_stale_header_from_a_moved_script_is_a_miss(tmp_path):
    script = write_script(tmp_path / "a.wy")
    cache.save(script, make_tree(1))

    other = write_script(tmp_path / "b.wy")
    # Hand b.wy a.wy's own cache file directly, to simulate a copied/renamed
    # source landing on a stale header still claiming the old path.
    import shutil
    shutil.move(str(tmp_path / cache.CACHE_DIR_NAME / "a.wyc"),
                str(tmp_path / cache.CACHE_DIR_NAME / "b.wyc"))
    assert cache.load(other) is None


def test_an_unwritable_local_dir_just_leaves_the_run_uncached(tmp_path):
    script = write_script(tmp_path / "a.wy")
    blocker = tmp_path / cache.CACHE_DIR_NAME
    blocker.write_text("not a directory")  # __wycache__/ can't be created here

    cache.save(script, make_tree(1))  # best-effort: no exception
    assert cache.load(script) is None


def test_global_cache_is_opt_in_and_overrides_the_local_default(tmp_path, monkeypatch):
    global_dir = tmp_path / "shared-cache"
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(tmp_path / "wyrm-config"))
    config.set_option("global_cache", str(global_dir))

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = write_script(scripts_dir / "a.wy")

    digest = hashlib.sha256(os.path.abspath(script).encode("utf-8")).hexdigest()
    assert cache.cache_file_for(script) == str(global_dir / f"{digest}.wyc")

    tree = make_tree(7)
    cache.save(script, tree)
    assert cache.load(script) == tree
    assert not (scripts_dir / cache.CACHE_DIR_NAME).exists(), \
        "global_cache set means no local __wycache__/ is touched"
