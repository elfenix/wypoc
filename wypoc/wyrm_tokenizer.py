"""Hand-rolled tokenizer for Wyrm source, producing tokenize.TokenInfo-compatible
tokens that pegen's Tokenizer wrapper (and pegen-generated parsers) can consume.

Python's stdlib `tokenize` module can't be reused as-is: Wyrm uses a bare `'`
sigil (symbol literals, `'(` pair-lists, `'{` dicts) which the stdlib tokenizer
treats as the start of a string literal, plus operators (`::`, `?=`, `<=>`,
`!`, `$`) it doesn't know about. This tokenizer implements Wyrm's own lexical
rules directly (see doc/grammar.ebnf section 0), including Haskell/Python-style
layout (INDENT/DEDENT) and bracket-depth-aware implicit line joining.
"""
import token
import tokenize
from typing import Iterator, List, Tuple

TokenInfo = tokenize.TokenInfo

KEYWORDS = {
    "and", "break", "catch", "class", "co", "continue", "defer", "defined",
    "elif", "else", "false", "fn", "for", "from", "getter", "if", "import",
    "in", "is", "not", "or", "pass", "return", "setter",
    "slot", "static", "super", "this", "true", "try", "undefined", "using",
    "while", "with", "yield",
}
# Not included above: "init" is no longer a reserved word - a class
# constructor is just an ordinary method named `init` (see wyrm.gram's
# class_member_item/fn_def). "new" was dropped entirely: classes are
# constructed by calling them like any other value (`MyClass(args)`).
# Not included above: "on" / "error" are soft keywords, reserved only in the
# `defer on error` construct (see wyrm.gram) - they remain valid identifiers
# elsewhere (e.g. `slot on: bool`).

# Multi-character operators, longest first so scanning is maximal-munch.
MULTI_OPS = sorted(
    ["**", "->", "<-", "<=", ">=", "==", "!=", "::", "?="],
    key=len, reverse=True,
)

SINGLE_OPS = set("()[]{}.,:+-*/%&|^<>=!$'")

OPEN_BRACKETS = "(["
CLOSE_BRACKETS = ")]"


class TokenizeError(SyntaxError):
    pass


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident_cont(c: str) -> bool:
    return c.isalnum() or c == "_"


class _Lexer:
    def __init__(self, src: str):
        # Normalize so the source always ends with a newline; simplifies EOF handling.
        if not src.endswith("\n"):
            src += "\n"
        self.lines = src.splitlines(keepends=True)
        self.lineno = 0
        self.col = 0
        self.line = self.lines[0] if self.lines else ""
        self.bracket_depth = 0
        self.brace_depth = 0
        self.indent_stack = [0]
        self.at_line_start = True
        self.paren_stack: List[Tuple[str, int, int]] = []

    # -- low level char access -------------------------------------------------
    def _advance_line(self) -> bool:
        self.lineno += 1
        if self.lineno >= len(self.lines):
            self.line = ""
            return False
        self.line = self.lines[self.lineno]
        self.col = 0
        return True

    def peekc(self, offset: int = 0) -> str:
        if self.col + offset < len(self.line):
            return self.line[self.col + offset]
        return ""

    def error(self, msg: str) -> TokenizeError:
        return TokenizeError(msg, ("<wyrm>", self.lineno + 1, self.col + 1, self.line))

    # -- main entry --------------------------------------------------------
    def tokens(self) -> Iterator[TokenInfo]:
        while True:
            if self.at_line_start and self.bracket_depth == 0 and self.brace_depth == 0:
                if not self._handle_indentation():
                    break
            yield from self._scan_line_tokens()
            if self.lineno >= len(self.lines):
                break
        # Final DEDENTs + ENDMARKER
        while len(self.indent_stack) > 1:
            self.indent_stack.pop()
            yield TokenInfo(token.DEDENT, "", (self.lineno + 1, 0), (self.lineno + 1, 0), "")
        yield TokenInfo(token.ENDMARKER, "", (self.lineno + 1, 0), (self.lineno + 1, 0), "")

    def _handle_indentation(self) -> bool:
        """Consume blank/comment-only lines and emit INDENT/DEDENT for the
        next logical line. Returns False at EOF."""
        while True:
            if self.lineno >= len(self.lines):
                return False
            line = self.line
            i = 0
            while i < len(line) and line[i] in " \t":
                i += 1
            rest = line[i:]
            if rest in ("\n", "") or rest.startswith("#"):
                # blank or comment-only line: skip without affecting indentation
                if not self._advance_line():
                    return False
                continue
            break
        indent = i
        self.col = i
        if indent > self.indent_stack[-1]:
            self.indent_stack.append(indent)
            self.pending_indent = True
        else:
            self.pending_indent = False
        self.at_line_start = False
        return True

    def _emit_dedents_if_needed(self, indent: int) -> Iterator[TokenInfo]:
        while indent < self.indent_stack[-1]:
            self.indent_stack.pop()
            yield TokenInfo(token.DEDENT, "", (self.lineno + 1, self.col), (self.lineno + 1, self.col), self.line)
        if indent != self.indent_stack[-1]:
            raise self.error("unindent does not match any outer indentation level")

    def _scan_line_tokens(self) -> Iterator[TokenInfo]:
        # Emit INDENT for the very first token of a logical line, if needed.
        line_start_col = self.col
        if getattr(self, "pending_indent", False):
            yield TokenInfo(token.INDENT, self.line[:self.col], (self.lineno + 1, 0), (self.lineno + 1, self.col), self.line)
            self.pending_indent = False
        elif self.bracket_depth == 0 and self.brace_depth == 0:
            yield from self._emit_dedents_if_needed(self.col)

        while True:
            c = self.peekc()
            if c == "" or c == "\n":
                # end of physical line
                if self.bracket_depth == 0:
                    yield TokenInfo(token.NEWLINE, "\n", (self.lineno + 1, self.col), (self.lineno + 1, self.col + 1), self.line)
                    self.at_line_start = True
                    self._advance_line()
                    return
                else:
                    if not self._advance_line():
                        return
                    continue
            if c in " \t":
                self.col += 1
                continue
            if c == "\\" and self.peekc(1) in ("\n", ""):
                # explicit line continuation
                self._advance_line()
                continue
            if c == "#":
                # comment runs to end of physical line
                self.col = len(self.line)
                continue
            start = (self.lineno + 1, self.col)
            if c == '"':
                yield self._scan_string(start)
                continue
            if c == "\\":
                yield self._scan_char(start)
                continue
            if (c == "R" or c == "r") and self.peekc(1) == '"':
                yield self._scan_raw_string(start)
                continue
            if c.isdigit():
                yield self._scan_number(start)
                continue
            if _is_ident_start(c):
                yield self._scan_name(start)
                continue
            if c in OPEN_BRACKETS:
                self.bracket_depth += 1
                self.col += 1
                yield TokenInfo(token.OP, c, start, (self.lineno + 1, self.col), self.line)
                continue
            if c in CLOSE_BRACKETS:
                self.bracket_depth = max(0, self.bracket_depth - 1)
                self.col += 1
                yield TokenInfo(token.OP, c, start, (self.lineno + 1, self.col), self.line)
                continue
            if c == "{":
                # Explicit brace-delimited blocks opt out of the layout
                # rule for their contents (INDENT/DEDENT suppressed), but
                # NEWLINE/";" remain significant as statement separators.
                self.brace_depth += 1
                self.col += 1
                yield TokenInfo(token.OP, c, start, (self.lineno + 1, self.col), self.line)
                continue
            if c == "}":
                self.brace_depth = max(0, self.brace_depth - 1)
                self.col += 1
                yield TokenInfo(token.OP, c, start, (self.lineno + 1, self.col), self.line)
                continue
            if c == ";":
                self.col += 1
                yield TokenInfo(token.NEWLINE, ";", start, (self.lineno + 1, self.col), self.line)
                continue
            matched = None
            for op in MULTI_OPS:
                if self.line.startswith(op, self.col):
                    matched = op
                    break
            if matched:
                self.col += len(matched)
                yield TokenInfo(token.OP, matched, start, (self.lineno + 1, self.col), self.line)
                continue
            if c in SINGLE_OPS:
                self.col += 1
                yield TokenInfo(token.OP, c, start, (self.lineno + 1, self.col), self.line)
                continue
            raise self.error(f"unexpected character {c!r}")

    def _scan_name(self, start) -> TokenInfo:
        i = self.col
        line = self.line
        j = i + 1
        while j < len(line) and _is_ident_cont(line[j]):
            j += 1
        text = line[i:j]
        self.col = j
        return TokenInfo(token.NAME, text, start, (self.lineno + 1, j), line)

    def _scan_number(self, start) -> TokenInfo:
        line = self.line
        i = self.col
        if line.startswith("0x", i) or line.startswith("0X", i):
            j = i + 2
            while j < len(line) and (line[j] in "0123456789abcdefABCDEF_"):
                j += 1
            text = line[i:j]
            self.col = j
            return TokenInfo(token.NUMBER, text, start, (self.lineno + 1, j), line)
        j = i
        while j < len(line) and (line[j].isdigit() or line[j] == "_"):
            j += 1
        is_float = False
        if j < len(line) and line[j] == "." and j + 1 < len(line) and line[j + 1].isdigit():
            is_float = True
            j += 1
            while j < len(line) and (line[j].isdigit() or line[j] == "_"):
                j += 1
        if j < len(line) and line[j] in "eE":
            k = j + 1
            if k < len(line) and line[k] in "+-":
                k += 1
            if k < len(line) and line[k].isdigit():
                is_float = True
                j = k
                while j < len(line) and line[j].isdigit():
                    j += 1
        text = line[i:j]
        self.col = j
        return TokenInfo(token.NUMBER, text, start, (self.lineno + 1, j), line)

    def _scan_char(self, start) -> TokenInfo:
        """Clojure-style character literal: `\\a` (a single char), or a
        named char like `\\newline`/`\\space`/`\\tab` (see CHAR_NAMES in
        wyrm_eval_parse_tree.py, which does the actual name-to-codepoint
        lookup - this just captures the raw `\\...` text, same division of
        labor as _scan_string/eval_string_literal).

        Wire type is deliberately token.STRING, same as a plain string
        literal: pegen's generated parser only dispatches by token .type
        for a small fixed set of kinds (NAME/NUMBER/STRING/OP/...), and
        adding a genuinely new kind means patching the vendored pegen
        package's generator. Piggybacking on STRING and telling char and
        string literals apart by their leading character (`\\` vs `"`) in
        wyrm.gram's literal rule action avoids that, at the cost of the two
        sharing a token kind on the wire."""
        line = self.line
        i = self.col  # at the backslash
        j = i + 1
        if j >= len(line):
            raise self.error("unterminated character literal")
        first = line[j]
        if _is_ident_start(first):
            k = j + 1
            while k < len(line) and _is_ident_cont(line[k]):
                k += 1
        else:
            # Not a name (a digit, punctuation, even a space) - always a
            # single literal character, e.g. \(, \5, \\, \ .
            k = j + 1
        text = line[i:k]
        self.col = k
        return TokenInfo(token.STRING, text, start, (self.lineno + 1, k), line)

    def _scan_string(self, start) -> TokenInfo:
        line = self.line
        i = self.col
        if line.startswith('"""', i):
            return self._scan_multiline_string(start)
        j = i + 1
        chars = ['"']
        while True:
            if j >= len(line):
                raise self.error("unterminated string literal")
            c = line[j]
            if c == "\\":
                if j + 1 >= len(line):
                    raise self.error("unterminated string literal")
                chars.append(line[j:j + 2])
                j += 2
                continue
            if c == '"':
                chars.append('"')
                j += 1
                break
            if c == "\n":
                raise self.error("unterminated string literal")
            chars.append(c)
            j += 1
        text = "".join(chars)
        self.col = j
        return TokenInfo(token.STRING, text, start, (self.lineno + 1, j), line)

    def _scan_multiline_string(self, start) -> TokenInfo:
        # Opens with three double quotes, closes at the first bare `""`
        # (two quotes) encountered thereafter, per doc/language-spec.md.
        chars = ['"""']
        self.col += 3
        while True:
            c = self.peekc()
            if c == "" or c == "\n":
                if not self._advance_line():
                    raise self.error("unterminated multiline string literal")
                chars.append("\n")
                continue
            if c == '"' and self.peekc(1) == '"':
                chars.append('""')
                self.col += 2
                break
            chars.append(c)
            self.col += 1
        text = "".join(chars)
        return TokenInfo(token.STRING, text, start, (self.lineno + 1, self.col), self.line)

    def _scan_raw_string(self, start) -> TokenInfo:
        line = self.line
        i = self.col
        assert line[i] in "Rr" and line[i + 1] == '"'
        j = i + 2
        tag_start = j
        while j < len(line) and (line[j].isalnum() or line[j] == "_"):
            j += 1
        tag = line[tag_start:j]
        if j >= len(line) or line[j] != "(":
            raise self.error("malformed raw string literal (expected '(')")
        j += 1
        closer = ")" + tag + '"'
        # Raw strings may span multiple physical lines: scan line-by-line for
        # the closer, same approach as _scan_multiline_string.
        chars = [line[i:j]]
        self.col = j
        while True:
            idx = self.line.find(closer, self.col)
            if idx != -1:
                chars.append(self.line[self.col:idx + len(closer)])
                self.col = idx + len(closer)
                break
            chars.append(self.line[self.col:])
            if not self._advance_line():
                raise self.error("unterminated raw string literal")
        text = "".join(chars)
        return TokenInfo(token.STRING, text, start, (self.lineno + 1, self.col), self.line)


def generate_tokens(src: str) -> Iterator[TokenInfo]:
    """Tokenize Wyrm source text, yielding tokenize.TokenInfo tuples."""
    lexer = _Lexer(src)
    yield from lexer.tokens()
