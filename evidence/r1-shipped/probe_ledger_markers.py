#!/usr/bin/env python3
"""Why the chain's own ledger could not have caught R1 even if it had a row for the app module.

pyz_ledger.py's discriminator is `markers_for()`:
    pilot_only = string constants of the PILOT tree's source  MINUS
                 string constants of the DEMO tree's source
and the only const kind collected is `isinstance(const, str)` -- bytes consts are never seen.

R1's app.py hunk adds: `import socket`, two module-level BYTES literals (BUSY_BODY,
BUSY_RESPONSE) built by concatenating byte literals, a method def, and a call site.
So the question is whether R1 adds ANY new str constant.  Measure it.
"""
import subprocess
import types

PILOT = "/Users/evinova-self/Documents/playground/mycelium-core-pilot"
DEMO = "/Users/evinova-self/Documents/playground/mycelium-a13-macos-demo"
REL = "mycelium_a13_macos/app.py"


def walk(code, strings, names, depth=0):
    if depth > 10:
        return
    for const in code.co_consts:
        if isinstance(const, str):
            strings.add(const)
        elif isinstance(const, types.CodeType):
            walk(const, strings, names, depth + 1)
    names.update(code.co_names)


def strs_of_bytes(data, label):
    s, n = set(), set()
    walk(compile(data, label, "exec"), s, n)
    return s, n


def git_show(rev, rel):
    r = subprocess.run(["git", "-C", PILOT, "show", "%s:%s" % (rev, rel)], capture_output=True)
    if r.returncode != 0:
        raise SystemExit("git show failed for %s:%s" % (rev, rel))
    return r.stdout


old_src = git_show("1330ae9", REL)   # .82's build head
new_src = git_show("9d68291", REL)   # .83's build head
old_s, old_n = strs_of_bytes(old_src, REL)
new_s, new_n = strs_of_bytes(new_src, REL)

print("app.py: str constants  .82(1330ae9)=%d  .83(9d68291)=%d" % (len(old_s), len(new_s)))
added_s = sorted(new_s - old_s)
removed_s = sorted(old_s - new_s)
print("  str constants ADDED by R1: %d %s" % (len(added_s), added_s))
print("  str constants REMOVED by R1: %d %s" % (len(removed_s), removed_s))
print()
print("app.py: co_names  .82=%d  .83=%d" % (len(old_n), len(new_n)))
added_n = sorted(new_n - old_n)
print("  co_names ADDED by R1: %s" % added_n)
print("  (co_names ARE collected by pyz_ledger.walk(), but its strings_of_source() call returns")
print("   only [0] -- the strings set -- so co_names never reaches the marker set.)")
print()

# The exact marker computation the ledger uses: pilot tree vs demo tree, str constants only.
demo_s, _ = strs_of_bytes(open(DEMO + "/" + REL, "rb").read(), REL)
pilot_only = sorted(new_s - demo_s)
print("demo tree readable: %s" % (DEMO + "/" + REL))
print("  pilot_only (pilot str consts absent from the demo tree), the ledger's marker set:")
print("    count=%d  first few=%s" % (len(pilot_only), [m[:40] for m in pilot_only[:5]]))
r1_tokens = ["server_busy", "503", "Retry-After", "Cache-Control", "answer_busy",
             "BUSY_BODY", "BUSY_RESPONSE", "MSG_DONTWAIT"]
hits = [m for m in pilot_only if any(t in m for t in r1_tokens)]
print("  markers among them that name ANY R1 fact: %s" % (hits or "NONE"))
