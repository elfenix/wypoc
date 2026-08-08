/*
 * A stub of the interpreter surface `wyrm --compile`'s output depends on.
 *
 * test_compiler_c.py compiles every generated fixture against this header
 * with `gcc -fsyntax-only`, which catches the mistakes pure string assertions
 * miss: a malformed statement, a mismatched brace, a value read through the
 * wrong union field, a call with the wrong arity.
 *
 * It is a stub on purpose. The real header lives in the interpreter's own
 * repository, and a test that reached for it there would pass or skip
 * depending on what happens to be checked out beside this one - so the check
 * would quietly stop running exactly when someone had no sibling repo. This
 * file is instead the *contract*: everything the generated C is allowed to
 * assume, written down in one place. Widening what the compiler emits means
 * widening this header first, which is the point - it makes the dependency
 * explicit and reviewable rather than implicit in a sibling checkout.
 *
 * Nothing here has a body. Syntax and types are all the check needs.
 */
#ifndef WYPOC_TEST_LANG_INTERNAL_H_
#define WYPOC_TEST_LANG_INTERNAL_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define WYRM_NULL NULL

typedef intptr_t wyrm_word;
typedef uintptr_t wyrm_uword;
typedef double wyrm_float;
typedef unsigned wyrm_symtab_entry;

typedef enum wyrm_error {
    WYRM_ERR_NONE = 0,
    WYRM_ERR_EXISTS,
    WYRM_ERR_INVAL
} wyrm_error;

typedef enum wyrm_type_tag {
    WYRM_TYPE_TAG_NIL = 0,
    WYRM_TYPE_TAG_BOOL,
    WYRM_TYPE_TAG_WORD,
    WYRM_TYPE_TAG_FLOAT
} wyrm_type_tag;

/* A value: a type tag plus a register-sized payload. */
typedef struct wyrm_value {
    wyrm_type_tag type;
    union {
        wyrm_word word;
        wyrm_uword uword;
        bool flag;
        wyrm_float fp;
        void* ptr;
        wyrm_symtab_entry symtab_entry;
    } data;
} wyrm_value;

typedef struct wyrm_context wyrm_context;
typedef struct wyrm_patch_dict wyrm_patch_dict;

/* Held by value on the vm, so the stub needs a complete type - the contents
   are the real header's business, not the generated code's. */
typedef struct wyrm_patch_symtab {
    void* opaque;
} wyrm_patch_symtab;

typedef struct wyrm_state {
    wyrm_context* context;
} wyrm_state;

typedef struct wyrm_lang_vm {
    wyrm_state* state;
    wyrm_patch_symtab symtab;
} wyrm_lang_vm;

/* An unwrapped payload: what an object header carries, with no type tag of
   its own. A class's name is one of these, holding an interned symbol. */
typedef union wyrm_primitive {
    wyrm_word word;
    wyrm_uword uword;
    bool flag;
    wyrm_float fp;
    void* ptr;
    wyrm_symtab_entry symtab_entry;
} wyrm_primitive;

/* A class: a named object with a slot table and a method dictionary. */
typedef struct wyrm_patch_class_base {
    wyrm_primitive sym_name;
} wyrm_patch_class_base;

typedef struct wyrm_patch_class {
    wyrm_patch_class_base base;
    wyrm_patch_dict* methods;
} wyrm_patch_class;

/* -- value constructors -------------------------------------------------- */

wyrm_value lang_value_nil(void);
wyrm_value lang_value_bool(bool b);
wyrm_value lang_value_int(wyrm_word w);
wyrm_value lang_value_float(wyrm_float f);

/* -- diagnostics --------------------------------------------------------- */

void lang_vm_runtime_error(wyrm_lang_vm* vm, const char* fmt, ...);

/* -- classes ------------------------------------------------------------- */

wyrm_error wyrm_patch_class_new(wyrm_context* context, wyrm_patch_class** out);
wyrm_error wyrm_patch_dict_new(wyrm_context* context, wyrm_patch_dict** out);
wyrm_error wyrm_patch_symtab_intern(wyrm_patch_symtab* symtab, const char* name,
                                    wyrm_symtab_entry* out);
wyrm_error wyrm_patch_class_add_slot(wyrm_context* context, wyrm_patch_class* cls,
                                     wyrm_symtab_entry sym, wyrm_value default_value);

/* -- the registration table a compiled module ends with ------------------- */

typedef enum lang_builtin_id {
    BI_PLAIN = 0,
    BI_NEXT,
    BI_SEND
} lang_builtin_id;

typedef struct lang_builtin {
    const char* name;
    lang_builtin_id id;
    wyrm_uword min_args;
    wyrm_uword max_args;
    /* Returns false on fatal error, with the error already recorded on the
       vm. The result goes through *out. This is the shape every compiled
       `fn` has. */
    bool (*fn)(wyrm_lang_vm* vm, wyrm_value* args, wyrm_uword argc, wyrm_value* out);
} lang_builtin;

#endif /* WYPOC_TEST_LANG_INTERNAL_H_ */
