#!/usr/bin/env python3
"""Regenerate wypoc/compiler_bc/include/wyrm/opcode.h from the opcode table.

`wypoc/compiler_bc/opcodes.py` is the single source of truth for the v1
instruction set (doc/llm-bytecode.md section 3).  The VM decodes what this
compiler encodes, so its enum and accessors are generated from that table
rather than written a second time and kept in step by hand - the header is
meant to be adopted verbatim by the wyrm VM tree when that work starts.

    .venv/bin/python tools/generate_opcode_header.py

test_compiler_bc_format.py asserts the checked-in header still matches, so a
change to the table that skips this script fails the suite rather than
shipping a stale header.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wypoc.compiler_bc import opcodes  # noqa: E402


def header_path() -> str:
    package = os.path.dirname(os.path.abspath(opcodes.__file__))
    return os.path.join(package, *opcodes.C_HEADER_PATH.split("/"))


def main() -> int:
    path = header_path()
    generated = opcodes.c_header()
    existing = None
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read()
    if existing == generated:
        print(f"{path}: already up to date")
        return 0
    with open(path, "w") as f:
        f.write(generated)
    print(f"{path}: regenerated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
