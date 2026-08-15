"""~/.wyrm/config: reading the options a session starts with, and writing
them back (wypoc/config.py).

conftest.py's autouse `_wyrm_config` fixture points WYRM_CONFIG at a
per-test temporary file, so nothing here touches the real one; a test
wanting to see the file itself asks for that path through `config_path()`.
"""
import os

import pytest

from wypoc import config
from wypoc.repl import Session, run_command


@pytest.fixture
def path():
    """The config file this test is working against - not created yet."""
    return config.config_path()


def write(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


def test_no_config_file_means_the_built_in_defaults(path):
    assert not os.path.exists(path)
    assert config.load() == {"compact": False, "tui": False, "path": "", "project_root": ""}


def test_the_wyrm_section_supplies_a_sessions_defaults(path):
    write(path, "[wyrm]\ncompact = true\ntui = true\n")
    assert config.load() == {"compact": True, "tui": True, "path": "", "project_root": ""}
    assert Session().options["compact"] is True


def test_options_outside_the_wyrm_section_are_not_read(path):
    write(path, "compact = true\n\n[other]\ncompact = true\n")
    assert config.load()["compact"] is False


def test_an_unknown_option_is_reported_and_skipped(path):
    write(path, "[wyrm]\ncolour = true\ncompact = true\n")
    said = []
    assert config.load(warn=said.append)["compact"] is True, "the rest still applies"
    assert len(said) == 1 and "colour" in said[0]


def test_a_wrong_typed_value_is_reported_and_skipped(path):
    write(path, "[wyrm]\ncompact = 'sometimes'\n")
    said = []
    assert config.load(warn=said.append)["compact"] is False
    assert "compact" in said[0] and "boolean" in said[0]


def test_a_broken_config_file_costs_the_customisation_not_the_interpreter(path):
    write(path, "[wyrm\ncompact = true\n")
    said = []
    assert config.load(warn=said.append) == {
        "compact": False, "tui": False, "path": "", "project_root": ""}
    assert said, "and it says so rather than failing silently"


@pytest.mark.parametrize("text,expected", [
    ("compact=true", True),
    ("compact=false", False),
    ("compact=on", True),
    ("compact=off", False),
    ("compact=yes", True),
    ("compact=1", True),
    ("compact=0", False),
    ("compact", True),          # a bare boolean option means "turn it on"
    (" compact = TRUE ", True),
])
def test_assignments_are_read_the_way_a_person_writes_them(text, expected):
    assert config.parse_assignment(text) == ("compact", expected)


@pytest.mark.parametrize("text", ["colour=true", "compact=maybe", "=true", ""])
def test_a_bad_assignment_is_an_error(text):
    with pytest.raises(config.ConfigError):
        config.parse_assignment(text)


def test_setting_an_option_creates_the_file_and_its_directory(tmp_path, monkeypatch):
    target = tmp_path / "fresh" / "dir" / "config"
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(target))
    config.set_option("tui", True)
    assert target.read_text() == "[wyrm]\ntui = true\n"
    assert config.load()["tui"] is True


def test_writing_one_option_keeps_the_rest_of_the_file(path):
    write(path, "# my wyrm settings\n[wyrm]\ncompact = true  # one line, please\n"
                "\n[future]\nsomething = 1\n")
    config.set_option("tui", True)
    text = open(path).read()
    assert "# my wyrm settings" in text and "# one line, please" in text
    assert "[future]" in text and "something = 1" in text
    assert config.load() == {"compact": True, "tui": True, "path": "", "project_root": ""}


def test_a_config_file_that_is_not_toml_is_not_overwritten(path):
    write(path, "[wyrm\nnonsense\n")
    with pytest.raises(config.ConfigError):
        config.set_option("tui", True)
    assert open(path).read() == "[wyrm\nnonsense\n", "the file is left alone"


def test_set_config_writes_the_option_as_well_as_setting_it(path):
    session = Session()
    kind, message = run_command(session, ":set config compact")
    assert kind == "message"
    assert session.options["compact"] is True, "this session, right now"
    assert path in message
    assert config.load()["compact"] is True, "and every session after it"

    run_command(session, ":unset config compact")
    assert session.options["compact"] is False
    assert config.load()["compact"] is False


def test_set_config_alone_shows_the_file_and_what_it_says(path):
    _, message = run_command(Session(), ":set config")
    assert path in message and "not created yet" in message
    config.set_option("tui", True)
    _, message = run_command(Session(), ":set config")
    assert "tui  on" in message and "compact  off" in message


def test_set_config_of_an_unknown_option_writes_nothing(path):
    _, message = run_command(Session(), ":set config colour")
    assert "unknown option 'colour'" in message
    assert not os.path.exists(path)
