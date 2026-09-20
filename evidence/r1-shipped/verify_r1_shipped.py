#!/usr/bin/env python3
"""Is the R1 fix (app.py: an explicit HTTP 503 on saturation) actually IN the shipped preview.83 bytes?

Technique reused from runs/wp8-02-g1/tmp/verify_pump_shipped_v2.py:
  locate the PYZ by its magic, marshal.loads the table of contents, zlib-decompress each entry,
  then BYTE-DIFFERENTIAL each module's DECOMPRESSED code object between two previews.

WHY A DIFFERENTIAL AND NOT A STRING NEEDLE (ERRATA-6 / v1's failure):
  A raw search of the executable cannot see inside compressed PYZ entries at all.  And R1's hunk
  adds a method, module-level BYTES literals and a call site -- measured: 0 new *str* constants
  (probe_ledger_markers.py) -- so a needle absent from BOTH builds is not a failure, it simply
  does not discriminate.  Only a per-module byte differential with IDENTICAL controls is evidence.

THE PAIR IS IDEALLY CLEAN, verified from each bundle's own
third_party/compiler-source-identity.json and git:
  .83 built from 9d68291;  .82 built from 1330ae9.
  `git merge-base --is-ancestor c43a2ab 1330ae9` is FALSE -> R1 is NOT in .82's lineage.
  Between those two heads the ONLY changed application-package .py is mycelium_a13_macos/app.py
  (3 hunks, 24 insertions / 1 deletion = exactly R1); the only other changed file is
  docs/handover/mycelium-completion-checklist.v1.json, which is not a module.
  Prediction: app DIFFERS; every other module IDENTICAL.  A control that is not identical would
  falsify a positive result -- that is what keeps the positive result honest.

TWO EXE VARIANTS, BECAUSE THEY ARE NOT THE SAME FILE:
  the bare dist exe and the .app bundle's Contents/MacOS exe are equal in SIZE but differ in
  sha256.  The chain's own ledger read the .app one; this task's NEW/OLD are the bare one.
  Both are analysed, so the verdict covers the file the chain read too.

Exit 0 only if every expectation holds in BOTH variants.
"""
from __future__ import annotations

import hashlib
import marshal
import pathlib
import struct
import sys
import types
import zlib

DIST = pathlib.Path("/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-g1/dist")
NEW_V, OLD_V = "0.1.0-preview.83", "0.1.0-preview.82"
VARIANTS = [
    ("bare (this task's NEW/OLD)", "Mycelium A13 Demo/Mycelium A13 Demo"),
    ("app bundle (.app) -- the file the chain's ledger read",
     "Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo"),
]

TOUCHED = ["mycelium_a13_macos.app"]  # confirmed against the TOC, not assumed
CONTROLS = [
    "mycelium_a13_macos.serving",
    "mycelium_a13_macos.workflow",
    "mycelium_a13_macos.control_session",
    "mycelium_a13_macos.managed_observations",
    "mycelium_live.route",
    "mycelium_seed.http",
]
R1_NEW_NAMES = ["answer_busy", "BUSY_BODY", "BUSY_RESPONSE", "MSG_DONTWAIT", "sendall"]
R1_CONTROL_NAMES = ["process_request", "shutdown_request", "BoundedSemaphore", "MAX_BODY"]
# Substrings of the fixed BUSY_RESPONSE literal.  Tested as SUBSTRINGS over the joined blob of
# bytes constants, because the parser folds adjacent byte literals and the const boundaries are
# an artefact of folding, not part of the claim.
R1_NEW_BYTES = [b"HTTP/1.1 503 Service Unavailable", b"Content-Type: application/json",
                b"Cache-Control: no-store", b"Retry-After: 1", b'{"error":"server_busy"}']
# Byte constants measured present in BOTH builds (exact membership) -- the byte-level control.
R1_CONTROL_BYTES = [b"{}"]


def load(path: pathlib.Path):
    data = path.read_bytes()
    base = data.find(b"PYZ\0")
    if base < 0:
        raise SystemExit(f"no PYZ magic in {path}")
    raw = data[base:]
    entries = marshal.loads(raw[struct.unpack("!I", raw[8:12])[0]:])
    return raw, base, {n: (pos, ln) for n, (_t, pos, ln) in entries}


def code_bytes(raw: bytes, entry) -> bytes:
    pos, length = entry
    chunk = raw[pos : pos + length]
    try:
        return zlib.decompress(chunk)
    except Exception:
        return chunk  # uncompressed entry


def collect(code, names: set, strs: set, blobs: list, depth=0):
    if depth > 12:
        return
    names.update(code.co_names)
    names.update(code.co_varnames)
    for c in code.co_consts:
        if isinstance(c, str):
            strs.add(c)
        elif isinstance(c, bytes):
            blobs.append(c)
        elif isinstance(c, types.CodeType):
            collect(c, names, strs, blobs, depth + 1)


def flat_codes(code, prefix="", out=None, depth=0):
    if out is None:
        out = {}
    if depth > 12:
        return out
    name = prefix or "<module>"
    out[name] = code
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            flat_codes(c, f"{name}.{c.co_name}", out, depth + 1)
    return out


def analyse(label, new_path, old_path):
    print("=" * 100)
    print(f"VARIANT: {label}")
    print(f"  NEW: {new_path}")
    print(f"  OLD: {old_path}")
    for tag, p in (("NEW", new_path), ("OLD", old_path)):
        print(f"  sha256 {tag}: {hashlib.sha256(p.read_bytes()).hexdigest()}")

    raw_new, base_new, toc_new = load(new_path)
    raw_old, base_old, toc_old = load(old_path)
    print(f"  PYZ magic at offset {base_new} (new) / {base_old} (old); "
          f"modules in TOC: .83={len(toc_new)} .82={len(toc_old)}")

    ok = True
    print(f"\n  {'module':42s} {'role':10s} {'identical?':11s} verdict   (decompressed bytes)")
    print("  " + "-" * 94)
    for role, mods, expect_differ in (("TOUCHED", TOUCHED, True), ("control", CONTROLS, False)):
        for mod in mods:
            if mod not in toc_new or mod not in toc_old:
                print(f"  {mod:42s} {role:10s} {'-':11s} NOT IN PYZ TOC")
                ok = False
                continue
            bn, bo = code_bytes(raw_new, toc_new[mod]), code_bytes(raw_old, toc_old[mod])
            same = bn == bo
            good = (not same) == expect_differ
            what = "DIFFERS as expected" if expect_differ else "identical as expected"
            print(f"  {mod:42s} {role:10s} {str(same):11s} "
                  f"{what if good else '*** UNEXPECTED ***'}   ({len(bo)} -> {len(bn)})")
            ok &= good

    common = sorted(set(toc_new) & set(toc_old))
    only_new = sorted(set(toc_new) - set(toc_old))
    only_old = sorted(set(toc_old) - set(toc_new))
    differing, unreadable = [], []
    for mod in common:
        try:
            if code_bytes(raw_new, toc_new[mod]) != code_bytes(raw_old, toc_old[mod]):
                differing.append(mod)
        except Exception as exc:
            unreadable.append((mod, type(exc).__name__))
    sweep_ok = differing == TOUCHED and not only_new and not only_old and not unreadable
    print(f"\n  FULL SWEEP over every TOC module: compared={len(common)} differing={len(differing)} "
          f"unreadable={len(unreadable)} only_in_.83={only_new or '(none)'} only_in_.82={only_old or '(none)'}")
    for mod in differing:
        print(f"    DIFFERS: {mod}{'   <-- R1 (touched)' if mod in TOUCHED else '   <-- UNEXPECTED'}")
    for mod, e in unreadable:
        print(f"    UNREADABLE: {mod} ({e})")
    print("    -> " + ("ONLY the touched app module differs, across all "
                       + str(len(common)) + " modules" if sweep_ok
                       else "MORE THAN THE TOUCHED MODULE CHANGED"))
    ok &= sweep_ok

    # ---- attribution: is the difference R1 itself? --------------------------------
    mod = TOUCHED[0]
    new_names, new_strs, new_blobs = set(), set(), []
    old_names, old_strs, old_blobs = set(), set(), []
    cn = marshal.loads(code_bytes(raw_new, toc_new[mod]))
    co = marshal.loads(code_bytes(raw_old, toc_old[mod]))
    collect(cn, new_names, new_strs, new_blobs)
    collect(co, old_names, old_strs, old_blobs)
    blob_new, blob_old = b"\x00".join(new_blobs), b"\x00".join(old_blobs)

    print(f"\n  ATTRIBUTION on {mod}: are the changes R1's own?")
    for name in R1_NEW_NAMES:
        a, b = name in new_names, name in old_names
        good = a and not b
        ok &= good
        print(f"    name    {name:16s} .83={str(a):5s} .82={str(b):5s} "
              + ("OK (new in .83, absent in .82)" if good else "*** NOT DISCRIMINATING ***"))
    for lit in R1_NEW_BYTES:
        a, b = lit in blob_new, lit in blob_old
        good = a and not b
        ok &= good
        print(f"    bytes   {lit!r:36s} .83={str(a):5s} .82={str(b):5s} "
              + ("OK" if good else "*** NOT DISCRIMINATING ***"))
    for name in R1_CONTROL_NAMES:
        a, b = name in new_names, name in old_names
        good = a and b
        ok &= good
        print(f"    ctrl-nm {name:16s} .83={str(a):5s} .82={str(b):5s} "
              + ("OK (present in both)" if good else "*** MISSING ***"))
    for lit in R1_CONTROL_BYTES:
        a, b = lit in new_blobs, lit in old_blobs
        good = a and b
        ok &= good
        print(f"    ctrl-by {lit!r:36s} .83={str(a):5s} .82={str(b):5s} "
              + ("OK (present in both)" if good else "*** MISSING ***"))

    # Classify every changed code object: SEMANTIC (a name changed, or a const changed for a
    # reason other than line renumbering) vs bookkeeping (line numbers only, or the codegen side
    # effect of R1's new import).
    fn, fo = flat_codes(cn), flat_codes(co)
    semantic, bookkeeping = [], []
    for name in sorted(fn):
        if name not in fo:
            semantic.append(name + "  [ADDED]")
            continue
        a, b = fn[name], fo[name]
        ca = tuple(x for x in a.co_consts if not isinstance(x, types.CodeType))
        cb = tuple(x for x in b.co_consts if not isinstance(x, types.CodeType))
        if a.co_names != b.co_names:
            added_n = sorted(set(a.co_names) - set(b.co_names))
            semantic.append(f"{name}  [+names {added_n}]")
            continue
        added_c = sorted(set(ca) - set(cb), key=str)
        removed_c = sorted(set(cb) - set(ca), key=str)
        shift = a.co_firstlineno - b.co_firstlineno
        # A class body embeds its own first line number as an int const (3.13+ __firstlineno__),
        # so a pure line shift shows up here too.  Treat ints differing by exactly the line shift
        # as renumbering, not semantics.
        line_only = (len(added_c) == len(removed_c)
                     and all(isinstance(x, int) and isinstance(y, int) and y + shift == x
                             for x, y in zip(added_c, removed_c)))
        if added_c or removed_c:
            if line_only:
                bookkeeping.append(f"{name}  [line-number const {removed_c} -> {added_c}]")
            else:
                semantic.append(f"{name}  [+consts {added_c} / -consts {removed_c}]")
        elif a.co_code != b.co_code:
            bookkeeping.append(name)
    print(f"\n  SEMANTIC changes (name/const level; these ARE the diff's real content) -- {len(semantic)}:")
    for s in semantic:
        print(f"    {s}")
    print(f"  code objects differing only below name/const level -- {len(bookkeeping)}:")
    for s in bookkeeping:
        print(f"    {s}")
    print("    (co_firstlineno shifts from R1 inserting lines above them, and -- verified in")
    print("     probe_import_repro.py -- CPython 3.14 emitting the plain LOAD_ATTR;PUSH_NULL;CALL")
    print("     form instead of the method form for socket.socket() inside DemoApp.action, which")
    print("     the compiler does once a MODULE-LEVEL `import socket` exists.  Same source line,")
    print("     no R1 semantics.)")
    return ok


def main() -> int:
    ok = True
    for label, rel in VARIANTS:
        ok &= analyse(label, DIST / NEW_V / rel, DIST / OLD_V / rel)
        print()
    print("=" * 100)
    print("OVERALL RESULT: " + ("the R1 module differs by exactly R1 and every control is "
          "identical, in BOTH exe variants" if ok else "SEE THE TABLES -- something did not "
          "match the expectation"))
    print()
    print("WHAT THIS DOES NOT PROVE:")
    print("  * NOT that the copy deployed to the laptop carries R1 -- only these two dist trees")
    print("    were read.  (The chain's [6/6] digest compare covers the COPY, not the content.)")
    print("  * NOT that the app EXERCISES the 503 path at runtime, nor that the refusal is")
    print("    correct under a real saturated accept loop; that needs a live round, which was")
    print("    not run.  This is a statement about bytes at rest.")
    print("  * NOT that the R1 hunk is sufficient for the round it was written for.")
    print("  * NOT anything about the previews' retained evidence beyond these two executables.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
