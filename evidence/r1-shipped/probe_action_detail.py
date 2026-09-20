#!/usr/bin/env python3
"""Ground truth for the DemoApp.action codegen divergence: which SOURCE LINE, what call."""
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
a = flat(marshal.loads(cb(rn, tn[mod])))["<module>.DemoApp.action"]
b = flat(marshal.loads(cb(ro, to[mod])))["<module>.DemoApp.action"]

ia = list(dis.get_instructions(a))
ib = list(dis.get_instructions(b))
print("=== .82 (old) instructions 755..800")
for i in ib[755:800]:
    print("   " + str(i.offset).rjust(6) + " L" + str(i.starts_line if i.starts_line else i.positions.lineno)
          + " " + i.opname.ljust(20) + " " + str(i.argrepr)[:70])
print("=== .83 (new) instructions 755..802")
for i in ia[755:802]:
    print("   " + str(i.offset).rjust(6) + " L" + str(i.starts_line if i.starts_line else i.positions.lineno)
          + " " + i.opname.ljust(20) + " " + str(i.argrepr)[:70])
