"""Regenerate wypoc/parser.py from wypoc/wyrm.gram.

    .venv/bin/python tools/generate_parser.py

This replaces the plain `python -m pegen wypoc/wyrm.gram -o wypoc/parser.py`
invocation. pegen's `LOCATIONS` magic (used throughout wyrm.gram to attach
source spans to AST nodes) expands to whatever `location_formatting` the
generator was constructed with, and pegen's own CLI hardcodes CPython's
`lineno=.., col_offset=.., end_lineno=.., end_col_offset=..` shape - four
separate keyword arguments that would mean four extra fields on all ~60
node dataclasses. wyrm nodes instead carry one `pos` 4-tuple (see
ast_nodes.Span), which needs the format string below, which is only
reachable through the generator API. Hence this script.

Generating with plain `python -m pegen` still produces a parser that
*works*, but one whose every action raises TypeError on the unexpected
`lineno=` keyword - so it fails loudly rather than silently dropping
positions.
"""
import argparse
import pathlib
import sys

from pegen.build import build_parser
from pegen.python_generator import PythonParserGenerator

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_GRAMMAR = REPO_ROOT / "wypoc" / "wyrm.gram"
DEFAULT_OUTPUT = REPO_ROOT / "wypoc" / "parser.py"

# What `LOCATIONS` becomes in a grammar action: a trailing `pos=` keyword
# argument holding one (line, col, end_line, end_col) tuple.
LOCATION_FORMATTING = "pos=(start_lineno, start_col_offset, end_lineno, end_col_offset)"


def generate(grammar_file: str, output_file: str) -> None:
    grammar, _, _ = build_parser(grammar_file)
    with open(output_file, "w") as file:
        PythonParserGenerator(
            grammar, file, location_formatting=LOCATION_FORMATTING
        ).generate(grammar_file)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("grammar", nargs="?", default=str(DEFAULT_GRAMMAR))
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    generate(args.grammar, args.output)
    print(f"wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
