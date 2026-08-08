"""The REPL engine: when an entry is still being typed (is_incomplete), and
what evaluating one in a persistent session answers (Session.evaluate).

The front ends are tested separately - test_repl_tui.py for the Textual UI;
the readline loop's own wiring is covered here through run_command, which is
the only piece of it that isn't `input()`."""
import pytest

from wypoc.repl import Result, Session, is_incomplete, run_command


@pytest.mark.parametrize("source", [
    "f(1,",                     # unclosed call
    "x = [1, 2",                # unclosed array
    "x = '(1 . 2",              # unclosed pair list
    'x = "abc',                 # unterminated string
    "fn add(a, b):",            # block header, body still to come
    "if x:",
    "class Point:",
    "if x:\n    y = 1",         # inside a block, last line not blank
])
def test_incomplete_entries_keep_prompting(source):
    assert is_incomplete(source)


@pytest.mark.parametrize("source", [
    "",
    "   ",
    "1 + 2",
    "var x = 5",
    "f(1,\n2)",                     # bracket closed again, however many lines
    "x = '{1: 2}",
    "if x: y = 1",                  # a one-line block is a finished entry
    "fn f(a):\n    return a\n",     # blank last line ends the block
    "if x:\n    y = 1\n",
])
def test_complete_entries_are_run(source):
    assert not is_incomplete(source)


def test_a_broken_entry_is_not_treated_as_unfinished():
    # A tokenizer complaint that isn't "you stopped half way" has to reach
    # the user as an error, not as a prompt that never returns.
    assert not is_incomplete("x = `nope`")


def test_expression_value_is_displayed():
    result = Session().evaluate("1 + 2")
    assert result.display == "3"
    assert not result.failed


def test_statements_with_no_value_display_nothing():
    session = Session()
    assert session.evaluate("nil").display is None
    assert session.evaluate("fn f():\n    return 1\n").display is None
    assert session.evaluate("import std::io").display is None


@pytest.mark.parametrize("source,shown", [
    ("x := 5", "5"),                    # := shorthand
    ("var y = 7", "7"),                 # var declaration
    ("a, b := 1, 2", "(1, 2)"),         # multi-target: what each name holds
])
def test_a_binding_answers_with_what_it_bound(source, shown):
    assert Session().evaluate(source).display == shown


def test_reassigning_an_existing_name_answers_with_its_new_value():
    session = Session()
    session.evaluate("var y = 7")
    assert session.evaluate("y = 9").display == "9"


def test_a_forward_declaration_binds_no_value_and_shows_none():
    result = Session().evaluate("var z: int")
    assert not result.failed
    assert result.display is None


def test_assigning_through_an_attribute_or_index_stays_quiet():
    # These mutate what a target expression points at rather than binding a
    # name, and re-evaluating that expression to show it could repeat a call.
    session = Session()
    session.evaluate("class P:\n    slot n: int = 3\n")
    session.evaluate("p := P()")
    session.evaluate("arr := [1, 2]")
    assert session.evaluate("p.n = 4").display is None
    assert session.evaluate("arr[0] = 9").display is None
    assert session.evaluate("arr").display == "[9, 2]", "but the assignment happened"


def test_strings_echo_in_repr_form():
    # `'hi'` (a str), told apart from `hi` (str's characters) and `'hi` (a
    # symbol) - see wyrm_builtins.display.
    assert Session().evaluate('"hi"').display == "'hi'"
    assert Session().evaluate("'hi").display == "'hi"


def test_bindings_survive_from_one_entry_to_the_next():
    session = Session()
    session.evaluate("var x = 41")
    assert session.evaluate("x + 1").display == "42"


def test_functions_defined_in_one_entry_are_callable_in_another():
    session = Session()
    assert not session.evaluate("fn add(a, b):\n    return a + b\n").failed
    assert session.evaluate("add(20, 22)").display == "42"


def test_classes_defined_in_one_entry_are_usable_in_another():
    session = Session()
    source = "class Point:\n    slot x: float = 2.0\n\n    fn twice() -> float:\n        return x * 2.0\n"
    assert not session.evaluate(source).failed
    session.evaluate("p := Point()")
    assert session.evaluate("p!twice()").display == "4.0"
    assert session.evaluate("p.x").display == "2.0"


def test_printed_output_is_captured_not_leaked_to_the_terminal(capsys):
    result = Session().evaluate('print("hello")')
    assert result.output == "hello"
    assert capsys.readouterr().out == ""


def test_syntax_errors_come_back_as_errors():
    result = Session().evaluate("fn (")
    assert result.failed
    assert result.error.startswith("SyntaxError:")


def test_runtime_errors_come_back_as_errors_and_leave_the_session_usable():
    session = Session()
    assert session.evaluate("nope").error.startswith("NameError:")
    assert session.evaluate("1 + 1").display == "2"


def test_a_wyrm_error_value_is_reported_as_an_error():
    result = Session().evaluate('error("bad")')
    assert result.failed
    assert "bad" in result.error


def test_imports_persist_across_entries():
    session = Session()
    assert not session.evaluate("from std::io import println").failed
    assert session.evaluate('println("x")').output == "x\n"


def test_clear_forgets_previous_bindings():
    session = Session()
    session.evaluate("var x = 1")
    assert run_command(session, ":clear") == ("message", "session cleared")
    assert session.evaluate("x").failed


def test_results_are_pretty_printed_by_default():
    session = Session()
    assert session.evaluate("$[1, 2, 3]").display == "(1 2 3)"


def test_set_compact_returns_to_one_line_results():
    session = Session()
    assert run_command(session, ":set compact") == ("message", "compact on")
    assert session.evaluate("$[1, 2, 3]").display == "$[1, 2, 3]"
    assert run_command(session, ":unset compact") == ("message", "compact off")
    assert session.evaluate("$[1, 2, 3]").display == "(1 2 3)"


def test_compact_is_off_to_begin_with():
    assert Session().options == {"compact": False}


def test_bare_set_lists_the_options_and_their_state():
    session = Session()
    assert run_command(session, ":set") == ("message", "compact  off")
    run_command(session, ":set compact")
    assert run_command(session, ":set") == ("message", "compact  on")


def test_an_unknown_option_is_reported_rather_than_ignored():
    session = Session()
    kind, message = run_command(session, ":set colour")
    assert kind == "message"
    assert "unknown option 'colour'" in message and "compact" in message
    assert session.options == {"compact": False}, "and nothing was changed"


def test_the_width_a_session_breaks_results_at_is_settable():
    session = Session()
    session.width = 12
    assert "\n" in session.evaluate('{"a": 1, "b": 2}').display
    session.width = 80
    assert "\n" not in session.evaluate('{"a": 1, "b": 2}').display


def test_quit_command_is_recognised():
    assert run_command(Session(), ":quit")[0] == "quit"
    assert run_command(Session(), " :q ")[0] == "quit"


def test_ordinary_source_is_not_a_command():
    assert run_command(Session(), "1 + 2") is None


def test_result_reports_failure():
    assert not Result("x").failed
    assert Result("x", error="boom").failed
