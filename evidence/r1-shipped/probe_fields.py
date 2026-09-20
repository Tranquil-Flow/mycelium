#!/usr/bin/env python3
"""Probe: WHICH field of each changed code object differs, and derive real bytes-constant controls."""
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


def bset(c, out=None, d=0):
    if out is None:
        out = set()
    if d > 12:
        return out
    for k in c.co_consts:
        if isinstance(k, bytes):
            out.add(k)
        elif isinstance(k, types.CodeType):
            bset(k, out, d + 1)
    return out


rn, tn = load(DIST / "0.1.0-preview.83" / REL)
ro, to = load(DIST / "0.1.0-preview.82" / REL)
mod = "mycelium_a13_macos.app"
cn = marshal.loads(cb(rn, tn[mod]))
co = marshal.loads(cb(ro, to[mod]))
fn, fo = flat(cn), flat(co)

FIELDS = ["co_code", "co_names", "co_varnames", "co_firstlineno", "co_linetable",
          "co_flags", "co_nlocals", "co_argcount", "co_exceptiontable", "co_qualname", "co_name"]
for name in sorted(fn):
    if name not in fo:
        print(name + ": ADDED in .83 (absent in .82)")
        continue
    a, b = fn[name], fo[name]
    diffs = [f for f in FIELDS if getattr(a, f) != getattr(b, f)]
    cas = tuple(x for x in a.co_consts if not isinstance(x, types.CodeType))
    cbs = tuple(x for x in b.co_consts if not isinstance(x, types.CodeType))
    if cas != cbs:
        diffs.append("co_consts(non-code)")
    if diffs:
        print(name + ": differing fields = " + str(diffs))
        if "co_names" in diffs:
            print("    names added: " + str(sorted(set(a.co_names) - set(b.co_names)))
                  + "  removed: " + str(sorted(set(b.co_names) - set(a.co_names))))
        if "co_code" in diffs:
            print("    co_code len " + str(len(b.co_code)) + " -> " + str(len(a.co_code)))
        if "co_consts(non-code)" in diffs:
            print("    consts added: " + str(sorted(set(cas) - set(cbs), key=str)[:8]))
            print("    consts removed: " + str(sorted(set(cbs) - set(cas), key=str)[:8]))
        if "co_firstlineno" in diffs:
            print("    firstlineno " + str(b.co_firstlineno) + " -> " + str(a.co_firstlineno))

print()
bn, bo = bset(cn), bset(co)
both = sorted(bn.intersection(bo), key=lambda x: (len(x), x))
only_new = sorted(bn.difference(bo), key=len, reverse=True)
only_old = sorted(bo.difference(bn), key=len, reverse=True)
print("bytes consts present in BOTH builds: " + str(len(both)))
print("  samples: " + str([x[:60] for x in both[:6]]))
print("bytes consts ONLY in .83: " + str(len(only_new)))
for x in only_new:
    print("    " + repr(x))
print("bytes consts ONLY in .82: " + str(len(only_old)))
for x in only_old:
    print("    " + repr(x))
