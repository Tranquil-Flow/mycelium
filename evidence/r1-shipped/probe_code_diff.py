#!/usr/bin/env python3
"""Why do DemoApp / DemoError / DemoApp.action differ in co_code with unchanged names?
Disassemble both builds' versions and diff the instruction streams."""
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

for name in ["<module>.DemoApp", "<module>.DemoError", "<module>.DemoApp.action"]:
    a, b = fn[name], fo[name]
    ia = [(i.opname, i.argrepr) for i in dis.get_instructions(a)]
    ib = [(i.opname, i.argrepr) for i in dis.get_instructions(b)]
    print("=== " + name + ": " + str(len(ib)) + " -> " + str(len(ia)) + " instructions")
    only_a = [x for x in ia if x not in ib]
    only_b = [x for x in ib if x not in ia]
    print("    only in .83: " + str(only_a))
    print("    only in .82: " + str(only_b))
    # also compare the raw co_code as hex to show the magnitude
    print("    co_code equal? " + str(a.co_code == b.co_code)
          + "  len " + str(len(b.co_code)) + " -> " + str(len(a.co_code)))
    if len(a.co_code) == len(b.co_code):
        diffpos = [i for i in range(len(a.co_code)) if a.co_code[i] != b.co_code[i]]
        print("    differing byte positions: " + str(diffpos))
        for p in diffpos:
            print("      at " + str(p) + ": .82=" + hex(b.co_code[p]) + " .83=" + hex(a.co_code[p]))
    print()
