"""`wyrm --tui`: the REPL of repl.py driven by a full-screen Textual UI,
laid out the way an agentic coding tool's terminal UI is - the transcript
fills the screen, the prompt sits just above the bottom:

    +--------------------------------------------------+
    |  session log (scrollable, read-only, navigable)   |
    |--------------------------------------------------|
    |  > prompt input (grows as the entry does)         |
    |--------------------------------------------------|
    |  status bar                                       |
    +--------------------------------------------------+

Keys:
    enter        run the entry - unless it's still open (an unclosed
                  bracket, a trailing `:`, an unfinished block: exactly
                  what would draw a continuation prompt in readline mode),
                  in which case it inserts a newline instead
    shift+enter  always insert a newline, never run
    up / down    move by line inside the entry; at the buffer's first/last
                  line they step into history instead, landing on the last
                  (respectively first) line of the neighbouring entry
    ctrl+up      the whole previous entry, from wherever the cursor is
    ctrl+down    the whole next one (alt+p / alt+n do the same, per
                  readline's own history keys)
    ctrl+o, F6   move between the prompt and the log; whichever one doesn't
                  have focus keeps a dimmed "shadow" cursor, so it's
                  obvious where focus will land coming back
    ctrl+c       copy the selection (in either the log or the prompt)
    ctrl+q       quit

Both halves are real text areas, so the mouse works throughout: the wheel
scrolls the log's backlog, a click moves that pane's cursor (and focus),
and a drag selects text to copy. Selecting with the keyboard - shift with
the arrow/home/end keys - does the same from the log's cursor.

Evaluation runs on a worker thread, so a slow (or looping) entry leaves the
UI responsive rather than freezing the whole terminal; the prompt is
read-only for the duration and the status bar says so.
"""
from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Rule, Static, TextArea

from wypoc.pretty import DELIMITERS
from wypoc.repl import HELP, Result, Session, is_incomplete, run_command

# What the log puts in front of each kind of line - a submitted entry, and
# everything that entry answered.
ENTRY_MARKER = "> "
CONTINUATION_MARKER = "  "

# What `:help` adds to repl.py's own help text (see the module docstring -
# this is the same list, in the form the log shows it).
KEYS = """\
enter                run the entry (or continue an unfinished one)
shift+enter          add a line without running (ctrl+j / alt+enter too)
up / down            move by line; at the buffer's edges, step into history
ctrl+up / ctrl+down  the previous / next entry (alt+p / alt+n too)
ctrl+o, F6           switch between the prompt and the log
ctrl+c               copy the selection
ctrl+q               quit"""

# repl.py's banner names ctrl-d, which is the readline front end's way out;
# here the app's own quit key is the one to name.
BANNER = "wyrm REPL - :help for help, ctrl+q to quit"

# One level of indentation, for the prompt's auto-indent (see
# PromptArea._newline). wyrm's own samples and corelib are 4-space indented.
INDENT = "    "

# "there is no such entry" - distinct from None, which is a real position in
# history (the buffer being typed, past the newest entry).
_NO_MOVE = object()


class PromptArea(TextArea):
    """The input box. A TextArea (not an Input) because an entry is allowed
    to be several lines - a whole `fn`, a `class`, a `for` loop.

    It also owns the session's history. Recall is multi-line aware: `up` and
    `down` move by line inside the entry being typed, and only cross into
    history at the buffer's edges - `up` from the first line lands on the
    *last* line of the previous entry, so holding `up` walks back through a
    multi-line entry line by line and then on into the one before it (and
    `down` mirrors it, landing on the first line). ctrl+up/ctrl+down (and
    readline's own alt+p/alt+n) skip a whole entry at a time from wherever
    the cursor is.

    Edits made to a recalled entry are kept while browsing (`_drafts`), the
    way a shell does - including the half-typed buffer you were on when you
    started, which `down` past the newest entry brings back. Submitting
    clears all of that."""

    class Submitted(Message):
        def __init__(self, source: str) -> None:
            self.source = source
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(soft_wrap=True, tab_behavior="indent", **kwargs)
        self.show_line_numbers = False
        self.cursor_blink = True
        self._history: list = []
        # Which entry is being browsed: an index into _history, or None for
        # the buffer being typed (which is "after" the newest entry).
        self._index = None
        self._drafts: dict = {}  # index (None included) -> edited text

    @property
    def _draw_cursor(self) -> bool:
        """Keep drawing the cursor while focus is elsewhere - styled dimly
        by this module's CSS (`PromptArea:blur`), it's the "shadow cursor"
        marking where typing resumes on the way back from the log."""
        if not self.has_focus:
            return True
        return super()._draw_cursor

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            # `enter` submits a finished entry and continues an unfinished
            # one, so the same text that would keep prompting in readline
            # mode keeps growing here (see repl.is_incomplete).
            if is_incomplete(self.text):
                self._newline()
            else:
                self.post_message(self.Submitted(self.text))
            event.prevent_default()
            event.stop()
            return
        if event.key in ("shift+enter", "ctrl+j", "alt+enter"):
            # Terminals that speak the kitty keyboard protocol deliver a
            # real shift+enter; ctrl+j and alt+enter are the same request
            # for everything else.
            self._newline()
            event.prevent_default()
            event.stop()
            return
        if event.key in ("ctrl+up", "alt+p") or (
                event.key == "up" and self.cursor_location[0] == 0):
            if self.recall(-1):
                event.prevent_default()
                event.stop()
                return
        if event.key in ("ctrl+down", "alt+n") or (
                event.key == "down" and self.cursor_location[0] == self.document.line_count - 1):
            if self.recall(1):
                event.prevent_default()
                event.stop()
                return
        await super()._on_key(event)

    # -- history -----------------------------------------------------------

    def remember(self, source: str) -> None:
        """Files a submitted entry and leaves history browsing - the next
        `up` starts from the newest entry again. A repeat of the entry just
        before it isn't filed twice, the way a shell's history behaves."""
        if source.strip() and (not self._history or self._history[-1] != source):
            self._history.append(source)
        self._index = None
        self._drafts.clear()

    def recall(self, step: int) -> bool:
        """Moves `step` entries through history (-1 = older, +1 = newer),
        answering whether there was anywhere to go - False leaves the key
        that asked for it to do its ordinary job (`up` on the first line of
        the oldest entry is just `up`)."""
        target = self._neighbour(step)
        if target is _NO_MOVE:
            return False
        self._drafts[self._index] = self.text
        self._index = target
        self.load_text(self._entry_text(target))
        # Coming from below, land on the entry's last line so the next `up`
        # keeps walking up through it; coming from above, its first line.
        # Returning to the half-typed buffer lands where typing left off.
        at_end = step < 0 or target is None
        self.move_cursor(self.document.end if at_end else (0, 0))
        return True

    def _neighbour(self, step: int):
        """The index `step` entries away, or `_NO_MOVE` at either end."""
        if step < 0:
            if self._index is None:
                return len(self._history) - 1 if self._history else _NO_MOVE
            return self._index - 1 if self._index > 0 else _NO_MOVE
        if self._index is None:
            return _NO_MOVE
        return self._index + 1 if self._index + 1 < len(self._history) else None

    def _entry_text(self, index) -> str:
        if index in self._drafts:
            return self._drafts[index]
        return "" if index is None else self._history[index]

    def _newline(self) -> None:
        """A newline that keeps the entry's shape: the current line's own
        indentation, one level deeper when that line opens a block. Without
        it every continuation line of a `fn`/`class`/`for` would start back
        at column 0 and have to be re-indented by hand."""
        row, _ = self.cursor_location
        line = self.document.get_line(row)
        indent = line[:len(line) - len(line.lstrip())]
        if line.strip().endswith(":"):
            indent += INDENT
        self.insert("\n" + indent)


class SessionLog(TextArea):
    """The transcript: a read-only TextArea rather than a RichLog, so it has
    the things a transcript you actually want to *read* needs - a cursor to
    navigate with, keyboard and mouse selection, and copy - which a widget
    that only knows how to append lines can't offer. Nothing here edits the
    document except this class's own `write_*` methods (`read_only` blocks
    every keyboard edit path; `insert` below is deliberately not one).

    Each document line remembers what kind of thing it is (`_kinds`), and
    `get_line` colours it accordingly - that's how one widget holding one
    plain-text document still renders entries, values, output, errors and
    notes differently, and it keeps the *copied* text clean: what you select
    and paste is the text, without any of the styling."""

    COMPONENT_CLASSES = TextArea.COMPONENT_CLASSES | {
        "session-log--entry",
        "session-log--output",
        "session-log--value",
        "session-log--error",
        "session-log--note",
        "session-log--delimiter",
    }

    def __init__(self, **kwargs) -> None:
        super().__init__("", read_only=True, show_cursor=True, soft_wrap=True,
                         highlight_cursor_line=False, **kwargs)
        self._kinds: list = []  # one style kind per document line

    @property
    def _draw_cursor(self) -> bool:
        """Same shadow cursor the prompt keeps (see PromptArea): the log's
        place is still marked while focus is in the prompt."""
        if not self.has_focus:
            return True
        return super()._draw_cursor

    # The highlighted cursor *line* is a different matter from the cursor
    # itself: it's a wide band, and while you're typing at the prompt it
    # just draws the eye to a line of transcript you aren't working on. It
    # appears when the log is where the keys are going, and not before.
    def on_focus(self) -> None:
        self.highlight_cursor_line = True

    def on_blur(self) -> None:
        self.highlight_cursor_line = False

    async def _on_key(self, event) -> None:
        # Nothing in a read-only document wants `enter`, and reaching for it
        # after reading (or copying) something means "right, back to
        # typing" - so it hands focus back to the prompt, as `escape` does.
        if event.key in ("enter", "escape"):
            self.post_message(self.Dismissed())
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)

    class Dismissed(Message):
        """The log is done being read - give the prompt focus back."""

    def get_line(self, line_index: int):
        line = super().get_line(line_index)
        if line_index >= len(self._kinds):
            return line
        kind = self._kinds[line_index]
        line.stylize(self.get_component_rich_style(f"session-log--{kind}"))
        if kind == "value":
            # Pick the structure out of the data - the parens of a pair
            # list, the braces and brackets of a dict or array (see
            # pretty.py, which produced them).
            line.highlight_regex(
                DELIMITERS, style=self.get_component_rich_style(
                    "session-log--delimiter"))
        return line

    # -- writing -----------------------------------------------------------

    def write_entry(self, source: str) -> None:
        """Echoes a submitted entry. A multi-line entry is collapsed to its
        first line plus `...` - the log is a record of what was run, and a
        20-line class definition re-printed in full would bury the answers
        it produced."""
        lines = source.strip().splitlines() or [""]
        text = ENTRY_MARKER + lines[0] + (" ..." if len(lines) > 1 else "")
        self._write(text, "entry")

    def write_output(self, output: str) -> None:
        self._write(_indent(output.rstrip("\n")), "output")

    def write_value(self, display: str) -> None:
        self._write(_indent(display), "value")

    def write_error(self, error: str) -> None:
        self._write(_indent(error), "error")

    def write_note(self, note: str) -> None:
        self._write(note, "note")

    def _write(self, text: str, kind: str) -> None:
        # Every line written is newline-terminated, so the document always
        # ends with an empty line - the place the cursor rests, at column 0,
        # the way a terminal leaves it under the last thing printed.
        self.insert(text + "\n", location=self.document.end)
        self._kinds.extend([kind] * len(text.split("\n")))
        # Follow the tail only while the log isn't the focused widget:
        # once it is, the user is reading (or selecting) somewhere, and
        # yanking them to the bottom mid-selection would be maddening.
        if not self.has_focus:
            self.move_cursor(self.document.end)
            self.scroll_end(animate=False)


def _indent(text: str) -> str:
    """Everything an entry answered is indented under it, however many
    lines it came out as."""
    return CONTINUATION_MARKER + text.replace("\n", "\n" + CONTINUATION_MARKER)


class StatusBar(Static):
    """The bottom bar: what the session has done, and what focus is on."""

    # The keys worth naming depend on where focus is - the full list is what
    # `:help` is for. This bar is one line and has to stay readable at 80
    # columns, so it names the two or three that aren't guessable from
    # having used any other REPL.
    HINTS = {
        "prompt": "^o=switch  ^up/dn=history  ^q=quit",
        "log": "^c=copy  enter=prompt  ^o=switch  ^q=quit",
    }

    def update_status(self, entries: int, focus: str, busy: bool) -> None:
        state = "running..." if busy else "ready"
        text = Text()
        text.append(f" wyrm {state} ", style="bold")
        text.append(f"| {entries} entr{'y' if entries == 1 else 'ies'} ")
        text.append(f"| {focus} ", style="dim")
        text.append(f"| {self.HINTS[focus]}", style="dim")
        self.update(text)


class WyrmReplApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    SessionLog {
        height: 1fr;
        padding: 0 1;
        border: none;
        background: $background;
        scrollbar-size-vertical: 1;
    }
    /* The submitted entry stands out as an inverted band - the transcript
       is read by scanning for "what did I run?", so that line is the one
       the eye should land on. */
    SessionLog .session-log--entry {
        background: $accent;
        color: $background;
        text-style: bold;
    }
    SessionLog .session-log--value {
        color: $text-accent;
    }
    SessionLog .session-log--delimiter {
        color: $text-primary;
        text-style: bold;
    }
    SessionLog .session-log--error {
        color: $text-error;
        text-style: bold;
    }
    SessionLog .session-log--note {
        color: $text-muted;
    }
    SessionLog:blur .text-area--cursor {
        background: $foreground 30%;
        color: $background;
        text-style: none;
    }
    Rule.-separator {
        height: 1;
        margin: 0;
        color: $panel-lighten-2;
    }
    #prompt-row {
        height: auto;
        max-height: 12;
        padding: 0 1;
        background: $background;
    }
    #prompt-marker {
        width: 2;
        padding: 0;
        color: $accent;
        text-style: bold;
    }
    PromptArea {
        height: auto;
        max-height: 12;
        width: 1fr;
        border: none;
        padding: 0;
        background: $background;
    }
    PromptArea:blur .text-area--cursor {
        background: $foreground 30%;
        color: $background;
        text-style: none;
    }
    StatusBar {
        height: 1;
        background: $panel;
        color: $text;
    }
    """

    BINDINGS = [
        # ctrl+o - "other pane", the emacs C-x o mnemonic - because it's
        # free in every terminal and unambiguous in the bytes (unlike ctrl+j,
        # which *is* line feed and so arrives as `enter` on any terminal
        # without the kitty keyboard protocol). F6 is the same command under
        # the desktop convention for cycling panes. priority, so neither
        # text area sees it first.
        Binding("ctrl+o", "toggle_focus", "Prompt/log", priority=True, show=False),
        Binding("f6", "toggle_focus", "Prompt/log", priority=True, show=False),
    ]
    # ctrl+c is deliberately *not* bound to quit: in both the log and the
    # prompt it copies the selection, which is the whole point of having a
    # cursor in the log. Textual's own default bindings cover the rest -
    # ctrl+q quits, and a reflexive ctrl+c with nothing selected answers
    # with a "press ctrl+q to quit" notification rather than doing nothing.

    def __init__(self, session: "Session | None" = None) -> None:
        super().__init__()
        self.session = session or Session()
        self.busy = False

    def compose(self) -> ComposeResult:
        yield SessionLog(id="log")
        yield Rule(classes="-separator")
        # The marker is a widget beside the input rather than text inside
        # it: a TextArea has no gutter to put a prompt in, and text in the
        # buffer would be text the user could edit (or submit).
        with Horizontal(id="prompt-row"):
            yield Static(ENTRY_MARKER, id="prompt-marker")
            yield PromptArea(id="prompt")
        yield Rule(classes="-separator")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.log_widget = self.query_one(SessionLog)
        self.prompt = self.query_one(PromptArea)
        self.status = self.query_one(StatusBar)
        self.log_widget.write_note(BANNER)
        self.prompt.focus()
        self._refresh_status()

    # -- focus -------------------------------------------------------------

    def on_session_log_dismissed(self, message: SessionLog.Dismissed) -> None:
        self.prompt.focus()
        self._refresh_status()

    def action_toggle_focus(self) -> None:
        if self.prompt.has_focus:
            self.log_widget.focus()
        else:
            self.prompt.focus()
        self._refresh_status()

    def _refresh_status(self) -> None:
        focus = "prompt" if self.prompt.has_focus else "log"
        self.status.update_status(self.session.entries, focus, self.busy)

    def on_descendant_focus(self) -> None:
        self._refresh_status()

    def on_descendant_blur(self) -> None:
        self._refresh_status()

    # -- running an entry --------------------------------------------------

    def on_prompt_area_submitted(self, message: PromptArea.Submitted) -> None:
        source = message.source
        if not source.strip():
            return
        self.prompt.remember(source)
        self.prompt.text = ""
        # The width a result may use before it's broken across lines - read
        # now rather than cached, so a resized terminal is accounted for.
        self.session.width = max(
            20, self.log_widget.content_size.width - len(CONTINUATION_MARKER))
        self.log_widget.write_entry(source)

        command = run_command(self.session, source)
        if command is not None:
            kind, payload = command
            if kind == "quit":
                self.exit()
            else:
                # repl.py's help describes the readline prompt's rules; the
                # keys are this front end's own business, so `:help` here
                # answers with both.
                if payload is HELP:
                    payload = f"{payload}\n\n{KEYS}"
                self.log_widget.write_note(payload)
            self._refresh_status()
            return

        self.busy = True
        self.prompt.read_only = True
        self._refresh_status()
        self._evaluate(source)

    @work(thread=True)
    def _evaluate(self, source: str) -> None:
        """Runs one entry off the UI thread; `call_from_thread` is how its
        answer gets back onto it."""
        result = self.session.evaluate(source)
        self.call_from_thread(self._show_result, result)

    def _show_result(self, result: Result) -> None:
        if result.output:
            self.log_widget.write_output(result.output)
        if result.error:
            self.log_widget.write_error(result.error)
        elif result.display:
            self.log_widget.write_value(result.display)
        self.busy = False
        self.prompt.read_only = False
        self._refresh_status()


def run_tui(session: "Session | None" = None) -> int:
    WyrmReplApp(session).run()
    return 0
