"""One activation: the L window, the P frame, and where execution is.

doc/wyc-format.md §1.1 gives a fiber two stacks, and a frame is one slice of
each. This module owns the *shape* of that slice - how many L slots, what the
P frame holds and in what order - so the interpreter loop can address a
register with a shift and an index and nothing else.

The P frame's layout is the whole reason a compiled call is cheap: the
compiler knew every slot's index, so nothing here searches for a parameter by
name (`build_pframe` is the only place a name is even consulted, and only to
say what went wrong).
"""

from wypoc.compiler_bc import opcodes

from .errors import TrapError

P_BIT = opcodes.P_BIT  # bit 15 of a u16 register reference selects the P stack


class Frame:
    """One function activation.

    `l` and `p` are plain lists because that is exactly what a register
    reference indexes: `reg & 0x7fff` into one of them, chosen by bit 15.
    L slots start as nil - the spec leaves their initial contents
    unspecified (§1.1) and compiled code always writes before it reads, so
    this is a debugging convenience rather than a semantic.
    """

    __slots__ = ("l", "p", "ip", "function", "module", "defers")

    def __init__(self, nlocals, pframe=(), ip=0, function=None, module=None):
        self.l = [None] * nlocals
        self.p = list(pframe)
        self.ip = ip
        self.function = function  # the image `Function`, or None for init
        self.module = module
        # `(closure, mode)` per armed `defer_reg`, in arming order. Defers
        # belong to the frame, not to the block that wrote them (spec 10), so
        # this is where they live and `return` is when they run.
        self.defers = []

    # -- register access ----------------------------------------------------
    #
    # The interpreter loop inlines both of these (§5.1 is two instructions in
    # C, and a Python method call per operand would dwarf it). They exist for
    # everything *around* the loop - tests, tracing, the call sequence.

    def get(self, reg):
        return self.p[reg & 0x7FFF] if reg & P_BIT else self.l[reg]

    def set(self, reg, value):
        if reg & P_BIT:
            self.p[reg & 0x7FFF] = value
        else:
            self.l[reg] = value

    def window(self, base, count):
        """The `count` L slots at `base` - a call's argument or result window
        (§1.3). An empty window reads nothing, whatever `base` says."""
        return self.l[base:base + count] if count else []

    @property
    def name(self):
        return self.function.name if self.function is not None else "<init>"

    def __repr__(self):
        return f"Frame({self.name}, {len(self.l)} locals, {len(self.p)} P slots, ip={self.ip})"


def build_pframe(function, args, this=(), captures=(), statics=(), kwargs=None):
    """The P frame for a call: this values, parameters, captures (§1.1).

    The parameter rules are the language's, not the format's, so they are the
    ones `_bind_params` applies in the tree walker: positionally in order,
    then by keyword, then a declared default, and an error naming the
    parameter if nothing supplied it. `*args`/`**kwargs` are ordinary P slots
    that arrive pre-collected - only the function's flags say a slot is one
    (§8.5) - and they are always last, in that order.
    """
    params = function.params
    plain = len(params) - bool(function.has_varargs) - bool(function.has_kwargs)

    if (
        not this
        and not captures
        and not kwargs
        and plain == len(params)
        and len(args) == plain
        and not function.dispatch
    ):
        # The common shape - a plain function called with exactly its
        # parameters - is the whole P frame, with nothing left to check that
        # the length has not already answered. This is per call.
        return list(args)

    args = list(args)
    this = list(this)
    captures = list(captures)
    kwargs = dict(kwargs or {})
    if len(this) != len(function.dispatch):
        raise TrapError(
            f"{function.name}: {len(this)} this values for a dispatch arity of "
            f"{len(function.dispatch)}"
        )
    if len(captures) != function.ncaptures:
        raise TrapError(
            f"{function.name}: {len(captures)} captures for {function.ncaptures} "
            "capture slots"
        )

    bound = []
    taken = 0
    for param in params[:plain]:
        if taken < len(args):
            bound.append(args[taken])
            taken += 1
        elif param.name in kwargs:
            bound.append(kwargs.pop(param.name))
        elif param.default is not None:
            bound.append(statics[param.default])
        else:
            raise TrapError(
                f"{function.name}() missing required argument: {param.name!r}"
            )

    leftover = args[taken:]
    if function.has_varargs:
        bound.append(tuple(leftover))
    elif leftover:
        raise TrapError(
            f"{function.name}() takes {plain} positional argument(s) but "
            f"{len(args)} were given"
        )

    if function.has_kwargs:
        bound.append(dict(kwargs))
    elif kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TrapError(
            f"{function.name}() got unexpected keyword argument(s): {unexpected}"
        )

    return this + bound + captures


def for_function(function, args=(), this=(), captures=(), module=None, kwargs=None) -> Frame:
    """A frame ready to execute `function`'s body from its first instruction."""
    return Frame(
        function.nlocals,
        build_pframe(
            function, args, this, captures,
            statics=module.statics if module is not None else (),
            kwargs=kwargs,
        ),
        ip=function.code_offset,
        function=function,
        module=module,
    )


def for_init(image, module=None) -> Frame:
    """The module init routine's frame: code offset 0, the header's `l`, no
    parameters (§1.4)."""
    return Frame(image.init_nlocals, (), ip=0, module=module)
