"""wyrm's user configuration file: `~/.wyrm/config`, TOML, read at startup.

One section, `[wyrm]`, holding the defaults for the same options the REPL's
`:set`/`:unset` toggle live - so an option a session turns on by hand every
time can be turned on once, here:

    [wyrm]
    compact = true
    tui = true

Three ways in, all landing on this module:

    wyrm --config compact=true      set it in the file and exit (cli.py)
    :set config compact             set it in the file *and* in this
                                     session, right now (repl.py)
    :set compact                    this session only - the file is untouched

Anything the file says that this module doesn't know about (a stale option
name, a value of the wrong shape) is reported and skipped rather than being
an error: a config file written by a newer wyrm shouldn't stop an older one
from starting.

Written back through `tomlkit`, which round-trips a document rather than
re-serialising it - so comments, key order and spacing a person put in the
file survive `--config`/`:set config` rewriting it.
"""
import os
import sys
from typing import Any, Callable

import tomlkit

# Where the file lives, and the one section in it that holds options. The
# WYRM_CONFIG environment variable names a different file (tests use it; so
# can a script wanting a config of its own), the way WYRM_PATH overrides the
# module search path.
CONFIG_ENV_VAR = "WYRM_CONFIG"
CONFIG_DIR = "~/.wyrm"
CONFIG_FILE = "config"
SECTION = "wyrm"

# Every option, with its default - the schema this module validates against
# and the REPL's own option table (repl.OPTIONS is this dict). A value's type
# here is the type the file must supply.
#
#   compact       one-line results instead of pretty-printed ones
#   tui           `wyrm` with no arguments starts the full-screen REPL, as if
#                 --tui had been passed
#   path          extra module search directories, colon-separated like
#                 WYRM_PATH - inserted *before* WYRM_PATH's own entries (see
#                 wyrm_modules.search_paths). A relative entry is resolved
#                 against the project root (see project.py), or the working
#                 directory when there isn't one; `${project_root}` in an
#                 entry expands to the same root explicitly (project.py's
#                 resolve_search_paths, PROJECT_ROOT_TOKEN).
#   project_root  pins the REPL's project root instead of scanning upward
#                 from the working directory for a `.wyrm/` directory (see
#                 project.find_project_root) - set this and the scan is
#                 skipped, the given directory is used as-is.
#   global_cache  an opt-in directory to cache every script's parsed AST in
#                 (see cache.py), shared across scripts instead of each
#                 getting its own `<script_dir>/__wycache__/` (the default,
#                 automatic, needs no config - like Python's __pycache__).
#                 Empty (the default) leaves each script caching locally.
OPTIONS: dict[str, Any] = {
    "compact": False,
    "tui": False,
    "path": "",
    "project_root": "",
    "global_cache": "",
}

# What each option means, for `wyrm --config` with no assignment and for the
# REPL's :help.
DESCRIPTIONS: dict[str, str] = {
    "compact": "answer with one-line results, the way `str()` spells them",
    "tui": "start the REPL in its full-screen UI, as if --tui were given",
    "path": "extra module search directories, colon-separated, before WYRM_PATH",
    "project_root": "pin the project root instead of scanning for .wyrm/",
    "global_cache": "opt-in shared AST cache directory, instead of each "
                     "script's own __wycache__/",
}

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}

# `wyrm --no-config`: process-wide, set once at startup (cli.py) before
# anything reads a config file. Every mode - REPL, `-c`, running a script,
# --compile/--dump-wys/--compile-py, --check - funnels through `load`/
# `load_overrides` below, so flipping this one flag makes all of them run on
# plain OPTIONS defaults with no file ever touched, no project root scanned,
# and no preamble run (project.load_options short-circuits to `root=None`
# the same way a scan that found nothing would).
_disabled = False


class ConfigError(Exception):
    """A bad option name or an unusable value - raised by the parsing and
    writing entry points, where the user is right there to be told. Reading
    the file never raises (see `load`)."""


def set_disabled(disabled: bool) -> None:
    """`--no-config`'s effect: suppress every config file this process would
    otherwise read for the rest of its life."""
    global _disabled
    _disabled = disabled


def is_disabled() -> bool:
    return _disabled


def config_path() -> str:
    """The config file this process reads and writes."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser(CONFIG_DIR), CONFIG_FILE)


def load(path: "str | None" = None,
         warn: "Callable[[str], None] | None" = None) -> dict:
    """The options a session starts with: `OPTIONS`' defaults, overlaid with
    whatever `[wyrm]` in the config file sets. Missing file, unreadable file,
    malformed TOML, unknown key, wrong-typed value - each is reported through
    `warn` (stderr by default) and otherwise ignored, so a broken config
    costs you your customisation, not your interpreter."""
    if _disabled:
        return dict(OPTIONS)
    options = dict(OPTIONS)
    options.update(load_overrides(path or config_path(), warn))
    return options


def load_overrides(path: str,
                    warn: "Callable[[str], None] | None" = None) -> dict:
    """Just the options `path`'s `[wyrm]` section actually sets - not
    overlaid on `OPTIONS`' defaults, so a caller layering several config
    files of increasing specificity (project.py: `~/.wyrm/config` then a
    project's own `.wyrm/config`) can overlay each in turn onto the one
    before it, and a file that mentions nothing doesn't reset what an
    earlier one set."""
    if _disabled:
        return {}
    warn = warn or _warn
    overrides: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            document = tomlkit.parse(f.read())
    except FileNotFoundError:
        return overrides
    except OSError as e:
        warn(f"can't read config {path}: {e.strerror}")
        return overrides
    except Exception as e:  # tomlkit raises its own ParseError hierarchy
        warn(f"{path}: {e}")
        return overrides

    section = document.get(SECTION)
    if section is None:
        return overrides
    if not hasattr(section, "items"):
        warn(f"{path}: [{SECTION}] is not a table; ignoring it")
        return overrides

    for name, value in section.items():
        if name not in OPTIONS:
            warn(f"{path}: unknown option {name!r}; ignoring it")
            continue
        try:
            overrides[name] = coerce(name, value)
        except ConfigError as e:
            warn(f"{path}: {e}")
    return overrides


def coerce(name: str, value) -> Any:
    """One option's value, as the option's type - taking either a TOML value
    of the right type already or a string spelling of one (`--config
    compact=on`, where the shell only ever hands over text)."""
    if name not in OPTIONS:
        raise ConfigError(f"unknown option {name!r} - known options: {known()}")
    wanted = type(OPTIONS[name])
    if wanted is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ConfigError(
            f"option {name!r} wants a boolean (true/false), not {value!r}")
    if isinstance(value, wanted):
        return value
    try:
        return wanted(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"option {name!r} wants {wanted.__name__}, not {value!r}") from None


def parse_assignment(text: str) -> tuple:
    """`--config`'s `name=value` argument, as the pair it names. `name`
    alone is read as turning a boolean option on, so `--config tui` means
    what `:set tui` does."""
    name, sep, value = text.partition("=")
    name = name.strip()
    if not name:
        raise ConfigError(f"expected name=value, got {text!r}")
    if not sep:
        if type(OPTIONS.get(name)) is not bool:
            raise ConfigError(f"expected name=value, got {text!r}")
        value = "true"
    return name, coerce(name, value.strip())


def set_option(name: str, value, path: "str | None" = None) -> None:
    """Writes one option into `[wyrm]`, creating the directory and the file
    if they aren't there yet. The rest of the document - other options,
    comments, other sections - is left exactly as it was."""
    value = coerce(name, value)
    path = path or config_path()
    document = _read_document(path)
    section = document.get(SECTION)
    if section is None or not hasattr(section, "items"):
        section = tomlkit.table()
        document[SECTION] = section
    section[name] = value

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(tomlkit.dumps(document))
    except OSError as e:
        raise ConfigError(f"can't write config {path}: {e.strerror}") from None


def known() -> str:
    return ", ".join(sorted(OPTIONS))


def _read_document(path: str):
    """The config file as a tomlkit document to edit in place - a fresh
    empty one when there's no file yet. A file that doesn't parse is *not*
    silently replaced: overwriting someone's hand-written config because of
    a typo in it would lose work."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return tomlkit.document()
    except OSError as e:
        raise ConfigError(f"can't read config {path}: {e.strerror}") from None
    try:
        return tomlkit.parse(text)
    except Exception as e:
        raise ConfigError(f"{path} is not valid TOML ({e}); "
                          f"fix or remove it first") from None


def _warn(message: str) -> None:
    print(f"wyrm: {message}", file=sys.stderr)
