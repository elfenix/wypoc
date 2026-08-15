"""Project-local REPL setup (wypoc/project.py): finding a `.wyrm/`
directory by walking up from the working directory (or a pinned
`project_root`), and reading its `config` and `preamble.wy`.
"""
import os

from wypoc import config, project


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def test_no_wyrm_directory_anywhere_up_means_no_project(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert project.find_project_root(str(nested)) is None


def test_a_wyrm_directory_is_found_by_walking_up(tmp_path):
    (tmp_path / ".wyrm").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert project.find_project_root(str(nested)) == str(tmp_path)


def test_the_nearest_wyrm_directory_wins(tmp_path):
    (tmp_path / ".wyrm").mkdir()
    inner = tmp_path / "a"
    (inner / ".wyrm").mkdir(parents=True)
    nested = inner / "b"
    nested.mkdir()
    assert project.find_project_root(str(nested)) == str(inner)


def test_a_configured_root_skips_the_scan(tmp_path):
    # No .wyrm/ directory here at all - a pinned root doesn't need one.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert project.find_project_root(str(tmp_path), configured=str(elsewhere)) == str(elsewhere)


def test_project_config_overlays_the_global_one(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(tmp_path / "global-config"))
    write(str(tmp_path / "global-config"), "[wyrm]\ncompact = true\ntui = true\n")
    (tmp_path / ".wyrm").mkdir()
    write(str(tmp_path / ".wyrm" / "config"), "[wyrm]\ntui = false\n")

    options, root = project.load_options(str(tmp_path))
    assert root == str(tmp_path)
    assert options["compact"] is True  # from the global file, untouched
    assert options["tui"] is False     # overridden by the project's own


def test_no_project_root_means_only_the_global_options(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(tmp_path / "global-config"))
    write(str(tmp_path / "global-config"), "[wyrm]\ncompact = true\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    options, root = project.load_options(str(nested))
    assert root is None
    assert options["compact"] is True


def test_preamble_is_read_when_present(tmp_path):
    (tmp_path / ".wyrm").mkdir()
    write(str(tmp_path / ".wyrm" / "preamble.wy"), "var x = 1\n")
    assert project.load_preamble(str(tmp_path)) == "var x = 1\n"


def test_no_preamble_file_means_none(tmp_path):
    (tmp_path / ".wyrm").mkdir()
    assert project.load_preamble(str(tmp_path)) is None
    assert project.load_preamble(None) is None


def test_relative_search_path_entries_resolve_against_the_project_root(tmp_path):
    root = str(tmp_path)
    assert project.resolve_search_paths("lib:./vendor", root) == [
        os.path.join(root, "lib"), os.path.join(root, "vendor")]


def test_relative_search_path_entries_resolve_against_cwd_without_a_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert project.resolve_search_paths("lib", None) == [str(tmp_path / "lib")]


def test_absolute_search_path_entries_pass_through(tmp_path):
    absolute = str(tmp_path / "somewhere")
    assert project.resolve_search_paths(absolute, None) == [absolute]


def test_blank_search_path_entries_are_skipped():
    assert project.resolve_search_paths("", None) == []
    assert project.resolve_search_paths("a::b", "/root") == ["/root/a", "/root/b"]


def test_project_root_token_expands_to_the_root():
    assert project.resolve_search_paths("${project_root}/wy", "/root") == ["/root/wy"]


def test_project_root_token_can_appear_anywhere_in_the_entry(tmp_path):
    assert project.resolve_search_paths(
        "/opt/shared:${project_root}/../vendor/wy", "/root") == [
        "/opt/shared", "/vendor/wy"]


def test_project_root_token_falls_back_to_cwd_without_a_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert project.resolve_search_paths("${project_root}/wy", None) == [
        str(tmp_path / "wy")]
