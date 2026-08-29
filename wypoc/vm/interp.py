"""The dispatch loop: decode a word, do the thing, advance.

doc/wyc-format.md §5 is deliberately blunt about what execution costs - "a
dispatch loop reads word 0, switches on the low byte, and advances by 1 or 2
words" - and this is that loop. The decode is written out inline rather than
called through `opcodes.unpack`, because the whole argument for a bytecode VM
is that decoding is a handful of shifts; a per-instruction dict allocation
would be the one place the argument stops being true. Opcode *numbers* still
come from `compiler_bc.opcodes`, which stays the single source of truth for
the instruction set - nothing below spells a number of its own.

The semantics come from the tree walker. `add` is `BINOPS["+"]`, the same
function `a + b` reaches in interpreted source, so operator overloads, the
error-value results of division by zero, and the string/list/tuple behaviour
of `+` are all already right and cannot drift. What is new here is only the
machine: registers, frames and the instruction pointer.

Every opcode the compiler emits is implemented (gen/bytecode-vm-plan.md,
through M6). Three are not, and each says so when reached rather than doing
something plausible: `super` (the tree walker has no `super` either, so there
would be nothing to be equivalent to), `return_cps` (reserved and unemitted in
v1, §6.3) and `new_primitive` (whose type-tag space is deliberately the VM's
own, and nothing emits it). Everything else in the
opcode table is refused by name and code offset rather than skipped - an
opcode this loop does not implement must stop the program, not quietly do
nothing.
"""

import struct

from wypoc import wyrm_builtins
from wypoc import wyrm_eval_parse_tree as ev
from wypoc.compiler_bc import opcodes

from . import classes
from . import frame as frames
from . import imports
from . import link
from .errors import TrapError
from .values import BytecodeCoroutine, BytecodeFunction, BytecodeMethod

AmbiguousName = ev.AmbiguousName

# --------------------------------------------------------------------------
# the opcode numbers this loop knows, taken from the table rather than spelled


def _op(name):
    return opcodes.BY_NAME[name].value


OP_NOOP = _op("noop")
OP_TRAP = _op("trap")
OP_RETURN = _op("return")
OP_LNIL = _op("lnil")
OP_LBOOL = _op("lbool")
OP_LUNSET = _op("lunset")
OP_IMPORT_STAR = _op("import_star")

OP_I8 = _op("i8")
OP_MOVE = _op("move")
OP_GGET = _op("gget")
OP_GSET = _op("gset")
OP_IMPORT = _op("import")
OP_LSYM = _op("lsym")
OP_LCONST = _op("lconst")
OP_JF = _op("jf")
OP_JT = _op("jt")
OP_JERR = _op("jerr")
OP_JNERR = _op("jnerr")
OP_JMP = _op("jmp")
OP_NEG = _op("neg")
OP_INV = _op("inv")
OP_NOT = _op("not")

OP_F32 = _op("f32")
OP_IS = _op("is")
OP_CALL = _op("call")
OP_CALL_VA = _op("call_va")
OP_MSG_VA = _op("msg_va")
OP_CLOSURE = _op("closure")
OP_TUPLE = _op("tuple")
OP_LIST = _op("list")
OP_DICT = _op("dict")
OP_PLIST = _op("plist")
OP_GETIDX = _op("getidx")
OP_SETIDX = _op("setidx")
OP_GETATTR = _op("getattr")
OP_SETATTR = _op("setattr")
OP_ITER = _op("iter")
OP_ITNEXT = _op("itnext")
OP_UNPACK = _op("unpack")
OP_DEFER_REG = _op("defer_reg")
OP_CLASS = _op("class")
OP_MSG = _op("msg")
OP_GETMSG = _op("getmsg")
OP_REG_MSG = _op("reg_msg")
OP_NEW_INSTANCE = _op("new_instance")
OP_GETSLOT = _op("getslot")
OP_SETSLOT = _op("setslot")
OP_YIELD = _op("yield")
OP_YIELD_FROM = _op("yield_from")
OP_GETSCOPE = _op("getscope")
OP_SETSCOPE = _op("setscope")

# `defer_reg`'s modes (§6.3): when an armed defer actually runs.
DEFER_ALWAYS = 0
DEFER_ON_ERROR = 1
DEFER_ON_ERROR_OR_NIL = 2

# Which operand of a pairable op moves into `f` in the compact form (§6.2),
# as a small int rather than the compiler's string: this is read on every
# pairable instruction, and an integer compare is the cheapest thing the loop
# can do with it.
SHAPE_REG_OR_INV, SHAPE_IMM, SHAPE_JCOND, SHAPE_JMP = range(4)
_SHAPE_CODES = {
    opcodes.SHAPE_INV: SHAPE_REG_OR_INV,
    opcodes.SHAPE_REG: SHAPE_REG_OR_INV,
    opcodes.SHAPE_IMM: SHAPE_IMM,
    opcodes.SHAPE_JCOND: SHAPE_JCOND,
    opcodes.SHAPE_JMP: SHAPE_JMP,
}
PAIR_SHAPES = {
    entry.value: _SHAPE_CODES[entry.shape]
    for entry in opcodes.OPS
    if entry.form == opcodes.PAIRABLE
}

# opcode -> the runtime's own implementation of that operation. Every
# three-address op in §6.3 but `is` has the same shape - `a0 <- a1 OP a2` -
# so one table lookup collapses nineteen cases, and each of them is the
# function interpreted source already calls (§6.3's "dispatch honours the
# __add__-family operator overloads" holds here because it holds there).
THREE_ADDRESS = {
    _op(name): ev.BINOPS[symbol]
    for name, symbol in (
        ("add", "+"), ("sub", "-"), ("mul", "*"), ("div", "/"), ("mod", "%"),
        ("pow", "**"), ("band", "&"), ("bor", "|"), ("shl", "<<"), ("shr", ">>"),
        ("eq", "=="), ("ne", "!="), ("lt", "<"), ("le", "<="), ("gt", ">"),
        ("ge", ">="), ("bxor", "^"),
        # `in` takes (item, container) and `cmp3` is `<=>`: same operand
        # layout, same table.
        ("in", "in"), ("cmp3", "<=>"),
    )
}

TRAP_CODES = {
    # §6.1: a compiler emits `trap 0` as the body of a function it could not
    # lower, so saying only "trap" would turn a known cause into a mystery.
    0: "unreachable code reached - or a function body the compiler could not lower",
    1: "debugger break",
}


def _signed(value, bits):
    limit = 1 << bits
    return value - limit if value & (limit >> 1) else value


# --------------------------------------------------------------------------
# entry points


def call_function(module, index, args=(), this=(), captures=(), kwargs=None):
    """Run the body of `functions[index]` and answer its returned values.

    Nothing links here any more. A body's external names are global slots
    filled before init ran or as each import ran (doc/addendum.md), so the
    first call is no different from the thousandth.

    The result is the raw return window: padding it to what a caller asked
    for is `backfill`'s job, and only a `call` instruction knows that number.
    """
    function = module.image.functions[index]
    frame = frames.for_function(function, args, this, captures, module=module, kwargs=kwargs)
    if function.is_coroutine:
        # §8.5's `co` flag: calling a coroutine constructs it. The body runs
        # later, on the instance's own thread, when something drives it.
        return [BytecodeCoroutine(module, function, frame)]
    return execute(module, frame)


def enter(fn_value, positional, kwargs=None):
    """Call a `BytecodeFunction` value."""
    return call_function(
        fn_value.module, fn_value.index, positional,
        captures=fn_value.captures, kwargs=kwargs,
    )


def invoke(module, callee, positional, kwargs=None):
    """Call any value at all, and answer its results as a list.

    Three cases, and two of them are the runtime's. A `ContextualBuiltin`
    wants the calling module's scope as its first argument (that is what
    makes `println` able to ask a class how it prints), which is the one
    thing `call_value` cannot do for itself; everything else - Python
    builtins, interpreted functions, classes, bound messages - goes through
    `call_value` exactly as it does for interpreted source.
    """
    if isinstance(callee, BytecodeFunction):
        return enter(callee, positional, kwargs)
    if isinstance(callee, ev.ContextualBuiltin):
        return [callee.fn(module.scope, *positional, **(kwargs or {}))]
    return [ev.call_value(callee, positional, kwargs or {})]


def run_defers(module, frame, results, failed=False):
    """Run a frame's armed defers, most-recently-armed first.

    §10: compiled code arms defers per *frame*, so they run here, at return,
    rather than at the end of the block that wrote them - a deliberate
    narrowing the compiler makes and the format records. `on error` bodies
    additionally need the frame to be leaving badly, which is the same
    condition `run_scoped_block` applies in the tree walker: an error value
    on the way out, or an exception.
    """
    if not frame.defers:
        return
    value = results[0] if results else wyrm_builtins.NIL
    is_error = failed or wyrm_builtins.is_error(value)
    is_nil = not failed and (value is None or value is wyrm_builtins.NIL)
    defers, frame.defers = frame.defers, []
    for closure, mode in reversed(defers):
        if mode == DEFER_ON_ERROR and not is_error:
            continue
        if mode == DEFER_ON_ERROR_OR_NIL and not (is_error or is_nil):
            continue
        invoke(module, closure, [])


def send(module, message, receivers, positional, kwargs=None):
    """Dispatch one message and answer its results as a list.

    The bind table holds the message *identity* (§7.3), so the common case is
    a table read followed by `resolve_overload` - no name lookup at the send
    site, which is the point of binding at scope start. A `Module` receiver is
    the exception the runtime already handles: `mod ! name(...)` resolves
    against that module's own table rather than this one's, so it takes the
    long way round through `send_message`, which is also the only branch that
    needs the message's name at all.
    """
    kwargs = kwargs or {}
    method = module.bound(message)
    if isinstance(method, ev.Method) and not (
        len(receivers) == 1 and isinstance(receivers[0], ev.Module)
    ):
        overload = ev.resolve_overload(method, receivers)
        return [ev.call_overload(overload, receivers, positional, kwargs)]
    name = module.symbols[module.image.messages[message].path[-1]]
    return [ev.send_message(name, receivers, positional, kwargs, module.scope)]


def receivers_of(value):
    """A `msg`'s receiver register: one value, or a tuple of them for
    multiple dispatch (§6.3)."""
    return list(value) if isinstance(value, tuple) else [value]


def slot_names(cls):
    """A class's slots in §8.6's layout order - every ancestor's from the
    root, then its own - which is the numbering `getslot`/`setslot` use.

    `all_slots` builds exactly that order for the runtime's own instances, so
    the two agree by construction rather than by a second convention.
    """
    return list(cls.all_slots())


def backfill(frame, base, results, nres):
    """§1.3's backfill rule, in the one place it lives.

    "The VM pads missing results with nil and discards extra ones." That
    single sentence is what makes multi-value return, single-value return and
    "this statement produced no value" one mechanism, so `call`, and later
    `msg`, `super` and `yield`, all land here rather than each deciding.
    """
    L = frame.l
    for i in range(nres):
        L[base + i] = results[i] if i < len(results) else wyrm_builtins.NIL


def execute(module, frame):
    """Run `frame` from its instruction pointer until it returns."""
    code = module.image.code
    ncode = len(code)
    statics = module.statics
    symbols = module.symbols
    L = frame.l
    P = frame.p
    ip = frame.ip
    # Locals, not globals: a name the loop reads per instruction is an array
    # index as a local and a dict hit as a module global, and the dispatch
    # chain reads several of them per instruction.
    shapes = PAIR_SHAPES
    three_address = THREE_ADDRESS
    p_bit = frames.P_BIT

    # Register access, §5.1. Closures rather than the same two lines repeated
    # in forty places; the operand *decode* around them is spelled out inline
    # instead, which is where the measurements said the time actually went.
    def get(reg):
        return P[reg & 0x7FFF] if reg & p_bit else L[reg]

    def put(reg, value):
        if reg & p_bit:
            P[reg & 0x7FFF] = value
        else:
            L[reg] = value

    def stop(message, op=None):
        """Halt, naming the instruction that did it.

        `ip` has already advanced past the instruction by the time anything
        fails, so the offset is recovered from the opcode's length rather than
        by saving the address on every instruction that does not fail.
        """
        frame.ip = at = ip - (2 if (op or 0) & 0x80 else 1)
        return TrapError(
            message, offset=at, opcode=op, module=module.name,
            source=module.image.source_location(at),
        )

    # Whatever happens in the loop, the frame's defers run on the way out
    # (§10 arms them per frame): normally at `return`, and here for every
    # other exit - a trap, a runtime error, a signal - which is also what
    # tells them the frame is leaving badly.
    try:
        while True:
            word0 = code[ip]
            op = word0 & 0xFF
            f = (word0 >> 8) & 0xFF
            a0 = (word0 >> 16) & 0xFFFF
            if op & 0x80:
                if ip + 1 >= ncode:
                    frame.ip = ip
                    raise TrapError(
                        "two-word instruction runs past the end of the code section",
                        offset=ip, opcode=op, module=module.name,
                        source=module.image.source_location(ip),
                    )
                word1 = code[ip + 1]
                a1 = (word1 >> 16) & 0xFFFF
                a2 = word1 & 0xFFFF
                ip += 2
            else:
                word1 = a1 = a2 = 0
                ip += 1

            # -- core one-word ops (§6.1) --------------------------------------
            if op < 0x40:
                if op == OP_RETURN:
                    frame.ip = ip
                    results = L[a0:a0 + f] if f else []
                    run_defers(module, frame, results)
                    return results
                if op == OP_LNIL:
                    put(a0, wyrm_builtins.NIL)
                    continue
                if op == OP_LBOOL:
                    put(a0, bool(f))
                    continue
                if op == OP_LUNSET:
                    put(a0, ev.UNSET)
                    continue
                if op == OP_NOOP:
                    continue
                if op == OP_TRAP:
                    raise stop(TRAP_CODES.get(f, f"trap {f}"), op)
                raise stop(_unimplemented(op), op)

            # -- pairable ops, either encoding (§6.2) --------------------------
            if op < 0x80 or op >= 0xC0:
                compact = op < 0x80
                base = op & 0x7F
                shape = shapes.get(base)
                if shape is None:
                    raise stop(f"invalid opcode 0x{op:02X}", op)
                if shape == SHAPE_REG_OR_INV:
                    # A table index or a register, then a register; the reg8
                    # widening is §5.1's, spelled out rather than called.
                    primary = a0
                    if compact:
                        secondary = (p_bit | (f & 0x7F)) if f & 0x80 else f
                    else:
                        secondary = a1
                elif shape == SHAPE_IMM:
                    primary = a0
                    if compact:
                        secondary = f - 256 if f & 0x80 else f
                    else:
                        secondary = word1 - 0x100000000 if word1 & 0x80000000 else word1
                elif shape == SHAPE_JCOND:
                    if compact:
                        primary = (p_bit | (f & 0x7F)) if f & 0x80 else f
                        secondary = a0 - 65536 if a0 & 0x8000 else a0
                    else:
                        primary = a0
                        secondary = word1 - 0x100000000 if word1 & 0x80000000 else word1
                else:  # SHAPE_JMP
                    primary = 0
                    if compact:
                        secondary = a0 - 65536 if a0 & 0x8000 else a0
                    else:
                        secondary = word1 - 0x100000000 if word1 & 0x80000000 else word1

                # §5.2: an offset counts words from the instruction *after*
                # the jump - which is where `ip` already points, so a taken
                # jump is one addition. The convention lives here and nowhere
                # else.
                if base == OP_JMP:
                    ip += secondary
                    continue
                if base == OP_JF:
                    if not get(primary):
                        ip += secondary
                    continue
                if base == OP_JT:
                    if get(primary):
                        ip += secondary
                    continue
                if base == OP_JERR:
                    if wyrm_builtins.is_error(get(primary)):
                        ip += secondary
                    continue
                if base == OP_JNERR:
                    if not wyrm_builtins.is_error(get(primary)):
                        ip += secondary
                    continue
                if base == OP_I8:
                    put(primary, secondary)
                    continue
                if base == OP_MOVE:
                    put(primary, get(secondary))
                    continue
                if base == OP_GGET:
                    value = module.globals[primary]
                    if (value.__class__ is AmbiguousName
                            or (value is ev.UNSET
                                and primary in module.free_slots)):
                        # Error at the point of use (doc/addendum.md): a name
                        # two wildcards supplied, or one nothing filled. Both
                        # are ordinary global slots, so this is where they
                        # surface - the same place the walker's lookup raises.
                        raise stop(module.global_fault(primary), op)
                    put(secondary, value)
                    continue
                if base == OP_GSET:
                    module.globals[primary] = get(secondary)
                    continue
                if base == OP_IMPORT:
                    # §6.2: the module object, loading and initialising the
                    # dependency the first time anyone asks for it.
                    path = statics[primary].split("::")
                    target = imports.import_path(path)
                    # Layer 1: this import is what makes `path` - and anything
                    # under it - reachable, so it is what fills those slots.
                    link.fill_from_import(module, path, target)
                    put(secondary, target)
                    continue
                if base == OP_LCONST:
                    put(secondary, statics[primary])
                    continue
                if base == OP_LSYM:
                    put(secondary, wyrm_builtins.Symbol(symbols[primary]))
                    continue
                if base == OP_NEG:
                    put(primary, -get(secondary))
                    continue
                if base == OP_INV:
                    put(primary, ~get(secondary))
                    continue
                if base == OP_NOT:
                    put(primary, not get(secondary))
                    continue
                raise stop(_unimplemented(op), op)

            # -- two-word ops (§6.3) -------------------------------------------
            operation = three_address.get(op)
            if operation is not None:
                put(a0, operation(get(a1), get(a2)))
                continue
            if op == OP_IMPORT_STAR:
                # The path is a constant string and the except-list is a
                # window of interned symbols, read the way `tuple` reads its
                # items - no table entry of any kind (doc/addendum.md).
                imports.register_wildcard(
                    module, statics[a0], [get(a1 + i) for i in range(f)]
                )
                continue
            if op == OP_CALL:
                # §1.3: the callee sits at the window base, arguments above it,
                # and the results land back on top of both.
                results = invoke(module, L[a0], L[a0 + 1:a0 + 1 + f])
                backfill(frame, a0, results, a1)
                continue
            if op == OP_CALL_VA:
                # `f(*a, **k)`: the callee, then the positional tuple and the
                # keyword dict the compiler already joined for us (§6.3).
                results = invoke(module, L[a0], list(L[a0 + 1]), dict(L[a0 + 2]))
                backfill(frame, a0, results, a1)
                continue
            if op == OP_MSG_VA:
                results = send(
                    module, a1, receivers_of(L[a0]),
                    list(L[a0 + 1]), dict(L[a0 + 2]),
                )
                backfill(frame, a0, results, a2)
                continue
            if op == OP_CLOSURE:
                # Captures are copied now, in P-frame capture order (§1.1) -
                # by value, because a capture that must stay shared is a cell
                # the compiler built out of `plist`/`getidx`/`setidx`, and the
                # register holds that cell rather than the variable.
                put(a0, BytecodeFunction(module, a1, L[a2:a2 + f]))
                continue
            if op == OP_CLASS:
                put(a0, classes.realize(module, a1))
                continue
            if op == OP_MSG:
                results = send(module, a1, receivers_of(L[a0]), L[a0 + 1:a0 + 1 + f])
                backfill(frame, a0, results, a2)
                continue
            if op == OP_GETMSG:
                # The bound closure for `receiver ! name`, without calling it.
                receivers = receivers_of(get(a1))
                method = module.bound(a2)
                if not isinstance(method, ev.Method):
                    raise stop(
                        f"getmsg: {module.image.messages[a2].spell(symbols)} is "
                        "not a message identity", op,
                    )
                put(a0, ev.BoundMessage(receivers, ev.resolve_overload(method, receivers)))
                continue
            if op == OP_REG_MSG:
                # `fn [T1, T2] name(...)` outside a class body: one more
                # overload on the identity the `messages` entry names.
                method = module.bound(a0)
                body = get(a1)
                if not isinstance(method, ev.Method) or not isinstance(body, BytecodeFunction):
                    raise stop(
                        f"reg_msg {module.image.messages[a0].spell(symbols)}: "
                        f"expected a message identity and a closure, got "
                        f"{method!r} and {body!r}", op,
                    )
                signature = get(a2)
                method.add_overload(
                    tuple(signature) if isinstance(signature, tuple) else (signature,),
                    ev.NativeBody(BytecodeMethod(module, body.index)),
                    {},
                )
                continue
            if op == OP_NEW_INSTANCE:
                put(a0, ev.new_instance(get(a1)))
                continue
            if op == OP_GETSLOT:
                instance = get(a1)
                put(a0, ev.unwrap(instance.attrs[slot_names(instance.cls)[a2]]))
                continue
            if op == OP_SETSLOT:
                instance = get(a0)
                ev.bind(slot_names(instance.cls)[a1], get(a2), instance.attrs)
                continue
            if op == OP_YIELD:
                # Suspend where we stand: the body is on its coroutine's own
                # thread, so this blocks inside the loop and comes back with
                # whatever was sent in (§6.3).
                window = L[a0:a0 + f]
                sent = ev._yield_value(
                    window[0] if f == 1 else (tuple(window) if f else wyrm_builtins.NIL)
                )
                L[a0] = sent
                continue
            if op == OP_YIELD_FROM:
                put(a0, ev._yield_from(get(a1)))
                continue
            if op == OP_GETSCOPE:
                # §7.3: the one lookup performed at execution time, and a
                # different namespace from `getattr`'s.
                found = link.scope_member(module, get(a1), symbols[a2])
                if found is link.MISSING:
                    raise stop(
                        f"{get(a1)!r} has no `::{symbols[a2]}`", op
                    )
                put(a0, ev.unwrap(found))
                continue
            if op == OP_SETSCOPE:
                binding = link.scope_member(module, get(a0), symbols[a1])
                if not isinstance(binding, ev.Variable):
                    raise stop(
                        f"cannot assign to `::{symbols[a1]}` of {get(a0)!r}", op
                    )
                binding.value = get(a2)
                continue
            if op == OP_TUPLE:
                put(a0, tuple(L[a1:a1 + f]))
                continue
            if op == OP_LIST:
                put(a0, list(L[a1:a1 + f]))
                continue
            if op == OP_DICT:
                # The window holds k, v, k, v ... - `f` pairs, 2f registers.
                window = L[a1:a1 + 2 * f]
                put(a0, dict(zip(window[0::2], window[1::2])))
                continue
            if op == OP_PLIST:
                chain = wyrm_builtins.NIL
                for item in reversed(L[a1:a1 + f]):
                    chain = wyrm_builtins.Pair(item, chain)
                put(a0, chain)
                continue
            if op == OP_UNPACK:
                for reg, value in enumerate(_unpacked(get(a1), f)):
                    L[a0 + reg] = value
                continue
            if op == OP_GETIDX:
                put(a0, ev.index_value(get(a1), get(a2)))
                continue
            if op == OP_SETIDX:
                ev.set_index(get(a0), get(a1), get(a2))
                continue
            if op == OP_GETATTR:
                put(a0, ev.attr_value(get(a1), symbols[a2]))
                continue
            if op == OP_SETATTR:
                ev.set_attr(get(a0), symbols[a1], get(a2))
                continue
            if op == OP_ITER:
                put(a0, ev._iter_values(get(a1), module.scope))
                continue
            if op == OP_ITNEXT:
                item = next(get(a1), _EXHAUSTED)
                if item is _EXHAUSTED:
                    ip += _signed(a2, 16)
                else:
                    put(a0, item)
                continue
            if op == OP_DEFER_REG:
                frame.defers.append((get(a0), f))
                continue
            if op == OP_F32:
                # The literal was rounded to binary32 when it was compiled, so
                # what comes back is the f32 value widened, not the source text's
                # decimal (§1.2).
                put(a0, struct.unpack("<f", struct.pack("<I", word1))[0])
                continue
            if op == OP_IS:
                put(a0, type_matches(get(a1), get(a2)))
                continue
            raise stop(_unimplemented(op), op)

    except BaseException:
        run_defers(module, frame, [], failed=True)
        raise


_EXHAUSTED = object()  # `itnext`: the iterator had nothing left


def _unpacked(source, count):
    """`unpack`: exactly `count` items, or the error value in every register.

    §6.3 is explicit that a length *or* type mismatch fills every destination
    with the error rather than half-filling them - so a botched unpack cannot
    leave one register holding a stale value that reads as real.
    """
    try:
        items = list(source)
    except TypeError:
        return [wyrm_builtins.error(
            f"cannot unpack {type(source).__name__} into {count} values"
        )] * count
    if len(items) != count:
        return [wyrm_builtins.error(
            f"cannot unpack {len(items)} values into {count}"
        )] * count
    return items


def _unimplemented(op):
    entry = opcodes.BY_VALUE.get(op)
    if entry is None:
        return f"invalid opcode 0x{op:02X}"
    # i8/i32 is the one pair whose two encodings are spelled differently; the
    # message should name the form that is actually in the code.
    wide = op >= opcodes.LONG_START and entry.form == opcodes.PAIRABLE
    name = entry.wide_name if wide and entry.wide_name else entry.name
    return f"{name}: not implemented yet by this VM"


# --------------------------------------------------------------------------
# `is` (§6.3)


def type_matches(value, type_value) -> bool:
    """`value is type_value`, where the type operand is a class value, a
    string naming a primitive type, or a tuple of either for a sum type.

    The answers match the tree walker's `_matches_type` because they come from
    the same two predicates: the primitive-type table for `int`/`str`/... and
    class ancestry for everything else, so `x is Shape` matches a Circle.

    A primitive arrives as its name, not as whatever that name is bound to,
    because the walker answers these by name and the bindings disagree with
    it - see the compiler's `_type_value`. A `PrimitiveType` value is still
    accepted: `int` and friends are bound to one, and an image is free to
    have loaded the binding.
    """
    if isinstance(type_value, tuple):
        return any(type_matches(value, one) for one in type_value)
    if isinstance(type_value, str):
        check = ev._PRIMITIVE_TYPE_CHECKS.get(type_value)
        return bool(check and check(value))
    if isinstance(type_value, wyrm_builtins.PrimitiveType):
        check = ev._PRIMITIVE_TYPE_CHECKS.get(type_value.name)
        return bool(check and check(value))
    if isinstance(type_value, ev.Class):
        return isinstance(value, ev.ClassInstance) and ev._class_distance(value.cls, type_value) is not None
    return False
