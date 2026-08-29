# The `.wyc` Module Image Format

A complete definition of the binary wyrm module image: the container, every
section, the instruction encoding, the opcode set, and the load-and-link
semantics a virtual machine must implement. It is written to be sufficient on
its own — a VM author should not need any other document to read a `.wyc` file
and run it.

Companion documents, none of them required reading:
[llm-bytecode.md](llm-bytecode.md) is the compiler-facing specification — how
each source construct lowers to the instructions defined here. `language-spec.md`
in the wyrm repository defines the language itself. `wypoc/compiler_bc/` is the
reference compiler, and `wypoc/compiler_bc/include/wyrm/opcode.h` is generated
from the same table §6 is written from, so a VM can include it rather than
transcribing the opcode values.

Everything in this document is **normative** unless a paragraph says
otherwise. Where it says a loader MUST reject something, accepting it produces
undefined behaviour.

**Conventions.** `u8`/`u16`/`u32`/`i8`/`i16`/`i32` are fixed-width integers,
**little-endian** throughout. `f32` is IEEE-754 binary32, `f64` binary64.
Offsets and lengths are byte counts unless the text says *word*, which always
means one `u32` of the code array. Bit 0 is the least significant.

---

## 1. Machine model

The image is written for a **register machine**. Nothing here is optional
background: the section schemas below only make sense against it.

### 1.1 Two stacks per fiber

| Stack | Holds | Addressed as |
|---|---|---|
| **L** (locals) | one frame per active call: that function's named locals and expression temporaries | `L0…L32767` |
| **P** (parameters) | one frame per active call: the `this` values, then the declared parameters, then the captured closure variables | `P0…P32767` |

"Registers" are stack slots, not a separate file. A function's L frame size is
fixed at compile time and recorded (`l` in its `functions` entry, or `l` in the
`header` for the init routine); the VM reserves exactly that many slots on
entry. Their initial contents are unspecified — compiled code always writes a
slot before reading it, and a VM may leave them uninitialised, nil, or
poisoned as it prefers.

The P frame layout is fully known at compile time:

```
P0        .. P(t-1)       the `this` values      (t = dispatch arity; 0 for a plain fn)
Pt        .. P(t+n-1)     declared parameters    (n = parameter count)
P(t+n)    ..              captured variables     (copied at CLOSURE time, in capture order)
```

### 1.2 Values

Native scalar types are `i32` and `f32`; both have immediate-load
instructions. Everything else arrives through the static pool, the symbol
table, or runtime construction. The image says nothing about how a VM
represents values, only which ones it must be able to build.

Two values are named by the instruction set and must exist:

- **nil** — the unit/absent value (`lnil`).
- **Unset** — an *error* value meaning "declared but never assigned"
  (`lunset`). It is one of the error values `jerr`/`jnerr` test for.

Errors in wyrm are ordinary values, not a control-flow mechanism: a failing
operation yields an error value, and code tests for it. `jerr`/`jnerr` are the
only instructions that inspect that property.

### 1.3 Calls, windows and result backfill

Every call reads its arguments from, and writes its results to, a **contiguous
window of L slots in the caller's frame**. There are no separate result
operands.

For `call base, argc, nres`:

```
L[base]                     the callee            (before the call)
L[base+1] .. L[base+argc]   the arguments         (before the call)
L[base]   .. L[base+nres-1] the results           (after the call)
```

The callee's own frame is fresh; the arguments are copied into its P frame.

**The backfill rule.** A function returns 0–128 values. The caller declares how
many it wants (`nres`). The VM **pads missing results with nil and discards
extra ones**. This single rule implements multi-value return, single-value
return, and "the statement produced no value" — a compiler relies on it, so a
VM must implement it exactly.

`msg`, `msg_va`, `call_va`, `super` and `yield` use the same window discipline;
each opcode's row in §5 says where its window starts and how wide it is.

**A window base is always an L slot.** A VM may assume `base` never has bit 15
set. An *empty* window (`count`/`argc`/`nres` of 0) reads and writes nothing,
and its base may point anywhere, including one past the frame — do not
range-check it.

### 1.4 The module init routine

Code word offset 0 is the module's **init routine**. It is an ordinary
function taking 0 parameters and returning 0 values. It has no `functions`
entry; its L frame size is the header's `l`. §7 says when it runs.

---

## 2. Container layout

```
offset  size    field
0       4       magic       57 59 43 00   ("WYC\0")
4       1       version     = 1
5       1       section count (n)
6       2       reserved    = 0
8       12·n    directory, ascending section id
8+12n   ...     section payloads, in directory order
```

Each directory entry is 12 bytes:

```
offset  size  field
0       1     section id (§3)
1       1     reserved = 0
2       2     reserved = 0
4       4     offset of the payload, from the start of the file
8       4     length of the payload in bytes
```

**Rules a loader MUST enforce:**

1. The magic is exactly `57 59 43 00`. Reject anything else.
2. `version` is 1. Reject any other value — this format is not
   forward-compatible by design.
3. Directory entries are sorted by ascending section id, and no id appears
   twice.
4. Every `offset + length` lies within the file.
5. An unrecognised section id is **rejected**, not skipped. The one exception
   is `debug` (id 9), which a VM MUST ignore but MUST NOT reject.

**Alignment.** Every payload starts at a 4-byte aligned offset; the writer
inserts zero padding between payloads to achieve it, and after the last one.
Because the header is 8 bytes and each directory entry is 12, the first
payload is always aligned without padding. This exists so `code` can be cast
to `const uint32_t*` and used in place, which is the whole point of the
format — a loader on a memory-mapped image need not copy it.

**Endianness.** The canonical file is little-endian. A big-endian target either
byte-swaps the `code` array at load (nothing else needs swapping beyond the
BSON integers it parses anyway) or uses the `.c` container, which is
host-order by construction.

**No checksum in v1.** Section id `0` is reserved for a future CRC section.

---

## 3. Sections

| Id | Name | Payload | Required |
|---|---|---|---|
| 1 | `header` | BSON document | yes |
| 2 | `statics` | BSON array | no |
| 3 | `slot_defaults` | BSON document | no |
| 4 | `symbols` | BSON array | no |
| 5 | `functions` | BSON array | no |
| 6 | `classes` | BSON array | no |
| 7 | `messages` | BSON array | no |
| 8 | `code` | raw `u32[]`, **not** BSON | yes |
| 9 | `debug` | BSON document | no |
| 10 | `exports` | BSON document | no |
| 11 | `free` | BSON document | no |

An absent optional section means "empty". A writer omits a section rather than
emitting an empty container, so a module with no classes has no section 6 at
all. A loader MUST treat absence and emptiness identically.

Every index into a table (static, symbol, message, function, class, global)
is 0-based and dense. A loader MUST reject any index that is out of range for
its table.

---

## 4. The BSON subset

Structured sections use standard BSON framing with a **pinned subset of eight
element types**. The restriction is deliberate: a conforming reader is about
200 lines of C with no schema negotiation and no allocator surprises.

### 4.1 Framing

A **document** is:

```
int32   total length, including this field and the terminator
        elements, back to back
u8      0x00 terminator
```

`total length` counts the whole document. The shortest legal document is
`05 00 00 00 00` (empty).

An **element** is:

```
u8      type tag
cstring key: UTF-8 bytes, then a 0x00 terminator (the key may not contain 0x00)
        value, per the tag
```

An **array** is a document whose keys are the decimal indices `"0"`, `"1"`,
`"2"`, … in order. A reader MAY verify this; a writer MUST produce it.

**Key order is significant to writers, not readers.** The reference compiler
emits keys in the order the schemas below list them, which is what makes
output byte-reproducible. A reader MUST NOT depend on order and MUST accept
any order.

### 4.2 The eight permitted types

| Tag | Type | Value encoding |
|---|---|---|
| `0x01` | double | 8 bytes, IEEE-754 binary64 |
| `0x02` | string | `int32` byte length *including* the terminator, then UTF-8 bytes, then `0x00` |
| `0x03` | document | a nested document, exactly as §4.1 |
| `0x04` | array | a nested document with index keys |
| `0x05` | binary | `int32` byte length (not counting the subtype), `u8` subtype = 0, then the bytes |
| `0x08` | bool | 1 byte, `0x00` false or `0x01` true; any other value is invalid |
| `0x0A` | null | no bytes |
| `0x10` | int32 | 4 bytes, signed |

A loader encountering **any other tag MUST reject the module.** BSON has many
more types; none of them appear here, and accepting one would mean a value the
rest of the system cannot represent.

Only binary subtype 0 is permitted.

### 4.3 Conventions in the schemas

Keys are 1–2 characters to keep images small and the C reader trivial.

An **optional key is simply absent** when it does not apply. There are no
sentinel values anywhere in this format — no `-1` meaning "none". A reader
supplies the documented default for a missing key.

---

## 5. Instruction encoding

An instruction is **one or two little-endian `u32` words**.

```
word 0:   bits  0–7    op   (u8)
          bits  8–15   f    (u8)
          bits 16–31   a0   (u16)

word 1:   bits  0–15   a2   (u16)
          bits 16–31   a1   (u16)
```

In C:

```c
op = code[0] & 0xff;
f  = (code[0] >> 8)  & 0xff;
a0 = (code[0] >> 16) & 0xffff;
a1 = (code[1] >> 16) & 0xffff;
a2 =  code[1]        & 0xffff;
```

A few instructions use word 1 as a single 32-bit payload rather than as
`a1`/`a2`; the table calls that field `w1`.

**Length.** Bit 7 of the opcode selects the length, and nothing else does:

```c
#define WYRM_OP_LONG_START 0x80
n_words = (op & 0x80) ? 2 : 1;
```

There is no other length decoding. A dispatch loop reads word 0, switches on
the low byte, and advances by 1 or 2 words.

**Opcode ranges:**

| Range | Meaning |
|---|---|
| `0x00–0x3F` | one-word ops with no wide form |
| `0x40–0x7F` | one-word **compact** forms of pairable ops |
| `0x80–0xBF` | two-word-only ops |
| `0xC0–0xFF` | two-word **wide** forms; `wide = compact \| 0x80` |

A pairable op has two encodings of the same operation. The compact form packs
one operand into `f` (an 8-bit register reference or an 8-bit immediate); the
wide form carries that operand in `a1`, or a 32-bit payload in word 1. **The
two forms are semantically identical** — a VM may implement the wide form by
widening its operand and falling through to the compact case.

Any opcode value not in the table below is invalid; a VM SHOULD trap on it
rather than fall through.

### 5.1 Register references

A **u16 register reference** (`a0`, `a1`, `a2` where the table says *reg*):

```
bit 15 clear -> L slot, index = value            (0..32767)
bit 15 set   -> P slot, index = value & 0x7fff   (0..32767)
```

So `P3` is `0x8003`.

An **8-bit register reference** (`f`, where the table says *reg8*):

```
bit 7 clear -> L slot, index = value        (0..127)
bit 7 set   -> P slot, index = value & 0x7f (0..127)
```

A compiler emits the wide form when a register does not fit in a reg8.

### 5.2 Jump offsets

Jump offsets are **signed word counts relative to the address of the
instruction following the jump** — that is, relative to the jump's own address
plus its length in words.

```
target = jump_address + jump_words + offset
```

Compact conditional jumps carry an `i16` in `a0`; wide forms carry an `i32` in
word 1. `itnext` carries its `i16` in `a2`.

A jump always lands on an instruction boundary inside the same function body.
A VM may assume this; the reference compiler's verifier enforces it before an
image is ever written.

### 5.3 Operand kinds

| Kind | Meaning |
|---|---|
| *reg* | u16 register reference (§5.1) |
| *reg8* | 8-bit register reference in `f` (§5.1) |
| *base* | u16 register reference; always an L slot, the start of a window |
| *global* | index into the module's global slots |
| *static* | index into `statics` |
| *symbol* | index into `symbols` |
| *message* | index into `messages` (§8.7) |
| *function* | index into `functions` |
| *class* | index into `classes` |
| *count* | unsigned count, 0–255 in `f`, 0–65535 in `a1`/`a2` |
| *imm* | unsigned immediate |
| *i8*, *i32*, *f32* | signed 8-bit, signed 32-bit, IEEE-754 binary32 |
| *rel* | jump offset (§5.2) |
| *slot* | fixed slot number within an instance |

---

## 6. The opcode set

### 6.1 Core one-word ops (`0x00–0x3F`)

| Op | Mnemonic | Operands | Semantics |
|---|---|---|---|
| `0x00` | `noop` | — | Do nothing. Emitted for patching and alignment only. |
| `0x01` | `trap` | `f`=code | Halt with an error. Code 0 = unreachable code was reached; 1 = debugger break; 2–255 reserved. A compiler emits `trap 0` as the body of a function that could not be compiled, so calling it is a program error. |
| `0x02` | `return` | `a0`=base, `f`=count | Return `count` values from `L[base]…L[base+count-1]`. Runs this frame's registered defers first (`defer_reg`, §6.3). With `count` 0 the base is meaningless. |
| `0x03` | `lnil` | `a0`=dst | `dst ← nil` |
| `0x04` | `lbool` | `a0`=dst, `f`=0\|1 | `dst ← false` / `true` |
| `0x05` | `lunset` | `a0`=dst | `dst ←` the Unset error value |
| `0x06` | — | | **Retired and reserved.** `import_star` moved to `0xB5` when it grew an except-window. |
| `0x07` | — | | **Retired and reserved.** Was `resolve`, which bound the module's referenced-name set in one batch. Names are global slots filled by whatever supplies them (§7.2), so there is nothing left to batch. |
| `0x08–0x3F` | — | | Reserved. |

### 6.2 Pairable ops (compact `0x40–0x7F`, wide `0xC0–0xFF`)

For each row, the compact form is listed first. Ops marked **[INV]** put the
table index in `a0` — so it keeps the full u16 range even in the compact form —
and move the register into `f`/`a1`.

| Compact / Wide | Mnemonic | Compact operands | Wide operands | Semantics |
|---|---|---|---|---|
| `0x40` / `0xC0` | `i8` / `i32` | `a0`=dst, `f`=i8 | `a0`=dst, `w1`=i32 | `dst ←` integer immediate, sign-extended |
| `0x41` / `0xC1` | `move` | `a0`=dst, `f`=src reg8 | `a0`=dst, `a1`=src | `dst ← src` |
| `0x42` / `0xC2` | `gget` **[INV]** | `a0`=global, `f`=dst reg8 | `a1`=dst | `dst ←` module global |
| `0x43` / `0xC3` | `gset` **[INV]** | `a0`=global, `f`=src reg8 | `a1`=src | module global `← src` |
| `0x44` / `0xC4` | — | | | **Retired and reserved.** Was `rget`. Every name is a global slot now, so `gget` reads it (§7.2). |
| `0x45` / `0xC5` | `lsym` **[INV]** | `a0`=symbol, `f`=dst reg8 | `a1`=dst | `dst ←` the interned symbol |
| `0x46` / `0xC6` | `lconst` **[INV]** | `a0`=static, `f`=dst reg8 | `a1`=dst | `dst ←` static pool value |
| `0x47` / `0xC7` | `import` **[INV]** | `a0`=static, `f`=dst reg8 | `a1`=dst | `dst ←` the module object named by the `::`-joined path in that static, loading and initialising the dependency on first import (§7.1). Also fills every free slot this import supplies (§7.2). |
| `0x48` / `0xC8` | `neg` | `a0`=dst, `f`=src reg8 | `a1`=src | `dst ← −src` |
| `0x49` / `0xC9` | `inv` | same | same | `dst ← ~src` |
| `0x4A` / `0xCA` | `not` | same | same | `dst ←` boolean negation (uses `__bool__`) |
| `0x4B` / `0xCB` | `jf` | `f`=cond reg8, `a0`=rel i16 | `a0`=cond, `w1`=rel i32 | Jump if `cond` is falsy |
| `0x4C` / `0xCC` | `jt` | same | same | Jump if `cond` is truthy |
| `0x4D` / `0xCD` | `jerr` | same | same | Jump if `cond` is an error value |
| `0x4E` / `0xCE` | `jnerr` | same | same | Jump if `cond` is **not** an error value |
| `0x4F` / `0xCF` | `jmp` | `a0`=rel i16 (`f` unused) | `w1`=rel i32 (`a0` unused) | Unconditional jump |
| `0x50` / `0xD0` | — | | | **Retired and reserved.** Was `rset`, the write-through twin of `rget`. A module writes its own globals with `gset`, and a name it does not define is a free slot nothing in it assigns (§7.2). |
| `0x51–0x7F` | — | | | Reserved. Immediate-operand arithmetic is the planned tenant. |

### 6.3 Two-word ops (`0x80–0xBF`)

#### Loads and data construction

| Op | Mnemonic | Operands | Semantics |
|---|---|---|---|
| `0x80` | `f32` | `a0`=dst, `w1`=IEEE-754 binary32 bits | `dst ←` float immediate |
| `0x81` | `tuple` | `a0`=dst, `a1`=base, `f`=count | `dst ←` tuple of `L[base]…L[base+count-1]` |
| `0x82` | `list` | same | `dst ←` list of that window |
| `0x83` | `dict` | `a0`=dst, `a1`=base, `f`=pair count | `dst ←` dict; the window holds `k,v,k,v,…`, i.e. `2·count` registers |
| `0x84` | `plist` | `a0`=dst, `a1`=base, `f`=count | `dst ←` a proper pair list (`$[…]`) of the window |

#### Arithmetic and comparison

All three-address: `a0`=dst, `a1`=lhs, `a2`=rhs. Dispatch honours the
`__add__`-family operator overloads.

| Op | Mnemonic | | Op | Mnemonic | | Op | Mnemonic |
|---|---|---|---|---|---|---|---|
| `0x85` | `add` | | `0x8B` | `band` | | `0x91` | `lt` |
| `0x86` | `sub` | | `0x8C` | `bor` | | `0x92` | `le` |
| `0x87` | `mul` | | `0x8D` | `shl` | | `0x93` | `gt` |
| `0x88` | `div` | | `0x8E` | `shr` | | `0x94` | `ge` |
| `0x89` | `mod` | | `0x8F` | `eq` | | `0x95` | `bxor` |
| `0x8A` | `pow` | | `0x90` | `ne` | | | |

`add` is also the concatenation and merge operation: a compiler emits it to
join the positional tuples and keyword dicts of a spread call (`call_va`/`msg_va`, §6.3), so a VM's
tuple, list and dict types must implement it.

| Op | Mnemonic | Operands | Semantics |
|---|---|---|---|
| `0x96` | `in` | `a0`=dst, `a1`=item, `a2`=container | `dst ←` membership bool |
| `0x97` | `is` | `a0`=dst, `a1`=value, `a2`=type | `dst ←` type-check bool. `a2` holds a class value, a **string naming a primitive type** (`"int"`, `"error"`, … — the primitives are tested by name, not by whatever the name is bound to), or a tuple of either for a sum type. |
| `0xB2` | `cmp3` | `a0`=dst, `a1`=lhs, `a2`=rhs | `dst ←` −1 / 0 / 1 three-way comparison (`<=>`) |

#### Object access

| Op | Mnemonic | Operands | Semantics |
|---|---|---|---|
| `0x98` | `getidx` | `a0`=dst, `a1`=obj, `a2`=index reg | `dst ← obj[index]` |
| `0x99` | `setidx` | `a0`=obj, `a1`=index reg, `a2`=src | `obj[index] ← src` |
| `0x9A` | `getattr` | `a0`=dst, `a1`=obj, `a2`=symbol | `dst ←` property lookup by name |
| `0x9B` | `setattr` | `a0`=obj, `a1`=symbol, `a2`=src | property `← src` |
| `0x9C` | `getslot` | `a0`=dst, `a1`=obj, `a2`=slot number | `dst ←` slot by fixed index (§8.6) |
| `0x9D` | `setslot` | `a0`=obj, `a1`=slot number, `a2`=src | slot `← src` |
| `0xB3` | `getscope` | `a0`=dst, `a1`=obj, `a2`=symbol | `dst ←` a `::` namespace member of a runtime value — a module, class, or function statics. Distinct from the property namespace, and the only lookup performed at execution time (§7.3). |
| `0xB4` | `setscope` | `a0`=obj, `a1`=symbol, `a2`=src | namespace member `← src` |
| `0xB5` | `import_star` | `a0`=static, `a1`=base, `f`=count | Register the module named by the `::`-joined path in that static, minus the `count` interned symbols at `L[base]…`, in this module's wildcard search list, and fill every free slot it supplies as layer 2 (§7.2). Appears only in init code. |

`getattr`/`setattr` address the **property** namespace; `getscope`/`setscope`
address the **`::`** namespace. They are different namespaces and a VM must
keep them apart.

#### Calls, dispatch and returns

| Op | Mnemonic | Operands | Semantics |
|---|---|---|---|
| `0xA0` | `call` | `a0`=base, `f`=argc, `a1`=nres | Callee at `L[base]`, arguments at `L[base+1]…`. Results land at `L[base]…` with nil backfill (§1.3). |
| `0xA1` | `call_va` | `a0`=base, `a1`=nres | Callee at `L[base]`, positional tuple at `L[base+1]`, keyword dict at `L[base+2]`. For `f(*a, **k)` shapes. |
| `0xA2` | `msg` | `a0`=base, `f`=argc, `a1`=message, `a2`=nres | Receiver at `L[base]` — a value, or a tuple of values for multiple dispatch — arguments at `L[base+1]…`. Dispatch on the message that entry names; results at `L[base]…`. |
| `0xA3` | `msg_va` | `a0`=base, `a1`=message, `a2`=nres | Receiver, positional tuple and keyword dict at `L[base]…L[base+2]` |
| `0xA4` | `getmsg` | `a0`=dst, `a1`=receiver, `a2`=message | `dst ←` the bound closure for `receiver ! name`, without calling it |
| `0xA5` | `super` | `a0`=base, `f`=argc, `a1`=nres | Chain to the next-most-general method of the dispatch already in progress. **No receiver slot**: arguments start at `L[base]`, results land there. |
| `0xA6` | `return_cps` | `a0`=base, `f`=argc, `a1`=continuation reg | Tail call reusing the frame, the continuation receiving the result. **Reserved in v1** — no compiler emits it; a VM may trap on it. |
| `0xA7` | `yield` | `a0`=base, `f`=count | Suspend, yielding `count` values from `L[base]…`. On resume the sent value is written to `L[base]`. |
| `0xB0` | `yield_from` | `a0`=dst, `a1`=sub coroutine | Delegate: every yield of the sub-coroutine flows out of this one and every sent value flows in, until it returns. `dst ←` its return value. |

#### Construction and registration

| Op | Mnemonic | Operands | Semantics |
|---|---|---|---|
| `0xA8` | `closure` | `a0`=dst, `a1`=function, `a2`=first capture reg, `f`=ncaps | `dst ←` a closure over that `functions` entry, copying `ncaps` registers starting at `a2` into its environment, in P-frame capture order (§1.1) |
| `0xA9` | `class` | `a0`=dst, `a1`=class | `dst ←` a class object realised from that `classes` entry, registering its message map |
| `0xAA` | `new_instance` | `a0`=dst, `a1`=class reg | `dst ←` an uninitialised instance, slots at their defaults or Unset. For runtime-generated constructors; script code constructs by `call`ing the class value. |
| `0xAB` | `new_primitive` | `a0`=dst, `f`=type tag | `dst ←` a fresh empty primitive (list, dict, …). Runtime-internal helper; the tag space is the VM's own. |
| `0xAC` | `reg_msg` | `a0`=message, `a1`=closure reg, `a2`=types tuple reg | Register a method for multiple dispatch — what `fn [T1, T2] name(...)` outside a class body compiles to |
| `0xAD` | `defer_reg` | `a0`=closure reg, `f`=mode | Arm a defer for this frame. Mode 0 = always, 1 = on error, 2 = on error or nil. |

#### Iteration and unpacking

| Op | Mnemonic | Operands | Semantics |
|---|---|---|---|
| `0xAE` | `iter` | `a0`=dst, `a1`=src | `dst ←` an iterator over `src` (the `__iter__` contract) |
| `0xAF` | `itnext` | `a0`=dst, `a1`=iter, `a2`=rel i16 | Advance: write the next value to `dst`, or jump by `a2` on exhaustion |
| `0xB1` | `unpack` | `a0`=dst base, `a1`=src, `f`=count | Unpack an indexable of **exactly** `count` items into `L[dst]…L[dst+count-1]`. On a length or type mismatch every destination register receives the error value. |
| `0xB6–0xBF` | — | | Reserved |

---

## 7. Loading and linking

### 7.1 Load sequence

Given an image, a loader MUST:

1. Parse `header`. Allocate `g` global slots, all **Unset**.
2. Apply `slot_defaults` to those slots.
3. Intern every `symbols` entry.
4. Allocate one bind slot per `messages` entry, all *unbound*; each binds on
   its first read (§7.3). Then fill every free global slot (§8.11) the
   builtins supply — layer 3 of §7.2, and the reason it can run this early is
   that nothing weaker can displace it.
5. Register `functions` and `classes` as prototypes. **Execute nothing.**
6. Publish the module object under its name, **then** run code word offset 0 as
   a zero-argument call.

Step 6's order matters, though not for the reason it originally did. Import
cycles are illegal (`doc/addendum.md`), so publishing early is no longer a way
to survive one: it is what lets `::` navigation into a package work while that
package is still initialising, and it is what makes a cycle *detectable*. A
loader MUST treat an import that reaches a module already running its own init
as an error naming the cycle, not as a cache hit returning a half-built
module.

### 7.2 Names are global slots

**There is no bind table for names, and no batched resolution.** A name a
module references but does not define is a *free* global slot: storage like
any other, listed by name in the `free` section (§8.11), read by an ordinary
`gget`. A module's own definitions already had slots; free names give the same
treatment to everything else, so `gget` is the one instruction that reads a
name and there is nothing to check on the hot path.

This is what banning import cycles buys. With an acyclic import graph a
dependency has finished its own init before the importing module's runs, so
there is never a moment when a name is visible but incomplete — and so nothing
has to be deferred to a function's first call. A body's external names are the
module's, filled at the same moments.

**Three layers fill the slots**, and each knows its own layer rather than
relying on when it runs:

| Layer | Filled by | When |
|---|---|---|
| 1 | this module's own definitions, and named or aliased imports | `gset`, or the `import` that supplies the name |
| 2 | `import mod::*` | that `import_star` instruction |
| 3 | the builtins and the prelude | at load, before init runs |

A fill takes effect when its layer is at least as strong as whatever already
holds the slot, so **the answer does not depend on the order the imports
appear in**. `import a::*` before `import a::x` means what the reverse means.

An `import` fills the slot named by its own path and every slot naming a path
*through* it: `import foo` is what makes `foo::bar::baz` reachable, so it is
what fills that slot. An `import_star` offers only the names the target module
itself declared — never the builtins and prelude its scope also holds, which
every scope has of its own.

Two **different** layer-2 sources with different values for one name is an
ambiguity: the slot takes a marker, and reading it is an error naming both
sources (§7.3). A slot nothing ever fills stays Unset, and reading it fails the
way any declared-but-unassigned variable does.

**The relocation table is gone.** Of the two things it still held, only one
needed a table. A **message identity** does — it resolves in its own namespace
and caches its binding, so `messages` (§8.7) survives as a single-purpose
table. An **import path** does not: it is a constant string, and a wildcard's
except-list is a window of interned symbols the instruction reads the way
`tuple` reads its items. Neither was ever a name bound to an address, and only
the one that caches a binding earns an entry.

### 7.3 Resolving one entry

Only two things resolve at run time now.

**A message identity** (`t: 2`) resolves in the *message* namespace, which is
separate from the variable one: a module may have an unrelated `describe`
variable and `describe` message. A single-component path that resolves to
nothing pre-existing **creates** the identity — this is how a module's own
messages come into being before `reg_msg` populates them. A qualified
`mod::name` asks that module's own table. Bound on first read rather than in a
batch: there is no import for a message to wait on, and no ordering to get
right.

**A `::` path at execution time**, for `getscope`/`setscope` only. Every name
written in the source is a global slot; this is the reflective path.

**Failure is Unset, not a value.** A free slot nothing filled holds Unset, and
`gget` on it faults the way reading any declared-but-unassigned variable does.
The NotFound error value the bind table used to hold is gone: an unresolved
name and an unassigned one are now the same condition, reported the same way.

**Ambiguity is an error at the point of use.** A name two layer-2 imports both
supply, with different values, leaves the slot holding an ambiguity marker;
`gget` faults on it, naming both sources. Two wildcards that overlap on a name
the module never reads are legal — only a referenced name can be ambiguous,
and a referenced name is the only kind that has a slot.

## 8. Section schemas

### 8.1 `header` (id 1) — BSON document

```
{ n:  string    module name, fully qualified, "::"-separated
  v:  int32     format version, = 1
  g:  int32     module global slot count
  l:  int32     L-frame size of the init routine (locals + temps high-water) }
```

All four keys are required.

There is no referenced-name set. `u` held the entries init's `resolve` bound
in one batch; with binding no longer batched, nothing reads it. A writer MUST
NOT emit it and a loader MUST ignore it if one appears.

`l` is here rather than in `functions` because the init routine has no
`functions` entry of its own; without it a loader would not know how large a
frame to give the call in load step 6.

### 8.2 `statics` (id 2) — BSON array

Constant values referenced by index from `lconst`, from parameter defaults,
and from class slot defaults. Each element is a string, int32, double, bool,
null or binary. Symbols do **not** appear here — they live in `symbols`.

The compiler deduplicates identical constants, and does so type-aware: `1`,
`1.0` and `true` are three different entries.

### 8.3 `slot_defaults` (id 3) — BSON document

```
{ "<global index as a decimal string>": <constant value>, ... }
```

Maps a module global slot to its constant initial value, applied at load step
2. Globals absent from this map start as **Unset**. Non-constant initialisers
are not represented here; they compile into init code.

### 8.4 `symbols` (id 4) — BSON array

Every name the module interns or looks up at runtime: message path
components, the names in a wildcard import's except-list, `getattr`/`setattr`/`getscope`/`setscope` names, `lsym` literals,
and class member names. Deduplicated; position defines the index space.

A symbol is at most 255 bytes of UTF-8. Only the first 31 codepoints are
significant for identity — the compiler emits the full text and the loader
interns on the significant prefix.

### 8.5 `functions` (id 5) — BSON array

Entry *i* defines function index *i*, which is what `closure`'s `a1` names.

```
{ n:  string    name; a trailing "!" marks a message
  p:  [ { n: string    parameter name
          d: int32     static pool index of its default   (optional) } ... ]
  l:  int32     L-frame size: named locals plus temporaries, high-water
  c:  int32     code offset in u32 words
  f:  int32     flags, see below
  t:  [int32]   dispatch types as *global slot* indices (messages only, optional)
  k:  int32     capture count, the P-frame tail size  (optional, default 0)
  r:  int32     the widest `return` in the body       (optional, default 1) }
```

`n`, `p`, `l`, `c` and `f` are always present; `p` may be an empty array.

**Flag bits (`f`):**

| Bit | Value | Meaning |
|---|---|---|
| 0 | 1 | coroutine — calling it constructs a coroutine rather than running the body |
| 1 | 2 | message — dispatched, and P0… hold the `this` values |
| 2 | 4 | `*args` — the last-but-one parameter arrives as a collected tuple |
| 3 | 8 | `**kwargs` — the last parameter arrives as a collected dict |

The P frame is `len(t)` this values, then `len(p)` parameters, then `k`
captures — in that order (§1.1). Parameter defaults are constants; a VM
applies `d` when a call supplies no value for that parameter.

### 8.6 `classes` (id 6) — BSON array

Entry *i* defines class index *i*, which is what `class`'s `a1` names.

```
{ n:  string    class name
  s:  int32     global slot index of the superclass   (optional; absent = object)
  sl: [ { n: string    slot name
          d: int32     static index of a constant default  (optional)
          g: int32     getter function index, virtual slot (optional)
          s: int32     setter function index, virtual slot (optional) } ... ]
  i:  int32     init function index                  (optional)
  m:  [ { y: int32 symbol index, f: int32 function index } ... ]   message map
  st: [ { n: string name, g: int32 global index } ... ]            class statics }
```

`n`, `sl`, `m` and `st` are always present; the last three may be empty
arrays.

**The superclass is a global slot, never a class index** — even for a class in
the same module. Nothing is resolved across a module boundary at compile time,
and a base class may live anywhere; a name is a name, and this module's own
classes have slots like anything else it defines. The slot is read at the
moment the class is realised, which is what lets a base defined further down
the same file work.

The message map holds **at most 16 entries**, so a VM may use a fixed-size
scan or cache. A slot with `g` or `s` is *virtual*: reading or writing it calls
that function instead of touching storage. `i` is the constructor: the VM's
construct-on-call performs `new_instance`, applies slot defaults, then calls
`i`; an error returned from `i` becomes the result of the construction.

Class statics live in module global slots because they are module-lifetime;
`Class::name` resolves through the class object to the recorded slot. Their
initialisers run in init code like any other top-level effect.

**Slot layout.** Instances lay slots out base-first: every ancestor's slots in
order from the root, then the class's own. This is the numbering `getslot` and
`setslot` use, and a VM must match it for those instructions to be meaningful.

### 8.7 `messages` (id 7) — BSON array

Entry *i* defines message index *i* — one message identity, by path.

```
{ p: [int32]    the qualified path as symbol indices:
                `name` -> [sym("name")], `mod::name` -> [sym("mod"), sym("name")] }
```

The last table with a runtime binding behind it, and the only kind of name
that is not a global slot. A message resolves in a namespace of its own — a
module may have an unrelated `describe` variable and `describe` message — and
an unqualified path that resolves to nothing pre-existing **creates** the
identity, which is how a module's own messages come into being before
`reg_msg` populates them (§7.3).

Identical paths share one entry: one message name is one identity, which is
what the table is for. Entries bind on first read.

This section used to be `relocations`, and used to be a symbol table for every
external name a module mentioned. Those are global slots now (§7.2); the
import instructions carry their own paths; and what is left is one kind, so
there is no kind field.

### 8.8 `code` (id 8) — raw `u32[]`

The instruction stream, §5 encoding, 4-byte aligned. The payload length is
always a multiple of 4. Word offset 0 is the init routine's entry point;
function bodies follow, located by the `c` field of their `functions` entries.

Bodies are laid out end to end in `c` order with no padding between them. A VM
that wants to bound a body may take the next-highest `c`, or the end of the
section for the last one.

### 8.9 `debug` (id 9) — BSON document, optional

```
{ f:  string    source file name
  ln: { "<code word offset>": int32 source line, ... }
  lv: [ ... ]   reserved for local-name maps }
```

**A VM MUST ignore this section entirely.** It exists for the disassembler and
the debug adapter. It is omitted when a module is compiled with `--strip`.

The line table has one entry per *run* of instructions sharing a line, not one
per instruction: to find the line for a code offset, take the entry with the
greatest key not exceeding it. `f` is the source file's name, never the path
it was compiled from, so images do not differ between build machines.

---

### 8.10 `exports` (id 10) — BSON document

```
{ "<name>": int32 global slot index, ... }
```

Every global the module **defines**, by the name the source gave it. Not
every slot: a free name (§8.11) is a slot this module only reads, and a
block-local shadow slot is storage rather than a member. Neither is something
`mod::name` should answer with.

This is what lets a module answer a `::` lookup. Resolving `geometry::UNITS`
(§7.3) walks to the module object and then asks it for `UNITS` by name — and
the `header` records only how many global slots exist, not what they are
called. Without this section a compiled module can be imported but nothing can
be read out of it.

It is the mirror of `slot_defaults` (§8.3), which keys the same slots by index
to give them their initial values.

Absent when the module defines no globals. A VM SHOULD build its name → slot
map from this section at load, so `::` lookups and `getscope` are a dict hit.

### 8.11 `free` (id 11) — BSON document

```
{ "<name>": int32 global slot index, ... }
```

The fill list: every name this module **references but does not define**, by
the spelling the source wrote — `println`, or `geometry::UNITS`, `::` and all,
because the spelling is what has to be resolved.

These are the slots §7.2's three layers fill. A loader walks this document
against the builtins before running init, and each `import`/`import_star`
walks it again for whatever that import supplies. Iterating the *importer's*
free names rather than the dependency's exports is deliberate: the cost is
proportional to what this module actually uses, and a dependency's export set
never becomes part of its ABI.

A free slot is not exported (§8.10) — the module does not define the name — and
nothing in the module's own code ever writes it. One that nothing fills stays
Unset, and reading it faults (§7.3).

Absent when every name the module reads is one it defines.

---

## 9. Limits

A conforming image satisfies all of these. A compiler must reject a module
that exceeds any of them; a loader MAY assume they hold.

| Limit | Value | Why |
|---|---|---|
| Locals per function (L frame) | 32767 | bit 15 of a u16 register reference selects the P stack |
| P frame size (this + params + captures) | 32767 | same encoding |
| Parameters per call | 128 | `f`-field counts are 7-bit safe |
| Return values per call | 128 | same |
| `this` values (dispatch arity) | 4 minimum, 16 maximum | language minimum; message map bound |
| Tuple literal | 128 elements | language limit; larger data uses a list |
| Message map per class | 16 entries | fixed-size scan or cache on an MCU |
| Symbol length | 255 bytes UTF-8, 31 significant codepoints | language limit |
| Module globals | 65535 | u16 operand |
| Static pool entries | 65535 | u16 operand |
| Symbols per module | 65535 | u16 operand |
| Relocations per module | 65535 | u16 operand |
| Functions per module | 65535 | u16 operand |
| Classes per module | 65535 | u16 operand |
| Code per module | 2³² words | u32 offsets |
| Short jump reach | ±32K words | i16; the wide form carries an i32 |

---

## 10. Undecided behaviour

These are open questions in the VM's design. The format accommodates every
answer, so none of them blocks reading an image, but a VM author must pick one
and should know it is a choice.

Three of them now have an answer, marked **[decided]**: the reference VM in
`wypoc/vm/` had to choose, and a choice a running implementation has made is
worth more to the next VM author than an open question. They are decisions,
not format changes — a different VM may still answer differently.

- **Failed stores. [decided: a fault, never a value.]** `setattr`, `setidx`,
  `setslot` and `setscope` have no result register, so a failing store
  has nowhere to put its error. The reference VM raises: a store that cannot
  be performed stops the program at that instruction, naming it, rather than
  producing an error value somewhere `jerr` might or might not look. This
  matches what compiled code already assumes ("stores fault into a runtime
  error channel and never into a register") and keeps error *values* meaning
  what they mean everywhere else — a result some expression produced. The
  alternative, making the store's error the statement's value, was rejected
  because a statement's value is already defined by §1.3's backfill and would
  have to mean two things.
- **`return_cps`.** The continuation model — who creates the continuation
  closure, and how it interacts with defers — needs its own design pass.
  Reserved, and unemitted, in v1.
- **Defer granularity. [decided: the frame.]** Compiled code arms defers per
  *frame*, and the reference VM runs them at `return` — and on any other exit
  from the frame, including a trap or a host-level error, with `on error`
  bodies told that the frame is leaving badly. That is the same condition the
  tree walker applies when a block unwinds, so the two agree wherever the
  language's per-block scope and the frame coincide, which is every case a
  compiler emits today. The language spec's per-block dynamic scope would
  still need either compiler-inserted disarm points or a block-depth field on
  `defer_reg`; neither is a format change to what exists.
- **Wildcard shadowing. [decided: ambiguity is an error at the point of use.]**
  Resolved in `doc/addendum.md`, and no longer a VM author's choice. The two
  engines used to disagree by accident - the walker copied names into the
  importing scope as it went, so the *last* import won, while the VM searched
  a registered list and returned on the first hit, so the *first* one did.
  Neither was chosen; both were artifacts of a data structure. The rule now is
  layered precedence (a module's own definitions and named imports, then
  wildcards, then builtins), with a collision *between two wildcards* on a
  name the module actually references being a compile-time diagnostic. Because
  import cycles are illegal, the whole dependency graph is known before any
  module in it compiles, so no image ever encodes an ambiguity.
- **Message dispatch caching.** The 16-entry message map invites a per-site
  inline cache keyed on receiver class, which needs the VM's class-identity
  story first. `msg` has no spare operand, but a cache can be a side table
  keyed by code offset.

---

## Appendix A — a complete image, byte by byte

The whole of `test/bytecode/hello.wy`:

```wyrm
# hello.wy
fn greet(name):
    return "Hello " + name

println(greet("World"))
```

compiles to a 384-byte `.wyc`. Its container header and directory:

```
0000: 57 59 43 00 01 07 00 00    magic "WYC\0", version 1, 7 sections, reserved
0008: 01 00 00 00 5C 00 00 00 27 00 00 00    id 1  header      offset 92  len 39
0014: 02 00 00 00 84 00 00 00 20 00 00 00    id 2  statics     offset 132 len 32
0020: 05 00 00 00 A4 00 00 00 4B 00 00 00    id 5  functions   offset 164 len 75
002C: 08 00 00 00 F0 00 00 00 3C 00 00 00    id 8  code        offset 240 len 60
0038: 09 00 00 00 2C 01 00 00 2D 00 00 00    id 9  debug       offset 300 len 45
0044: 0A 00 00 00 5C 01 00 00 10 00 00 00    id 10 exports     offset 348 len 16
0050: 0B 00 00 00 6C 01 00 00 12 00 00 00    id 11 free        offset 364 len 18
```

There is no section 3, 4, 6 or 7: the module has no constant global defaults,
no classes, and no messages. `println` used to account for two of those — it
was a symbol, because a relocation path is spelled in symbol indices, and a
relocation, because that is how a name was reached. It is a global slot now,
and its name lives in `free`.

Decoded, the sections are:

```
header       { n: "hello", v: 1, g: 2, l: 3 }
statics      [ "Hello ", "World" ]
functions    [ { n: "greet", p: [ { n: "name" } ], l: 2, c: 11, f: 0 } ]
exports      { greet: 0 }
free         { println: 1 }
debug        { f: "hello.wy", ln: { "3": 5, "11": 3 } }
```

Two globals. `greet` is one the module defines, so it is exported. `println`
is one it only reads, so it is free: filled from the builtins at load, before
init runs, and read afterwards by an ordinary `gget` (§7.2). There is no
referenced-name set and no `resolve` — init opens straight into its own code.

The `statics` payload in full, showing the BSON framing of §4:

```
0000: 20 00 00 00                    document length = 32
      02 30 00                       string, key "0"
      07 00 00 00                    length 7 (6 bytes + terminator)
      48 65 6C 6C 6F 20 00           "Hello " NUL
      02 31 00                       string, key "1"
      06 00 00 00                    length 6
      57 6F 72 6C 64 00              "World" NUL
      00                             document terminator
```

And the `code` section, fifteen words, disassembled:

```
word  bytes                      instruction
   0  A8 00 00 00 00 00 00 00    closure L0 <- fn#0, 0 caps
   2  43 00 00 00                gset g0 <- L0            (greet)
   3  42 00 01 00                gget L0 <- g1            (println)
   4  42 01 00 00                gget L1 <- g0            (greet)
   5  46 02 01 00                lconst L2 <- static#1    ("World")
   6  A0 01 01 00 00 00 01 00    call base=L1 argc=1 nres=1
   8  A0 01 00 00 00 00 01 00    call base=L0 argc=1 nres=1
  10  02 00 00 00                return count=0
  11  46 00 00 00                lconst L0 <- static#0    ("Hello ")
  12  85 00 01 00 00 80 00 00    add L1 <- L0 + P0        (name)
  14  02 01 01 00                return L1 count=1
```

Words 0–10 are the init routine, running in a 3-slot frame (`header.l`). Words
11–14 are `greet`, at the `c: 11` its `functions` entry records, running in a
2-slot frame with `name` in P0.

Reading one encoding aloud: `add L1 <- L0 + P0` at word 13 is opcode `0x85`.
Word 0 is `85 00 01 00` — op `0x85`, `f` unused, `a0 = 0x0001` = L1. Word 1 is
`00 80 00 00`, which little-endian is `0x00008000`: `a1 = 0x0000` = L0 in the
high half, `a2 = 0x8000` = P0 in the low half.

Note the two calls. `greet("World")` is word 7, with its window based at L1 —
which is exactly the argument slot of the outer `println` call at word 9. The
result of the inner call lands where the outer call needs it, so nothing is
copied between them. That is the register-window convention (§1.3) doing its
job.

---

## Appendix B — a reader's checklist

A minimal conforming loader, in order:

1. **Container.** Check the magic and version. Read the section count and
   directory. Reject a duplicate or unknown id (except 9, which is ignored),
   an unsorted directory, or an out-of-range offset or length.
2. **BSON.** One reader for the eight types of §4. Reject any other tag or a
   binary subtype other than 0. Arrays are documents; you may ignore their
   keys.
3. **Header.** Read `n`, `v`, `g`, `l`. Allocate `g` globals as Unset.
4. **Tables.** Intern `symbols`. Apply `slot_defaults`. Read `statics`,
   `functions`, `classes`, `messages` into whatever shape suits you.
   Allocate one unbound bind slot per message, and read `exports` and `free`
   into name → slot maps. Then fill every `free` slot the builtins supply.
5. **Bounds.** Every static, symbol, message, function, class and global
   index in every table is in range. Doing this once at load lets the
   interpreter loop skip it entirely.
6. **Code.** Point at the `code` payload; it is aligned and needs no copying
   on a little-endian host.
7. **Publish, then run.** Register the module under `header.n` *before*
   calling word offset 0 with a `header.l`-slot frame and zero arguments.
8. **Dispatch loop.** Read word 0, switch on `op & 0xff`, advance 1 or 2
   words per bit 7. Implement the backfill rule of §1.3 in one place and use
   it for `call`, `msg`, `super` and function return alike.
9. **Filling names.** Nothing is deferred to a function's first call: a free
   slot is filled by the builtins at load, and by each `import`/`import_star`
   as it runs (§7.2). A body reads names with `gget`, like everything else.
