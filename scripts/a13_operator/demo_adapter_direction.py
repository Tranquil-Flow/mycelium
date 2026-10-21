#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Are the demo adapter's 11 differing modules STALE COPIES of pilot content, or a divergent
fork carrying work pilot lacks? Per module, with controls. Read-only.
"""
import difflib
import hashlib
import os
import subprocess

DEMO = os.path.expanduser("~/Documents/playground/mycelium-a13-macos-demo")
PILOT = os.path.expanduser("~/Documents/playground/mycelium-core-pilot")
ADAPTER = "mycelium_a13_macos"


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def blob(path):
    return git(PILOT, "hash-object", path) if path.startswith(PILOT) else None


def sha_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def demo_blob_sha(path):
    """Blob sha of a demo file, computed the same way git would."""
    r = subprocess.run(["git", "-C", DEMO, "hash-object", path], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def history(blob_sha):
    out = git(PILOT, "log", "--all", "--format=%h %ad %s", "--date=short",
              "--find-object=" + blob_sha, "--max-count=3")
    return out.splitlines() if out else []


def line_delta(rel):
    head = git(PILOT, "show", f"HEAD:{ADAPTER}/{rel}")
    if head is None:
        return None
    with open(os.path.join(DEMO, ADAPTER, rel)) as fh:
        demo_text = fh.read()
    diff = list(difflib.unified_diff(head.splitlines(), demo_text.splitlines(),
                                     lineterm="", n=0))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return removed, added


DIFFER = ["acquisition.py", "app.py", "concurrent_pump.py", "control_pump.py",
          "control_session.py", "endpoint_recovery.py", "managed_control.py",
          "managed_runtime.py", "serving.py", "worker.py", "workflow.py"]

print("=" * 78)
print("THE 11 DIFFERING ADAPTER MODULES  (demo vs pilot HEAD)")
print("=" * 78)
stale, foreign = [], []
for rel in DIFFER:
    dp = os.path.join(DEMO, ADAPTER, rel)
    bs = demo_blob_sha(dp)
    hist = history(bs) if bs else []
    delta = line_delta(rel)
    tag = "STALE(pilot-history)" if hist else "*** EXCLUSIVE-TO-DEMO ***"
    (stale if hist else foreign).append(rel)
    print(f"\n  {rel}   {tag}")
    if delta:
        print(f"     pilot-HEAD -> demo : -{delta[0]} +{delta[1]} lines")
    if hist:
        for h in hist:
            print(f"     last pilot commit holding demo's exact bytes: {h}")

print()
print("=" * 78)
print("VERDICT")
print("=" * 78)
print(f"  stale copies of pilot content : {len(stale)}")
print(f"  exclusive to the demo tree    : {len(foreign)} {foreign if foreign else ''}")

print()
print("=" * 78)
print("THE TWO FILES THIS SESSION FIXED  (do they carry the fix in each tree?)")
print("=" * 78)
checks = [
    (f"{ADAPTER}/workflow.py", "import hoist + narrowed except (c36272f)",
     "collect_managed_hello_observations"),
    ("mycelium_seed/plan_binding.py", "peer-level boot_id, no controller.boot_id_bindings (c36272f)",
     "boot_id_bindings"),
]
for rel, what, needle in checks:
    d = os.path.join(DEMO, rel)
    p = os.path.join(PILOT, rel)
    if not os.path.exists(d):
        print(f"\n  {rel}: ABSENT in demo tree")
        continue
    same = sha_file(d) == sha_file(p)
    print(f"\n  {rel}  [{what}]")
    print(f"     demo == pilot : {same}  ({'fix present/absent together' if same else 'TREES DIFFER'})")
    for label, path in (("demo ", d), ("pilot", p)):
        with open(path) as fh:
            text = fh.read()
        print(f"     {label}: '{needle}' occurrences = {text.count(needle)}")
    # the actual fix markers
    with open(d) as fh:
        dtext = fh.read()
    with open(p) as fh:
        ptext = fh.read()
    for marker in ["except ManagedObservationsError as exc", "controller.boot_id_bindings",
                   "except Exception"]:
        print(f"     marker {marker!r}: demo={dtext.count(marker)} pilot={ptext.count(marker)}")

print()
print("  CONTROL NEEDLE: the comparator must be able to say BOTH")
ctl = os.path.join(DEMO, "mycelium_live/route.py")
print(f"     known-stale file reported as differing : "
      f"{sha_file(ctl) != sha_file(os.path.join(PILOT, 'mycelium_live/route.py'))}  (expect True)")
ctl2 = os.path.join(DEMO, "mycelium_router/transports/iroh.py")
print(f"     known-equal file reported as equal     : "
      f"{sha_file(ctl2) == sha_file(os.path.join(PILOT, 'mycelium_router/transports/iroh.py'))}  (expect True)")
print()
print("READ-ONLY: nothing written to either tree.")
