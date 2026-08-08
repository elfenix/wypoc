"""`class` compilation: one wyrm class to a C builder function that
constructs the interpreter's class object for it.

    bool w_{module}_{class}(wyrm_lang_vm* vm, wyrm_patch_class** out)

Following the interpreter's own class construction: allocate the class, name
it from an interned symbol, give it a method table, then add one slot per
`slot` declaration. A slot carries its default *value*, which is what lets a
declared default (`slot v: int = 3`) compile - the previous backend's target
had no place to put one.

Not supported (raises `CompileError`): base classes, slot options
(setter/getter), and methods declared in the class body - a method is a
message, and message definitions are a separate gap (see functions.py).
"""
from wypoc import ast_nodes as ast

from .errors import err
from .expressions import compile_expr
from .wtypes import wtype


def compile_class(classdef: ast.ClassDef, module_ident: str) -> str:
    if classdef.bases:
        err(f"class '{classdef.name}': inheritance not supported by --compile", classdef)

    slots = []  # [(name, WType, boxed C default)]
    seen = set()
    for member in classdef.body:
        if not isinstance(member, ast.SlotDef):
            err(
                f"class '{classdef.name}': methods declared inside the class body are "
                f"not supported by --compile", member,
            )
        if member.name in seen:
            err(f"class '{classdef.name}': duplicate slot '{member.name}'", member)
        seen.add(member.name)
        if member.options:
            err(
                f"class '{classdef.name}' slot '{member.name}': slot options not "
                f"supported by --compile", member,
            )
        wt = wtype(member.type, f"class '{classdef.name}' slot '{member.name}'")
        slots.append((member.name, wt, _slot_default(classdef, member, wt)))

    entry_name = f"w_{module_ident}_{classdef.name}"
    lines = [
        f"bool {entry_name}(wyrm_lang_vm* vm, wyrm_patch_class** out)",
        "{",
        "    wyrm_patch_class* cls = WYRM_NULL;",
        "    if (wyrm_patch_class_new(vm->state->context, &cls) != WYRM_ERR_NONE) "
        "{ return false; }",
        "    if (wyrm_patch_dict_new(vm->state->context, &cls->methods) != WYRM_ERR_NONE) "
        "{ return false; }",
        "",
        "    wyrm_symtab_entry sym = 0;",
        "    wyrm_error err = wyrm_patch_symtab_intern(&vm->symtab, "
        f'"{classdef.name}", &sym);',
        "    if (err != WYRM_ERR_NONE && err != WYRM_ERR_EXISTS) { return false; }",
        "    cls->base.sym_name.symtab_entry = sym + 1;",
    ]
    for slot_name, _wt, default in slots:
        lines += [
            "",
            f'    err = wyrm_patch_symtab_intern(&vm->symtab, "{slot_name}", &sym);',
            "    if (err != WYRM_ERR_NONE && err != WYRM_ERR_EXISTS) { return false; }",
            f"    if (wyrm_patch_class_add_slot(vm->state->context, cls, sym + 1, "
            f"{default}) != WYRM_ERR_NONE) {{ return false; }}",
        ]
    lines += [
        "",
        "    *out = cls;",
        "    return true;",
        "}\n",
    ]
    return "\n".join(lines)


def _slot_default(classdef: ast.ClassDef, member: ast.SlotDef, wt) -> str:
    """A slot's default, boxed. With no default declared it is the type's
    zero value, matching the interpreter's own `_zero_value`.

    The default must be a constant: it is evaluated once when the class is
    built, with no function body around it, so there is nowhere for a call or
    a name to resolve. Compiling it in a scratch context that emitted any
    statement is the general form of that check - a hoisted call is exactly
    what a non-constant looks like - but a call is caught by name first, so
    the error says which rule was broken rather than "unknown function"."""
    if member.default is None:
        return wt.boxed(wt.zero)
    from .context import FnContext

    if any(isinstance(node, (ast.Call, ast.Name)) for node in member.default.walk()):
        err(
            f"class '{classdef.name}' slot '{member.name}': a slot default must be a "
            f"constant expression (no calls or names)", member,
        )
    scratch = FnContext(fndef=None, functions={}, module_ident="")
    value = compile_expr(scratch, member.default)
    if scratch.lines:
        err(
            f"class '{classdef.name}' slot '{member.name}': a slot default must be a "
            f"constant expression", member,
        )
    if value.type is not wt and wt.name != "float":
        err(
            f"class '{classdef.name}' slot '{member.name}': default is a "
            f"'{value.type.name}', not a '{wt.name}'", member,
        )
    return wt.boxed(value.expr)
