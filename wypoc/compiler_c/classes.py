"""`class` compilation - see DESIGN.md's "Classes" section for the supported
subset: bare typed slots only, no bases, defaults, slot options, or methods
(there's no dedicated constructor syntax any more - `init` is just an
ordinary method named "init", so it's rejected by the same "methods aren't
supported by --compile" path as any other class-body `fn`)."""
from wypoc import ast_nodes as ast

from .errors import err
from .wtypes import TYPES, ctype


def compile_class(classdef: ast.ClassDef, module_ident: str) -> str:
    """Compile a single `class` into a plain (non-`wyrm_exec_fn`) C function,
    `w_{module}_{class}`, that builds and returns the `wyrm_class*`
    describing it - one `wyrm_class_new` + a `wyrm_class_add_slot_f` per
    slot, named after the same `w_{module}_{name}` scheme `fn` entries use."""
    if classdef.bases:
        err(f"class '{classdef.name}': inheritance not supported by --compile", classdef)

    slots = []  # [(name, wyrm type name)]
    seen_slots = set()
    for member in classdef.body:
        if isinstance(member, ast.SlotDef):
            if member.name in seen_slots:
                err(f"class '{classdef.name}': duplicate slot '{member.name}'", member)
            seen_slots.add(member.name)
            if member.default is not None:
                err(
                    f"class '{classdef.name}' slot '{member.name}': default values not "
                    f"supported by --compile", member,
                )
            if member.options:
                err(
                    f"class '{classdef.name}' slot '{member.name}': slot options not "
                    f"supported by --compile", member,
                )
            slots.append((member.name, ctype(member.type, f"class '{classdef.name}' slot '{member.name}'")))
        else:
            err(
                f"class '{classdef.name}': methods declared inside the class body are not "
                f"supported by --compile (use 'fn [{classdef.name}] ...')", member,
            )

    entry_name = f"w_{module_ident}_{classdef.name}"
    lines = [
        f"wyrm_error {entry_name}(wyrm_context* context, wyrm_class** out)",
        "{",
        "    wyrm_machine* machine = wyrm_context_get_machine(context);",
        "    wyrm_class* cls = WYRM_NULL;",
        "    wyrm_error err = wyrm_class_new(context, &cls);",
        "    if (err != WYRM_ERR_NONE) { return err; }",
        "",
        "    wyrm_primitive sym_name = {0};",
        f'    err = wyrm_machine_insert_symbol(machine, "{classdef.name}", &sym_name);',
        "    if (err != WYRM_ERR_NONE) { return err; }",
        "    wyrm_class_set_name_f(cls, sym_name);",
    ]
    for slot_name, type_name in slots:
        _ctype, tag, _field = TYPES[type_name]
        sym = f"sym_slot_{slot_name}"
        lines.append("")
        lines.append(f"    wyrm_primitive {sym} = {{0}};")
        lines.append(f'    err = wyrm_machine_insert_symbol(machine, "{slot_name}", &{sym});')
        lines.append("    if (err != WYRM_ERR_NONE) { return err; }")
        lines.append(f"    err = wyrm_class_add_slot_f(cls, {sym}, {tag});")
        lines.append("    if (err != WYRM_ERR_NONE) { return err; }")
    lines.append("")
    lines.append("    *out = cls;")
    lines.append("    return WYRM_ERR_NONE;")
    lines.append("}\n")
    return "\n".join(lines)
