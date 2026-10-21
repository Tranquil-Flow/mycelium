#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Direct artifact checks for the two A fixes, plus the C3 ssh-audit instrument's presence."""
import marshal, struct, zlib, types, os

EXE = (os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-g1/dist/"
       "0.1.0-preview.74/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo"))

class Pyz:
    def __init__(self, path):
        data = open(path, 'rb').read()
        base = data.find(b'PYZ\0')
        self.raw = data[base:]
        self.toc = {n: (p, l) for n, (_t, p, l) in marshal.loads(self.raw[struct.unpack('!I', self.raw[8:12])[0]:])}
    def extract(self, name):
        p, l = self.toc[name]
        return marshal.loads(zlib.decompress(self.raw[p:p + l]))

def strings(code, acc):
    for c in code.co_consts:
        if isinstance(c, str):
            acc.add(c)
        elif isinstance(c, types.CodeType):
            strings(c, acc)
    return acc

pyz = Pyz(EXE)

print("=== fix A-2: the closed-schema field must be ABSENT from the shipped bytecode ===")
for module in ('mycelium_seed.plan_binding', 'mycelium_a13_macos.workflow'):
    got = strings(pyz.extract(module), set())
    print("  %-34s boot_id_bindings present: %s   (must be False)" % (module, 'boot_id_bindings' in got))
    print("  %-34s control 'observed_boot_ids' present: %s" % ('', 'observed_boot_ids' in got))

print("=== fix A-1: the guard must be the producer refusal type, not a bare Exception ===")
workflow_src = open(os.path.expanduser('~/Documents/playground/mycelium-core-pilot/'
                    'mycelium_a13_macos/workflow.py')).read()
block = workflow_src[workflow_src.index('observed_boot_ids=None'):workflow_src.index('bound=bind_operator_plan_document(')]
print("  'except ManagedObservationsError as exc:' in the guard block:", 'except ManagedObservationsError as exc:' in block)
print("  bare 'except Exception' in the guard block:", 'except Exception' in block, "(must be False)")
print("  import line above the call site:",
      workflow_src.index('from .managed_observations import') < workflow_src.index('collect_managed_hello_observations(pre_factory'))

print("=== the C3 ssh-audit instrument (PATH shim) must exist or its measurement is silent ===")
shim = '/tmp/asm/ssh-shim/ssh'
print("  %s exists=%s executable=%s" % (shim, os.path.exists(shim), os.access(shim, os.X_OK)))
print("  %s exists=%s" % ('/tmp/asm/ssh-shim', os.path.isdir('/tmp/asm/ssh-shim')))
