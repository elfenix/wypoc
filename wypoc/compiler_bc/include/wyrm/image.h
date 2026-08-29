/* wyrm module image descriptors - the declarations a generated .c container
 * needs (doc/llm-bytecode.md 5.2).
 *
 * This header lives here so the compiler's .c output compiles standalone
 * under -Wall -Werror today; it is written to be adopted verbatim by the VM
 * tree as wyrm/image.h when that work starts. */
#ifndef WYRM_IMAGE_H
#define WYRM_IMAGE_H

#include <stdint.h>

typedef struct wy_section_ref {
    const uint8_t *data;
    uint32_t len;
} wy_section_ref;

enum {
    WY_SEC_HEADER = 1,
    WY_SEC_STATICS,
    WY_SEC_SLOT_DEFAULTS,
    WY_SEC_SYMBOLS,
    WY_SEC_FUNCTIONS,
    WY_SEC_CLASSES,
    WY_SEC_MESSAGES,
    WY_SEC_CODE,
    WY_SEC_DEBUG,
    WY_SEC_EXPORTS,
    WY_SEC_FREE,
    WY_SEC_COUNT
};

typedef struct wy_module_image {
    const char *name;
    wy_section_ref sections[WY_SEC_COUNT];
} wy_module_image;

#endif /* WYRM_IMAGE_H */
