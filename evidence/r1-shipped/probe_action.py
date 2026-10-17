#!/usr/bin/env python3
"""Is DemoApp.action's +4 co_code bytes and +2 'instructions' anything but line-number bookkeeping?"""
import dis
import marshal
import pathlib
import struct
import types
import zlib

DIST = pathlib.Path("/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-g1/dist")
REL = "Mycelium A13 Demo/Mycelium A13 Demo"


def load(p):
    d = p.read_bytes()
    b = d.find(b"PYZ\0")
    raw = d[b:]
    tp = struct.unpack("!I", raw[8:12])[0]
    e = marshal.loads(raw[tp:])
    return raw, {n: (pos, ln) for n, (_t, pos, ln) in e}


def cb(raw, e):
    pos, ln = e
    try:
        return zlib.decompress(raw[pos:pos + ln])
    except Exception:
        return raw[pos:pos + ln]


def flat(c, pre="", out=None, d=0):
    if out is None:
        out = {}
    if d > 12:
        return out
    n = pre or "<module>"
    out[n] = c
    for k in c.co_consts:
        if isinstance(k, types.CodeType):
            flat(k, n + "." + k.co_name, out, d + 1)
    return out


rn, tn = load(DIST / "0.1.0-preview.83" / REL)
ro, to = load(DIST / "0.1.0-preview.82" / REL)
mod = "mycelium_a13_macos.app"
fn = flat(marshal.loads(cb(rn, tn[mod])))
fo = flat(marshal.loads(cb(ro, to[mod])))
a, b = fn["<module>.DemoApp.action"], fo["<module>.DemoApp.action"]

# OPNAME-ONLY sequences (the semantic content), caches excluded by dis
oa = [i.opname for i in dis.get_instructions(a)]
ob = [i.opname for i in dis.get_instructions(b)]
print("DemoApp.action opname-only sequences identical? " + str(oa == ob))
print("  instruction counts: .82=" + str(len(ob)) + "  .83=" + str(len(oa)))
if oa != ob:
    diffidx = [i for i in range(min(len(oa), len(ob))) if oa[i] != ob[i]]
    print("  first differing opname indices: " + str(diffidx[:20]))
    for i in diffidx[:10]:
        print("    idx " + str(i) + ": .82=" + ob[i] + " .83=" + oa[i])

# also opcode-number-only (ignoring argrepr and cache-adapted opcode numbers)
oa2 = [i.opcode for i in dis.get_instructions(a)]
ob2 = [i.opcode for i in dis.get_instructions(b)]
print("DemoApp.action opcode-number sequences identical? " + str(oa2 == ob2))

# LOAD_CONST consts, substituting line numbers for the code objects so bookkeeping is removed
def cseq(c):
    out = []
    for i in dis.get_instructions(c):
        if i.opname in ("LOAD_CONST", "RETURN_CONST") and isinstance(i.argval, types.CodeType):
            out.append(("CODE", i.argval.co_name, i.argval.co_firstlineno - c.co_firstlineno))
        elif i.opname == "LOAD_CONST" and isinstance(i.argval, int):
            out.append(("INT", i.argval, None))
        else:
            out.append((i.opname, None, None))
    return out


def rel_lines(c):
    """Code-object consts expressed as offsets from their parent: removes pure line shift."""
    return [(i.argval.co_name, i.argval.co_firstlineno - c.co_firstlineno)
            for i in dis.get_instructions(c)
            if isinstance(getattr(i, "argval", None), types.CodeType)]


print("DemoApp.action relative code-const line offsets identical? " + str(rel_lines(a) == rel_lines(b)))
print("  .82 offsets: " + str(rel_lines(b)[:6]))
print("  .83 offsets: " + str(rel_lines(a)[:6]))
