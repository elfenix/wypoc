"""Driver: tokenize Wyrm source with wyrm_tokenizer and parse it with the
pegen-generated parser (wypoc/parser.py, built from wyrm.gram)."""
import sys

from pegen.tokenizer import Tokenizer

from wypoc.parser import GeneratedParser
from wypoc.wyrm_tokenizer import generate_tokens


def parse(src: str, verbose: bool = False, filename: str = "<string>"):
    tokengen = generate_tokens(src)
    tokenizer = Tokenizer(tokengen, verbose=verbose)
    parser = GeneratedParser(tokenizer, verbose=verbose)
    tree = parser.start()
    if tree is None:
        raise parser.make_syntax_error("invalid syntax", filename)
    return tree


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path) as f:
        src = f.read()
    print(parse(src, verbose="-v" in sys.argv, filename=path))
