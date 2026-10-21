#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""One command that answers "which tree is the app's source of truth?" with measurements.

Prints three tables:
  1. the two trees' branches/HEADs and their relationship (is one an ancestor?)
  2. every dirty file in the demo tree classified against pilot blobs
     (== pilot HEAD / == an older pilot commit / unique-to-demo)
  3. each preview bundle's BUILD-TIME compiler-source-identity.json vs both trees

Read-only. Runs under any python3 (stdlib only).
"""
import hashlib, json, os, subprocess

P = os.path.expanduser("~/Documents/playground/mycelium-core-pilot")
D = os.path.expanduser("~/Documents/playground/mycelium-a13-macos-demo")
DIST = (os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-g1/dist"))

def git(*args, cwd=P):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True).stdout.strip()

def blob_sha(cwd, spec):
    r = subprocess.run(["git", "-C", cwd, "cat-file", "blob", spec], capture_output=True)
    return hashlib.sha256(r.stdout).hexdigest() if r.returncode == 0 else None

def file_sha(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None

print("=== 1. the two trees ===")
for label, root in (("pilot", P), ("demo ", D)):
    print("  %s %-58s %s @ %s" % (label, root, git("rev-parse", "--abbrev-ref", "HEAD", cwd=root),
                                  git("rev-parse", "--short", "HEAD", cwd=root)))
demo_head = git("rev-parse", "HEAD", cwd=D)
contained = subprocess.run(["git", "-C", P, "cat-file", "-e", demo_head + "^{commit}"]).returncode == 0
print("  demo's HEAD commit exists in pilot: %s" % contained)

print("\n=== 2. demo tree dirt, classified against pilot blobs ===")
dirty = [l.split(maxsplit=1)[1] for l in git("status", "--porcelain", "--untracked-files=no",
                                             cwd=D).splitlines() if l.strip()]
print("  tracked modified: %d   untracked entries: %d"
      % (len(dirty), len([l for l in git("status", "--porcelain", "--untracked-files=all",
                                         cwd=D).splitlines() if l.startswith("??")])))
tracked_app = git("ls-files", "mycelium_a13_macos", cwd=D)
print("  mycelium_a13_macos/ tracked files in the demo repo: %d"
      % len([l for l in tracked_app.splitlines() if l]))
hist = git("ls-tree", "-r", "--name-only", "HEAD", "mycelium_a13_macos/", cwd=D)
print("  mycelium_a13_macos/ paths in the demo repo's HEAD: %d" % len([l for l in hist.splitlines() if l]))
head_ver = {}
for rel in dirty:
    dig = file_sha(os.path.join(D, rel))
    if blob_sha(P, "HEAD:" + rel) == dig:
        print("  ==PILOT_HEAD      %s" % rel)
        continue
    found = None
    for commit in git("log", "--format=%H", "-n", "60", "--", rel).splitlines():
        if blob_sha(P, "%s:%s" % (commit, rel)) == dig:
            found = git("log", "-1", "--format=%h %ad %s", "--date=short", commit)
            break
    print("  %s   %s" % ("==PILOT_COMMIT " + found[:60] if found else "UNIQUE_TO_DEMO", rel))

print("\n=== 3. build-time compiler-source-identity.json (provenance recorded BY the build) ===")
for entry in sorted(os.listdir(DIST)) if os.path.isdir(DIST) else []:
    path = ("%s/%s/Mycelium A13 Demo.app/Contents/Resources/third_party/"
            "compiler-source-identity.json" % (DIST, entry))
    if not os.path.exists(path):
        continue
    files = json.load(open(path)).get("sha256", {})
    pilot_n = demo_n = other = 0
    for rel, dig in files.items():
        if blob_sha(P, "HEAD:" + rel) == dig:
            pilot_n += 1
        elif file_sha(os.path.join(D, rel)) == dig:
            demo_n += 1
        else:
            other += 1
    covers_live = any(r.startswith("mycelium_live/") for r in files)
    print("  %-20s files=%-4d ==pilot HEAD %-4d ==demo tree %-3d neither %-3d  covers mycelium_live: %s"
          % (entry, len(files), pilot_n, demo_n, other, covers_live))
print("""
  READING THIS
    * '==pilot HEAD' for every file means that bundle was built from the pilot tree.
    * a file that matches the DEMO tree instead usually means pilot's copy of that file changed
      AFTER the bundle was built (so the bundle holds the pre-change content, which the stale
      demo tree still has). Cross-check with the bundle's mtime before calling it a second source.
    * 'covers mycelium_live: False' is a real gap: mycelium_live is NOT shipped as
      a13-runtime-source, so its provenance is invisible here. Use tools/pyz_ledger.py for it.""")
