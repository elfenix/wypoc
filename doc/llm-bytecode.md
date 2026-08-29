# Wyrm Bytecode: Format and Compiler Specification (v1 draft)

This document formalizes [bytecode.md](bytecode.md) into something a compiler
can be written against. It defines:

1. the abstract machine model the bytecode assumes,
2. the instruction encoding and the v1 opcode set,
3. the module image — the sections that make up a compiled module,
4. the three interchangeable containers for an image: `.wy_a` (ASCII
   listing), `.c` (C arrays), `.wyc` (binary),
5. lowering recipes: how each language construct compiles,
6. the plan for the `.wy → .wy_a / .c / .wyc` compiler inside the Python
   POC (`wypoc/compiler_bc/`).

**Not in scope:** the VM/interpreter implementation itself (that comes later,
also likely staged in this codebase), garbage collection, and the runtime
value representation. Where the VM's behavior matters to the compiler (call
convention, result backfill, import order) it is specified here; how the VM
achieves it is not.

Design pressure throughout is **small code size and fast dispatch** — the
targets are MCUs and desktop GUI apps from one image format:

- Fixed-size instructions (one or two `u32` words) decode with one `switch`
  and no length decoding beyond a single opcode bit.
- Call arguments and results live in overlapping register windows, so a call
  copies nothing that isn't already in place.
- Section payloads are usable as loaded: statics and code can stay in flash
  (`const` arrays in the `.c` container). Name references resolve in
  batches into a bind table — at module init and at each function's first
  call (§6.2) — so no code is ever rewritten and the hot path never checks
  a binding.
- Structured sections are a pinned BSON subset: one ~200-line reader on the
  C side, no schema negotiation.

Deviations from the bytecode.md brainstorm are listed in
[Appendix B](#appendix-b-deviations-from-bytecodemd) — review those first if
you wrote the brainstorm.

---

## 1. Machine model

The VM is **register-based**. "Registers" are stack slots reserved when a
function is entered; there is no separate register file.

Two stacks exist per fiber:

- **L stack (locals)** — one frame per active call, holding that function's
  named locals and expression temporaries. A function's frame size
  (`nlocals`) is computed at compile time and is the high-water mark of
  named locals plus temps.
- **P stack (parameters / CPS)** — one frame per active call, holding, in
  order: the `this` values (messages only), the declared parameters, then
  the captured closure variables. The layout is fully known at compile time.

Registers are named `L0…` and `P0…` in this document and in `.wy_a`
disassembly comments.

Frame layout of the P stack for a call:

```
P0 .. P(t-1)      this values        (t = dispatch arity; 0 for plain fn)
Pt .. P(t+n-1)    declared params    (n = parameter count)
P(t+n) ..         captured variables (closure environment, copied at
                                      CLOSURE time)
```

Native scalar types are `i32` and `f32`; both have immediate-load opcodes.
Everything else arrives through the static pool, the symbol table, or
runtime construction.

Every function returns 0–128 values from a contiguous L-window. The caller
declares how many results it wants; the VM **backfills missing results with
`nil` and drops extras**. This one rule implements both multi-value return
and "statement value is nil when nothing ran".

Module code offset 0 is the **module init routine**: it runs at import, after
the loader has processed the sections (see §6). It is an ordinary function
with 0 parameters and returns 0 values.

### 1.1 Limits

| Limit | Value | Why |
|---|---|---|
| Locals per function (L frame) | 32767 | bit 15 of a u16 register ref selects the P stack |
| P frame size (this + params + captures) | 32767 | same encoding |
| Parameters per call | 128 | `f`-field counts are 7-bit safe |
| Return values per call | 128 | same |
| this values (dispatch arity) | 4 min, 16 max | language spec minimum; message map bound |
| Tuple literal | 128 elements | language limit; larger data uses `list` |
| Message map per class | 16 entries | fixed-size scan/cache on MCU |
| Symbol length | 255 bytes UTF-8, 31 codepoints significant | language limit |
| Module globals | 65535 | u16 operand |
| Static pool entries | 65535 | u16 operand |
| Symbols per module | 65535 | u16 operand |
| Relocations per module | 65535 | u16 operand |
| Functions per module | 65535 | u16 operand |
| Classes per module | 65535 | u16 operand |
| Code per module | 2³² words | u32 offsets |
| Short jump reach | ±32K words | i16; wide form carries i32 |

The compiler MUST reject a module that exceeds any of these with a
`CompileError` naming the limit — never emit silently-wrong code.

---

## 2. Instruction encoding

An instruction is one or two little-endian `u32` words.

```
word 0:  [ a0 : u16 ][ f : u8 ][ op : u8 ]     (op in bits 0–7)
word 1:  [ a1 : u16 ][ a2 : u16 ]              (a1 in bits 16–31)
```

Accessors (this fixes the field collision in the reference `opcode.h` —
see Appendix B, D1):

```c
op = code[0] & 0xff;
f  = (code[0] >> 8) & 0xff;
a0 = (code[0] >> 16) & 0xffff;
a1 = (code[1] >> 16) & 0xffff;
a2 =  code[1] & 0xffff;
```

**Length rule:** bit 7 of the opcode selects the length. `op < 0x80` is one
word; `op >= 0x80` is two words. `WYRM_OP_LONG_START == 0x80` exactly.

**Opcode ranges:**

| Range | Meaning |
|---|---|
| `0x00–0x3F` | one-word ops with no wide form |
| `0x40–0x7F` | one-word **compact** forms of pairable ops |
| `0x80–0xBF` | two-word-only ops |
| `0xC0–0xFF` | two-word **wide** forms: `wide = compact \| 0x80` |

A pairable op's compact form packs one operand into `f` (an 8-bit register
ref or an 8-bit immediate); its wide form carries the same operand in `a1`
(or a 32-bit payload in word 1). The compiler emits the compact form
whenever operands fit and MUST fall back to the wide form when they don't.

### 2.1 Register references

- **u16 register ref** (`a0`/`a1`/`a2` when the operand column says *reg*):
  bit 15 clear → L slot (`0–32767`); bit 15 set → P slot (low 15 bits).
  `P3` encodes as `0x8003`.
- **u8 register ref** (`f` when the column says *reg8*): bit 7 clear → L slot
  `0–127`; bit 7 set → P slot `0–127`. An operand outside that range forces
  the wide form.

### 2.2 Jump offsets

Jump offsets are signed word counts **relative to the address of the next
instruction** (i.e. after the current instruction's 1 or 2 words). Compact
conditional jumps carry `i16` in `a0`; wide forms carry `i32` in word 1.

---

## 3. Opcode set (v1)

Operand column legend: `reg` = u16 register ref, `reg8` = 8-bit register ref
in `f`, `imm` = immediate, `idx` = index into the named module table,
`rel` = jump offset (§2.2). "base" = start of a register window in the
current L frame.

### 3.1 Core one-word ops (`0x00–0x3F`)

| Op | Mnemonic | Fields | Semantics |
|---|---|---|---|
| `0x00` | `noop` | — | no operation (`pass` compiles to nothing; `noop` exists for patching/alignment) |
| `0x01` | `trap` | `f`=code | halt with error; code 0 = unreachable, 1 = debugger break, 2–255 reserved |
| `0x02` | `return` | `a0`=base, `f`=count | return `count` values starting at L`base`; runs registered defers first |
| `0x03` | `lnil` | `a0`=dst | dst ← nil |
| `0x04` | `lbool` | `a0`=dst, `f`=0\|1 | dst ← false/true |
| `0x05` | `lunset` | `a0`=dst | dst ← the Unset error value |
| `0x06` | `import_star` | `a0`=reloc idx (wildcard) | register the target module's namespace (minus the relocation's except-list) for resolution search (§6.2); appears only in module init code — wildcard imports are top-level statements |
| `0x07` | `resolve` | — | resolve every not-yet-bound entry in the module's referenced-name set (header `u`, §4.2); emitted once in module init, immediately after the import sequence (§6.2) |
| `0x08–0x3F` | — | | reserved |

### 3.2 Pairable ops (`0x40–0x7F` compact, `0xC0–0xFF` wide)

Ops marked **[INV]** are *inverted* per the brainstorm convention: `a0`
carries the table index (needs the full u16 range even in compact form) and
the register moves to `f`/`a1`.

| Op (compact/wide) | Mnemonic | Compact fields | Wide fields | Semantics |
|---|---|---|---|---|
| `0x40`/`0xC0` | `i8` / `i32` | `a0`=dst, `f`=imm8 (sign-ext) | `a0`=dst, word1=i32 | dst ← integer immediate |
| `0x41`/`0xC1` | `move` | `a0`=dst, `f`=src reg8 | `a0`=dst, `a1`=src | dst ← src |
| `0x42`/`0xC2` | `gget` **[INV]** | `a0`=global idx, `f`=dst reg8 | `a1`=dst | dst ← module global |
| `0x43`/`0xC3` | `gset` | `a0`=global idx, `f`=src reg8 | `a1`=src | module global ← src |
| `0x44`/`0xC4` | `rget` **[INV]** | `a0`=reloc idx, `f`=dst reg8 | `a1`=dst | dst ← the bound value of the relocation entry — a plain table read; resolution already happened at scope start (§6.2), so an entry that failed to resolve yields its NotFound error here |
| `0x45`/`0xC5` | `lsym` **[INV]** | `a0`=symbol idx, `f`=dst reg8 | `a1`=dst | dst ← interned symbol |
| `0x46`/`0xC6` | `lconst` **[INV]** | `a0`=static idx, `f`=dst reg8 | `a1`=dst | dst ← static pool value |
| `0x47`/`0xC7` | `import` **[INV]** | `a0`=reloc idx (module), `f`=dst reg8 | `a1`=dst | dst ← module object, triggering the dependency's load and init on first import |
| `0x48`/`0xC8` | `neg` | `a0`=dst, `f`=src reg8 | `a1`=src | dst ← −src |
| `0x49`/`0xC9` | `inv` | same | same | dst ← ~src |
| `0x4A`/`0xCA` | `not` | same | same | dst ← boolean not (uses `__bool__`) |
| `0x4B`/`0xCB` | `jf` | `f`=cond reg8, `a0`=rel i16 | `a0`=cond, word1=rel i32 | jump if cond is falsy |
| `0x4C`/`0xCC` | `jt` | same | same | jump if cond is truthy |
| `0x4D`/`0xCD` | `jerr` | same | same | jump if cond is an `error` |
| `0x4E`/`0xCE` | `jnerr` | same | same | jump if cond is not an `error` |
| `0x4F`/`0xCF` | `jmp` | `a0`=rel i16 (`f` unused) | word1=rel i32 (`a0` unused) | unconditional jump |
| `0x50`/`0xD0` | `rset` | `a0`=reloc idx, `f`=src reg8 | `a1`=src | bound variable ← src; the write-through twin of `rget` (resolution per §6.2; an unresolved or non-variable binding makes this a failed store — Appendix C) |
| `0x51–0x7F` | — | | | reserved (immediate-operand arithmetic is the planned tenant) |

### 3.3 Two-word-only ops (`0x80–0xBF`)

**Loads and data:**

| Op | Mnemonic | Fields | Semantics |
|---|---|---|---|
| `0x80` | `f32` | `a0`=dst, word1=IEEE-754 bits | dst ← float immediate |
| `0x81` | `tuple` | `a0`=dst, `a1`=base, `f`=count | dst ← tuple of L`base`…L`base+count−1` |
| `0x82` | `list` | same | dst ← list of the window |
| `0x83` | `dict` | `a0`=dst, `a1`=base, `f`=pair count | dst ← dict; window holds k,v,k,v,… (2·count regs) |
| `0x84` | `plist` | `a0`=dst, `a1`=base, `f`=count | dst ← pair list (`$[…]`) of the window |

**Arithmetic / comparison** (three-address; `a0`=dst, `a1`=lhs, `a2`=rhs;
dispatch honors the `__add__`-family overloads):

| Op | Mnemonic | | Op | Mnemonic |
|---|---|---|---|---|
| `0x85` | `add` | | `0x8D` | `shl` |
| `0x86` | `sub` | | `0x8E` | `shr` |
| `0x87` | `mul` | | `0x8F` | `eq` |
| `0x88` | `div` | | `0x90` | `ne` |
| `0x89` | `mod` | | `0x91` | `lt` |
| `0x8A` | `pow` | | `0x92` | `le` |
| `0x8B` | `band` | | `0x93` | `gt` |
| `0x8C` | `bor` | | `0x94` | `ge` |
| `0x95` | `bxor` | | | |

| Op | Mnemonic | Fields | Semantics |
|---|---|---|---|
| `0x96` | `in` | `a0`=dst, `a1`=item, `a2`=container | dst ← membership bool |
| `0x97` | `is` | `a0`=dst, `a1`=value, `a2`=type | dst ← type-check bool; `a2` holds a class/type value or a tuple of them (sum type) |

**Object access:**

| Op | Mnemonic | Fields | Semantics |
|---|---|---|---|
| `0x98` | `getidx` | `a0`=dst, `a1`=obj, `a2`=index reg | dst ← obj[index] |
| `0x99` | `setidx` | `a0`=obj, `a1`=index reg, `a2`=src | obj[index] ← src |
| `0x9A` | `getattr` | `a0`=dst, `a1`=obj, `a2`=symbol idx | dst ← property lookup (property namespace only, per spec) |
| `0x9B` | `setattr` | `a0`=obj, `a1`=symbol idx, `a2`=src | property ← src |
| `0x9C` | `getslot` | `a0`=dst, `a1`=obj, `a2`=slot # imm | dst ← slot by fixed index — an optimization the compiler may emit only when the receiver's whole inheritance chain is module-local (§7.1); symbolic `getattr` is the general path |
| `0x9D` | `setslot` | `a0`=obj, `a1`=slot # imm, `a2`=src | slot ← src (same restriction) |

**Calls and returns:**

| Op | Mnemonic | Fields | Semantics |
|---|---|---|---|
| `0xA0` | `call` | `a0`=base, `f`=argc, `a1`=nres | callee at L`base`, args at L`base+1`…; results land at L`base`… with nil backfill (§1) |
| `0xA1` | `call_va` | `a0`=base, `a1`=nres | callee at L`base`, positional tuple at L`base+1`, kwargs dict at L`base+2`; for `f(*a, **k)` shapes |
| `0xA2` | `msg` | `a0`=base, `f`=argc, `a1`=reloc idx (message), `a2`=nres | receiver (value, or tuple for multi-dispatch) at L`base`, args at L`base+1`…; dispatch, results at L`base`… |
| `0xA3` | `msg_va` | `a0`=base, `a1`=reloc idx, `a2`=nres | receiver, positional tuple, kwargs dict at L`base`…L`base+2` |
| `0xA4` | `getmsg` | `a0`=dst, `a1`=receiver, `a2`=reloc idx | dst ← bound closure (`recv ! name` without call) |
| `0xA5` | `super` | `a0`=base, `f`=argc, `a1`=nres | chain to next-most-general method of the current dispatch; args at L`base`… |
| `0xA6` | `return_cps` | `a0`=base, `f`=argc, `a1`=continuation closure reg | tail call: reuse frame, continuation receives the result. **Reserved in v1** — the compiler does not emit it yet (Appendix C) |
| `0xA7` | `yield` | `a0`=base, `f`=count | yield `count` values from L`base`…; on resume the sent value is written to L`base` |

**Construction and registration:**

| Op | Mnemonic | Fields | Semantics |
|---|---|---|---|
| `0xA8` | `closure` | `a0`=dst, `a1`=function idx, `a2`=first capture reg, `f`=ncaps | dst ← closure over function table entry, copying `ncaps` regs into its environment (in P-frame capture order) |
| `0xA9` | `class` | `a0`=dst, `a1`=class idx | dst ← class object realized from the class section entry (registers its message map) |
| `0xAA` | `new_instance` | `a0`=dst, `a1`=class reg | dst ← uninitialized instance (slots at defaults/Unset); used by runtime-generated constructors — script code constructs via `call` on the class value |
| `0xAB` | `new_primitive` | `a0`=dst, `f`=type tag | dst ← fresh empty primitive (list/dict/…); runtime-internal helper |
| `0xAC` | `reg_msg` | `a0`=reloc idx (message), `a1`=closure/fn reg, `a2`=types tuple reg | register a method for multiple dispatch (`fn [T1, T2] name(...)` outside a class body) |
| `0xAD` | `defer_reg` | `a0`=closure reg, `f`=mode | arm a defer for this frame; mode 0 = always, 1 = on error, 2 = on error\|nil |

**Iteration:**

| Op | Mnemonic | Fields | Semantics |
|---|---|---|---|
| `0xAE` | `iter` | `a0`=dst, `a1`=src | dst ← iterator (`__iter__` contract) |
| `0xAF` | `itnext` | `a0`=dst, `a1`=iter, `a2`=rel i16 | advance; write next value to dst, or jump `a2` on exhaustion (StopIteration) |

**Delegation, unpacking, scope access:**

| Op | Mnemonic | Fields | Semantics |
|---|---|---|---|
| `0xB0` | `yield_from` | `a0`=dst, `a1`=sub coroutine reg | delegate: every yield of the subgenerator flows out of this coroutine, every sent/`next` value flows in, until the subgenerator returns; dst ← its return value |
| `0xB1` | `unpack` | `a0`=dst base, `a1`=src, `f`=count | unpack an indexable of exactly `count` items into L`dst`…L`dst+count−1`; on length or type mismatch every dst reg receives the error value |
| `0xB2` | `cmp3` | `a0`=dst, `a1`=lhs, `a2`=rhs | dst ← −1/0/1 three-way comparison (`<=>`) |
| `0xB3` | `getscope` | `a0`=dst, `a1`=obj, `a2`=symbol idx | dst ← `::` namespace member of a runtime value (module / class / function statics); a namespace lookup, distinct from the property namespace |
| `0xB4` | `setscope` | `a0`=obj, `a1`=symbol idx, `a2`=src | `::` namespace member ← src |
| `0xB5–0xBF` | — | | reserved |

---

## 4. Module image: sections

A compiled module is a set of **sections**. All three containers carry the
same section payloads byte-for-byte (except `code` formatting in `.c`,
§5.2). All multi-byte integers are little-endian.

| ID | Name | Payload |
|---|---|---|
| 1 | `header` | BSON document |
| 2 | `statics` | BSON array |
| 3 | `slot_defaults` | BSON document |
| 4 | `symbols` | BSON array |
| 5 | `functions` | BSON array |
| 6 | `classes` | BSON array |
| 7 | `relocations` | BSON array |
| 8 | `code` | raw `u32[]`, **not** BSON |
| 9 | `debug` | BSON document, optional; VM MUST ignore |
| 10 | `exports` | BSON document, optional |

### 4.1 The BSON subset

Standard BSON framing (int32 total length, elements, trailing 0x00), with
exactly these element types permitted:

| Tag | Type | Used for |
|---|---|---|
| `0x01` | double | float constants |
| `0x02` | string | names, string constants |
| `0x03` | document | nested records |
| `0x04` | array | lists (keys `"0"`, `"1"`, …) |
| `0x05` | binary (subtype 0) | byte/packed-int data |
| `0x08` | bool | flags, bool constants |
| `0x0A` | null | nil constants |
| `0x10` | int32 | all indices and counts |

A loader encountering any other element type MUST reject the module. Keys
are kept to 1–2 characters in the schemas below to keep images small and
the C reader trivial. The compiler ships its own ~100-line encoder
(`bsonlite.py`) — no external dependency.

Convention: an *optional* key is simply absent when not applicable; there
are no `-1` sentinels.

### 4.2 `header`

```
{ n: str        module name (fully qualified, "::"-separated)
  v: int32      format version, = 1
  g: int32      module global slot count
  l: int32      L-frame size of the init routine (locals + temps high-water)
  u: [int32]    module-level referenced-name set: reloc idxs used by module
                init code, bound by its `resolve` instruction (§6.2) }
```

The init routine is code offset 0 and has no `functions` entry of its own, so
`l` is where its frame size lives — the loader needs it to call init the same
way it calls anything else (§6.1 step 6).

### 4.3 `statics`

BSON array of constant values — string, int32, double, bool, null, or
binary. No symbols (those live in `symbols`). Referenced by index from
`lconst`, slot/param defaults, and class slot defaults. The compiler
deduplicates identical constants.

### 4.4 `slot_defaults`

BSON document mapping module-global index (as decimal string key, BSON
style) to a constant default value. Globals absent from this map start as
**Unset**. Non-constant initializers are not represented here — they compile
into the module init code.

### 4.5 `symbols`

BSON array of strings: every name the module needs to look up or intern at
runtime (relocation path components, `getattr`/`setattr` names, `lsym`
literals). Deduplicated; order defines the index space.

### 4.6 `functions`

BSON array; entry *i* defines function index *i*.

```
{ n: str        name ("!" suffix convention marks messages, e.g. "greet!")
  p: [ { n: str          parameter name
         d: int32        static idx of default   (optional)
       } ... ]
  l: int32      L-frame size (locals + temps high-water)
  c: int32      code offset in u32 words
  f: int32      flags: bit0 coroutine, bit1 message, bit2 *args, bit3 **kwargs
  t: [int32]    dispatch types as reloc idxs     (messages only)
  k: int32      capture count (P-frame tail size) (optional, default 0)
  r: int32      max results the body returns      (optional, default 1)
  u: [int32]    referenced-name set: reloc idxs this body uses, resolved by
                the VM at the function's first call (§6.2)  (optional) }
```

v1 restriction: parameter defaults MUST be constants (static pool refs). A
non-constant default is a `CompileError` (see Appendix C).

### 4.7 `classes`

BSON array; entry *i* defines class index *i* (the operand of `class`).

```
{ n: str        class name
  s: int32      reloc idx of superclass          (optional; absent = object)
  sl: [ { n: str        slot name
          d: int32      static idx of constant default   (optional)
          g: int32      getter fn idx (virtual slot)      (optional)
          s: int32      setter fn idx (virtual slot)      (optional)
        } ... ]
  i: int32      init fn idx                       (optional)
  m: [ { y: int32 symbol idx, f: int32 fn idx } ... ]   message map, ≤16
  st: [ { n: str name, g: int32 global idx } ... ]      class statics →
                                                        module-global slots }
```

Class statics live in module global slots (they are module-lifetime, and
`Class::name` addressing resolves through the class object to the recorded
global). Their initializers run in module init code like any other
top-level effect.

### 4.8 `relocations`

BSON array; entry *i* defines relocation index *i* — a late-bound reference
to something outside the module (or dynamically registered inside it).

```
{ t: int32      0 module | 1 class | 2 message | 3 function | 4 variable
                | 5 wildcard namespace
  p: [int32]    qualified path as symbol idxs;
                mod::a::b → [sym("mod"), sym("a"), sym("b")]
  x: [int32]    except-list as symbol idxs (t = 5 only, optional):
                the names excluded by `import ...::* except (...)` }
```

Entries bind in batches at scope start (§6.2). A message relocation with a
single-component path that resolves to nothing pre-existing *creates* the
message identity — this is how a module's own messages come into being
before `reg_msg` populates them.

### 4.9 `code`

Raw array of `u32` words, § 2 encoding, 4-byte aligned in every container.
Word offset 0 is the module init entry point. Function bodies follow,
located by the `c` field of their `functions` entries. No padding between
functions is required; `noop` is available if an implementation wants
alignment.

### 4.9a `exports` (optional)

```
{ "<name>": int32 global slot, ... }
```

Every module global by name. A `::` lookup into this module resolves through
it (§6.2): the header carries the slot *count*, not the names, so without this
a compiled module can be imported but nothing read out of it. Emitted whenever
the module has globals.

### 4.10 `debug` (optional)

```
{ f: str        source file name
  ln: { "<code word offset>": int32 line, ... }   line table
  lv: [ ... ]   reserved for local-name maps }
```

Emitted unless `--strip`. The VM MUST ignore it; the DAP adapter and the
`.wy_a` disassembler consume it.

---

## 5. Containers

One image, three serializations. `module.wy` compiles to `module.wy_a`,
`module.c`, and `module.wyc`; the three MUST decode to identical section
payloads.

### 5.1 `.wy_a` — ASCII listing

Purpose: human-readable ground truth, diffable in review, and **trivially
assemblable** — an assembler needs ~50 lines: strip comments, concatenate
hex bytes per section, wrap in the `.wyc` container.

Grammar (line-oriented, UTF-8, LF):

```
file      := magic line*
magic     := "WYA 1" [ " " module-name ] EOL
line      := section / data / comment / blank
section   := "SECTION " name EOL          ; name = lowercase section name (§4)
data      := addr ":" (" " hexbyte)+ [ " ;" text ] EOL
comment   := [ws] ";" text EOL
blank     := [ws] EOL
addr      := hexdigit{4,8}                ; byte offset within the section
hexbyte   := hexdigit hexdigit
```

Assembler rules — everything an assembler does, in full:

1. Verify the magic line.
2. `SECTION name` starts collecting bytes for that section.
3. For each data line: verify `addr` equals the count of bytes collected so
   far in this section (a guard against hand-edit slips), then append the
   bytes. Everything from `;` to EOL is discarded.
4. Comment-only and blank lines are discarded.
5. Wrap the collected sections as `.wyc` (§5.3).

Formatting rules for the *emitter* (not needed to parse):

- Lines fit 120 columns; the `;` comment column is 60 characters wide when
  present.
- BSON sections emit 16 bytes per line; the comment describes every entry
  that begins on that line, `; `-joined when more than one does.
- The `code` section emits **one instruction per line** (4 or 8 bytes,
  little-endian byte order); the comment is the disassembly, using the
  mnemonics of §3 and `L#`/`P#` register names, with source line numbers
  interleaved as comment-only lines when `debug` info exists.

### 5.2 `.c` — C arrays

Purpose: drop a compiled module into a firmware/desktop build with zero
load-time I/O. Emission rules:

- Module name is encoded by replacing `::` with `__`; the result must be a
  valid C identifier (else `CompileError`).
- Every BSON section becomes
  `static const uint8_t {enc}_{section}[] = { ... };`
- The code section becomes `static const uint32_t {enc}_code[] = { ... };`
  — `uint32_t`, not bytes, so alignment and host byte order are correct by
  construction (Appendix B, D5). One instruction per source line with the
  disassembly as a trailing comment.
- The file ends with the image descriptor:

```c
const wy_module_image {enc}_image = {
    .name = "{module::name}",
    .sections = {
        [WY_SEC_HEADER] = { {enc}_header, sizeof {enc}_header },
        /* ... one entry per emitted section ... */
        [WY_SEC_CODE]   = { (const uint8_t*) {enc}_code, sizeof {enc}_code },
    },
};
```

with the supporting declarations (these belong in the future
`wyrm/image.h`):

```c
typedef struct wy_section_ref { const uint8_t* data; uint32_t len; } wy_section_ref;
enum { WY_SEC_HEADER = 1, WY_SEC_STATICS, WY_SEC_SLOT_DEFAULTS, WY_SEC_SYMBOLS,
       WY_SEC_FUNCTIONS, WY_SEC_CLASSES, WY_SEC_RELOCATIONS, WY_SEC_CODE,
       WY_SEC_DEBUG, WY_SEC_EXPORTS, WY_SEC_COUNT };
typedef struct wy_module_image { const char* name; wy_section_ref sections[WY_SEC_COUNT]; } wy_module_image;
```

The generated `.c` includes only `<stdint.h>` plus the image header, so it
compiles standalone under `-Wall -Werror` (same bar `compiler_c` holds its
output to).

### 5.3 `.wyc` — binary

```
offset  size  field
0       4     magic: 57 59 43 00            ("WYC\0")
4       1     container version = 1
5       1     section count
6       2     reserved = 0
8       12·n  directory entries, ascending section id:
              { u8 id; u8 reserved; u16 reserved; u32 offset; u32 length }
...           section payloads, each 4-byte aligned, in directory order
```

Offsets are from file start. Canonical byte order is little-endian
throughout (a big-endian MCU target byte-swaps code at load, or uses the
`.c` container which is host-order by construction). No checksum in v1; a
directory entry id `0` is reserved for a future CRC section.

---

## 6. Import and linking semantics

(Formalizing bytecode.md's "import process".)

### 6.1 Load

Given an image, the loader:

1. Parses `header`; allocates `g` global slots, all **Unset**.
2. Applies `slot_defaults` to those slots.
3. Interns every `symbols` entry.
4. Allocates the relocation bind table (`nrelocs` slots, all *unbound*).
5. Registers `functions` and `classes` as prototypes (no execution).
6. Publishes the module object under its name, **then** runs code offset 0
   as a zero-arg call. (Publishing first makes import cycles observable
   rather than divergent — a cyclic importer sees the module with whatever
   globals its init has populated so far, which matches the POC
   interpreter's behavior.)

### 6.2 Name resolution

The relocation table is exactly the set of names the source references —
the compiler knows every one of them from the AST — and the bind table is
that table's cache. Resolution is **batched at scope start**, never checked
per instruction:

- **Module level.** The compiler hoists the import sequence to the top of
  the init routine. Each `import`/`import_star` triggers the dependency to
  load and run its own init ("build"). Immediately after the last import
  the compiler emits `resolve`, which binds every entry in the header's
  `u` set. Init code before the `resolve` point MUST NOT reference
  external names except through the import ops themselves
  (compiler-enforced).
- **Function level.** Each function entry lists the relocation entries its
  body uses (`u`, §4.6). At the function's **first call** the VM resolves
  any of them still unbound, before the body runs. (Entries already bound
  — by module init or an earlier function — cost nothing.)

After its owning scope has started, every `rget`/`rset`/`msg`/`getmsg` is
a straight table read with no bind check on the hot path.

Resolving one entry walks the path left-to-right; the first component
resolves through, in order:

1. this module's import/alias table,
2. the builtin namespace,
3. the wildcard namespaces registered by `import_star`, in registration
   order — first match wins, except-lists filtering each;

subsequent components via `::` scope lookup. A failure binds the entry to
a NotFound error value rather than aborting anything: the error surfaces
at each use through normal error flow (`rget` yields it; a store through
it is a failed store, Appendix C). Entries resolve at most once —
namespaces are assumed stable after a module's init has run.

Only **purely dynamic references** — `getscope`/`setscope` on a runtime
value — ever perform a lookup at execution time; anything with a name in
the source goes through the table. Resolution never mutates code — the
bind table is the only mutable linking state, so code stays in ROM.

### 6.3 What the compiler may assume

- **Names are never resolved statically across a module boundary.** Bindings
  local to the module being compiled (its globals, functions, classes,
  statics) compile to table indices; every reference to anything outside it
  is emitted as a symbolic name reference — a relocation path — resolved at
  bind time, even when the compiler could peek at the dependency. The
  dependency's interface at run time is authoritative, not its interface at
  compile time.
- Name references are emitted **after decorator expansion**: decorators run
  at compile time (§7.2) and the expanded tree is what gets lowered, so
  decorator-generated code resolves exactly like handwritten code.
- Global indices, function indices, class indices, static indices, symbol
  indices, and relocation indices are all module-local and dense from 0.
- The same external name reached from two places in the module SHOULD share
  one relocation entry (wildcard entries are per-import-statement, since
  each carries its own except-list).
- Builtins (`println`, `len`, `next`, `send`, `pair`, `error`, type names
  used by `is`, …) are reached by relocation with a single-component path —
  resolution falls through the §6.2 order. An identifier that resolves to
  nothing known at compile time is still a `CompileError` **unless** a
  wildcard import is in scope, in which case it compiles to a
  single-component relocation and resolves (or errors) at bind time.

---

## 7. Lowering recipes

How the compiler translates each construct. `T#` denotes a temp slot from
the expression temp stack (§8.3); labels are patched to §2.2 offsets.

### 7.1 Expressions

| Construct | Lowering |
|---|---|
| int literal | `i8`/`i32` (fits i8 → compact) |
| float literal | `f32` |
| `true`/`false`/`nil` | `lbool` / `lnil` |
| string literal | pool → `lconst` |
| symbol literal | symbols → `lsym` |
| big int (> i32) | v1: `CompileError` (no bigint on target) |
| name (local) | its L slot, no code |
| name (param/capture) | its P slot, no code |
| name (module global) | `gget` |
| name (external/builtin) | `rget` |
| `a op b` | eval both, three-address op §3.2 |
| `-a`, `~a`, `not a` | `neg`/`inv`/`not` |
| `a and b` | `move T,a; jf T,done; move T,b; done:` |
| `a or b` | `move T,a; jt T,done; move T,b; done:` |
| `a in b`, `a is T` | `in` / `is` (type expr: `rget` the class(es), tuple for sums; generic params as in `list[int]` collapse to the base type check — runtime enforcement is limited per spec) |
| `a <=> b` | `cmp3` |
| `obj.name` | `getattr` (symbol idx) |
| `obj[i]` | `getidx` |
| slot name inside class fn | `getattr`/`setattr` on `this` — symbolic, because an external superclass makes absolute slot offsets unknowable at compile time (§6.3). The compiler MAY emit `getslot`/`setslot` as an optimization only when the entire inheritance chain is module-local |
| `mod::a::b` (path of names) | one relocation → `rget` (§6.2 — never resolved at compile time) |
| `expr::name` (dynamic base) | `getscope` |
| `f(a, b)` | window: callee, args contiguous at fresh temps; `call base, argc, nres` |
| any call with spreads or keywords — `f(*a, **k)`, `f(x, key=1)`, mixed | build the positional `tuple` (concatenating spreads via runtime concat) and the kwargs `dict`; `call_va` |
| `recv ! name(args)` | receiver at base, args after; `msg base, argc, reloc(name), nres` |
| `recv ! mod::name(args)` | same, reloc path `[mod, name]` |
| `(a, b) ! name(args)` | `tuple` the receivers first; identical `msg` |
| `recv ! name` (no call) | `getmsg` |
| `super(args)` | `super` |
| tuple/list/dict/`$[…]` literal | elements into a window; `tuple`/`list`/`dict`/`plist` |
| lambda / nested `fn` | compile body as function entry; `closure dst, fn#, caps` |
| `do:` block | inlined — compile the block in the current frame into a result temp (it is only *scoping*, which the compiler resolves statically; no call) |
| `try expr` | `move T,expr; jnerr T,+1words; return T,1` (defers still run — `return` semantics) |
| `expr catch handler` | `move T,expr; jnerr T,done; <handler → T>; done:` |
| `expr catch return v` | `move T,expr; jnerr T,done; <v → T2>; return T2,1; done:` |
| `yield v` | window with v; `yield base,1`; expression value = L`base` after resume |
| `yield a, b` | same with count 2 |
| `yield from e` | eval e (the sub coroutine) into a temp; `yield_from dst, T` |

### 7.2 Statements

| Construct | Lowering |
|---|---|
| `var x = e` / `x := e` | eval into x's L slot |
| `var x: T` | `lunset x` |
| `x = e` | eval into x's slot; global target → `gset`; `obj.a = e` → `setattr`; `obj[i] = e` → `setidx` |
| `mod::x = e` / cross-module static | `rset` (static path); `setscope` when the base is a runtime value; own-module statics are just `gset` |
| `a, b = call()` (multivalue) | eval call with nres 2 into a window; `move` out |
| `a, b = e` (tuple/iterable value) | eval e; `unpack base, e, 2`; `move` out. RHS that is a literal tuple expression (`a, b = b, a`) is decomposed by the compiler instead — no tuple is materialized |
| `x ?= e` | `jnerr x, done; <e → x>; done:` |
| `if/elif/else` as value | each branch ends `move R, branch_value`; skipped-entirely → `lnil R` before the chain |
| `while` | standard; the loop's own value is `lnil R` before the loop (see below) |
| `for x in e / else` | `iter T, e`; loop head `itnext x, T, else_lbl`; `break` jumps *past* else, exhaustion jumps *to* else |
| `continue` / `break` | `jmp` to head / end |
| `return a, b` | values to a window; `return base, 2` |
| bare `return` | `lnil T; return T, 1` |
| implicit return (last stmt) | function body keeps a result temp R; epilogue `return R, 1` (or `return R0, r` for multivalue tails) |
| `defer:` block | block → closure (captures by the closure rules); `closure T, fn#, caps; defer_reg T, 0`. v1 arms at **frame** granularity — defers run at function return, a deliberate narrowing of the spec's "containing block" (Appendix C) |
| `defer on error:` | mode 1; `defer on error \| nil:` mode 2 |
| `pass` | nothing |
| `import mod::a::b` | relocs for each prefix; module init: `import` op into the globals for `mod`, `mod::a` alias chain per spec; `as` aliases are compile-time renames only. All imports are hoisted to the top of module init, ahead of the `resolve` point (§6.2) |
| `import mod::*` / `except (...)` | one wildcard reloc (t = 5, except-list in `x`); module init: `import_star`. From then on, an identifier the compiler can't resolve compiles to a single-component relocation searched at bind time (§6.2) instead of a `CompileError` |
| `import static ...` | same lowering as `import` — `static` marks a dependency wanted at compile time only, not a runtime one, so nothing changes at the bytecode level; the compiler enforces the static-import usage restrictions (no closures / ctors / runtime messages) as `CompileError`s |
| top-level `fn name` | `closure T, fn#, 0; gset g(name), T` in module init |
| `fn [T1,T2] name` | as above, then `rget` types, `tuple`, `reg_msg reloc(name), T, types` |
| top-level `class C` | `class T, class#; gset g(C), T` in module init; statics init code follows |
| `static x: T = e` in fn/class | a module global; bound once where the owner is created (module init / class realization point) |
| `foo::$ast` | the definition's s-expression, built by the code — `lsym`/`lconst`/`i8`/`list`/`plist` over its leaves. Resolved at compile time against the module's own definitions, after decorator expansion, so the tree found is the one the binding would hold. A name from outside the module is a `CompileError`: nothing crosses a module boundary at compile time (§6.3), and a compiled dependency carries no ASTs to reach into. `sexpr()` is idempotent on the result, so code written against a tree box reads it unchanged |
| decorators | **run at compile time**: the POC's existing decorator machinery (`sexpr.py` + `wyrm_eval_parse_tree.py`) expands the tree before lowering; bytecode never sees a `Decorated` node. All name references are emitted after expansion (§6.3), so decorator-injected code resolves like handwritten code. A compiled module carries no ASTs; what it carries is the code that rebuilds the one s-expression a `::$ast` asked for (see the row above) |
| `co` functions | compiled as ordinary function with flag bit0; calling one constructs the coroutine (VM behavior); delegation via `yield_from` (§7.1) |
| `native::block` | `CompileError` — native modules go through `compiler_c`, not bytecode |

POC extensions beyond the language spec (`signal`/`emit`, `thread`/`task`
spawn) are v1 `CompileError`s naming the construct; opcode space
`0xB0–0xBF` is reserved partly for them.

`with` blocks are a different case: the language spec has **removed** the
construct, and `wypoc`'s grammar and interpreter still carry it only because
they have not caught up. It is a permanent `CompileError` here, worded so it
does not read as a gap the compiler will close — there is nothing to lower it
to.

Several constructs this document specifies cannot be exercised end to end in
`wypoc` today, because the POC's grammar or interpreter has not caught up with
the language spec. The compiler emits what is specified here regardless; §9
lists every such divergence and how each one is tested instead.

### 7.3 Classes in detail

For `class C(Base): ...`:

1. Slots with constant defaults → `sl[].d`. Non-constant slot defaults are
   a v1 `CompileError` (evaluate-at-class-body-time semantics can be added
   as init-code later without format change).
2. Virtual slots: `getter`/`setter` compile as message-flagged functions
   with dispatch on `[C]`; their fn idxs land in `sl[].g`/`sl[].s`.
3. `fn init(...)` compiles as a message on `[C]` whose body starts from a
   `new_instance`-provided `this` (the VM's construct-on-call does
   `new_instance`, applies slot defaults, then calls init; error return
   from init overrides the result per spec).
4. In-class methods → message map `m` (≤16, else `CompileError`).
5. External `fn [C] name` → `reg_msg` at its definition point.

---

## 8. The compiler in wypoc

### 8.1 Package layout — `wypoc/compiler_bc/`

Mirrors `compiler_c`'s architecture (registries, DAG of modules rooted at
pure-data leaves, fail-loud `CompileError`); shares nothing with it at
runtime except `wypoc.ast_nodes`.

| File | Owns |
|---|---|
| `errors.py` | `CompileError`, `err()` |
| `opcodes.py` | **single source of truth**: the §3 table as data (name, value, form, operand kinds); encode/decode helpers; the compact/wide fallback rule; disassembly for listings; and `c_header()`, which generates `include/wyrm/opcode.h` (see `tools/generate_opcode_header.py`) |
| `bsonlite.py` | the §4.1 BSON subset, encode + decode (decode is for tests and the disassembler) |
| `image.py` | `ModuleImage` (section payloads) + the three serializers `to_wya() / to_c() / to_wyc()`, and `assemble_wya()` (§5.1 rules) |
| `handlers.py` | dispatch registries keyed by AST class (statement / expression / toplevel), and the refusal wording — a construct not implemented *yet* reads differently from one the language has removed |
| `analysis.py` | the static questions asked before lowering: what a scope declares, a nested body's free names, what is assigned anywhere below — which is what capture lists and capture cells are built from |
| `context.py` | `ModuleContext` (pools: statics, symbols, relocs, globals, functions, classes; the per-scope referenced-name sets that become the `u` lists) and `FnContext` (slot allocation, temp stack, emit buffer, labels/patching) |
| `expressions.py` | §7.1 |
| `statements.py` | §7.2 |
| `functions.py` | function compilation: params, captures, epilogue, `functions` entry |
| `classes.py` | §7.3 |
| `module.py` | top-level walk, decorator expansion, import hoisting, module init code, image assembly, the `debug` section |
| `verify.py` | the structural check every emitted image passes (§8.4) |
| `include/wyrm/` | `image.h` (the §5.2 descriptors) and the generated `opcode.h`, written to be adopted verbatim by the VM tree |
| `__init__.py` | re-exports |

### 8.2 CLI

```
wyrm --build-bc [-o DIR] [--emit wya,c,wyc] [--strip] <file.wy>
```

A mode flag rather than a subcommand, which is how `cli.py` is built —
alongside `--dump-wys` and `--check`, and reusing their `-o`. Default emits
all three containers next to the source (or under `-o`); `--emit` narrows
that, and `--strip` leaves out the `debug` section.

The module name is the source file's stem, which is what a single-file build
has to assume until `import` lowering is given a real module path to work
from.

### 8.3 Register allocation

Deliberately simple and deterministic:

- Named locals get fixed L slots in declaration order (shadowing gets a
  fresh slot; `for` loop variables get a fresh slot per §"loop variable is
  a declaration" — one slot reused across iterations unless captured).
- **A block is a scope.** A `do:`, a loop body, an `if`/`elif`/`else` arm:
  each declares into its own scope, so a `var` inside one takes a slot of its
  own and the name it repeats keeps the enclosing binding — "declaring a name
  visible from an enclosing scope is permitted and shadows it for the
  duration of the inner scope". The slots are still allotted in the one
  declaration pass, before any instruction; only the *bindings* come and go
  as the emitter enters and leaves each block. A name is unreachable once its
  block ends, so reading it afterwards is a `CompileError`, not a stale slot.
  At the module's top level a block's declaration takes a global slot that is
  not interned by name and not exported: it is storage, not a module member.
  One case cannot be scoped this way — a shadowing declaration that a closure
  also captures, since spec 8.3 gives a name one cell per frame — and that is
  a refusal rather than a shared box.
- Above the named locals, expression temporaries are a **stack**: push to
  evaluate, pop when consumed. Call windows are just contiguous pushes.
  `nlocals` = high-water mark.
- A captured-and-later-assigned variable is hoisted into a one-element cell
  (implementation: `pair`) by the compiler; capture copies the cell, reads
  and writes go through it. The VM has no boxing opcode and needs none.

### 8.4 Testing

- Golden `.wy_a` fixtures under `test/` (they double as format
  documentation — this is the point of the ASCII container).
- Round-trip: `assemble_wya(to_wya(img)) == to_wyc(img)` byte-identical,
  and `.c` payloads byte-identical to `.wyc` sections.
- `bsonlite` decode cross-checked against a known-good BSON implementation
  in the test suite only.
- Generated `.c` compiled under `-Wall -Werror` in CI (same as
  `test_compiler_c.py`).

### 8.5 Staging

- **Stage 0 — prove the format end-to-end:** literals, locals, module
  globals, arithmetic/comparison, `if`/`while`/`for`-over-list,
  functions + calls + multi-value return, builtin calls (`println`),
  all three emitters, assembler, round-trip tests.
- **Stage 1 — the object language:** tuples/lists/dicts/pair lists,
  attr/index, `unpack` assignment, closures/lambda/`do`,
  `try`/`catch`/`?=`/`defer`, classes, slots, messages, multiple dispatch,
  `super`, imports between compiled modules, `rset`/scope access.
- **Stage 2 — the long tail:** coroutines + `yield` + `yield_from`,
  varargs/kwargs (`call_va`/`msg_va`), compile-time decorators, statics,
  `import static` checks, wildcard imports (`import_star`), `return_cps`
  tail calls, `debug` section + DAP hookup.

Each stage keeps the fail-loud rule: anything beyond the current stage is a
named `CompileError`, never wrong output.

---

## 9. Where the POC and this specification differ

`wypoc` is where the language design is validated (see AGENTS.md), so its
grammar and interpreter sometimes trail the language spec — and the bytecode
compiler, which targets the spec, then emits code the POC cannot run. None of
these are compiler bugs, and none are worked around: the lowering follows this
document, and the test that would have been a runnable golden fixture becomes
an assertion on the emitted listing instead.

The order of repair is always the same: grammar and interpreter first, then
this document, then the compiler.

**Loop values and `break v`.** doc/language-spec.md gives a loop a value — the
last statement executed, or the `else` clause's, and `break v` overrides it.
Neither is reachable from the POC today: `wyrm.gram` has no `break`-with-value
form (`ast_nodes.Break` carries no value), and the interpreter evaluates every
loop to `nil`. The compiler therefore lowers a loop's value as `lnil R` and
`break`/`continue` as bare jumps, matching what the POC can actually run. When
the grammar and interpreter gain `break v`, this row and §8.5 stage 0 change
together — PoC first, then the compiler (see AGENTS.md).

**Multi-value return vs. the POC's tuple.** doc/language-spec.md's `fn f() ->
int, str` returns *several values*, and `a, b := f()` binds them. The compiler
lowers both as the spec here describes — `return base, n` and a call with
`nres = n` — but the POC interpreter models `return a, b` as returning one
tuple, and rejects *every* multi-target declaration whose right-hand side is a
single expression: `a, b := f()` and `a, b := point` (the `unpack` case) both
raise "target/value count mismatch". So those two lowerings are exercised by
unit tests on the emitted listing rather than by runnable golden fixtures,
until the interpreter catches up. `a, b = b, a`, whose right-hand side is two
expressions, works in both and is in a golden.

**`super`, `mod::x = e`, and `import static`.** Three more places where the
compiler emits what this document specifies but the POC cannot exercise it:

- `super(...)` parses, and the compiler emits `super`, but the interpreter
  raises "cannot evaluate SuperCall". Proved on the listing, not in a golden.
- `wyrm.gram` has no `mod::x = e` form, so `rset` and `setscope` have no
  source spelling to compile from. Both opcodes stay unemitted until the
  grammar grows one; §7.2's row for them describes the intended lowering.
- `import static` lowers exactly like `import` (§7.2), and the compiler
  records which names it bound. `static` marks a dependency wanted at compile
  time only — a module supplying decorators, static functions or AST models —
  so that it does not become a runtime dependency of the importer. The
  language spec's restrictions on those names — "no closures, class
  construction, or runtime message invocations" — are stated about the
  imported module rather than about any syntax the importer writes, so
  enforcing them without false positives needs the rule pinned down first.
  The compiler does not check them today. It also does not adopt the
  dependency's message table, which the tree walker does here: that is an
  artifact of the walker's per-module message tables, not part of what a
  static import means.

**Virtual slot syntax.** The language spec writes a virtual slot as a block of
`fn getter()` / `fn setter()` definitions under the slot:

```wyrm
slot age:
    fn getter(): now() - this.birth_timestamp
    fn setter(age: int): this.birth_timestamp = now() - age
```

`wyrm.gram` instead parses `slot name: T = default with: getter = <expr>`,
whose accessors are arbitrary expressions rather than definitions. The
compiler follows the grammar, since that is what it is handed: a function
*literal* becomes the `sl[].g`/`sl[].s` function index, `= undefined` leaves
the key absent, and anything else is a `CompileError` — the format records an
index, so a value computed at run time has nowhere to live. When the grammar
adopts the spec's form, §7.3 and this handler change together. Note also that
the POC's form is spelled with `with:`, a keyword the language spec has since
removed.

**`...`** is a `wyrm.gram` addition with no counterpart in the language spec:
decorator template libraries use it to mark where the decorated body is
substituted (see `wypoc/samples/decolib.wy`). It is therefore a compile-time
placeholder, not a value, and one that survives to lowering means a template
was never expanded — which is what the `CompileError` says. No opcode or
static-pool type is reserved for it.

**Function bodies that will not lower.** A module may hold functions that are
never called — DSL and decorator *templates*, whose bodies exist to be spliced
into somewhere else. `wyrm/wy/wyrm/_dsl.wy` is full of them: `fn
$token_kind(kind)` uses `this` in a plain function purely so
`$token_kind::$ast` can hand its tree to a rule builder.

Refusing the whole module over one of those would make it uncompilable for
code that never runs; compiling it as if it were callable would be worse. So
such a function keeps its `functions` entry and its binding — its tree is
still reachable — and its body becomes a single `trap` (code 0, unreachable),
which is exactly what calling it would be. The reason is recorded on the image
and printed by `--build-bc`; nothing is swallowed, and
`compile_module(stub_unlowered=False)` refuses instead, which is what the test
suite uses so a genuine compiler gap still fails loudly.

Anything outside a function body is a refusal either way — there is no frame
to trap in.

**Message promotion.** The interpreter *promotes* a plain `fn name(...)` into
the wildcard overload of the message `name` becomes: after

```wyrm
fn describe():
    return "generic thing"

fn [Circle] describe():
    return "a circle"
```

a `Square` still answers `describe`, through the promoted plain function
(`register_overload` in `wyrm_eval_parse_tree.py`, and
doc/language-spec.md's Messages section). The compiler emits the two
definitions independently — a global closure for the plain `fn`, and one
`reg_msg` for the typed one — so a compiled module's message has only the
typed arm, and a receiver with no applicable overload gets "no overload
matches" where the interpreter answers `"generic thing"`.

Closing it is compiler work, not a format change: promotion is decidable at
compile time (the module knows both definitions), and the lowering is one more
`reg_msg` with an empty type tuple, registering the plain function as the
wildcard arm. Until then `wypoc/samples/eval_messages.wy` is the VM sample
sweep's one named divergence.

**Decorator expansion** runs through the POC's own pass (§7.2), which executes
a module's top-level *imports* before expanding but nothing else. A decorator
that needs a module-level `var` therefore cannot expand — `--dump-wys` has the
same limit, since it is the same pass — and `wypoc/samples/decorators.wy` is
one such module. The bytecode fixture under `test/bytecode/decorators/` is
self-contained for that reason rather than being shared with the sample.

## Appendix A — worked example

```wyrm
# hello.wy
fn greet(name):
    return "Hello " + name

println(greet("World"))
```

Image: 1 global (`g0` = greet), statics `#0` = `"Hello "`, `#1` = `"World"`,
symbol `#0` = `"println"`, relocation `#0` = `{t:3, p:[0]}` (function
`println`), header `u:[0]` (init references `println`), function `#0` =
greet (1 param, `l:2`, code word offset 12, no `u` — its body references
nothing external).

The full `statics` and `code` sections as `.wy_a` (header, symbols,
functions, relocations elided with `…` here; a real emitter always writes
them out):

```
WYA 1 hello

SECTION header
; …

SECTION statics
0000: 20 00 00 00 02 30 00 07 00 00 00 48 65 6C 6C 6F ; [0] str "Hello "
0010: 20 00 02 31 00 06 00 00 00 57 6F 72 6C 64 00 00 ; [1] str "World"

SECTION symbols
0000: 14 00 00 00 02 30 00 08 00 00 00 70 72 69 6E 74 ; [0] "println"
0010: 6C 6E 00 00

SECTION functions
; …  [0] greet  p:[name]  l:2  c:12  f:0

SECTION relocations
; …  [0] function println

SECTION code
; module init (no imports to hoist; resolve binds header u:[0])
0000: 07 00 00 00                                     ; resolve
0004: A8 00 00 00 00 00 00 00                         ; closure L0 <- fn#0, 0 caps
000C: 43 00 00 00                                     ; gset g0 <- L0        (greet)
0010: 44 00 00 00                                     ; rget L0 <- reloc#0   (println)
0014: 42 01 00 00                                     ; gget L1 <- g0        (greet)
0018: 46 02 01 00                                     ; lconst L2 <- static#1 ("World")
001C: A0 01 01 00 00 00 01 00                         ; call base=L1 argc=1 nres=1
0024: A0 01 00 00 00 00 01 00                         ; call base=L0 argc=1 nres=1
002C: 02 00 00 00                                     ; return count=0
; fn greet (word offset 12)
0030: 46 00 00 00                                     ; lconst L0 <- static#0 ("Hello ")
0034: 85 00 01 00 00 80 00 00                         ; add L1 <- L0 + P0    (name)
003C: 02 01 01 00                                     ; return L1 count=1
```

Reading one encoding out loud: `add L1 <- L0 + P0` is opcode `0x85`, word 0
= `85 00 01 00` (op, f unused, a0 = 0x0001 = L1), word 1 = `00 80 00 00`
(a2 = 0x8000 = P0 in the low half, a1 = 0x0000 = L0 in the high half) —
little-endian bytes of `a1<<16 | a2`.

## Appendix B — deviations from bytecode.md

- **D1 — `f` field moved to bits 8–15.** The reference `opcode.h` packs
  `flag` at bits 24–31, which collide with `a0` at 16–31 (and left 8–15
  unused). Fixed layout: `[a0:16][f:8][op:8]`. `opcode.h`'s packers/getters
  need the corresponding one-line changes when the VM work starts.
- **D2 — `WYRM_OP_LONG_START = 128` exactly**, high bit of the opcode = two
  words (the sketch had `>= 127`, an off-by-one against its own "high bit"
  rule). Pairing is formalized as `wide = compact | 0x80`.
- **D3 — locals per function capped at 32767**, not 65535: bit 15 of a u16
  register ref selects the P stack, which is what lets every three-address
  op read parameters and captures directly.
- **D4 — Lua-style call windows.** `call`/`msg`/`yield` results land at the
  window base with declared-count backfill, instead of separate
  result-target operands. This is what keeps `call` inside two words with
  room for `nres`, and makes multi-value returns free.
- **D5 — the C container emits `code` as `uint32_t[]`**, not `uint8_t[]`:
  a byte array has alignment 1, and casting it to `u32*` is undefined (and
  genuinely faults on Cortex-M0). All other sections stay byte arrays as
  brainstormed.
- **D6 — `pass` compiles to nothing**; `noop` (0x00) is kept for patching.
  A dedicated `PASS` opcode bought nothing.
- **D7 — no `-1` sentinels in BSON schemas** — optional keys are absent.
  Slightly smaller images, simpler C reader (missing key = default).
- **D8 — `return_cps` is specified but reserved** (Appendix C) rather than
  half-specified.

## Appendix C — open questions (decide before the VM, none block the compiler)

- **CPS / tail calls.** `return_cps`'s continuation model (who creates the
  continuation closure, interaction with defers) needs its own design pass;
  v1 compiles tail calls as ordinary `call` + `return`.
- **Failed stores.** `setattr`/`setidx`/`setslot`/`rset`/`setscope` have no
  result register, so where a failing store's error surfaces (the
  statement's value? a fiber-level fault the next `jerr` can see?) needs
  the VM's error-channel design. The compiler currently assumes stores
  fault into the runtime's error channel, never into a register.
- **Defer granularity.** v1 arms defers per frame; the spec's per-block
  dynamic scope would need either compiler-inserted disarm points or a
  block-depth field on `defer_reg`.
- **Wildcard shadowing.** §6.2 picks first-registered-wins across multiple
  wildcard namespaces and caches on first bind; if the language wants
  ambiguity to be an error instead, the bind-time search reports it —
  format unaffected either way.
- **Message dispatch caching.** The 16-entry message map invites per-site
  inline caches keyed on receiver class; needs the VM's class-identity
  story first. Format already leaves room (`msg` has no spare operand, but
  caches can be side-tables keyed by code offset).
- **Non-constant slot and parameter defaults.** Both are v1
  `CompileError`s; the natural home is generated init/prologue code and
  needs no format change — just compiler work.
- **Interning symbols beyond 31 significant codepoints** — where truncation
  happens (compiler emits pre-truncated? loader truncates?). Proposal:
  compiler emits full text, loader interns on the significant prefix.
- **Endianness of `.wyc` on big-endian targets** — currently "swap at
  load"; revisit only if a real BE target appears.
