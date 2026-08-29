"""`import` and `import_star`: reaching another module (doc/wyc-format.md §7.1).

A dependency may arrive as either kind of module - a compiled image or a `.wy`
file the tree walker runs - and a compiled module must not care which. That is
the whole of this file: find it, load it the right way, and hand back one
`Module` object either way.

Two rules keep the two worlds from drifting apart:

* **One cache, one instance.** A module runs once per process, and the register
  is the walker's own module cache. A compiled module publishes itself there
  before its init runs (§7.1 step 6), so an interpreted importer finds it
  rather than starting a second copy. Import cycles are illegal
  (doc/addendum.md), and because both engines share that one cache and one
  import stack, a cycle is diagnosed identically whichever engine closes it.
* **An image wins here, source wins there.** The VM looks for a `.wyc` first:
  it is running compiled code, and the image is what its dependency was built
  alongside. `import_module` in the evaluator keeps preferring source and
  reaches for an image only when there is no `.wy` at all. Neither side has to
  guess what the other would have done, because both preferences are stated.
"""

from wypoc import wyrm_eval_parse_tree as ev
from wypoc import wyrm_modules

from .errors import LinkError


def import_path(path_segments, dynamic=True):
    """Load (or find already loaded) the module an import path names."""
    segments = list(path_segments)
    key = "::".join(segments)

    cached = ev.module_cache().get(key)
    if cached is not None:
        # A cache hit for a module still running its own top level is a cycle,
        # not a hit (doc/addendum.md). Same check the walker makes, against the
        # same stack, so the diagnostic does not depend on which engine got
        # here first.
        ev.check_import_cycle(key)
        return cached

    resolved = wyrm_modules.resolve_image_file(segments)
    if resolved is not None:
        return load_image(resolved[0], key)

    try:
        return ev.import_module(segments, dynamic=dynamic)
    except ImportError as error:
        if len(segments) > 1:
            # `import a::b::c` is ambiguous in the source and stays ambiguous
            # here: the compiler emits an `import` for every prefix, and the
            # last one may name a member of the module before it rather than a
            # module of its own. Same fallback the interpreter's `eval_import`
            # applies, so both read the statement the same way.
            parent = import_path(segments[:-1], dynamic)
            member = _member(parent, segments[-1])
            if member is not None:
                return member
        raise LinkError(str(error)) from error


def _member(namespace, name):
    """One name out of a module namespace, or None."""
    ctx = getattr(namespace, "ctx", None)
    if ctx is None:
        return None
    found = ctx.get(name)
    if found is None:
        found = ev.message_table(ctx).get(name)
    return ev.unwrap(found) if found is not None else None


def load_image(path, name=None):
    """Load, link and run one `.wyc`, and answer its module object."""
    from .image import load_file
    from .run import load_module

    return load_module(load_file(path), path=path).as_module()


def register_wildcard(module, spelling, except_names) -> None:
    """`import_star`: put a whole namespace into this module's search list.

    §7.3 step 3 searches these in registration order, each filtered by the
    except-list of the import that registered it - which is why a wildcard
    import statement carries its own, as instruction operands rather than as
    a table entry.
    """
    from . import link

    path = spelling.split("::")
    namespace = import_path(path)
    # `lsym` pushes interned Symbols, whose `str` keeps the leading quote
    # (`'SHARED`). The except-list is compared against plain names, so take
    # `.name` - the same unwrapping every other reader of a Symbol does.
    excepts = frozenset(
        getattr(name, "name", name) for name in except_names
    )
    module.wildcards.append((namespace, excepts))
    # Layer 2, as this instruction runs. The registered list above is still
    # what `getscope` and the message machinery search; this is what puts the
    # names into the module's own slots (doc/addendum.md).
    link.fill_from_wildcard(module, namespace, excepts, spelling)
    adopt_messages(module, namespace)


def adopt_messages(module, namespace) -> None:
    """Make the dependency's messages visible here, as an interpreted import
    does - a message is addressed by the module that defines it, and an
    importer that could not see it would have to qualify every send that an
    interpreted importer writes bare."""
    if isinstance(namespace, ev.Module):
        ev._adopt_messages(namespace, module.scope)
