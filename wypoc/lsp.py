"""Language Server for wyrm (.wy/.wyrm files).

Reuses wypoc's own tokenizer + pegen parser as the sole source of truth: on
every open/change/save the document is re-parsed from scratch, any error is
reported back as a diagnostic, and the resulting AST is handed to
`symbols.py` for a static symbol table. Five features today:

  - **diagnostics** - syntax errors, ranged over the offending token
  - **documentSymbol** - the outline: classes, methods, slots, functions
  - **definition** - go-to-definition, including through `import`
    statements and across files (see `symbol_index.py`)
  - **hover** - the declaration's signature and where it came from
  - **completion** - triggered on `.`, `::` and `!` as well as while typing
    a plain name (see `completion.py`)

No references or rename yet - see wypoc/README.md's "Known gaps".
Everything here is a thin protocol adapter: the actual analysis is in
`symbols.py` (one module), `symbol_index.py` (across files), and
`completion.py`, none of which imports anything LSP, so all three stay
unit-testable without a server.

Installed as the `wyrm-lsp` console script (see pyproject.toml's
[project.scripts] and the `lsp` optional dependency group); also runnable
directly via `python -m wypoc.lsp`. Talks LSP over stdio, the way every
editor spawns a language server.
"""
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

from lsprotocol import types
from pygls.lsp.server import LanguageServer

from wypoc import completion, symbols
from wypoc.parse import parse
from wypoc.symbol_index import SymbolIndex

SERVER_NAME = "wyrm-lsp"
SERVER_VERSION = "0.1.0"

server = LanguageServer(SERVER_NAME, SERVER_VERSION)

# One index for the whole session: open documents are pushed into it as
# they change, and files reached by following an import are read (and
# cached) from disk on demand.
index = SymbolIndex()

# wyrm symbol kinds -> the LSP enum the editor renders icons from.
_SYMBOL_KINDS = {
    symbols.FUNCTION: types.SymbolKind.Function,
    symbols.COROUTINE: types.SymbolKind.Function,
    symbols.METHOD: types.SymbolKind.Method,
    symbols.CLASS: types.SymbolKind.Class,
    symbols.SLOT: types.SymbolKind.Field,
    symbols.VARIABLE: types.SymbolKind.Variable,
    symbols.CONSTANT: types.SymbolKind.Constant,
    symbols.STATIC: types.SymbolKind.Variable,
    symbols.PARAM: types.SymbolKind.Variable,
    symbols.IMPORT: types.SymbolKind.Module,
    symbols.MODULE: types.SymbolKind.Module,
}

# Kinds that are scaffolding for lookup rather than something a reader
# wants in an outline.
_OUTLINE_EXCLUDED = (symbols.PARAM, symbols.ANONYMOUS)

# Completion kinds -> the LSP enum. Mostly the same mapping as above, plus
# the three kinds only completion produces.
_COMPLETION_KINDS = dict(
    _SYMBOL_KINDS,
    **{
        symbols.PARAM: types.CompletionItemKind.Variable,
        completion.KEYWORD: types.CompletionItemKind.Keyword,
        completion.BUILTIN: types.CompletionItemKind.Function,
        completion.MESSAGE: types.CompletionItemKind.Method,
        completion.DOLLAR: types.CompletionItemKind.Property,
    },
)
# _SYMBOL_KINDS' values are SymbolKind; the two enums agree on the members
# that matter here (Function/Method/Class/Field/Variable/Constant/Module), so
# they are remapped by name rather than kept as a second hand-written table.
_COMPLETION_KINDS = {
    kind: getattr(types.CompletionItemKind, value.name, types.CompletionItemKind.Text)
    if isinstance(value, types.SymbolKind) else value
    for kind, value in _COMPLETION_KINDS.items()
}

# The characters that make the editor ask without the user pressing a key.
# `!` and `.` are single characters; `::` is two, so the second `:` is the
# trigger and completion.context_at reads the pair off the text itself.
COMPLETION_TRIGGERS = [".", ":", "!", "@", "$"]


def uri_to_path(uri: str) -> str:
    return unquote(urlparse(uri).path)


def path_to_uri(path: str) -> str:
    return "file://" + pathname2url(path)


def span_to_range(span) -> types.Range:
    """ast_nodes.Span (1-based line, 0-based column) -> LSP Range (both
    0-based). A missing span degenerates to the start of the file rather
    than raising - an editor request should never fail over a position."""
    line, col, end_line, end_col = span or (1, 0, 1, 0)
    return types.Range(
        start=types.Position(line=max(line - 1, 0), character=col),
        end=types.Position(line=max(end_line - 1, 0), character=end_col),
    )


def position_to_point(position: types.Position):
    """LSP Position -> the (1-based line, 0-based column) pair symbols.py
    works in."""
    return position.line + 1, position.character


def _error_range(e: SyntaxError) -> types.Range:
    """The editor range to squiggle for a SyntaxError.

    SyntaxError counts lines from 1 and columns from 1; LSP counts both
    from 0. `end_lineno`/`end_offset` are set by wypoc.parse.syntax_error
    (they cover the whole offending token), but not by every raiser - the
    tokenizer's own errors (unterminated string, bad dedent) report a
    single point - so fall back to a one-character range rather than
    collapsing to a zero-width one an editor would render as a bare caret,
    or nothing at all."""
    line = max((e.lineno or 1) - 1, 0)
    col = max((e.offset or 1) - 1, 0)
    end_line = max((e.end_lineno or e.lineno or 1) - 1, 0)
    end_col = max((e.end_offset or (e.offset or 1) + 1) - 1, 0)
    if (end_line, end_col) <= (line, col):
        end_line, end_col = line, col + 1
    return types.Range(
        start=types.Position(line=line, character=col),
        end=types.Position(line=end_line, character=end_col),
    )


def diagnostics_for_source(text: str) -> list:
    """Pure function, no LSP/server dependency, so it's directly unit
    testable: parse `text` and turn any error into a list of Diagnostics
    (empty if it parses cleanly). wyrm only ever reports one syntax error
    per parse (no error recovery), so this is at most a single-element list
    today, but returns a list so multi-diagnostic support is a non-breaking
    future change."""
    try:
        parse(text)
    except SyntaxError as e:
        rng = _error_range(e)
        message = e.msg or str(e)
    except Exception as e:  # pragma: no cover - defensive: a parser bug shouldn't crash the server
        rng = types.Range(
            start=types.Position(line=0, character=0),
            end=types.Position(line=0, character=1),
        )
        message = f"{type(e).__name__}: {e}"
    else:
        return []
    return [
        types.Diagnostic(
            range=rng,
            message=message,
            severity=types.DiagnosticSeverity.Error,
            source=SERVER_NAME,
        )
    ]


# ---------------------------------------------------------------------
# Outline / navigation, built on the symbol table
#
# Each of these is a pure function of (source, position) so it can be
# tested without a running server; the @server.feature handlers below are
# only responsible for unwrapping the protocol types.
# ---------------------------------------------------------------------

def document_symbols(source: str, path: str = None) -> list:
    """The outline for one document, nested the way the code is."""
    table = index.table_for_source(source, path)
    if table is None:
        return []

    def convert(symbol):
        children = [convert(c) for c in symbol.children
                    if c.kind not in _OUTLINE_EXCLUDED]
        return types.DocumentSymbol(
            name=symbol.name,
            kind=_SYMBOL_KINDS.get(symbol.kind, types.SymbolKind.Variable),
            detail=symbol.detail,
            range=span_to_range(symbol.pos),
            selection_range=span_to_range(symbol.name_pos or symbol.pos),
            children=children,
        )

    return [convert(s) for s in table.symbols if s.kind not in _OUTLINE_EXCLUDED]


def definition_locations(source: str, path: str, line: int, col: int) -> list:
    """Every definition for the thing at (1-based line, 0-based col), as
    LSP Locations. More than one is normal: a `!` message has an overload
    per receiver type, and the editor offers a picker."""
    index.set_document(path, source)
    table = index.table_for_path(path)
    locations = []
    for target in index.definitions_at(table, line, col):
        target_path = target.path or path
        locations.append(types.Location(
            uri=path_to_uri(target_path), range=span_to_range(target.span)))
    return locations


def hover_for(source: str, path: str, line: int, col: int):
    """The hover card for a position, or None if there's nothing to say."""
    index.set_document(path, source)
    table = index.table_for_path(path)
    result = index.hover_at(table, line, col)
    if result is None:
        return None
    markdown, span = result
    return types.Hover(
        contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=markdown),
        range=span_to_range(span),
    )


def completions(source: str, path: str, line: int, col: int) -> list:
    """The completion list for a position, as LSP CompletionItems.

    Each item carries an explicit `text_edit` over the word being typed
    rather than relying on the editor's own idea of a word boundary: after
    `obj!` or `mod::` those boundaries differ between editors, and an
    explicit range means `append` replaces `app` rather than `!app`.

    `sort_text` is set from the candidate's group so the ordering
    completion.py computed survives - locals before module level before
    builtins before keywords - instead of being re-sorted alphabetically by
    the client."""
    ctx = completion.context_at(source, line, col)
    candidates = completion.complete(index, source, path, line, col)
    edit_range = types.Range(
        start=types.Position(line=max(line - 1, 0), character=ctx.start_col),
        end=types.Position(line=max(line - 1, 0), character=ctx.start_col + len(ctx.prefix)),
    )
    return [
        types.CompletionItem(
            label=candidate.label,
            kind=_COMPLETION_KINDS.get(candidate.kind, types.CompletionItemKind.Text),
            detail=candidate.detail or None,
            sort_text=f"{candidate.sort_group}{candidate.label}",
            filter_text=candidate.label,
            text_edit=types.TextEdit(range=edit_range, new_text=candidate.label),
        )
        for candidate in candidates
    ]


def _publish(ls: LanguageServer, uri: str, text: str) -> None:
    index.set_document(uri_to_path(uri), text)
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics_for_source(text))
    )


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams) -> None:
    _publish(ls, params.text_document.uri, params.text_document.text)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: types.DidChangeTextDocumentParams) -> None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    _publish(ls, params.text_document.uri, doc.source)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams) -> None:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    _publish(ls, params.text_document.uri, doc.source)


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: LanguageServer, params: types.DidCloseTextDocumentParams) -> None:
    # Back to reading this file from disk; the client is no longer telling
    # us what's in it.
    index.forget_document(uri_to_path(params.text_document.uri))


@server.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def document_symbol(ls: LanguageServer, params: types.DocumentSymbolParams) -> list:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    return document_symbols(doc.source, uri_to_path(params.text_document.uri))


@server.feature(types.TEXT_DOCUMENT_DEFINITION)
def definition(ls: LanguageServer, params: types.DefinitionParams) -> list:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    line, col = position_to_point(params.position)
    return definition_locations(doc.source, uri_to_path(params.text_document.uri), line, col)


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: types.HoverParams):
    doc = ls.workspace.get_text_document(params.text_document.uri)
    line, col = position_to_point(params.position)
    return hover_for(doc.source, uri_to_path(params.text_document.uri), line, col)


def _is_lone_colon_trigger(params: types.CompletionParams, source: str, line: int, col: int) -> bool:
    """Whether this request is the protocol event fired by typing a single
    `:` that isn't the second half of `::` - the case COMPLETION_TRIGGERS
    has to register `:` for (so the *real* `::` half is caught) but that
    `complete` shouldn't act on."""
    context = params.context
    if context is None or context.trigger_kind != types.CompletionTriggerKind.TriggerCharacter:
        return False
    if context.trigger_character != ":":
        return False
    lines = source.splitlines() or [""]
    text = lines[line - 1] if 1 <= line <= len(lines) else ""
    return not text[:col].rstrip().endswith("::")


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=COMPLETION_TRIGGERS),
)
def complete(ls: LanguageServer, params: types.CompletionParams) -> types.CompletionList:
    doc = ls.workspace.get_text_document(params.text_document.uri)
    line, col = position_to_point(params.position)
    if _is_lone_colon_trigger(params, doc.source, line, col):
        # `:` is only a real trigger as the second half of `::` (a module
        # path); a block's own `:` (`fn f():`) fires the same protocol
        # event, and popping the ordinary name list up over it would just
        # be noise on a character that isn't starting a completion at all.
        return types.CompletionList(is_incomplete=False, items=[])
    items = completions(doc.source, uri_to_path(params.text_document.uri), line, col)
    # `is_incomplete=False`: the list is complete for this prefix, so the
    # editor may filter it down as the user keeps typing rather than asking
    # again on every keystroke.
    return types.CompletionList(is_incomplete=False, items=items)


def main() -> None:
    server.start_io()


if __name__ == "__main__":
    main()
