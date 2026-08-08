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
from wypoc import pretty as pretty_mod
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
:clear         forget every binding and start a fresh session
:set NAME      turn an option on (:set alone lists them)
:unset NAME    turn it off

Options:
  compact      answer with one-line results, the way `str()` spells them.
               Off by default, so a pair prints as a lisp list, a dict and
               an array in JSON layout, and a class instance in the shape of
               its class definition - each broken across lines only when it
               doesn't fit.

Entries spanning several lines: an entry with an unclosed bracket, or one
opening a block (a trailing `:`), keeps prompting for more. Finish a block
with an empty line."""

# The REPL's own options and their defaults, as `:set`/`:unset` toggle them.
# `compact` off means results are pretty-printed (see pretty.py).
OPTIONS = {"compact": False}

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
        session.reset()
        return ("message", "session cleared")
    words = command.split()
    if words and words[0] in (":set", ":unset"):
        return ("message", _set_option(session, words[0] == ":set", words[1:]))
    return None


def _set_option(session: "Session", on: bool, names: list) -> str:
    """`:set name` / `:unset name`, and bare `:set` as "what are they?".
    Answers the line to show - an unknown name is reported rather than
    quietly doing nothing, since a typo'd option would otherwise look like
    it had taken effect."""
    if not names:
        return "\n".join(f"{name}  {'on' if value else 'off'}"
                         for name, value in sorted(session.options.items()))
    said = []
    for name in names:
        if name not in session.options:
            known = ", ".join(sorted(session.options))
            return f"unknown option {name!r} - known options: {known}"
        session.options[name] = on
        said.append(f"{name} {'on' if on else 'off'}")
    return ", ".join(said)


class Session:
    """One REPL session: a scope that outlives each entry, plus the
    evaluate-and-render step both front ends drive."""

    def __init__(self, script_root: "str | None" = None):
        self.entries = 0
        self.options = dict(OPTIONS)
        # How wide a result may be before it's broken across lines; a front
        # end that knows its terminal sets this (see run_readline and the
        # TUI's on_resize).
        self.width = pretty_mod.DEFAULT_WIDTH
        self.reset(script_root)

    def reset(self, script_root: "str | None" = None) -> None:
        # The working directory plays the role a script's own directory
        # plays for `wyrm script.wy` (see cli.py), so `import static
        # decolib` finds a module sitting next to where the REPL was
        # started, with no WYRM_PATH set.
        wyrm_modules.set_script_root(script_root or os.getcwd())
        self.ctx = Scope()
        populate_globals(self.ctx)
        expose(self.ctx, "__ARGS", ())

    def evaluate(self, source: str) -> Result:
        """Parses and runs one entry, capturing anything it printed. Never
        raises: a syntax error, a wyrm error value and a Python-level
        exception out of the evaluator all come back as `Result.error`,
        since a REPL that dies on a typo is no REPL at all."""
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
# readline front end
# --------------------------------------------------------------------------

def _install_readline() -> None:
    """Line editing, history and a history file, when the platform has
    readline; a terminal without it still gets a working (if plainer)
    prompt, so this failing is not fatal."""
    try:
        import readline
    except ImportError:
        return
    history = os.path.expanduser("~/.wyrm_history")
    try:
        readline.read_history_file(history)
    except OSError:
        pass
    readline.set_history_length(1000)
    import atexit
    atexit.register(_save_history, readline, history)


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
    _install_readline()
    if banner:
        console.print(f"[dim]{BANNER}[/dim]")

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
