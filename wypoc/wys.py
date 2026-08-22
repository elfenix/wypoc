"""Load and dump a whole compiled unit in wyrm's canonical s-expression wire
format (see sexpr.py) - the seam that decouples *getting* an `ast.Program`
from *running* one.

    source.wy --parse.parse--> ast.Program --wys.dumps--> "script.wys"
    "script.wys" --wys.loads--> ast.Program --eval_program--> (runs)

`wyrm_eval_parse_tree.eval_program` takes a Program regardless of which side
of that it came from - a script parsed straight from `.wy` source, or one
loaded back from a `.wys` file this module (or another wys producer) wrote.

## Why this format

`$['kind, field, ...]` is already wyrm's own literal syntax (a pairlist of
quoted symbols and values) and already the wire format a decorator crosses
in sexpr.py's encode/decode table. Reusing it for a whole compiled unit
means:

  * writing one needs only a small, generic printer over the five shapes a
    wire value can take (nil/bool, symbol, string, number, pair-list/list) -
    sexpr.py already supplies the two irregular spellings (`quote_string`,
    `spell_number`) it can't get from `type(value).__name__` alone;
  * reading one is running the existing tokenizer/parser/evaluator over it
    as an ordinary expression, then `sexpr.decode` - no separate reader;
  * it ports trivially to another host, since the whole file is just
    parenthesized, self-describing data - no binary layout, no schema
    version to track beyond what `sexpr.ROWS` already documents.

## What's covered

Only what sexpr.py's `ROWS`/`_ENCODERS` table carries crosses today:
literals, most expressions, control flow, `fn`, and `import`. A class,
coroutine, or anything else `sexpr._CANNOT_CROSS` still lists raises
`sexpr.SexprError` by name, the same way it would crossing a decorator -
this module adds no coverage of its own, it only adds the `program` row and
`loads`/`dumps` around the existing table.

The reference implementation's own AST dumps (`ast_dumps/*.wys` in the
sibling wyrm project) use a different node vocabulary throughout (`'qual_id`,
`'import_all`, ...) - see sexpr.py's module docstring's "Where this differs"
section - so reading *those* isn't in scope here: `loads` is for files this
module's own `dumps` (or a future, fuller port of sexpr.py) produced.

## Decorators

`dumps` refuses a tree still holding a raw `'decorator`/`'decorated` node -
that's what `sexpr.encode` already does for `ast.Decorator`/`ast.Decorated`,
since neither has a row (they cross *raw*, only inside `expand_decorated`'s
own machinery). Run `wyrm_eval_parse_tree.expand_decorators` first to get a
tree with none left.
"""
from wypoc import ast_nodes as ast
from wypoc import sexpr
from wypoc.parse import parse as parse_source
from wypoc.wyrm_builtins import NIL, Pair, Symbol


class WysError(Exception):
    """A `.wys` text that doesn't parse as wyrm source, isn't a single
    expression, or isn't a well-formed `$['program, ...]` s-expression."""


def _write(value) -> str:
    """`value` (a wire value built out of nil/bool/Symbol/str/int/float and
    Pair chains/lists of the same - what `sexpr.encode` produces) as the
    wyrm source text that reads back to it: `$[...]` for a pair chain,
    `'name` for a symbol, a proper `"..."` string literal (via
    sexpr.quote_string) rather than the REPL's single-quoted display form,
    and so on. Unlike `wyrm_builtins.display`, every rule here has to hold
    for the text to be valid wyrm source, not merely readable."""
    if value is None or value is NIL:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Symbol):
        return f"'{value.name}"
    if isinstance(value, str):
        return sexpr.quote_string(value)
    if isinstance(value, (int, float)):
        return sexpr.spell_number(value)
    if isinstance(value, Pair):
        parts = []
        node = value
        while isinstance(node, Pair):
            parts.append(_write(node.car))
            node = node.cdr
        tail = "" if node is NIL or node is None else f" . {_write(node)}"
        return f"$[{', '.join(parts)}{tail}]"
    if isinstance(value, list):
        return "[" + ", ".join(_write(v) for v in value) + "]"
    raise WysError(f"cannot write a {type(value).__name__} as .wys text")


def _literal_ctx():
    """A scope just complete enough to evaluate a `$[...]` literal - in
    particular, one where the bare name `nil` resolves (see sexpr.py's
    module docstring: "`nil` is a builtin binding" here). Reuses the same
    globals every script starts with rather than hand-picking a minimal
    subset, since a `.wys` file is themselves-and-only-data - evaluating it
    runs no wyrm code of the loaded program's own."""
    from wypoc.wyrm_eval_parse_tree import Scope, populate_globals

    ctx = Scope()
    populate_globals(ctx)
    return ctx


def loads(text: str, filename: str = "<wys>") -> ast.Program:
    """The `ast.Program` a `.wys` file's text decodes to - the inverse of
    `dumps`."""
    from wypoc.wyrm_eval_parse_tree import eval_expr

    try:
        tree = parse_source(text, filename=filename)
    except SyntaxError as exc:
        raise WysError(f"{filename}: not valid .wys text: {exc}") from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.ExprStmt):
        raise WysError(f"{filename}: expected a single $['program, ...] expression")
    value = eval_expr(tree.body[0].value, _literal_ctx())
    try:
        program = sexpr.decode(value)
    except sexpr.SexprError as exc:
        raise WysError(f"{filename}: {exc}") from None
    if not isinstance(program, ast.Program):
        raise WysError(
            f"{filename}: expected a $['program, ...] form, "
            f"not {type(program).__name__}"
        )
    return program


def load(path: str) -> ast.Program:
    """`loads` on `path`'s contents."""
    with open(path, encoding="utf-8") as f:
        return loads(f.read(), filename=path)


def dumps(program: ast.Program) -> str:
    """`program`'s canonical `.wys` text. Decorators must already be fully
    expanded - see wyrm_eval_parse_tree.expand_decorators - and every
    construct in `program` must be one sexpr.py's table carries; either
    failure raises a `WysError` (or, for an unsupported construct,
    `sexpr.SexprError` naming it).

    sexpr.py itself happily encodes a raw `'decorator`/`'decorated` node -
    that's what lets an *outer* decorator inspect an unexpanded inner one
    (see wyrm_eval_parse_tree.expand_decorated) - so this checks for one
    explicitly rather than relying on encode() to refuse it: a `.wys` file
    is meant to be readable by a host with no decorator machinery at all, so
    none may be left in a tree this writes."""
    for stmt in program.body:
        for n in stmt.walk():
            if isinstance(n, (ast.Decorator, ast.Decorated)):
                raise WysError(
                    "program still has an unexpanded decorator - "
                    "run wyrm_eval_parse_tree.expand_decorators first"
                )
    return _write(sexpr.encode(program)) + "\n"


def dump(program: ast.Program, path: str) -> None:
    """`dumps` written to `path`."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(program))
