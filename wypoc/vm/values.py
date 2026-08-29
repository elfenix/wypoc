"""Values the VM introduces to the runtime.

Exactly one so far. Everything else a compiled module produces - strings,
lists, class instances, errors - is the tree walker's own value, built by the
runtime functions the interpreter loop calls into, which is what lets a
compiled module and an interpreted one exchange values without conversion.
"""

from wypoc import wyrm_builtins
from wypoc import wyrm_eval_parse_tree as ev


class BytecodeFunction:
    """A compiled function, as a value: which module, which `functions` entry,
    and the variables it captured when `closure` built it (spec 6.3).

    It is *callable in Python*, and that is the whole interop seam. The tree
    walker's `call_value` tries `callable(func)` first, so an interpreted
    caller reaches a compiled function through the path it already had - no
    case to add there, and no import from the evaluator into the VM (which
    would be a cycle) or the other way.

    A Python call answers one value, because that is what an expression in
    interpreted source can consume. Compiled callers do not come through
    here: `call` reads the whole return window and applies the backfill rule
    itself (spec 1.3).
    """

    __slots__ = ("module", "index", "captures")

    def __init__(self, module, index, captures=()):
        self.module = module
        self.index = index
        self.captures = tuple(captures)

    @property
    def function(self):
        """The image's `functions` entry - the body's shape and location."""
        return self.module.image.functions[self.index]

    @property
    def name(self):
        return self.function.name

    def __call__(self, *positional, **kwargs):
        # Imported here rather than at module scope: the interpreter loop
        # needs this class, so the dependency has to run one way only.
        from .interp import enter

        results = enter(self, list(positional), kwargs)
        return results[0] if results else wyrm_builtins.NIL

    def __repr__(self):
        return f"<compiled fn {self.module.name}::{self.name}>"


class BytecodeMethod:
    """A compiled method body, in the shape message dispatch already accepts.

    `call_overload` calls a `NativeBody`'s function as
    `fn(receiver_or_receivers, *positional, **kwargs)` - which is exactly a
    method call with `this` in front. Wrapping a compiled body that way means
    an image's methods enter the runtime's dispatch tables as ordinary
    overloads: `resolve_overload` ranks them against interpreted ones by the
    same rules, and neither side can tell which kind it called.

    The receivers arrive as one value, or as a tuple for multiple dispatch;
    the P frame wants one slot per `this` (spec 1.1), which is what the
    function's own dispatch arity says.
    """

    __slots__ = ("module", "index")

    def __init__(self, module, index):
        self.module = module
        self.index = index

    @property
    def function(self):
        return self.module.image.functions[self.index]

    def __call__(self, this, *positional, **kwargs):
        from .interp import call_function

        arity = len(self.function.dispatch)
        receivers = list(this) if arity > 1 else [this]
        results = call_function(
            self.module, self.index, list(positional), this=receivers, kwargs=kwargs
        )
        return results[0] if results else wyrm_builtins.NIL

    def __repr__(self):
        return f"<compiled method {self.module.name}::{self.function.name}>"


class _CompiledBody:
    """What a `CoroutineInstance` shows of the definition it is running.

    The runtime reads `node.name` for its repr and its error messages; a
    compiled coroutine has a `functions` entry rather than a `CoDef`, and
    this is the one field of it that anything asks for.
    """

    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name


class BytecodeCoroutine(ev.CoroutineInstance):
    """A compiled `co`, driven by the same next()/send() the walker's is.

    Only `_run` differs from an interpreted coroutine: the body is a frame
    and a dispatch loop rather than a tree and a scope. Everything that makes
    it a coroutine - the thread, the two events, the "already finished"
    behaviour of next() after exhaustion - is inherited, so `next`, `send`,
    `for x in co` and `yield from` treat both kinds identically without
    knowing there are two.

    Suspension needs no VM support at all. The body runs on the instance's
    own thread, so the `yield` instruction just calls the runtime's
    `_yield_value` and blocks inside the interpreter loop, exactly where an
    interpreted body blocks inside `eval_expr`.
    """

    def __init__(self, module, function, frame):
        super().__init__(_CompiledBody(function.name), {})
        self.module = module
        self.frame = frame

    def _run(self):
        from .interp import execute

        ev._current_coroutine.instance = self
        try:
            results = execute(self.module, self.frame)
            self._result = results[0] if results else wyrm_builtins.NIL
        except BaseException as exc:  # noqa: BLE001
            # Same convention as the interpreted body: a crash inside a
            # coroutine ends it with an error result rather than surfacing as
            # a traceback on whichever thread happened to drive it.
            self._result = wyrm_builtins.error(str(exc))
        finally:
            self._finished = True
            self._to_caller.set()
