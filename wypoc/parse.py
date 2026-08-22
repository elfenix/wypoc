"""Driver: tokenize Wyrm source with wyrm_tokenizer and parse it with the
pegen-generated parser (wypoc/parser.py, built from wyrm.gram)."""
import sys
import threading
import token as token_mod

from pegen.tokenizer import Tokenizer

from wypoc import ast_nodes as ast
from wypoc.parser import GeneratedParser
from wypoc.wyrm_tokenizer import RESERVED_DOLLAR_NAMES, generate_tokens

# pegen's generated parser chains roughly 20 Python stack frames per level of
# expression nesting - one method per precedence rule in wyrm.gram's
# comparison/bitwise/shift/additive/mult/unary/power/postfix/primary/
# paren-group ladder - so even ~50 nested parens exhausts the interpreter's
# default 1000-frame recursion limit. Parsing on a dedicated thread with a
# generous C stack (rather than just raising sys.setrecursionlimit() in
# place, which would trade a clean RecursionError for a C-level segfault
# once the *actual* stack runs out) buys real headroom cheaply. Serialized
# with a lock because both knobs are process-global, not per-thread.
_PARSE_STACK_SIZE = 64 * 1024 * 1024  # 64 MiB
_PARSE_RECURSION_LIMIT = 20000
_parse_lock = threading.Lock()


def _run_with_big_stack(fn):
    """Runs fn() to completion on a worker thread with `_PARSE_STACK_SIZE`
    of C stack and `_PARSE_RECURSION_LIMIT` headroom, returning its result
    or re-raising whatever it raised (with its original traceback)."""
    result: dict = {}

    def target():
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, _PARSE_RECURSION_LIMIT))
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            result["error"] = exc
        finally:
            sys.setrecursionlimit(old_limit)

    with _parse_lock:
        previous_stack_size = threading.stack_size()
        threading.stack_size(_PARSE_STACK_SIZE)
        try:
            worker = threading.Thread(target=target)
            worker.start()
            worker.join()
        finally:
            threading.stack_size(previous_stack_size)

    if "error" in result:
        raise result["error"]
    return result["value"]

# `$ast` is a NAME token like any other now that `$` is an identifier
# character, and `wyrm.gram` matches it by its exact text (see scope_op).
# Reserving it - so `$ast = 1` or a parameter called `$ast` is a syntax
# error rather than an ordinary binding - means keeping it out of pegen's
# `name()` rule, which skips whatever is in the parser's KEYWORDS. The
# grammar can't put it there itself: pegen only treats a quoted string as a
# keyword when it matches `[a-zA-Z_]\w*`, which `$ast` does not. Extending
# the tuple here is the whole of the reservation - `expect("$ast")`
# compares token text and is unaffected by it.
GeneratedParser.KEYWORDS = GeneratedParser.KEYWORDS + tuple(sorted(RESERVED_DOLLAR_NAMES))

# How to describe a token that carries no useful text of its own, for the
# "unexpected ..." half of a syntax error message. A NEWLINE reported as
# `unexpected '\n'` reads as noise; "end of line" is what a person would
# say. (INDENT/DEDENT are the layout rule's own virtual tokens - see
# wyrm_tokenizer.py - and surface as an indentation complaint.)
_TOKEN_DESCRIPTIONS = {
    token_mod.NEWLINE: "end of line",
    token_mod.ENDMARKER: "end of file",
    token_mod.INDENT: "indent",
    token_mod.DEDENT: "dedent",
}


def _describe(tok) -> str:
    described = _TOKEN_DESCRIPTIONS.get(tok.type)
    if described is not None:
        return described
    text = tok.string.strip()
    return f"{text!r}" if text else "end of line"


def syntax_error(parser, filename: str = "<unknown>") -> SyntaxError:
    """The SyntaxError for a parse that failed, positioned over the whole
    offending token rather than a single column.

    pegen's own `Parser.make_syntax_error` reports a bare "invalid syntax"
    at `offset`..`offset` - a zero-width point, which an editor can only
    render as a one-character squiggle sitting next to the problem. The
    furthest token the parse reached (`diagnose()`) knows its own end, so
    the diagnostic can cover the actual token and name it.

    `end_lineno`/`end_offset` are the 6-tuple form of SyntaxError's args,
    which Python has understood since 3.10 (this package's floor)."""
    tok = parser._tokenizer.diagnose()
    start_line, start_col = tok.start
    end_line, end_col = tok.end
    return SyntaxError(
        f"unexpected {_describe(tok)}",
        (filename, start_line, start_col + 1, tok.line, end_line, end_col + 1),
    )


def _strip_comment_marker(stripped_line: str) -> str:
    """`# foo` / `#: foo` / `#foo` -> `foo`. `stripped_line` is a source line
    with leading/trailing whitespace already removed, known to start with
    `#` (see `_doc_comment_above`)."""
    text = stripped_line[1:]
    if text.startswith(":"):
        text = text[1:]
    if text.startswith(" "):
        text = text[1:]
    return text


def _doc_comment_above(lines: list, line_no: int):
    """The doc-comment text immediately preceding 1-based `line_no`, or
    `None` if the line directly above isn't a `#` comment.

    Collects the contiguous run of `#`-comment lines directly above
    `line_no` - no blank line in between - so a comment block only attaches
    to the declaration it's actually touching; a blank line between a
    comment and the next `fn`/`class` means "not a doc comment for this"."""
    collected = []
    idx = line_no - 2  # 0-based index of the line directly above line_no
    while idx >= 0:
        stripped = lines[idx].strip()
        if not stripped.startswith("#"):
            break
        collected.append(_strip_comment_marker(stripped))
        idx -= 1
    if not collected:
        return None
    collected.reverse()
    return "\n".join(collected)


def _attach_doc_comments(src: str, program: "ast.Program") -> None:
    """Populates `.doc` on every `FnDef`/`CoDef`/`ClassDef` in `program`
    from the sphinx/doxygen-style `#`/`#:` comment block directly above it
    (see `_doc_comment_above`), and `program.doc` from the file's leading
    comment block, when one exists and is set off from the first statement
    by a blank line (otherwise that block belongs to the first statement,
    not the module - see `help()` in wyrm_builtins.py, the consumer)."""
    lines = src.splitlines()

    def visit(node, outer_anchor=None):
        if isinstance(node, ast.Decorated):
            anchor = outer_anchor if outer_anchor is not None else node.pos
            visit(node.inner, outer_anchor=anchor)
            return
        if isinstance(node, (ast.FnDef, ast.CoDef, ast.ClassDef)):
            anchor = outer_anchor if outer_anchor is not None else node.pos
            if anchor is not None:
                node.doc = _doc_comment_above(lines, anchor[0])
        for child in node.children():
            visit(child)

    visit(program)

    lead_end = 0  # 0-based index of the first line after the file's leading comment block
    while lead_end < len(lines) and lines[lead_end].strip().startswith("#"):
        lead_end += 1
    if lead_end == 0:
        return
    lead_text = "\n".join(_strip_comment_marker(lines[i].strip()) for i in range(lead_end))

    if not program.body:
        program.doc = lead_text
        return
    first_stmt = program.body[0]
    if first_stmt.pos is not None and first_stmt.pos[0] > lead_end + 1:
        program.doc = lead_text


def parse(src: str, verbose: bool = False, filename: str = "<string>"):
    def _do_parse():
        tokengen = generate_tokens(src)
        tokenizer = Tokenizer(tokengen, verbose=verbose)
        parser = GeneratedParser(tokenizer, verbose=verbose)
        tree = parser.start()
        if tree is None:
            raise syntax_error(parser, filename)
        _attach_doc_comments(src, tree)
        return tree

    return _run_with_big_stack(_do_parse)


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path) as f:
        src = f.read()
    print(parse(src, verbose="-v" in sys.argv, filename=path))
