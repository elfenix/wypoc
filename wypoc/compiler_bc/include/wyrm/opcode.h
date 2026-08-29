/* wyrm bytecode opcodes - GENERATED from wypoc/compiler_bc/opcodes.py.
 *
 * Do not hand-edit: run tools/generate_opcode_header.py after changing the
 * opcode table. doc/llm-bytecode.md section 3 is the prose alongside it.
 *
 * Instruction encoding (section 2), little-endian:
 *
 *   word 0:  [ a0 : u16 ][ f : u8 ][ op : u8 ]
 *   word 1:  [ a1 : u16 ][ a2 : u16 ]
 *
 * The accessors below place `f` at bits 8-15, which is the fix Appendix B
 * D1 records: the reference opcode.h packed it at 24-31, colliding with a0. */
#ifndef WYRM_OPCODE_H
#define WYRM_OPCODE_H

#include <stdint.h>

/* Bit 7 of the opcode selects the instruction length. */
#define WYRM_OP_LONG_START 0x80
#define WYRM_OP_WORDS(op) (((op) & WYRM_OP_LONG_START) ? 2u : 1u)

#define WYRM_OP(code)  ((uint8_t)((code)[0] & 0xff))
#define WYRM_F(code)   ((uint8_t)(((code)[0] >> 8) & 0xff))
#define WYRM_A0(code)  ((uint16_t)(((code)[0] >> 16) & 0xffff))
#define WYRM_A1(code)  ((uint16_t)(((code)[1] >> 16) & 0xffff))
#define WYRM_A2(code)  ((uint16_t)((code)[1] & 0xffff))

/* Register references (section 2.1): bit 15 of a u16 ref, or bit 7
 * of the 8-bit `f` form, selects the P stack. */
#define WYRM_REG_P_BIT   0x8000u
#define WYRM_REG_IS_P(r) (((r) & WYRM_REG_P_BIT) != 0)
#define WYRM_REG_INDEX(r) ((r) & 0x7fffu)
#define WYRM_REG8_IS_P(r) (((r) & 0x80u) != 0)
#define WYRM_REG8_INDEX(r) ((r) & 0x7fu)

typedef enum wy_opcode {
    WY_OP_NOOP          = 0x00,  /* core */
    WY_OP_TRAP          = 0x01,  /* core */
    WY_OP_RETURN        = 0x02,  /* core */
    WY_OP_LNIL          = 0x03,  /* core */
    WY_OP_LBOOL         = 0x04,  /* core */
    WY_OP_LUNSET        = 0x05,  /* core */
    WY_OP_I8            = 0x40,  /* pairable */
    WY_OP_I32_WIDE      = 0xC0,  /* wide */
    WY_OP_MOVE          = 0x41,  /* pairable */
    WY_OP_MOVE_WIDE     = 0xC1,  /* wide */
    WY_OP_GGET          = 0x42,  /* pairable */
    WY_OP_GGET_WIDE     = 0xC2,  /* wide */
    WY_OP_GSET          = 0x43,  /* pairable */
    WY_OP_GSET_WIDE     = 0xC3,  /* wide */
    WY_OP_LSYM          = 0x45,  /* pairable */
    WY_OP_LSYM_WIDE     = 0xC5,  /* wide */
    WY_OP_LCONST        = 0x46,  /* pairable */
    WY_OP_LCONST_WIDE   = 0xC6,  /* wide */
    WY_OP_IMPORT        = 0x47,  /* pairable */
    WY_OP_IMPORT_WIDE   = 0xC7,  /* wide */
    WY_OP_NEG           = 0x48,  /* pairable */
    WY_OP_NEG_WIDE      = 0xC8,  /* wide */
    WY_OP_INV           = 0x49,  /* pairable */
    WY_OP_INV_WIDE      = 0xC9,  /* wide */
    WY_OP_NOT           = 0x4A,  /* pairable */
    WY_OP_NOT_WIDE      = 0xCA,  /* wide */
    WY_OP_JF            = 0x4B,  /* pairable */
    WY_OP_JF_WIDE       = 0xCB,  /* wide */
    WY_OP_JT            = 0x4C,  /* pairable */
    WY_OP_JT_WIDE       = 0xCC,  /* wide */
    WY_OP_JERR          = 0x4D,  /* pairable */
    WY_OP_JERR_WIDE     = 0xCD,  /* wide */
    WY_OP_JNERR         = 0x4E,  /* pairable */
    WY_OP_JNERR_WIDE    = 0xCE,  /* wide */
    WY_OP_JMP           = 0x4F,  /* pairable */
    WY_OP_JMP_WIDE      = 0xCF,  /* wide */
    WY_OP_F32           = 0x80,  /* long */
    WY_OP_TUPLE         = 0x81,  /* long */
    WY_OP_LIST          = 0x82,  /* long */
    WY_OP_DICT          = 0x83,  /* long */
    WY_OP_PLIST         = 0x84,  /* long */
    WY_OP_ADD           = 0x85,  /* long */
    WY_OP_SUB           = 0x86,  /* long */
    WY_OP_MUL           = 0x87,  /* long */
    WY_OP_DIV           = 0x88,  /* long */
    WY_OP_MOD           = 0x89,  /* long */
    WY_OP_POW           = 0x8A,  /* long */
    WY_OP_BAND          = 0x8B,  /* long */
    WY_OP_BOR           = 0x8C,  /* long */
    WY_OP_SHL           = 0x8D,  /* long */
    WY_OP_SHR           = 0x8E,  /* long */
    WY_OP_EQ            = 0x8F,  /* long */
    WY_OP_NE            = 0x90,  /* long */
    WY_OP_LT            = 0x91,  /* long */
    WY_OP_LE            = 0x92,  /* long */
    WY_OP_GT            = 0x93,  /* long */
    WY_OP_GE            = 0x94,  /* long */
    WY_OP_BXOR          = 0x95,  /* long */
    WY_OP_IN            = 0x96,  /* long */
    WY_OP_IS            = 0x97,  /* long */
    WY_OP_GETIDX        = 0x98,  /* long */
    WY_OP_SETIDX        = 0x99,  /* long */
    WY_OP_GETATTR       = 0x9A,  /* long */
    WY_OP_SETATTR       = 0x9B,  /* long */
    WY_OP_GETSLOT       = 0x9C,  /* long */
    WY_OP_SETSLOT       = 0x9D,  /* long */
    WY_OP_CALL          = 0xA0,  /* long */
    WY_OP_CALL_VA       = 0xA1,  /* long */
    WY_OP_MSG           = 0xA2,  /* long */
    WY_OP_MSG_VA        = 0xA3,  /* long */
    WY_OP_GETMSG        = 0xA4,  /* long */
    WY_OP_SUPER         = 0xA5,  /* long */
    WY_OP_RETURN_CPS    = 0xA6,  /* long */
    WY_OP_YIELD         = 0xA7,  /* long */
    WY_OP_CLOSURE       = 0xA8,  /* long */
    WY_OP_CLASS         = 0xA9,  /* long */
    WY_OP_NEW_INSTANCE  = 0xAA,  /* long */
    WY_OP_NEW_PRIMITIVE = 0xAB,  /* long */
    WY_OP_REG_MSG       = 0xAC,  /* long */
    WY_OP_DEFER_REG     = 0xAD,  /* long */
    WY_OP_ITER          = 0xAE,  /* long */
    WY_OP_ITNEXT        = 0xAF,  /* long */
    WY_OP_YIELD_FROM    = 0xB0,  /* long */
    WY_OP_UNPACK        = 0xB1,  /* long */
    WY_OP_CMP3          = 0xB2,  /* long */
    WY_OP_GETSCOPE      = 0xB3,  /* long */
    WY_OP_SETSCOPE      = 0xB4,  /* long */
    WY_OP_IMPORT_STAR   = 0xB5,  /* long */
} wy_opcode;

#define WYRM_OP_COUNT 88

#endif /* WYRM_OPCODE_H */
