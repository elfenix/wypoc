"""Minimal diagnostics-only Language Server for wyrm (.wy/.wyrm files).

Reuses wypoc's own tokenizer + pegen parser as the sole source of truth: on
every open/change/save, the document is re-parsed from scratch and any
error is reported back as an LSP diagnostic. There is no semantic analysis
here (no hover, go-to-definition, completion) - see wypoc/README.md's
"Known gaps" for what that would need (source positions on most AST nodes,
a symbol table, etc.); this is deliberately just "does it parse".

Installed as the `wyrm-lsp` console script (see pyproject.toml's
[project.scripts] and the `lsp` optional dependency group); also runnable
directly via `python -m wypoc.lsp`. Talks LSP over stdio, the way every
editor spawns a language server.
"""
from lsprotocol import types
from pygls.lsp.server import LanguageServer

from wypoc.parse import parse

SERVER_NAME = "wyrm-lsp"
SERVER_VERSION = "0.1.0"

server = LanguageServer(SERVER_NAME, SERVER_VERSION)


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
        line = max((e.lineno or 1) - 1, 0)
        col = max((e.offset or 1) - 1, 0)
        message = e.msg or str(e)
    except Exception as e:  # pragma: no cover - defensive: a parser bug shouldn't crash the server
        line, col = 0, 0
        message = f"{type(e).__name__}: {e}"
    else:
        return []
    return [
        types.Diagnostic(
            range=types.Range(
                start=types.Position(line=line, character=col),
                end=types.Position(line=line, character=col + 1),
            ),
            message=message,
            severity=types.DiagnosticSeverity.Error,
            source=SERVER_NAME,
        )
    ]


def _publish(ls: LanguageServer, uri: str, text: str) -> None:
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


def main() -> None:
    server.start_io()


if __name__ == "__main__":
    main()
