# VM Style

- Register based; registers are STACK ENTRIES reserved during call [called slots]
- Maximum 128 parameters / value returns (see limitations) to a function call

- 2 stacks maintained:
  - locals stack: stores all local variables
  - parameter/cps stack: stores parameters and the closure variables

- class messages are pre-registered in the class definition
- closure bound messages and multi-dispatch messages are registered using opcode (part of run flow)
- bytecode 0 is the start of a routine that runs when module is imported

- import process:

Generally, we want to create runtime versions of all the main things teh module defines. 
These are 'reserved' and then initialized when referenced or populated by an opcode.

  - read symbol table, add to global symbols
  - reserve relocation space; relocations are late binding (happen when observed)
  - reserve variable space; variables initialized to unset, then to defaults is processed
  - start running at bytecode 0


# File Structure

3 variants - 1 ASCII text; 2 C Code; 3 Binary

### ASCII Text

#### Lines:

Forms:
 
 - `SECTION name` -- indicate section
 - `` - spacing line / blank line
 - ` ; comment` - continued comment for second column overflow (likely for byte codes)
 - `address xx xx xx xx xx xx ; comment`

The `SECTION name` indicates start of a data section. The header of the the bytecode
file indicates this. 

Goal: lines encoded to limit of 120 characters. 60 characters reserved for comment.
Lines attempt to encode a full definition / line of code / variable. The comment
is the originating data/source as appropriate.

Example (NOTE: Convert to Hex instead of decimal!):

00000 72 101 108 108 111 32 87 111 114 108 100 ; Hello World 


sections (add as needed):

BSON encoded sections:

  - header: defines module name, total module globals / slots
  - static variables: a list of different objects, str/integers/integer arrays (no symbols)
  - slot defaults: integer:value dict with encoded default values for each slot
  - symbol table: list of symbol strings that need looked up
  - function definitions:
     o function name, parameter names, default parameters, local variable count, and bytecode index
     o messages use '!' in name to indicate they are a message, first n parameters reserved for 'this'
  - class definitions:
     o class name, slot names, constructor function reference
     o message map for class defined messages
  - relocations: mapping of symbols to an import or name
     o type (u8) - enum of: module, class, message, function, variable
     o name (list of int) - using symbol table, dereferenced name (given mod_name::n1::n2::n3 becomes [r_0, r_1, r_2, r_2] - where each r_n is a symbol table index)

BYTE CODE:

  - an array of u32 values encoded as described
  - bytecode offset 0 is start of global run; 


### C File

Each section is a C uint8 array. Each is named '{encoded module}_{section_name}'; contents
are the simple binary contents of the BSON encoded. The purpose is to allow dropping into
a code base with the runtime.


# Initial Proposal of Binary Opcode Structure

 - 32 bit opcode with 64 bit extension
 - 32 bit - allows 1 u16 reg reference, 1 u8 flag/reg reference, and 1 u8 opcode

VM / Language system limitations
 
  - maximum u16 local variables
  - maximum 128 function parameters (0-127)
  - symbols are maximum 255 count u8 (>31 minimum code points); the leading char is u8 count of bytes, after char/uchar
  - tuple size max is 128 entries, after must be list
  - message map max is 16 entries

Core Instructions:

  - i32 is the native integer type for the system, f32 is native floating point, these 
    are supported as immediate opcodes.

Machine Theory:

  - dual stack (not yet implemented in wyrm, see ../wyrm/ )

Scheme thoughts on opcode layout:

  - high bit indicates 64/32 bit reg
  - opcodes 64-127 / 192-255 are generally layed out as 32/64 bit option (f u8 vs a1)
  - generally a0 is the _target_; INVERTED opcodes use f u8 as the target

The u8 flag variable:
    - High bit indicates function parameter, remaining 7 bits indicate parameter offset
    - If low bit, 128 possible local references



```c++

#ifndef WYRM_WOPCODE_H
#define WYRM_WOPCODE_H

#include <wyrm/core.h>

WYRM_BEGIN_DECLS

typedef enum wy_opcode
{                                   // Arguments
    WYRM_OP_NOOP,                   // noop
    WYRM_OP_PASS,                   // pass

    WYRM_OP_RETURN,                 // primitive a0: index of return variables, f: return variable count


    WYRM_OP_NEW_PRIMITIVE,          // primitive a0: target f: data type
    WYRM_OP_NEW_INSTANCE,           // instance a0: target a1: local class instance
    WYRM_OP_REG_MESSAGE,            // register a message

    // 32/64 opt functions
    WYRM_OP_I8,                     // immediate load signed 8 bit integer a0: target, f/imm32[*]: immediate
    WYRM_OP_SET,                    // set a0: target f/a1[*]: source
    WYRM_OP_FETCH,                  // [INVERTED] fetch a0: static variable source f/a1[*]: target
    WYRM_IMPORT,                    // import module, a0: module idx, f/a1[*]: target variable for module
    WYRM_OP_LSYM,                   // lsym a0: target a1: symbol table entry



    WYRM_OP_LONG_START = 127,

    WYRM_OP_RETURN_CPS,             // Tail call - a0: index of tail call parameters, f: count of parameters, a1: variable holding continuation closure
    
    // 32/64 opt funcitons
    WYRM_OP_I32,                    // see WYRM_OP_I8, second opcode u32 is i32
    // see others above [continue pattern]


} wy_opcode;

WYRM_INLINE wy_u32 wy_opcode_p0(wy_opcode op)
{
    return (wy_u32) op;
}

WYRM_INLINE wy_u32 wy_opcode_p1(wy_opcode op, wy_u16 arg)
{
    return (wy_u32) op | ((wy_u32) arg << 16);
}

WYRM_INLINE wy_u32 wy_opcode_p1f(wy_opcode op, wy_u16 arg, wy_u8 flag)
{
    return (wy_u32) op | ((wy_u32) arg << 16) | ((wy_u32) flag << 24);
}

WYRM_INLINE void wy_opcode_pack(wy_u32 dest[2], wy_opcode op, wy_u8 flag, wy_u16 arg0, wy_u16 arg1, wy_u16 arg2)
{
    dest[0] = wy_opcode_p1f(op, arg0, flag);
    dest[1] = (wy_u32) arg1 << 16 | arg2;
}

WYRM_INLINE void wy_opcode_p2(wy_u32 dest[2], wy_opcode op, wy_u16 a0, wy_u16 a1)
{
    wy_opcode_pack(dest, op, 0, a0, a1, 0);
}

WYRM_INLINE void wy_opcode_p3(wy_u32 dest[2], wy_opcode op, wy_u16 a0, wy_u16 a1, wy_u16 a3)
{
    wy_opcode_pack(dest, op, 0, a0, a1, a3);
}

WYRM_INLINE wy_opcode wy_opcode_get(const wy_u32 code[])
{
    return (wy_opcode) (code[0] & 0x000000ffu);
}

WYRM_INLINE wy_u8 wy_opcode_get_flag(const wy_u32 code[])
{
    return (wy_u8) ((code[0] >> 24) & 0x000000ffu);
}

WYRM_INLINE wy_u16 wy_opcode_get_a0(const wy_u32 code[])
{
    return (wy_u16) ((code[0] >> 16) & 0x0000ffffu);
}

WYRM_INLINE wy_u16 wy_opcode_get_a1(const wy_u32 code[])
{
    return (wy_u16) ((code[1] >> 16) & 0x0000ffffu);
}

WYRM_INLINE wy_u16 wy_opcode_get_a2(const wy_u32 code[])
{
    return (wy_u16) (code[1] & 0x0000ffffu);
}

WYRM_INLINE bool wy_opcode_is_long(const wy_u32 code[])
{
    return wy_opcode_get(code) >= WYRM_OP_LONG_START;
}

WYRM_END_DECLS

#endif

```