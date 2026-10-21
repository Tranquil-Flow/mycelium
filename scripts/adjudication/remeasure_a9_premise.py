#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Independent re-measurement of the A9 demotion premise, head 349845b.

Written from scratch for the 2026-09-21 adjudication lane. READ-ONLY: every git
call uses --no-optional-locks; no checkout/reset/stash/branch/commit anywhere.
Does NOT read runs/a9-adjudication/*.json (the 2026-09-16 tranche-1 artifacts):
the enumeration rules below were re-implemented from packet a9-1's description
so the counts are an independent reproduction, not a replay.
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

PILOT = Path(os.path.expanduser("~/Documents/playground/mycelium-core-pilot"))
OUT = Path(os.path.expanduser("~/Documents/mycelium-core-program/runs/a9-adjudication/evidence-2026-09-21"))
CORE = "integration/core-pilot"
A9_TIP = "ba6abbe"
A9_BASE = "abe291c"


def git(*args: str) -> str:
    res = subprocess.run(
        ["git", "--no-optional-locks", "--no-pager", *args],
        cwd=str(PILOT), capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()[:300]}")
    return res.stdout


def git_ok(*args: str) -> bool:
    return subprocess.run(
        ["git", "--no-optional-locks", "--no-pager", *args],
        cwd=str(PILOT), capture_output=True, text=True,
    ).returncode == 0


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    out: dict[str, object] = {}

    # ---------- 0. identity ----------
    head = git("rev-parse", CORE).strip()
    out["core_head"] = head
    out["core_head_short"] = git("rev-parse", "--short", CORE).strip()
    out["tree_dirty_entries"] = len([l for l in git("status", "--porcelain").splitlines() if l.strip()])
    out["a9_tip"] = git("rev-parse", A9_TIP).strip()
    out["a9_base_claimed"] = A9_BASE
    out["a9_base_actual_merge_base"] = git("merge-base", CORE, A9_TIP).strip()
    out["a9_base_short"] = git("rev-parse", "--short", out["a9_base_actual_merge_base"]).strip()

    # ---------- 1. enumeration: unreachable from core across all refs ----------
    unreachable = git("rev-list", "--all", "--not", CORE).split()
    out["unreachable_all_refs_count"] = len(unreachable)
    out["refs_total"] = len([l for l in git("for-each-ref", "--format=%(refname)").splitlines() if l.strip()])
    out["refs_not_ancestor_of_core"] = len(
        [r for r in git("for-each-ref", "--format=%(refname)").splitlines() if r.strip()
         and not git_ok("merge-base", "--is-ancestor", r, CORE)]
    )

    # ---------- 2. candidate set reproduction (rules from packet a9-1 sec.1) ----------
    # (a) every commit reachable from a named candidate lane but not from core
    LANES = [
        "work/mycelium-5b-ynqf0bos", "work/mycelium-session8b-5_ry7xyo",
        "w6-27b-native-adapter", "work/c-uc1-runtime-contract",
        "work/c-uc1-codec", "work/mycelium-8c-gotjomhu",
    ]
    candidates: set[str] = set()
    for ref in LANES:
        if git_ok("rev-parse", "--verify", ref):
            candidates |= set(git("rev-list", ref, "--not", CORE).split())
    lane_only = len(candidates)

    # (b) unreferenced / doc-named commits: 7-40 hex tokens in docs/handover/* that
    #     resolve as commits and are NOT ancestors of core
    hex_tokens: set[str] = set()
    import re
    for p in sorted((PILOT / "docs/handover").glob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        hex_tokens |= set(re.findall(r"\b[0-9a-f]{7,40}\b", text))
    resolved_not_ancestor = []
    for tok in sorted(hex_tokens):
        if git_ok("rev-parse", "--verify", "--quiet", tok + "^{commit}"):
            full = git("rev-parse", tok + "^{commit}").strip()
            if not git_ok("merge-base", "--is-ancestor", full, CORE):
                resolved_not_ancestor.append((tok, full))
    out["doc_hex_tokens_distinct"] = len(hex_tokens)
    out["doc_tokens_resolving_as_commits_not_ancestor"] = len(resolved_not_ancestor)
    out["doc_token_examples"] = [{"token": t, "full": f,
                                 "in_unreachable": f in unreachable}
                                 for t, f in resolved_not_ancestor][:12]

    # (c) the review's cited hashes (per packet a9-1)
    REVIEW_CITED = ["7ddd74d", "a65fd54", "ba6abbe", "22796ad", "8141289", "824ffc6"]
    review_cited = []
    for s in REVIEW_CITED:
        full = git("rev-parse", s + "^{commit}").strip()
        review_cited.append({
            "short": s, "full": full,
            "is_ancestor_of_core": git_ok("merge-base", "--is-ancestor", full, CORE),
            "in_unreachable_all_refs": full in unreachable,
        })
    out["review_cited"] = review_cited

    # candidate union = lanes + doc-named + all unreachable-across-refs + review-cited
    cand = set(candidates)
    cand |= {f for _, f in resolved_not_ancestor}
    cand |= set(unreachable)
    cand |= {r["full"] for r in review_cited}
    out["candidates_found"] = len(cand)
    out["candidates_from_named_lanes_only"] = lane_only
    out["candidates_in_unreachable_only"] = len(set(unreachable))
    # the demotion's own arithmetic: unreachable + dangling-doc-named + integrated review-cited
    dangling = [f for _, f in resolved_not_ancestor if f not in unreachable]
    integrated_review = [r["full"] for r in review_cited if r["is_ancestor_of_core"]]
    out["demotion_arithmetic"] = {
        "unreachable": len(unreachable),
        "dangling_doc_named": len(dangling),
        "already_integrated_review_cited": len(integrated_review),
        "sum": len(unreachable) + len(dangling) + len(integrated_review),
        "dangling_hashes": dangling,
        "integrated_review_hashes": integrated_review,
    }

    # ---------- 3. A9 lane range (abe291c..ba6abbe) path census ----------
    base = out["a9_base_actual_merge_base"]
    rng = f"{base}..{A9_TIP}"
    lane_commits = git("rev-list", rng).split()
    out["a9_lane_commit_count"] = len(lane_commits)
    out["a9_lane_commits_ancestor_of_core"] = sum(
        1 for c in lane_commits if git_ok("merge-base", "--is-ancestor", c, CORE))

    status_lines = git("diff", "--name-status", "--no-renames", base, A9_TIP).splitlines()
    added = [l.split("\t", 1)[1] for l in status_lines if l.startswith("A\t")]
    modified = [l.split("\t", 1)[1] for l in status_lines if l.startswith("M\t")]
    deleted = [l.split("\t", 1)[1] for l in status_lines if l.startswith("D\t")]
    other = [l for l in status_lines if l[0] not in "AMD"]
    all_paths = added + modified + deleted
    out["a9_changed_paths_total"] = len(all_paths)
    out["a9_paths_added"] = len(added)
    out["a9_paths_modified"] = len(modified)
    out["a9_paths_deleted"] = len(deleted)
    out["a9_paths_other_status"] = len(other)

    # ---------- 4. per-path integration state at core HEAD ----------
    def blob_at(rev: str, path: str):
        r = subprocess.run(["git", "--no-optional-locks", "--no-pager", "rev-parse",
                            f"{rev}:{path}"], cwd=str(PILOT), capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def blob_of_commit(path: str, commit: str):
        return blob_at(commit, path)

    head_has_path, identical, divergent, absent = [], [], [], []
    for p in all_paths:
        lb = blob_of_commit(p, A9_TIP)
        hb = blob_at(CORE, p)
        if hb is None:
            absent.append(p)
        elif hb == lb:
            identical.append(p)
        else:
            divergent.append(p)
    out["a9_paths_absent_at_core_head"] = len(absent)
    out["a9_paths_identical_at_core_head"] = len(identical)
    out["a9_paths_present_but_divergent"] = len(divergent)
    out["a9_absent_sample"] = absent[:10]
    out["a9_divergent_sample"] = divergent[:10]

    added_existing = [p for p in added if blob_at(CORE, p) is not None]
    out["a9_added_paths_existing_at_head"] = len(added_existing)
    out["a9_added_paths_existing_sample"] = added_existing[:15]

    # ---------- 5. named sample paths, checked one by one ----------
    samples = [
        "mycelium_membership/v2.py",
        "mycelium_membership/capability_v2.py",
        "mycelium_membership/migration.py",
        "mycelium_router/native_cpu_provider.py",
        "docs/superpowers/specs/mycelium-membership-control-v2.md",
        "docs/a9-native-cpu-provider.md",
        "contracts/contract-manifest.v1.json",
        "mycelium_a13_macos/peer.py",
    ]
    sample_rows = []
    for p in samples:
        sample_rows.append({
            "path": p,
            "exists_at_core_head": blob_at(CORE, p) is not None,
            "exists_in_a9_tip": blob_at(A9_TIP, p) is not None,
            "identical_to_a9_tip": (blob_at(CORE, p) is not None
                                    and blob_at(CORE, p) == blob_at(A9_TIP, p)),
            "in_lane_changed_set": p in set(all_paths),
        })
    out["named_sample_paths"] = sample_rows

    # ---------- 6. overlap: lane paths also changed in core since the base ----------
    core_since = git("diff", "--name-only", "--no-renames", base, CORE).splitlines()
    both = sorted(set(all_paths) & set(core_since))
    out["core_paths_changed_since_a9_base"] = len(core_since)
    out["core_commits_since_a9_base"] = len(git("rev-list", f"{base}..{CORE}").split())
    out["lane_paths_also_changed_in_core"] = len(both)
    out["lane_paths_also_changed_in_core_sample"] = both[:20]

    # ---------- 7. per-commit patch-id match against core ----------
    # does any A9-lane commit's single-parent diff have a stable patch-id equal to a
    # core commit's? (the strongest form of "duplicate" in packet a9-1's rules)
    def patch_ids(revs: list[str]) -> dict[str, str]:
        res = {}
        for c in revs:
            d = subprocess.run(["git", "--no-optional-locks", "--no-pager", "diff-tree",
                                "--no-commit-id", "-p", "-r", c],
                               cwd=str(PILOT), capture_output=True, text=True)
            if d.returncode != 0 or not d.stdout.strip():
                continue
            pid = subprocess.run(["git", "--no-optional-locks", "patch-id", "--stable"],
                                 input=d.stdout, cwd=str(PILOT), capture_output=True, text=True)
            if pid.returncode == 0 and pid.stdout.strip():
                res[c] = pid.stdout.split()[0]
        return res

    a9_pids = patch_ids(lane_commits)
    core_pids = patch_ids(git("rev-list", CORE).split())
    core_pid_set = set(core_pids.values())
    matched = sorted(c for c, p in a9_pids.items() if p in core_pid_set)
    out["a9_lane_commits_with_patch_id_in_core"] = len(matched)
    out["a9_lane_commits_patch_id_matched_hashes"] = matched

    (OUT / "remeasure-2026-09-21.json").write_text(json.dumps(out, indent=1, sort_keys=False))
    for k in ("core_head", "unreachable_all_refs_count", "candidates_found",
              "demotion_arithmetic", "a9_lane_commit_count", "a9_changed_paths_total",
              "a9_paths_added", "a9_paths_modified",
              "a9_paths_absent_at_core_head", "a9_paths_identical_at_core_head",
              "a9_paths_present_but_divergent", "a9_added_paths_existing_at_head",
              "lane_paths_also_changed_in_core", "a9_lane_commits_ancestor_of_core",
              "a9_lane_commits_with_patch_id_in_core"):
        print(f"{k} = {out[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
