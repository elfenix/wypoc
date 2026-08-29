"""Running a module image: doc/wyc-format.md §7.1, steps 5 and 6.

Loading (image.py) and linking (module.py) stop short of executing anything.
This is where a loaded image becomes a running module: it gets the same
globals an interpreted module gets, it is published under its own name, and
*then* its init routine runs.

The publish-before-run order is the one thing here that is not obvious. §7.1
insists on it so that `::` navigation into a package works while that package
is still initialising. It used to serve a second purpose - making an import
cycle observable rather than divergent - but doc/addendum.md makes cycles
illegal, so the early publish is now what *detects* one: the module is in the
cache and on the import stack, and a re-entry during that window raises
instead of handing back a half-built module.
"""

from wypoc import wyrm_eval_parse_tree as ev
from wypoc import wyrm_sys

from . import frame as frames
from . import image as images
from . import interp
from . import link
from .module import LoadedModule


def load_module(image, path=None, argv=(), publish=True, scope=None) -> LoadedModule:
    """Bind an image to a runtime, publish it, and run its init routine.

    The scope handed to the module is a full one - `populate_globals`, the
    same call `import_module` makes for interpreted source - so a compiled
    module sees the builtins and the prelude that an interpreted one sees.
    Anything less and the two would disagree about what `range` means, which
    is precisely the kind of divergence output equivalence exists to catch.

    A caller may pass a `scope` of its own to run the image against a
    namespace it prepared - what an importer does when it has already built
    the environment the module should resolve its names in.
    """
    if scope is None:
        scope = ev.Scope()
        ev.populate_globals(scope, name=image.name)
    if argv is not None:
        # Always bound, like every run of a script: cli.py exposes `__ARGS`
        # whether or not any arguments were given, and code that reads it must
        # not find an unresolved name just because a module was run from a
        # test rather than a command line.
        ev.expose(scope, "__ARGS", tuple(argv))
        wyrm_sys.set_argv(list(argv))

    module = LoadedModule(image, scope=scope, path=path)
    # Layer 3, before a line of init runs: the builtins this module referenced
    # but does not define. Anything an import supplies displaces these, which
    # is why they can safely go first (doc/addendum.md, LoadedModule.fill).
    link.fill_from_builtins(module)
    if publish:
        # Step 6: published first, run second - see this module's docstring.
        # The walker's own cache is the register, so an interpreted `import`
        # of this name finds the compiled module rather than loading a second
        # copy from source.
        ev._module_cache[module.name] = module.as_module()
    # On the stack for exactly as long as init runs, so an import that reaches
    # back here during that window is reported as the cycle it is. Compiled
    # and interpreted modules share the one stack, so a cycle that crosses
    # between the two engines is caught the same way a same-engine one is.
    ev.import_stack().append(module.name)
    try:
        interp.execute(module, frames.for_init(image, module))
    finally:
        ev.import_stack().pop()
    return module


def run_image(image, argv=()) -> dict:
    """Run an image and answer the module's namespace."""
    return load_module(image, argv=argv).namespace()


def run_file(path, argv=()) -> LoadedModule:
    """Load and run a `.wyc` file."""
    return load_module(images.load_file(path), path=path, argv=argv)
