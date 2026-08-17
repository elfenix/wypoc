"""Transitive-import compile-graph driver.

CompileGraph.get_or_compile(path_segments) is what imports.py's
resolve_import calls for every `Import` node it sees: resolves the wyrm
module path to a `.wy` file via wyrm_modules (the same resolver `import`
uses at eval time - not reimplemented here), recursively compiles its own
parent package first (mirroring the interpreter's import_module, which
loads `std/__init__.wy` before `std/io.wy`), and caches the result keyed
by the *resolved absolute file path* so two different search-path routes
to the same file (or a diamond import) don't compile it twice - a
placeholder is cached before recursing into a module's own imports, the
same "cache before eval" trick the interpreter's _module_cache uses to
break import cycles.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, Set, Tuple

from wypoc import wyrm_modules
from wypoc.parse import parse

from .decorators_pass import expand_all_decorators
from .errors import err
from .naming import module_dotted


@dataclass
class CompiledModule:
    path_segments: Tuple[str, ...]
    dotted: str
    is_package: bool
    source: str
    public_names: Set[str] = field(default_factory=set)


class CompileGraph:
    def __init__(self):
        self.by_path: Dict[str, CompiledModule] = {}
        self.by_segments: Dict[Tuple[str, ...], str] = {}

    def get_or_compile(self, path_segments) -> CompiledModule:
        from .module import compile_module_with_meta

        key = tuple(path_segments)
        cached_path = self.by_segments.get(key)
        if cached_path is not None:
            return self.by_path[cached_path]

        if len(key) > 1:
            self.get_or_compile(key[:-1])  # parent package first

        resolved = wyrm_modules.resolve_module_file(list(key))
        if resolved is None:
            err(f"cannot find module '{'::'.join(key)}' "
                f"(searched: {', '.join(wyrm_modules.search_paths())})")
        file_path, is_package = resolved
        abs_path = os.path.abspath(file_path)
        if abs_path in self.by_path:
            self.by_segments[key] = abs_path
            return self.by_path[abs_path]

        dotted = module_dotted(key)
        placeholder = CompiledModule(key, dotted, is_package, "")
        self.by_path[abs_path] = placeholder
        self.by_segments[key] = abs_path

        with open(file_path, encoding="utf-8") as f:
            src = f.read()
        tree = parse(src, filename=file_path)
        tree = expand_all_decorators(tree, key)
        source, public_names = compile_module_with_meta(tree, key, self)
        compiled = CompiledModule(key, dotted, is_package, source, public_names)
        self.by_path[abs_path] = compiled
        return compiled
