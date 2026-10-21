#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Decompose the demo checkout's 21 tracked-dirty files against the PILOT tree.

For each dirty file answer three questions, read-only:
  1. does its blob exist anywhere in pilot history (any branch/commit)?
  2. if so, WHICH pilot commit(s) does it match -> is demo content older or newer?
  3. if it differs from pilot HEAD, how much (numstat) and in which direction?

No writes to either repo. Uses git plumbing only.
"""
import os
import subprocess
import sys

DEMO = os.path.expanduser("~/Documents/playground/mycelium-a13-macos-demo")
PILOT = os.path.expanduser("~/Documents/playground/mycelium-core-pilot")


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {r.stderr.strip()}")
    return r.stdout


def dirty_tracked():
    out = git(DEMO, "status", "--porcelain", "--untracked-files=no")
    rows = []
    for line in out.splitlines():
        st, path = line[:2], line[3:]
        # renames look like "R  old -> new"
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        rows.append((st.strip(), path))
    return rows


def matches_in_history(blob):
    """Which pilot commits contain this exact blob? Any ref, any history."""
    r = subprocess.run(
        ["git", "-C", PILOT, "log", "--all", "--format=%h %ad %s", "--date=short",
         "--find-object=" + blob, "--max-count=5"],
        capture_output=True, text=True,
    )
    return [l for l in r.stdout.splitlines() if l.strip()]


def main():
    print("pilot HEAD :", git(PILOT, "rev-parse", "--short", "HEAD").strip())
    print("demo  HEAD :", git(DEMO, "rev-parse", "--short", "HEAD").strip())
    print("demo branch:", git(DEMO, "rev-parse", "--abbrev-ref", "HEAD").strip())
    print()

    rows = dirty_tracked()
    print(f"tracked-dirty files: {len(rows)}")
    print()

    same_as_head, elsewhere, foreign = [], [], []
    for st, path in rows:
        blob = git(DEMO, "hash-object", path).strip()
        head_blob = git(PILOT, "rev-parse", f"HEAD:{path}", check=False).strip()
        if head_blob and blob == head_blob:
            same_as_head.append(path)
            continue
        hist = matches_in_history(blob)
        if hist:
            elsewhere.append((path, blob, head_blob, hist))
        else:
            foreign.append((path, blob, head_blob))

    print("=" * 78)
    print(f"A) IDENTICAL TO PILOT HEAD  ({len(same_as_head)})  -- demo dirt is just pilot content")
    for p in same_as_head:
        print("   =", p)
    print()

    print("=" * 78)
    print(f"B) CONTENT EXISTS IN PILOT HISTORY BUT NOT AT HEAD  ({len(elsewhere)})")
    for path, blob, head_blob, hist in elsewhere:
        print(f"   ~ {path}")
        print(f"       demo blob {blob[:12]}  pilot-HEAD blob {(head_blob or '(absent)')[:12]}")
        for h in hist:
            print(f"       last pilot commit holding it: {h}")
    print()

    print("=" * 78)
    print(f"C) CONTENT EXISTS NOWHERE IN PILOT  ({len(foreign)})  -- *** UNSAVED WORK? ***")
    for path, blob, head_blob in foreign:
        print(f"   ?? {path}")
        print(f"       demo blob {blob[:12]}  pilot-HEAD blob {(head_blob or '(absent)')[:12]}")
    print()

    # Direction + size of every difference against pilot HEAD
    print("=" * 78)
    print("D) DIFF AGAINST PILOT HEAD (numstat, +added/-removed lines)")
    targets = [p for p, _, _, _ in elsewhere] + [p for p, _, _ in foreign]
    if not targets:
        print("   none -- every dirty file equals pilot HEAD")
    for path in targets:
        r = subprocess.run(
            ["git", "-C", PILOT, "show", f"HEAD:{path}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"   {path}: absent at pilot HEAD (no diff possible)")
            continue
        with open(f"{DEMO}/{path}") as fh:
            demo_text = fh.read()
        import difflib
        diff = list(difflib.unified_diff(
            r.stdout.splitlines(), demo_text.splitlines(),
            fromfile=f"pilot-HEAD:{path}", tofile=f"demo:{path}", lineterm="", n=0,
        ))
        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        print(f"   {path}: -{removed} +{added}   (pilot-HEAD -> demo)")
    print()
    print("READ-ONLY: nothing written to either tree.")


if __name__ == "__main__":
    sys.exit(main())
