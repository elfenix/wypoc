"""Workspace-level symbol lookup: the part that needs a filesystem.

`symbols.py` answers questions about one parsed module in isolation. This
module adds the two things an editor needs on top of that: a cache of
symbol tables keyed by file (so `import std::io` can be followed without
re-parsing std/io.wy on every keystroke), and the resolution rules that
turn an `import` into an actual file and position.

Module resolution reuses `wyrm_modules.resolve_module_file`, so WYRM_PATH
and the corelib fallback behave exactly as they do at runtime - a jump the
editor offers is a jump the interpreter would make.

Nothing here evaluates wyrm code. Following an import means parsing the
target file and reading its declarations, not running it.
"""
import os
from dataclasses import dataclass
from typing import Optional

from wypoc import symbols, wyrm_modules
from wypoc.ast_nodes import Span
from wypoc.parse import parse

# A span at the very start of a file - where a jump lands when the target
# is a module rather than a particular declaration inside it.
FILE_START: Span = (1, 0, 1, 0)


@dataclass
class Target:
    """Somewhere to jump to: a file plus a span within it."""
    path: str
    span: Span
    symbol: Optional[symbols.Symbol] = None
    description: str = ""


class SymbolIndex:
    """Caches parsed symbol tables across a workspace.

    Two tiers, mirroring how an editor sees the world: documents currently
    open (source text pushed in by the client via `set_document`, which is
    authoritative and may differ from what's on disk) and everything else
    (read from disk, re-read when mtime changes).

    A file that fails to parse yields no table rather than an exception -
    an editor asks about half-typed files constantly, and a broken import
    target shouldn't take down the request that touched it.
    """

    def __init__(self, roots=None):
        # `roots` overrides the WYRM_PATH search path, for tests.
        self._roots = roots
        self._documents: dict = {}   # path -> source text (open documents)
        self._disk_cache: dict = {}  # path -> (mtime, SymbolTable | None)
        self._doc_cache: dict = {}   # path -> (source, SymbolTable | None)
        self._last_good: dict = {}   # path -> the last SymbolTable that parsed

    # -- documents -------------------------------------------------------
    def set_document(self, path: str, source: str) -> None:
        self._documents[path] = source

    def forget_document(self, path: str) -> None:
        self._documents.pop(path, None)
        self._doc_cache.pop(path, None)
        self._last_good.pop(path, None)

    # -- tables ----------------------------------------------------------
    def table_for_source(self, source: str, path: str = None):
        try:
            tree = parse(source, filename=path or "<document>")
        except Exception:
            # Including plain SyntaxError: an editor asks about half-typed
            # files constantly, and diagnostics already report the problem.
            return None
        return symbols.build(tree, path)

    def table_for_path(self, path: str):
        """The symbol table for a file, from the open document if there is
        one, otherwise from disk. Cached until the source changes."""
        if path in self._documents:
            source = self._documents[path]
            cached = self._doc_cache.get(path)
            if cached is not None and cached[0] == source:
                return cached[1]
            table = self.table_for_source(source, path)
            self._doc_cache[path] = (source, table)
            return table

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cached = self._disk_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            with open(path) as f:
                source = f.read()
        except OSError:
            return None
        table = self.table_for_source(source, path)
        self._disk_cache[path] = (mtime, table)
        return table

    def last_good_table(self, path: str):
        """The current table if the document parses, otherwise the last one
        that did.

        Completion is the reason this exists. Every other editor feature can
        answer "nothing" for an invalid document - the diagnostics already
        say why - but the moment a user most wants a completion is
        mid-identifier, which is exactly when the file doesn't parse. A
        moment ago's declarations are the right answer then; offering
        nothing is what "no completion support" used to mean here.

        Kept per path and only ever replaced by a *successful* parse, so it
        degrades to staleness rather than to emptiness."""
        table = self.table_for_path(path)
        if table is not None:
            self._last_good[path] = table
            return table
        return self._last_good.get(path)

    # -- module resolution -----------------------------------------------
    def module_file(self, path_segments) -> Optional[str]:
        roots = self._roots
        resolved = wyrm_modules.resolve_module_file(list(path_segments), roots)
        return None if resolved is None else resolved[0]

    def module_symbol(self, path_segments, name: str) -> Optional[Target]:
        """A single declaration inside a module, e.g. `println` in
        `std::io`. Returns None if the module or the name isn't there."""
        file_path = self.module_file(path_segments)
        if file_path is None:
            return None
        table = self.table_for_path(file_path)
        if table is None:
            return None
        for symbol in table.module_level():
            if symbol.name == name:
                return Target(file_path, symbol.name_pos, symbol, symbol.detail)
        return None

    def module_table(self, path_segments):
        """The symbol table of a module named by path, or None. A path that
        names a *symbol* inside a module (the ambiguous `import a::b::c`
        case) has no table of its own, so this answers None for it and the
        caller falls back to the parent."""
        file_path = self.module_file(list(path_segments))
        return None if file_path is None else self.table_for_path(file_path)

    def module_members(self, path_segments) -> list:
        """Everything a module declares at its top level - what another file
        sees when it imports it, and what completion offers after `::`."""
        table = self.module_table(path_segments)
        return [] if table is None else table.module_level()

    def module_members_for_chain(self, table: "symbols.SymbolTable", chain) -> list:
        """`mod::` completion: the members of whatever module a written `::`
        chain names, resolved through `table`'s imports the way
        `_scope_target` resolves the same chain for a jump.

        The chain's root is whatever an `import` bound locally (`io` from
        `import std::io` means module `std::io`), with the literal path as a
        fallback for a file that reached a module some other way."""
        chain = list(chain)
        if not chain:
            return []
        for prefix in self._chain_prefixes(table, chain[0]):
            members = self.module_members(tuple(prefix) + tuple(chain[1:]))
            if members:
                return members
        return []

    def _chain_prefixes(self, table: "symbols.SymbolTable", root: str) -> list:
        prefixes = []
        if table is not None:
            prefixes = [
                list(binding.module_path) for binding in table.imports
                if binding.binds_locally and binding.symbol_name is None
                and binding.bound_as == root
            ]
        prefixes.append([root])
        return prefixes

    def resolve_import(self, binding: symbols.ImportBinding) -> Optional[Target]:
        """Where one navigable piece of an `import` statement leads.

        Mirrors `wyrm_eval_parse_tree.eval_import`'s own order for the
        ambiguous case - a bare `import a::b::c` means module `a::b::c` if
        that exists, and otherwise symbol `c` exported by `a::b`."""
        if binding.symbol_name is not None:
            return self.module_symbol(binding.module_path, binding.symbol_name)

        file_path = self.module_file(binding.module_path)
        if file_path is not None:
            return Target(
                file_path, FILE_START,
                description=f"module {'::'.join(binding.module_path)}")

        if binding.ambiguous_leaf and len(binding.module_path) > 1:
            return self.module_symbol(binding.module_path[:-1], binding.module_path[-1])
        return None

    # -- the editor-facing queries ---------------------------------------
    def definitions_at(self, table: symbols.SymbolTable, line: int, col: int) -> list:
        """Every place the thing under the cursor is defined, best first.

        A `!` message legitimately has several answers (one per overload -
        see wypoc/README.md's message dispatch notes), which is why this
        returns a list rather than a single target."""
        if table is None:
            return []

        binding = table.import_at(line, col)
        if binding is not None:
            target = self.resolve_import(binding)
            return [target] if target is not None else []

        ref = table.reference_at(line, col)
        if ref is None:
            return []

        if ref.kind == symbols.REF_MESSAGE:
            overloads = table.messages_named(ref.name)
            if overloads:
                return [Target(table.path, s.name_pos, s, s.detail) for s in overloads]
            # A message defined in another module, reached through what
            # this file imported.
            return self._imported_targets(table, ref.name)

        if ref.kind == symbols.REF_SCOPE:
            through_module = self._scope_target(table, ref)
            if through_module is not None:
                return [through_module]

        local = table.resolve(ref.name, line, col)
        # A declaration in this file is the answer; only when there isn't
        # one does the name have to be chased through an import (which
        # resolves to the import statement locally, not to anything
        # useful, so it's followed through to the real definition).
        declared = [s for s in local if s.kind != symbols.IMPORT]
        if declared:
            return [Target(table.path, s.name_pos, s, s.detail) for s in declared]
        return self._imported_targets(table, ref.name)

    def _scope_target(self, table: symbols.SymbolTable, ref: symbols.Reference):
        """`io::println` - resolve the `::` chain to a declaration in
        another module. The chain's root is whatever name an `import`
        bound here (`io` from `import std::io` means module `std::io`), so
        the real module path is that binding's path plus any further
        segments."""
        chain = symbols.scope_chain(ref.node)
        if not chain or len(chain) < 2:
            return None
        root, middle, name = chain[0], chain[1:-1], chain[-1]

        prefixes = [
            binding.module_path for binding in table.imports
            if binding.binds_locally and binding.symbol_name is None
            and binding.bound_as == root
        ]
        # Fall back to reading the chain as a literal module path, for a
        # file that navigates a module it reached some other way.
        prefixes.append((root,))
        for prefix in prefixes:
            target = self.module_symbol(tuple(prefix) + tuple(middle), name)
            if target is not None:
                return target
            # The chain may name a submodule rather than a declaration.
            file_path = self.module_file(tuple(prefix) + tuple(middle) + (name,))
            if file_path is not None:
                return Target(file_path, FILE_START,
                              description=f"module {'::'.join(chain)}")
        return None

    def _imported_targets(self, table: symbols.SymbolTable, name: str) -> list:
        """Definitions for `name` reached through this file's imports -
        whatever a module binding, an item list, or a wildcard brought in."""
        targets = []
        for binding in table.imports:
            if binding.binds_locally and binding.bound_as == name:
                target = self.resolve_import(binding)
                if target is not None:
                    targets.append(target)
            elif binding.node.wildcard and binding.module_path == tuple(binding.node.path):
                if name in (binding.node.except_names or ()):
                    continue
                target = self.module_symbol(binding.module_path, name)
                if target is not None:
                    targets.append(target)
        return targets

    def hover_at(self, table: symbols.SymbolTable, line: int, col: int):
        """(markdown, span) for a hover card, or None.

        The span is what the editor highlights while the card is up, so it
        is the thing under the cursor - not the definition it points at,
        which may be in another file entirely."""
        if table is None:
            return None

        binding = table.import_at(line, col)
        if binding is not None:
            return self._import_hover(binding), binding.pos

        symbol = table.definition_at(line, col)
        if symbol is not None and symbol.kind != symbols.ANONYMOUS:
            return _symbol_markdown(symbol, table.path), symbol.name_pos

        ref = table.reference_at(line, col)
        if ref is None:
            return None
        targets = self.definitions_at(table, line, col)
        if not targets:
            return None
        if len(targets) > 1:
            # A message with several overloads: show them all, since which
            # one runs depends on the receiver's type at runtime.
            body = "\n".join(f"- `{t.description or t.span}`" for t in targets)
            return f"**{len(targets)} overloads of `{ref.name}`**\n\n{body}", ref.pos
        target = targets[0]
        if target.symbol is not None:
            return _symbol_markdown(target.symbol, target.path), ref.pos
        return f"`{target.description}`", ref.pos

    def _import_hover(self, binding: symbols.ImportBinding) -> str:
        target = self.resolve_import(binding)
        if target is None:
            return (f"`{'::'.join(binding.module_path)}` - **unresolved**\n\n"
                    f"Searched: {', '.join(self._search_paths())}")
        if target.symbol is not None:
            return _symbol_markdown(target.symbol, target.path)
        return f"**module** `{'::'.join(binding.module_path)}`\n\n`{target.path}`"

    def _search_paths(self) -> list:
        return self._roots if self._roots is not None else wyrm_modules.search_paths()


def _symbol_markdown(symbol: symbols.Symbol, path: str = None) -> str:
    """The hover card for one declaration: its signature, then where it
    came from."""
    lines = [f"```wyrm\n{symbol.detail or symbol.name}\n```"]
    context = []
    if symbol.receivers:
        context.append("message on " + ", ".join(f"`{r}`" for r in symbol.receivers))
    elif symbol.container:
        context.append(f"in `{symbol.container}`")
    if path:
        context.append(os.path.basename(path))
    if context:
        lines.append(" - ".join(context))
    return "\n\n".join(lines)
