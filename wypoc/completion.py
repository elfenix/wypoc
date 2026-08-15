"""Completion candidates for a position in a wyrm document.

Pure functions over (source, path, line, col) plus a `SymbolIndex`. Nothing
here imports anything LSP, so it is unit-testable without a server; `lsp.py`
turns a `Candidate` into a protocol type and nothing else.

## Why this is split in two

Completion is the one editor feature that has to answer about source that
does not parse. `fn f():\\n    x = obj.` is exactly the moment the user wants
an answer, and it is a syntax error. So the two halves are deliberately
sourced differently:

- **the context** - which trigger the cursor sits after (`.`, `::`, `!`, or
  none), and what has been typed since - is read from the **raw text**, by
  scanning backwards from the cursor. Text always parses.
- **the candidates** are read from a **symbol table**, which needs a parse -
  so it comes from the last version of this document that parsed, kept by
  `SymbolIndex.last_good_table`. A moment ago's declarations are the right
  answer while the current keystroke is mid-word; the alternative is offering
  nothing at all, which is what "no completion" used to mean here.

## What each trigger offers

| after | candidates |
|---|---|
| `!` | message selectors - every `fn [Cls] name` overload in scope, this file's and its imports', plus the native ones (`append`, `substr`, ...) |
| `::` | the members of whatever module the `::` chain names, resolved the way go-to-definition resolves it, plus `$ast` |
| `.` | slot names - every slot declared in this file or a module it imports |
| nothing | names in scope innermost-first, then module level, then imported names, then the interpreter's own globals, then keywords |

`.` deliberately offers *every* known slot rather than one class's. There is
no type inference here (see `symbols.py`'s "Known simplifications"), so
narrowing `obj.` to a single class would mean guessing; a superset the editor
filters by prefix is honest and still useful. The `detail` on each candidate
names the class it came from, so a wrong one is visible rather than
misleading.
"""
from dataclasses import dataclass, field
from typing import Optional

from wypoc import symbols
from wypoc.wyrm_tokenizer import KEYWORDS

# Candidate kinds beyond the ones symbols.py already names.
KEYWORD = "keyword"
BUILTIN = "builtin"
MESSAGE = "message"
DOLLAR = "dollar"

# Reserved only inside their own construct (see wyrm.gram), but worth
# offering: a user typing `defer on ...` wants `error` suggested.
SOFT_KEYWORDS = ("as", "except", "on", "error")

# `$ast` is the only member of the `$`-family that is built; the others are
# reserved (see ast_nodes.AstRef) and deliberately not offered. The `$` is
# part of the name now that it's an ordinary identifier character (see
# wyrm_tokenizer._is_ident_cont), so it's part of the label an editor
# inserts too.
DOLLAR_MEMBERS = ("$ast",)


@dataclass
class Candidate:
    """One completion. `label` is inserted; `detail` is the one-line
    signature an editor shows beside it; `sort_group` orders the list, lower
    first, so a local beats a builtin without depending on the editor's own
    ranking."""

    label: str
    kind: str
    detail: str = ""
    sort_group: int = 5


@dataclass
class Context:
    """What the raw text says about the cursor's position.

    `trigger` is the operator immediately before the word being typed, or
    `""` for a plain identifier. `prefix` is what has been typed after it.
    `qualifier` is the text before the trigger - a `::` chain, or the
    expression a `.`/`!` is being applied to - as written, unparsed.
    `start_col` is where `prefix` begins, which is the range an editor should
    replace."""

    trigger: str
    prefix: str
    qualifier: str
    start_col: int


# `$` included: it is an identifier character (`$ast`, `reg$0`), so a word
# being typed reaches back across one.
_IDENT_CONT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")


def context_at(source: str, line: int, col: int) -> Context:
    """Read the completion context out of the raw text, at (1-based line,
    0-based column). Never raises and never parses - this has to work on a
    document that is mid-keystroke and therefore invalid."""
    lines = source.splitlines() or [""]
    text = lines[line - 1] if 1 <= line <= len(lines) else ""
    col = max(0, min(col, len(text)))

    start = col
    while start > 0 and text[start - 1] in _IDENT_CONT:
        start -= 1
    prefix = text[start:col]

    # Whitespace between the operator and the name is legal wyrm (`b ! draw`,
    # `std :: io`), so the trigger is whatever non-space character precedes
    # the word rather than the character immediately before it.
    before = text[:start].rstrip()
    if before.endswith("::"):
        # `foo::$a` - the `$`-family, whose qualifier is the chain before
        # it; told from an ordinary `foo::bar` by the word's leading `$`,
        # which is part of the word rather than a trigger of its own.
        trigger = "::$" if prefix.startswith("$") else "::"
        return Context(trigger, prefix, before[:-2], start)
    if before.endswith("!"):
        return Context("!", prefix, before[:-1], start)
    if before.endswith("."):
        # Not a float being typed: `1.` is a number, not an attribute access.
        stripped = before[:-1].rstrip()
        if stripped and stripped[-1].isdigit() and not _ends_in_identifier(stripped):
            return Context("", prefix, "", start)
        return Context(".", prefix, before[:-1], start)
    if before.endswith("@"):
        # A decorator's name is a message selector, so the same candidates
        # `!` offers are the right ones here.
        return Context("@", prefix, "", start)
    return Context("", prefix, before, start)


def _ends_in_identifier(text: str) -> bool:
    """Whether `text` ends in a name rather than a bare number - `x1` does,
    `1` does not, which is what tells `x1.field` from `1.5`."""
    i = len(text)
    while i > 0 and text[i - 1] in _IDENT_CONT:
        i -= 1
    return i < len(text) and not text[i].isdigit()


# ---------------------------------------------------------------------
# The interpreter's own globals
#
# Derived by asking the interpreter what it installs, rather than by keeping
# a second list here in step with it - a builtin added to wyrm_builtins or
# corelib/prelude.wy shows up in completion with no change to this file.
# ---------------------------------------------------------------------

_INTERPRETER_NAMES: "Optional[tuple]" = None


def interpreter_names() -> tuple:
    """(global names, message names) the interpreter seeds every scope with.
    Computed once, on first use, since it parses corelib/prelude.wy."""
    global _INTERPRETER_NAMES
    if _INTERPRETER_NAMES is None:
        try:
            from wypoc.wyrm_eval_parse_tree import (
                Scope, message_table, populate_globals,
            )

            scope = Scope()
            populate_globals(scope)
            names = tuple(sorted(n for n in scope if isinstance(n, str)))
            messages = tuple(sorted(message_table(scope)))
        except Exception:  # pragma: no cover - a corelib error shouldn't kill completion
            names, messages = (), ()
        _INTERPRETER_NAMES = (names, messages)
    return _INTERPRETER_NAMES


# ---------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------

def complete(index, source: str, path: str, line: int, col: int) -> list:
    """Every candidate for the cursor, best first and de-duplicated by
    label."""
    ctx = context_at(source, line, col)
    table = table_for(index, source, path, line, ctx)

    if ctx.trigger == "::$":
        return _dedupe([Candidate(name, DOLLAR, "the definition's own tree", 0)
                        for name in DOLLAR_MEMBERS], ctx.prefix)
    if ctx.trigger == "::":
        return _dedupe(_scope_candidates(index, table, ctx), ctx.prefix)
    if ctx.trigger in ("!", "@"):
        return _dedupe(_message_candidates(index, table), ctx.prefix)
    if ctx.trigger == ".":
        return _dedupe(_slot_candidates(index, table), ctx.prefix)
    return _dedupe(_name_candidates(index, table, source, line, ctx), ctx.prefix)


def table_for(index, source: str, path: str, line: int, ctx: Context):
    """The symbol table to draw candidates from.

    Preferred: the last version of this document that parsed (see
    `SymbolIndex.last_good_table`). A document being edited was almost always
    valid a keystroke ago, so that is both cheap and accurate.

    Failing that - a document that has never parsed in this session, which is
    what a freshly opened file with a dangling `obj.` looks like - the
    dangling fragment the cursor is in is removed and the parse retried.
    Three repairs, in order of how little they throw away: drop the trigger
    and the partial word, replace the line with `pass` at the same
    indentation, then drop the line entirely. The middle one matters more than
    it looks - a half-typed statement is very often a block's *only*
    statement, and deleting the line then leaves an empty block, which is a
    second syntax error where there was one."""
    index.set_document(path, source)
    table = index.last_good_table(path)
    if table is not None:
        return table
    for repaired in _repairs(source, line, ctx):
        table = index.table_for_source(repaired, path)
        if table is not None:
            return table
    return None


def _repairs(source: str, line: int, ctx: Context):
    lines = source.splitlines()
    if not (1 <= line <= len(lines)):
        return
    text = lines[line - 1]
    # `x := obj.` -> `x := obj`; the trigger's own characters go too, since
    # `.` with nothing after it is the thing that doesn't parse.
    cut = max(ctx.start_col - len(ctx.trigger), 0)
    without_fragment = text[:cut].rstrip() + text[ctx.start_col + len(ctx.prefix):]
    if without_fragment.strip() != text.strip():
        yield "\n".join(lines[:line - 1] + [without_fragment] + lines[line:])
    indent = text[:len(text) - len(text.lstrip())]
    yield "\n".join(lines[:line - 1] + [indent + "pass"] + lines[line:])
    yield "\n".join(lines[:line - 1] + lines[line:])


def _dedupe(candidates, prefix: str) -> list:
    """Keep the first candidate per label (the innermost/most specific, since
    each producer yields in that order), filter by prefix, and order by
    group then label. The editor filters too, but doing it here means the
    same answer whichever editor is asking - and it is what the tests
    assert."""
    seen = {}
    for candidate in candidates:
        if prefix and not candidate.label.startswith(prefix):
            continue
        seen.setdefault(candidate.label, candidate)
    return sorted(seen.values(), key=lambda c: (c.sort_group, c.label))


def _from_symbol(symbol: symbols.Symbol, group: int) -> Candidate:
    return Candidate(symbol.name, symbol.kind, symbol.detail or symbol.name, group)


def _enclosing_for_caret(table, source: str, line: int, ctx: Context) -> list:
    """The scope chain a caret sits in.

    `SymbolTable.enclosing` wants a point *inside* a construct, and a caret
    is not one: a span ends at its last token, so typing at the end of a
    function body puts the caret one column past `compute`'s own span - and
    trailing whitespace puts it several. So every column from the word being
    typed back to the line's first non-blank one is probed, and the *deepest*
    chain any of them reaches is the answer.

    Deepest, not the first non-empty one: a caret past the end of a nested
    block still sits inside the enclosing function, so the rightmost probe
    answers a short chain rather than no chain, and stopping there would lose
    the block's own declarations - a `for` variable, most visibly. Probing
    can't stray into a different construct either way, since everything on
    the line belongs to the same statement."""
    if table is None:
        return []
    lines = source.splitlines() or [""]
    text = lines[line - 1] if 1 <= line <= len(lines) else ""
    floor = len(text) - len(text.lstrip())
    deepest = []
    for probe in range(ctx.start_col, floor - 1, -1):
        chain = table.enclosing(line, max(probe, 0))
        if len(chain) > len(deepest):
            deepest = chain
    return deepest


def _name_candidates(index, table, source: str, line: int, ctx: Context) -> list:
    """Names in scope: innermost enclosing declarations first, then module
    level, then what imports brought in, then the interpreter's globals, then
    keywords."""
    out = []
    if table is not None:
        # Innermost first, so a parameter is offered ahead of a module-level
        # function of the same name - the same order `resolve` ranks by.
        for symbol in reversed(_enclosing_for_caret(table, source, line, ctx)):
            for child in symbol.children:
                # A `local_to_range` child (a `for` variable) is only in scope
                # inside its own construct - and if the caret *is* inside it,
                # it appears in the chain in its own right and is added below.
                # So skipping it here is exact rather than approximate.
                if child.kind != symbols.ANONYMOUS and not child.local_to_range:
                    out.append(_from_symbol(child, 0))
            if symbol.kind != symbols.ANONYMOUS:
                out.append(_from_symbol(symbol, 1))
        for symbol in table.module_level():
            out.append(_from_symbol(symbol, 2))
        for binding in table.imports:
            if binding.binds_locally:
                out.append(Candidate(
                    binding.bound_as, symbols.IMPORT,
                    "::".join(binding.module_path), 3))
        out.extend(_wildcard_import_candidates(index, table))

    globals_, _messages = interpreter_names()
    out.extend(Candidate(name, BUILTIN, "builtin", 6) for name in globals_)
    out.extend(Candidate(name, KEYWORD, "keyword", 7) for name in sorted(KEYWORDS))
    out.extend(Candidate(name, KEYWORD, "soft keyword", 8) for name in SOFT_KEYWORDS)
    return out


def _wildcard_import_candidates(index, table) -> list:
    """What an `import mod::*` brought into this file. A wildcard has no
    names of its own to offer, so they have to come from the module."""
    out = []
    for binding in table.imports:
        node = binding.node
        if not getattr(node, "wildcard", False):
            continue
        if binding.module_path != tuple(node.path):
            continue
        excluded = set(node.except_names or ())
        for symbol in index.module_members(binding.module_path):
            if symbol.name not in excluded:
                out.append(_from_symbol(symbol, 4))
    return out


def _message_candidates(index, table) -> list:
    """Message selectors: `fn [Cls] name` overloads and class-body methods
    from this file, then from the modules it imports, then the native ones.

    One label per selector even when it has several overloads - the editor is
    completing a *name*, and which overload runs is a runtime question. The
    detail names the receivers so a selector's shape is still visible."""
    out = []
    if table is not None:
        out.extend(_message_candidates_from(table, 0))
        for binding in table.imports:
            module_table = index.module_table(binding.module_path)
            if module_table is not None:
                out.extend(_message_candidates_from(module_table, 2))

    _globals, messages = interpreter_names()
    out.extend(Candidate(name, MESSAGE, "native message", 6) for name in messages)
    return out


def _message_candidates_from(table, group: int) -> list:
    out = []
    for symbol in table.all_symbols():
        if symbol.receivers is None:
            continue
        receivers = ", ".join(symbol.receivers) if symbol.receivers else "any"
        out.append(Candidate(
            symbol.name, MESSAGE, f"{symbol.detail or symbol.name}  [{receivers}]", group))
    return out


def _slot_candidates(index, table) -> list:
    """Slot names, from this file and from the modules it imports. Every
    known slot rather than one class's - see the module docstring on why the
    superset is the honest answer, and why `detail` names the owner."""
    out = []
    if table is not None:
        out.extend(_slot_candidates_from(table, 0))
        for binding in table.imports:
            module_table = index.module_table(binding.module_path)
            if module_table is not None:
                out.extend(_slot_candidates_from(module_table, 2))
    return out


def _slot_candidates_from(table, group: int) -> list:
    out = []
    for symbol in table.all_symbols():
        if symbol.kind != symbols.SLOT:
            continue
        owner = f"  [{symbol.container}]" if symbol.container else ""
        out.append(Candidate(symbol.name, symbols.SLOT,
                             f"{symbol.detail or symbol.name}{owner}", group))
    return out


def _scope_candidates(index, table, ctx: Context) -> list:
    """`mod::` - the module-level declarations of whatever module the chain
    before the `::` names, resolved through this file's imports the same way
    go-to-definition resolves it."""
    chain = _trailing_scope_chain(ctx.qualifier)
    if not chain:
        return []
    members = index.module_members_for_chain(table, chain)
    return [_from_symbol(symbol, 0) for symbol in members]


def _trailing_scope_chain(text: str) -> list:
    """The `a::b::c` chain a `::` was just typed after, read off the end of
    the line. Stops at the first character that can't be part of one, so
    `f(std::io` gives `['std', 'io']`."""
    segments = []
    cursor = len(text)
    while True:
        end = cursor
        while cursor > 0 and text[cursor - 1] in _IDENT_CONT:
            cursor -= 1
        if cursor == end:
            break
        segments.append(text[cursor:end])
        if text[:cursor].endswith("::"):
            cursor -= 2
            continue
        break
    segments.reverse()
    return segments
