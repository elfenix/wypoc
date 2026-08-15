"""The interactive wyrm REPL - the engine, plus the plain readline front end.

Two front ends share this module's `Session` (one persistent scope, fed one
entry at a time) and `is_incomplete` (does this text still want another
line?):

    wyrm            -> `run_readline` below: print prompt, read a line,
                        keep reading continuation lines while the entry is
                        incomplete, evaluate, print the value
    wyrm --tui      -> repl_tui.py: the same session driven by a Textual
                        full-screen UI

Output uses `rich` for colour; nothing here needs a terminal, so a Session
is equally usable from a test (see test/test_repl.py).

Statements are evaluated one at a time in the *same* scope, so a function,
class or import from an earlier entry is still there for a later one - the
REPL's whole point. That's `eval_stmt` per statement rather than
`eval_program`, which would wrap the entry in a scope of its own and throw
away everything it bound. One consequence: a top-level `defer` registers on
the session scope and never runs, since the session scope is only torn down
when the REPL exits.
"""
import io
import os
import shutil
import sys
from dataclasses import dataclass

import token as token_mod

from wypoc import ast_nodes as ast
from wypoc import config as config_mod
from wypoc import pretty as pretty_mod
from wypoc import project as project_mod
from wypoc import wyrm_builtins, wyrm_io, wyrm_modules
from wypoc.parse import parse
from wypoc.wyrm_tokenizer import TokenizeError, generate_tokens
from wypoc.wyrm_eval_parse_tree import (
    Scope,
    eval_stmt,
    expose,
    lookup,
    populate_globals,
)

PROMPT = "wyrm> "
CONTINUATION_PROMPT = " ...> "

BANNER = "wyrm REPL - :help for help, :quit (or ctrl-d) to leave"

HELP = """\
:help          show this message
:quit, :q      leave the REPL
:clear         forget every binding and start a fresh session (ctrl-l too)
:set NAME      turn an option on for this session (:set alone lists them)
:unset NAME    turn it off for this session
:set config NAME    turn it on here *and* in ~/.wyrm/config, so every later
                     session starts with it on (:unset config NAME to undo;
                     bare :set config shows the file)

Options:
  compact      answer with one-line results, the way `str()` spells them.
               Off by default, so a pair prints as a lisp list, a dict and
               an array in JSON layout, and a class instance in the shape of
               its class definition - each broken across lines only when it
               doesn't fit.
  tui          start the REPL in its full-screen UI, as if `wyrm --tui` had
               been run. Only read at startup, so it's `:set config tui`
               that's worth setting - toggling it live changes nothing.
  path         extra module search directories, colon-separated, searched
               before WYRM_PATH. A relative entry resolves against the
               project root; `${project_root}` in an entry expands to it
               explicitly, useful when the option is set in ~/.wyrm/config
               and should still mean "this project's own directory".
               Only settable in a config file (global or project), not
               with :set - see `project_root` below.
  project_root string-valued like `path`; not a toggle either.

Project setup: starting `wyrm` in a directory walks up looking for a
`.wyrm/` directory (set `project_root` in ~/.wyrm/config to pin one and
skip the walk instead). When found, `.wyrm/config` overlays ~/.wyrm/config's
options, and `.wyrm/preamble.wy`, if present, is run once before the first
prompt - the way to give a project's own sessions a standard set of imports
or bindings to start from.

Entries spanning several lines: an entry with an unclosed bracket, or one
opening a block (a trailing `:`), keeps prompting for more. Finish a block
with an empty line."""

# The REPL's options and their defaults - see config.py, which owns the
# table (the same options are settable in ~/.wyrm/config) and which
# `:set`/`:unset` toggle here. `compact` off means results are
# pretty-printed (see pretty.py).
OPTIONS = config_mod.OPTIONS

# Brackets whose being open means "this entry isn't finished" - the same set
# the tokenizer joins lines inside (see wyrm_tokenizer's bracket_depth /
# brace_depth): `(`/`[` for calls, groups, arrays and `'(` pair lists, `{`
# for brace blocks and `'{` dicts.
_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}

# Tokens carrying no text of their own, skipped when asking "what did this
# entry really end with?"
_LAYOUT = (token_mod.NEWLINE, token_mod.NL, token_mod.INDENT, token_mod.DEDENT,
           token_mod.ENDMARKER, token_mod.COMMENT)


@dataclass
class Result:
    """What one evaluated entry produced. `display` is the value's rendering
    when the entry ended in an expression statement that answered something
    other than nil (nil is not worth echoing), None otherwise - so a caller
    can simply `if result.display:` to decide whether to show a value."""

    source: str
    output: str = ""
    value: object = None
    display: "str | None" = None
    error: "str | None" = None

    @property
    def failed(self) -> bool:
        return self.error is not None


def is_incomplete(source: str) -> bool:
    """Whether `source` is an entry the user is still in the middle of - the
    readline loop keeps prompting while this is true, and the TUI's `enter`
    inserts a newline instead of submitting.

    Three ways an entry stays open:

    * a bracket (or a string literal) is still unclosed,
    * it ends with a block's `:` and nothing after it (`fn f(a):`),
    * it *is* an indented block already, and the last line isn't blank -
      the same "finish a block with an empty line" rule Python's own REPL
      uses. Without it `fn f(a):` + `  return a` would run the moment the
      body's first line parsed, with no way to type a second one.
    """
    if not source.strip():
        return False

    try:
        tokens = list(generate_tokens(source))
    except TokenizeError as e:
        # An unterminated string is a half-typed entry; anything else the
        # tokenizer rejects (a stray character, say) is a real error and
        # should be reported now rather than swallowed by a prompt that
        # never ends.
        return "unterminated" in str(e)

    depth = 0
    opens_block = False
    previous = None
    for tok in tokens:
        if tok.type == token_mod.OP:
            if tok.string in _OPENERS:
                depth += 1
            elif tok.string in _CLOSERS:
                depth -= 1
        # A `:` immediately followed by the end of its line opens an indented
        # block; `if x: y = 1` (a block on one line) does not.
        if (tok.type in (token_mod.NEWLINE, token_mod.NL)
                and previous is not None
                and previous.type == token_mod.OP and previous.string == ":"):
            opens_block = True
        if tok.type not in _LAYOUT:
            previous = tok

    if depth > 0:
        return True
    if previous is not None and previous.type == token_mod.OP and previous.string == ":":
        return True
    if opens_block:
        # split("\n"), not splitlines(): a trailing newline *is* the empty
        # last line that finishes a block, and splitlines() would drop it.
        return source.split("\n")[-1].strip() != ""
    return False


def run_command(session: "Session", text: str) -> "tuple | None":
    """Handles the REPL's own `:`-prefixed commands, which are the front
    end's business rather than the language's. Answers None when `text`
    isn't one (so the caller evaluates it as wyrm), ("quit", None) to leave,
    or ("message", text) for something to show the user."""
    command = text.strip()
    if command in (":quit", ":q", ":exit"):
        return ("quit", None)
    if command == ":help":
        return ("message", HELP)
    if command == ":clear":
        session.clear()
        return ("message", "session cleared")
    words = command.split()
    if words and words[0] in (":set", ":unset"):
        return ("message", _set_option(session, words[0] == ":set", words[1:]))
    return None


def _set_option(session: "Session", on: bool, names: list) -> str:
    """`:set name` / `:unset name`, and bare `:set` as "what are they?".
    Answers the line to show - an unknown name is reported rather than
    quietly doing nothing, since a typo'd option would otherwise look like
    it had taken effect.

    `:set config name` does the same to this session *and* writes it into
    `~/.wyrm/config`, so the next session starts that way too; bare `:set
    config` shows what the file currently says."""
    persist = bool(names) and names[0] == "config"
    if persist:
        names = names[1:]
        if not names:
            return _config_listing()
    if not names:
        return "\n".join(f"{name}  {_format_value(value)}"
                         for name, value in sorted(session.options.items()))
    said = []
    for name in names:
        if name not in session.options:
            known = ", ".join(sorted(session.options))
            return f"unknown option {name!r} - known options: {known}"
        if type(session.options[name]) is not bool:
            return (f"{name!r} isn't a toggle - set it in ~/.wyrm/config "
                     "(or a project's .wyrm/config) instead")
        if persist:
            try:
                config_mod.set_option(name, on)
            except config_mod.ConfigError as e:
                return str(e)
        session.options[name] = on
        said.append(f"{name} {'on' if on else 'off'}")
    where = f" (saved in {config_mod.config_path()})" if persist else ""
    return ", ".join(said) + where


def _config_listing() -> str:
    """What the config file sets, for bare `:set config` - the file's own
    path first, so there's somewhere to go and edit it by hand."""
    path = config_mod.config_path()
    stored = config_mod.load(warn=lambda message: None)
    lines = [path if os.path.exists(path) else f"{path} (not created yet)"]
    lines += [f"{name}  {_format_value(value)}"
              for name, value in sorted(stored.items())]
    return "\n".join(lines)


def _format_value(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return value if value else "(not set)"


class Session:
    """One REPL session: a scope that outlives each entry, plus the
    evaluate-and-render step both front ends drive."""

    def __init__(self, script_root: "str | None" = None,
                 options: "dict | None" = None,
                 project_root: "str | None" = None,
                 preamble: "str | None" = None):
        self.entries = 0
        # Defaults from ~/.wyrm/config unless a caller hands over options it
        # has loaded (and possibly overridden) already - cli.py does, so a
        # `wyrm --tui` doesn't re-read the file the CLI just read.
        self.options = dict(options) if options is not None else config_mod.load()
        # The project root a caller (cli.py's run_repl) found for this
        # session - see project.py - only kept around for `path`'s relative
        # entries; the REPL itself doesn't otherwise care where it is.
        self.project_root = project_root
        # How wide a result may be before it's broken across lines; a front
        # end that knows its terminal sets this (see run_readline and the
        # TUI's on_resize).
        self.width = pretty_mod.DEFAULT_WIDTH
        # Kept around (rather than only used once here) so :clear can rerun
        # it - see `clear` below.
        self.preamble = preamble
        self._script_root = script_root
        self.reset(script_root)
        wyrm_modules.set_extra_search_paths(
            project_mod.resolve_search_paths(self.options.get("path", ""), project_root))
        # Run once, before the first prompt - not counted as an entry (see
        # `evaluate`'s `count`), so the status bar's "N entries" still means
        # "N things you typed".
        self.preamble_result = self.evaluate(preamble, count=False) if preamble else None

    def reset(self, script_root: "str | None" = None) -> None:
        # The working directory plays the role a script's own directory
        # plays for `wyrm script.wy` (see cli.py), so `import static
        # decolib` finds a module sitting next to where the REPL was
        # started, with no WYRM_PATH set.
        wyrm_modules.set_script_root(script_root or os.getcwd())
        self.ctx = Scope()
        populate_globals(self.ctx)
        expose(self.ctx, "__ARGS", ())

    def clear(self) -> None:
        """`:clear`'s own work: forgets every binding *and*, when this
        session started from a project preamble, reruns it - so a session
        just cleared looks like the one that greeted you at startup, not an
        emptier one that's lost the project's standard imports along with
        whatever you'd bound since."""
        self.reset(self._script_root)
        self.preamble_result = (
            self.evaluate(self.preamble, count=False) if self.preamble else None)

    def evaluate(self, source: str, *, count: bool = True) -> Result:
        """Parses and runs one entry, capturing anything it printed. Never
        raises: a syntax error, a wyrm error value and a Python-level
        exception out of the evaluator all come back as `Result.error`,
        since a REPL that dies on a typo is no REPL at all.

        `count=False` (the preamble's own call, in __init__) runs the entry
        without bumping `entries` - the status bar's count is of things the
        person at the keyboard ran, not of setup that ran on their behalf."""
        if count:
            self.entries += 1
        try:
            tree = parse(source, filename="<repl>")
        except SyntaxError as e:
            # TokenizeError is a SyntaxError subclass, so a lexical
            # complaint ("unterminated string literal") lands here too.
            return Result(source, error=_syntax_error_text(e))

        value = None
        error = None
        with _captured_output() as out:
            try:
                for stmt in tree.body:
                    value = eval_stmt(stmt, self.ctx)
                if tree.body:
                    value = self._bound_value(tree.body[-1], value)
            except KeyboardInterrupt:
                error = "interrupted"
            except Exception as e:  # noqa: BLE001 - a REPL survives everything
                error = f"{type(e).__name__}: {e}"
        output = out.getvalue()

        if error is not None:
            return Result(source, output=output, error=error)
        if wyrm_builtins.is_error(value):
            return Result(source, output=output, value=value,
                          error=wyrm_builtins.display(value))
        display = None
        if value is not None and value is not wyrm_builtins.NIL:
            display = self.render(value)
        return Result(source, output=output, value=value, display=display)

    def _bound_value(self, stmt, value):
        """The value an entry ending in a binding answers with: what the
        name now holds. `eval_stmt` returns None for a declaration or an
        assignment (only a bare expression statement carries a value - see
        its docstring, and `do:`'s semantics, which this must not change),
        so the REPL reads the binding back out of the scope instead, and
        `x := 5` echoes `5` the way `5` itself would.

        Only *name* bindings qualify. `p.x = 1` and `a[i] = 1` mutate
        something a target expression points at rather than binding a name,
        and re-evaluating that expression to display it could run a call a
        second time - so they stay silent, as before."""
        names = _bound_names(stmt)
        if not names:
            return value
        try:
            values = [lookup(name, self.ctx) for name in names]
        except NameError:
            # A forward declaration (`var x: int`) binds the name without
            # giving it a value yet; there's nothing to echo.
            return value
        return values[0] if len(values) == 1 else tuple(values)

    def render(self, value) -> str:
        """How a value is echoed: pretty-printed across lines as needed, or
        as a single `str()`-style line under `:set compact`."""
        if self.options["compact"]:
            return wyrm_builtins.display(value)
        return pretty_mod.pretty(value, width=self.width)


def _bound_names(stmt) -> list:
    """The names a statement binds, for Session._bound_value - empty for
    anything that isn't a binding, and for a binding that doesn't name
    anything (an attribute or index target)."""
    if isinstance(stmt, ast.VarDecl):
        return [target.name for target in stmt.targets]
    if isinstance(stmt, ast.StaticDecl):
        return [stmt.name]
    if isinstance(stmt, ast.Assign):
        if all(isinstance(target, ast.NameTarget) for target in stmt.targets):
            return [target.name for target in stmt.targets]
    return []


def _syntax_error_text(e: SyntaxError) -> str:
    where = f" (line {e.lineno})" if e.lineno else ""
    return f"SyntaxError: {e.msg}{where}"


class _captured_output:
    """Redirects wyrm's stdout/stderr into a buffer for the duration of one
    entry. wyrm's `print` writes through wyrm_io's handle table rather than
    Python's `print`, so swapping sys.stdout alone isn't enough - the
    handles have to be re-pointed at the swapped streams (that's exactly
    what wyrm_io._reset_std_handles is for)."""

    def __enter__(self) -> io.StringIO:
        self.buffer = io.StringIO()
        self.saved = (sys.stdout, sys.stderr)
        sys.stdout = sys.stderr = self.buffer
        wyrm_io._reset_std_handles()
        return self.buffer

    def __exit__(self, *exc_info) -> bool:
        sys.stdout, sys.stderr = self.saved
        wyrm_io._reset_std_handles()
        return False


# --------------------------------------------------------------------------
# history - shared by both front ends (repl_tui.py uses these directly; the
# readline front end below hands the same path to readline's own file
# format instead, since readline already manages that file itself)
# --------------------------------------------------------------------------

def _escape_history_line(source: str) -> str:
    """One entry as a single line: backslash-escapes an entry's own
    backslashes and newlines, so a multi-line entry (a `fn`, a `class`)
    still round-trips through a file that's one entry per line."""
    return source.replace("\\", "\\\\").replace("\n", "\\n")


def _unescape_history_line(line: str) -> str:
    """Reverses `_escape_history_line`."""
    out = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line) and line[i + 1] in "\\n":
            out.append("\n" if line[i + 1] == "n" else "\\")
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def load_history(path: str) -> list:
    """Entries from a history file written by `append_history`, oldest
    first - a missing file reads as no history, the same as a session that
    hasn't saved any yet."""
    try:
        with open(path, encoding="utf-8") as f:
            return [_unescape_history_line(line.rstrip("\n")) for line in f]
    except OSError:
        return []


def append_history(path: str, source: str) -> None:
    """Adds one entry to the history file, escaped onto its own line and
    written immediately - not batched until exit - so history survives a
    crash or a killed session, not just a clean one."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(_escape_history_line(source) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# readline front end
# --------------------------------------------------------------------------

def _install_readline(project_root: "str | None" = None) -> None:
    """Line editing, history and a history file, when the platform has
    readline; a terminal without it still gets a working (if plainer)
    prompt, so this failing is not fatal."""
    try:
        import readline
    except ImportError:
        return
    history = project_mod.history_path(project_root)
    try:
        readline.read_history_file(history)
    except OSError:
        pass
    readline.set_history_length(1000)
    import atexit
    atexit.register(_save_history, readline, history)
    # ctrl-l as a :clear hotkey (see repl_tui.py, which binds the same key
    # to the same effect): a macro that clears whatever's half-typed on the
    # line, types ":clear", and submits it - readline's own default use of
    # ctrl-l (clear the terminal) is given up for this, so the key means
    # the same thing in both front ends.
    try:
        readline.parse_and_bind(r'"\C-l": "\C-a\C-k:clear\r"')
    except Exception:  # pragma: no cover - a readline binding is best-effort
        pass


def _save_history(readline, path: str) -> None:
    try:
        readline.write_history_file(path)
    except OSError:
        pass


def run_readline(session: "Session | None" = None, banner: bool = True) -> int:
    """The default `wyrm` REPL: prompt, read, (continue reading), evaluate,
    print. Also the fallback when the TUI can't run."""
    from rich.console import Console

    console = Console()
    session = session or Session()
    _install_readline(session.project_root)
    if banner:
        console.print(f"[dim]{BANNER}[/dim]")
    if session.preamble_result is not None:
        _print_result(console, session.preamble_result)

    buffer: list = []
    while True:
        prompt = CONTINUATION_PROMPT if buffer else PROMPT
        try:
            line = input(_readline_prompt(prompt, bool(buffer)))
        except EOFError:
            console.print()
            break
        except KeyboardInterrupt:
            # ctrl-c abandons whatever is half-typed, it doesn't quit.
            console.print("[dim]^C[/dim]")
            buffer = []
            continue

        if not buffer:
            if not line.strip():
                continue
            command = run_command(session, line)
            if command is not None:
                kind, payload = command
                if kind == "quit":
                    break
                console.print(f"[dim]{payload}[/dim]", highlight=False)
                if line.strip() == ":clear" and session.preamble_result is not None:
                    _print_result(console, session.preamble_result)
                continue

        buffer.append(line)
        source = "\n".join(buffer)
        if is_incomplete(source):
            continue
        buffer = []
        # Re-read the width each time: a terminal can be resized mid-session.
        session.width = shutil.get_terminal_size().columns
        _print_result(console, session.evaluate(source))
    return 0


def _readline_prompt(text: str, continuation: bool) -> str:
    """The prompt string, coloured - with the escapes wrapped in readline's
    \\001/\\002 markers so readline doesn't count them towards the line's
    width and mangle the cursor position on a long line."""
    if not sys.stdout.isatty():
        return text
    colour = "\033[2m" if continuation else "\033[1;36m"
    return f"\001{colour}\002{text}\001\033[0m\002"


def _print_result(console, result: Result) -> None:
    # Everything printed here is user data (a value's rendering, an error
    # message, whatever the entry wrote) - printed as rich Text so a `[`
    # in it stays a `[` instead of being read as console markup.
    from rich.text import Text

    if result.output:
        text = result.output
        console.print(Text(text if text.endswith("\n") else text + "\n"), end="")
    if result.error:
        console.print(Text(result.error, style="bold red"))
    elif result.display:
        value = Text(result.display, style="cyan")
        # Pick the structure out of the data: the parens of a pair list, the
        # braces and brackets of a dict or array (see pretty.py).
        value.highlight_regex(pretty_mod.DELIMITERS, style="bold yellow")
        console.print(value)
