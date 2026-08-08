"""Driver: tokenize Wyrm source with wyrm_tokenizer and parse it with the
pegen-generated parser (wypoc/parser.py, built from wyrm.gram)."""
import sys
import token as token_mod

from pegen.tokenizer import Tokenizer

from wypoc.parser import GeneratedParser
from wypoc.wyrm_tokenizer import generate_tokens

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


def parse(src: str, verbose: bool = False, filename: str = "<string>"):
    tokengen = generate_tokens(src)
    tokenizer = Tokenizer(tokengen, verbose=verbose)
    parser = GeneratedParser(tokenizer, verbose=verbose)
    tree = parser.start()
    if tree is None:
        raise syntax_error(parser, filename)
    return tree


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path) as f:
        src = f.read()
    print(parse(src, verbose="-v" in sys.argv, filename=path))
