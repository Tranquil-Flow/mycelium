#!/usr/bin/env python3
"""Does a MODULE-LEVEL `import socket` change the bytecode of a function that does its OWN
local `import socket` and then `socket.socket()`?

If yes, that fully explains why DemoApp.action's codegen differs between .82 (no module-level
socket import) and .83 (R1 adds `import socket` at line 17) even though the function's own
source line is unchanged -- i.e. the divergence is a compiler bookkeeping consequence of R1's
import, not an R1 semantic change to that function.
"""
import dis


def show(label, src):
    code = compile(src, "<probe>", "exec")
    fn = next(c for c in code.co_consts if hasattr(c, "co_code") and c.co_name == "f")
    seq = [(i.opname, i.argrepr) for i in dis.get_instructions(fn)]
    print("=== " + label)
    for op, arg in seq:
        print("    " + op.ljust(20) + " " + str(arg)[:40])
    return seq


BODY = """
import socket
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
"""

no_module_import = "def f():\n" + "".join("    " + ln + "\n" for ln in BODY.strip().splitlines())
with_module_import = "import socket\n\ndef f():\n" + "".join("    " + ln + "\n" for ln in BODY.strip().splitlines())

print("--- source WITHOUT module-level import ---")
print(no_module_import)
print("--- source WITH module-level import ---")
print(with_module_import)

a = show("f() -- no module-level `import socket`", no_module_import)
b = show("f() -- WITH module-level `import socket`", with_module_import)
print()
print("bytecode sequences identical? " + str(a == b))
only_a = [x for x in a if x not in b]
only_b = [x for x in b if x not in a]
print("  only in no-module-import version: " + str(only_a))
print("  only in with-module-import version: " + str(only_b))
