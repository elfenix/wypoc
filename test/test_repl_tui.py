"""The Textual front end (`wyrm --tui`): key behaviour and what lands in the
log. Driven headlessly through Textual's own test pilot - no terminal
involved, so these run in CI like any other test.

Textual's `run_test` is async and this suite has no async plugin, so each
test drives one `asyncio.run` of a small coroutine."""
import asyncio

import pytest

pytest.importorskip("textual")

from wypoc.repl_tui import PromptArea, SessionLog, StatusBar, WyrmReplApp  # noqa: E402


def drive(steps):
    """Runs `steps(pilot, app)` against a freshly started app, and answers
    whatever it answered."""

    async def run():
        app = WyrmReplApp()
        async with app.run_test(size=(80, 24)) as pilot:
            return await steps(pilot, app)

    return asyncio.run(run())


async def submit(pilot, app, source: str) -> None:
    """Types `source` and runs it, waiting for the worker thread that
    evaluates it (see WyrmReplApp._evaluate) to finish."""
    for line in source.split("\n"):
        await pilot.press(*line)
        await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


def log_text(app) -> str:
    return app.query_one(SessionLog).text


def rendered_line(app, y: int):
    """The segments of one rendered screen row - text plus the style it was
    drawn with, for the styling assertions below."""
    strips = app.screen._compositor.render_strips()
    return [(segment.text, segment.style) for segment in strips[y]]


async def wheel(pilot, log, event_type, times: int = 5) -> None:
    """A mouse wheel turn over the log, as the terminal would deliver it."""
    for _ in range(times):
        log.post_message(event_type(
            widget=log, x=1, y=1, delta_x=0, delta_y=1, button=0,
            shift=False, meta=False, ctrl=False, screen_x=1, screen_y=1,
            style=None))
    await pilot.pause()


def test_a_finished_entry_runs_on_enter():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        return log_text(app), app.query_one(PromptArea).text

    log, prompt = drive(steps)
    assert "> 1 + 2" in log
    assert "3" in log
    assert prompt == "", "the prompt is cleared once its entry has been submitted"


def test_enter_on_an_unfinished_entry_adds_a_line_instead_of_running():
    async def steps(pilot, app):
        await pilot.press(*"fn f(a):")
        await pilot.press("enter")
        await pilot.pause()
        return app.query_one(PromptArea).text, log_text(app)

    prompt, log = drive(steps)
    # Still being typed: it grew a line (auto-indented) rather than running.
    assert prompt == "fn f(a):\n    "
    assert "> fn f(a):" not in log


def test_shift_enter_adds_a_line_to_an_entry_that_would_otherwise_run():
    async def steps(pilot, app):
        await pilot.press(*"1 + 2")
        await pilot.press("shift+enter")
        await pilot.pause()
        return app.query_one(PromptArea).text, log_text(app)

    prompt, log = drive(steps)
    assert prompt == "1 + 2\n"
    assert "3" not in log


def test_backspace_in_leading_whitespace_steps_back_a_whole_indent():
    async def steps(pilot, app):
        await pilot.press(*"fn f(a):")
        await pilot.press("enter")  # auto-indents to one level: "    "
        await pilot.press("backspace")
        await pilot.pause()
        return app.query_one(PromptArea).text

    assert drive(steps) == "fn f(a):\n"


def test_backspace_in_whitespace_snaps_down_to_the_indent_below_it():
    async def steps(pilot, app):
        await pilot.press(*"fn f(a):")
        await pilot.press("enter")     # "    "
        await pilot.press(*"if x:")
        await pilot.press("enter")     # "        "
        await pilot.press("space")     # "         " - one past the indent
        await pilot.press("backspace")
        await pilot.pause()
        return app.query_one(PromptArea).text

    text = drive(steps)
    assert text.endswith("\n        "), "back to the indent it was one past"


def test_backspace_with_code_before_the_cursor_deletes_one_character():
    async def steps(pilot, app):
        await pilot.press(*"1 + 2")
        await pilot.press("backspace")
        await pilot.pause()
        return app.query_one(PromptArea).text

    assert drive(steps) == "1 + "


def test_backspace_at_column_zero_joins_with_the_line_above():
    async def steps(pilot, app):
        await pilot.press(*"1 + 2")
        await pilot.press("shift+enter")
        await pilot.press("backspace")
        await pilot.pause()
        return app.query_one(PromptArea).text

    assert drive(steps) == "1 + 2"


def test_a_multi_line_definition_runs_and_is_callable_afterwards():
    async def steps(pilot, app):
        await submit(pilot, app, "fn double(a):\nreturn a * 2\n")
        await submit(pilot, app, "double(21)")
        return log_text(app)

    log = drive(steps)
    assert "42" in log


def test_a_multi_line_entry_is_collapsed_in_the_log():
    async def steps(pilot, app):
        await submit(pilot, app, "fn double(a):\nreturn a * 2\n")
        return log_text(app)

    log = drive(steps)
    assert "> fn double(a): ..." in log
    assert "return a * 2" not in log


def test_output_and_errors_reach_the_log():
    async def steps(pilot, app):
        await submit(pilot, app, 'print("hello")')
        await submit(pilot, app, "nope")
        return log_text(app)

    log = drive(steps)
    assert "hello" in log
    assert "NameError" in log


def test_ctrl_o_moves_focus_between_prompt_and_log():
    async def steps(pilot, app):
        first = type(app.focused)
        await pilot.press("ctrl+o")
        await pilot.pause()
        second = type(app.focused)
        await pilot.press("ctrl+o")
        await pilot.pause()
        return first, second, type(app.focused)

    first, second, third = drive(steps)
    assert (first, second, third) == (PromptArea, SessionLog, PromptArea)


def test_f6_switches_focus_too():
    async def steps(pilot, app):
        await pilot.press("f6")
        await pilot.pause()
        return type(app.focused)

    assert drive(steps) is SessionLog


def test_up_from_the_first_line_recalls_the_previous_entry_at_its_last_line():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        await submit(pilot, app, "fn f(a):\nreturn a\n")
        prompt = app.query_one(PromptArea)
        await pilot.press("up")
        await pilot.pause()
        return prompt.text, prompt.cursor_location, prompt.document.line_count

    text, cursor, lines = drive(steps)
    assert text.startswith("fn f(a):")
    assert cursor[0] == lines - 1, "the cursor lands on the entry's last line"


def test_up_walks_a_recalled_entry_line_by_line_before_leaving_it():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        await submit(pilot, app, "fn f(a):\nreturn a\n")
        prompt = app.query_one(PromptArea)
        await pilot.press("up")
        await pilot.pause()
        rows = [prompt.cursor_location[0]]
        texts = [prompt.text]
        for _ in range(prompt.document.line_count - 1):
            await pilot.press("up")
            await pilot.pause()
            rows.append(prompt.cursor_location[0])
            texts.append(prompt.text)
        await pilot.press("up")  # off the top of it, into the older entry
        await pilot.pause()
        return rows, texts, prompt.text

    rows, texts, after = drive(steps)
    assert rows == sorted(rows, reverse=True) and rows[-1] == 0
    assert len(set(texts)) == 1, "moving inside the entry doesn't change it"
    assert after == "1 + 2"


def test_down_from_the_last_line_recalls_the_next_entry_at_its_first_line():
    async def steps(pilot, app):
        await submit(pilot, app, "fn f(a):\nreturn a\n")
        await submit(pilot, app, "1 + 2")
        prompt = app.query_one(PromptArea)
        await pilot.press("ctrl+up")
        await pilot.press("ctrl+up")  # back to the fn
        await pilot.pause()
        on_fn = prompt.text
        await pilot.press("down")
        await pilot.pause()
        return on_fn, prompt.text, prompt.cursor_location

    on_fn, text, cursor = drive(steps)
    assert on_fn.startswith("fn f(a):")
    assert text == "1 + 2"
    assert cursor[0] == 0, "the cursor lands on the entry's first line"


@pytest.mark.parametrize("older,newer", [("ctrl+up", "ctrl+down"), ("alt+p", "alt+n")])
def test_history_keys_jump_a_whole_entry_from_anywhere_in_the_buffer(older, newer):
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        await submit(pilot, app, "fn f(a):\nreturn a\n")
        prompt = app.query_one(PromptArea)
        await pilot.press(older)
        await pilot.pause()
        first = prompt.text
        # Mid-buffer, where a plain `up` would only move a line.
        prompt.move_cursor((1, 0))
        await pilot.press(older)
        await pilot.pause()
        second = prompt.text
        await pilot.press(newer)
        await pilot.pause()
        return first, second, prompt.text

    first, second, back = drive(steps)
    assert first.startswith("fn f(a):")
    assert second == "1 + 2"
    assert back.startswith("fn f(a):")


def test_coming_back_down_restores_the_half_typed_entry():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        prompt = app.query_one(PromptArea)
        await pilot.press(*"half typed")
        await pilot.press("ctrl+up")
        await pilot.pause()
        recalled = prompt.text
        await pilot.press("ctrl+down")
        await pilot.pause()
        return recalled, prompt.text, prompt.cursor_location

    recalled, restored, cursor = drive(steps)
    assert recalled == "1 + 2"
    assert restored == "half typed"
    assert cursor == (0, len("half typed")), "typing resumes where it left off"


def test_history_does_not_file_the_same_entry_twice_in_a_row():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        await submit(pilot, app, "1 + 2")
        return list(app.query_one(PromptArea)._history)

    assert drive(steps) == ["1 + 2"]


def test_the_log_only_highlights_its_cursor_line_when_it_has_focus():
    async def steps(pilot, app):
        log = app.query_one(SessionLog)
        await submit(pilot, app, "1 + 2")
        blurred = log.highlight_cursor_line
        await pilot.press("ctrl+o")
        await pilot.pause()
        focused = log.highlight_cursor_line
        await pilot.press("ctrl+o")
        await pilot.pause()
        return blurred, focused, log.highlight_cursor_line

    blurred, focused, blurred_again = drive(steps)
    assert not blurred, "no highlighted line in the log while typing at the prompt"
    assert focused
    assert not blurred_again


def test_the_prompt_keeps_a_shadow_cursor_while_the_log_has_focus():
    async def steps(pilot, app):
        prompt = app.query_one(PromptArea)
        await pilot.press("ctrl+o")
        await pilot.pause()
        return prompt.has_focus, prompt._draw_cursor

    has_focus, draws_cursor = drive(steps)
    assert not has_focus
    assert draws_cursor


def test_the_log_is_read_only():
    async def steps(pilot, app):
        await pilot.press("ctrl+o")
        await pilot.press(*"xyz")
        await pilot.pause()
        return log_text(app), app.query_one(PromptArea).text

    log, prompt = drive(steps)
    assert "xyz" not in log
    assert prompt == "", "typing into the log doesn't leak into the prompt either"


def test_the_status_bar_tracks_what_the_session_has_run():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        status = app.query_one(StatusBar)
        return str(status.render()), app.session.entries

    rendered, entries = drive(steps)
    assert entries == 1
    assert "1 entry" in rendered
    assert "prompt" in rendered, "the bar names which pane has focus"


def test_quit_command_exits():
    async def steps(pilot, app):
        await submit(pilot, app, ":quit")
        return app.is_running

    assert drive(steps) is False


def test_the_log_keeps_a_cursor_of_its_own():
    async def steps(pilot, app):
        log = app.query_one(SessionLog)
        blurred = log._draw_cursor  # focus starts in the prompt
        await pilot.press("ctrl+o")
        await pilot.pause()
        return blurred, log._draw_cursor

    blurred, focused = drive(steps)
    assert blurred, "the log keeps a shadow cursor while the prompt has focus"
    assert focused


def test_dragging_the_mouse_over_the_log_selects_text_and_ctrl_c_copies_it():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        await pilot.mouse_down(SessionLog, offset=(2, 1))
        await pilot.hover(SessionLog, offset=(9, 1))
        await pilot.mouse_up(SessionLog, offset=(9, 1))
        await pilot.pause()
        selected = app.query_one(SessionLog).selected_text
        await pilot.press("ctrl+c")
        await pilot.pause()
        return selected, app.clipboard

    selected, clipboard = drive(steps)
    assert "1 + 2" in selected
    assert clipboard == selected, "ctrl+c copies exactly what's selected"


def test_clicking_the_log_moves_focus_and_enter_hands_it_back():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        await pilot.click(SessionLog, offset=(3, 1))
        await pilot.pause()
        clicked = type(app.focused)
        await pilot.press("enter")
        await pilot.pause()
        return clicked, type(app.focused)

    clicked, after_enter = drive(steps)
    assert (clicked, after_enter) == (SessionLog, PromptArea)


def test_the_wheel_scrolls_the_backlog_and_new_entries_return_to_the_tail():
    from textual.events import MouseScrollDown, MouseScrollUp

    async def steps(pilot, app):
        for n in range(12):
            await submit(pilot, app, f"{n} + 100")
        log = app.query_one(SessionLog)
        at_tail = log.scroll_offset.y
        await wheel(pilot, log, MouseScrollUp)
        scrolled_back = log.scroll_offset.y
        await wheel(pilot, log, MouseScrollDown)
        return log.max_scroll_y, at_tail, scrolled_back, log.scroll_offset.y

    max_scroll, at_tail, scrolled_back, back_at_tail = drive(steps)
    assert max_scroll > 0, "twelve entries is more than one screen of log"
    assert at_tail == max_scroll, "the log follows its tail as entries arrive"
    assert scrolled_back < at_tail
    assert back_at_tail == max_scroll


def test_the_submitted_entry_is_the_line_that_stands_out():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        # Row 0 is the banner, row 1 the entry, row 2 its value.
        return rendered_line(app, 1), rendered_line(app, 2)

    entry, value = drive(steps)
    entry_style = next(style for text, style in entry if "1 + 2" in text)
    value_style = next(style for text, style in value if "3" in text)
    # Inverted: the entry's own background, with the log's background as its
    # text colour - the value line has neither.
    assert entry_style.bgcolor != value_style.bgcolor
    assert entry_style.bold


def test_the_log_ends_with_an_empty_line_and_rests_the_cursor_on_it():
    async def steps(pilot, app):
        await submit(pilot, app, "1 + 2")
        log = app.query_one(SessionLog)
        return log.text, log.cursor_location, log.document.line_count

    text, cursor, lines = drive(steps)
    assert text.endswith("\n"), "every written line is newline-terminated"
    assert cursor == (lines - 1, 0), "the cursor rests at the start of the next line"


def test_a_pretty_printed_result_reaches_the_log_indented_line_by_line():
    async def steps(pilot, app):
        await submit(pilot, app, "class Point:\nslot x: float = 1.0\nslot y: float = 2.0\n")
        await submit(pilot, app, "Point()")
        return log_text(app)

    log = drive(steps)
    assert "  Point:" in log
    assert "      x: 1.0" in log, "the value's own indentation survives the log's"


def test_the_delimiters_of_a_value_are_highlighted():
    async def steps(pilot, app):
        await submit(pilot, app, "$[1, 2]")
        # Row 0 is the banner, row 1 the entry, row 2 its value: `  (1 2)`.
        return rendered_line(app, 2)

    segments = drive(steps)
    paren = next(style for text, style in segments if text == "(")
    digits = next(style for text, style in segments if "1" in text)
    assert paren.color != digits.color, "parens are picked out from the data"
    assert paren.bold


def test_set_compact_reaches_the_session_from_the_prompt():
    async def steps(pilot, app):
        await submit(pilot, app, ":set compact")
        await submit(pilot, app, "$[1, 2]")
        return log_text(app), app.session.options["compact"]

    log, compact = drive(steps)
    assert compact
    assert "$[1, 2]" in log


def test_widgets_are_stacked_log_prompt_status_top_to_bottom():
    async def steps(pilot, app):
        return [(type(w).__name__, w.region.y) for w in app.screen.children]

    order = drive(steps)
    assert [name for name, _ in order] == [
        "SessionLog", "Rule", "Horizontal", "Rule", "StatusBar"]
    assert [y for _, y in order] == sorted(y for _, y in order)
