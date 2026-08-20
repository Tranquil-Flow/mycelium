from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

from .manifest import AuditManifest, LaneSpec


AUDIT_PROTOCOL = "mycelium.lane_topology_audit.v1"
CLAIM_BOUNDARY = (
    "read-only declared ownership and structural Git topology only; does not run or "
    "verify tests, semantics, conflict resolution, physical qualification, route "
    "readiness, or release readiness"
)


class AuditError(RuntimeError):
    """Raised when repository topology cannot be inspected deterministically."""


class _GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        probe = self.run("rev-parse", "--git-dir", allowed=(0, 128))
        if probe.returncode != 0:
            raise AuditError("repo root is not inside a Git worktree")

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        allowed: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
        )
        if result.returncode not in allowed:
            diagnostic = result.stderr.decode("utf-8", "replace").strip()
            raise AuditError(
                f"git command failed with exit {result.returncode}: "
                f"{' '.join(args)}: {diagnostic}"
            )
        return result

    def branch_head(self, branch: str) -> str | None:
        result = self.run(
            "rev-parse",
            "--verify",
            f"refs/heads/{branch}^{{commit}}",
            allowed=(0, 128),
        )
        if result.returncode != 0:
            return None
        return result.stdout.decode("ascii").strip()

    def commit_exists(self, sha: str) -> bool:
        result = self.run("cat-file", "-e", f"{sha}^{{commit}}", allowed=(0, 128))
        return result.returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self.run(
            "merge-base", "--is-ancestor", ancestor, descendant, allowed=(0, 1)
        )
        return result.returncode == 0

    def commits_ahead(self, base: str, head: str) -> int:
        result = self.run("rev-list", "--count", f"{base}..{head}")
        return int(result.stdout.decode("ascii").strip())

    def changed_paths(self, base: str, head: str) -> list[str]:
        result = self.run(
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"{base}..{head}",
            "--",
        )
        return _decode_nul_paths(result.stdout)

    def worktrees_by_branch(self) -> dict[str, Path]:
        result = self.run("worktree", "list", "--porcelain")
        records = result.stdout.decode("utf-8", "surrogateescape").strip().split("\n\n")
        worktrees: dict[str, Path] = {}
        for record in records:
            fields: dict[str, str] = {}
            for line in record.splitlines():
                key, separator, value = line.partition(" ")
                if separator:
                    fields[key] = value
            branch_ref = fields.get("branch")
            worktree = fields.get("worktree")
            if branch_ref and worktree and branch_ref.startswith("refs/heads/"):
                worktrees[branch_ref.removeprefix("refs/heads/")] = Path(worktree)
        return worktrees

    def dirty_paths(self, worktree: Path) -> list[str]:
        result = self.run(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            cwd=worktree,
        )
        return _parse_porcelain_v1_z(result.stdout)


def _decode_nul_paths(payload: bytes) -> list[str]:
    return sorted(
        {
            item.decode("utf-8", "surrogateescape")
            for item in payload.split(b"\0")
            if item
        }
    )


def _parse_porcelain_v1_z(payload: bytes) -> list[str]:
    entries = payload.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise AuditError("could not parse Git porcelain status")
        status = entry[:2]
        paths.add(entry[3:].decode("utf-8", "surrogateescape"))
        if b"R" in status or b"C" in status:
            if index >= len(entries) or not entries[index]:
                raise AuditError("could not parse renamed Git porcelain status")
            paths.add(entries[index].decode("utf-8", "surrogateescape"))
            index += 1
    return sorted(paths)


def _path_is_allowed(path: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            if path == prefix or path.startswith(prefix + "/"):
                return True
        elif fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _fixed_pattern_prefix(pattern: str) -> str:
    positions = [
        position
        for token in ("*", "?", "[")
        if (position := pattern.find(token)) >= 0
    ]
    return pattern if not positions else pattern[: min(positions)]


def _patterns_overlap(left: str, right: str) -> bool:
    """Conservatively detect whether two ownership patterns can name one path."""

    if left == right:
        return True
    left_literal = _fixed_pattern_prefix(left) == left
    right_literal = _fixed_pattern_prefix(right) == right
    if left_literal:
        return _path_is_allowed(left, (right,))
    if right_literal:
        return _path_is_allowed(right, (left,))

    left_prefix = _fixed_pattern_prefix(left).rstrip("/")
    right_prefix = _fixed_pattern_prefix(right).rstrip("/")
    if not left_prefix or not right_prefix:
        return True
    return (
        left_prefix == right_prefix
        or left_prefix.startswith(right_prefix + "/")
        or right_prefix.startswith(left_prefix + "/")
    )


def _structural_state(
    *,
    branch_exists: bool,
    base_exists: bool,
    base_is_ancestor: bool | None,
    ownership_violations: list[str],
    dirty: bool,
    commits_ahead: int | None,
    target_path_overlap: list[str],
) -> str:
    if not branch_exists:
        return "missing_branch"
    if not base_exists:
        return "missing_expected_base"
    if base_is_ancestor is not True:
        return "base_mismatch"
    if ownership_violations:
        return "ownership_violation"
    if dirty:
        return "in_progress_dirty"
    if commits_ahead == 0:
        return "no_feature_commit"
    if target_path_overlap:
        return "reviewable_with_target_overlap"
    return "structurally_reviewable"


def _audit_lane(
    git: _GitRepository,
    lane: LaneSpec,
    *,
    target_head: str | None,
    worktrees: dict[str, Path],
) -> dict[str, Any]:
    head = git.branch_head(lane.branch)
    branch_exists = head is not None
    base_exists = git.commit_exists(lane.expected_base)
    base_is_ancestor: bool | None = None
    commits_ahead: int | None = None
    committed_paths: list[str] = []
    target_path_overlap: list[str] = []
    target_contains_expected_base: bool | None = None

    if branch_exists and base_exists and head is not None:
        base_is_ancestor = git.is_ancestor(lane.expected_base, head)
        commits_ahead = git.commits_ahead(lane.expected_base, head)
        committed_paths = git.changed_paths(lane.expected_base, head)

    if base_exists and target_head is not None:
        target_contains_expected_base = git.is_ancestor(lane.expected_base, target_head)
        target_delta_paths = set(git.changed_paths(lane.expected_base, target_head))
    else:
        target_delta_paths = set()

    worktree = worktrees.get(lane.branch)
    dirty_paths = git.dirty_paths(worktree) if worktree is not None else []
    effective_paths = sorted(set(committed_paths) | set(dirty_paths))
    ownership_violations = sorted(
        path
        for path in effective_paths
        if not _path_is_allowed(path, lane.allowed_paths)
    )
    target_path_overlap = sorted(set(effective_paths) & target_delta_paths)
    dirty = bool(dirty_paths)

    return {
        "name": lane.name,
        "branch": lane.branch,
        "expected_base": lane.expected_base,
        "allowed_paths": list(lane.allowed_paths),
        "branch_exists": branch_exists,
        "head": head,
        "expected_base_exists": base_exists,
        "base_is_ancestor": base_is_ancestor,
        "target_contains_expected_base": target_contains_expected_base,
        "commits_ahead": commits_ahead,
        "worktree_present": worktree is not None,
        "dirty": dirty,
        "committed_paths": committed_paths,
        "dirty_paths": dirty_paths,
        "effective_paths": effective_paths,
        "ownership_violations": ownership_violations,
        "target_path_overlap": target_path_overlap,
        "structural_state": _structural_state(
            branch_exists=branch_exists,
            base_exists=base_exists,
            base_is_ancestor=base_is_ancestor,
            ownership_violations=ownership_violations,
            dirty=dirty,
            commits_ahead=commits_ahead,
            target_path_overlap=target_path_overlap,
        ),
    }


def audit_repository(repo_root: str | Path, manifest: AuditManifest) -> dict[str, Any]:
    root = Path(repo_root)
    if not root.is_dir():
        raise AuditError("repo root must be an existing directory")
    git = _GitRepository(root)
    target_head = git.branch_head(manifest.target_branch)
    worktrees = git.worktrees_by_branch()
    lanes = [
        _audit_lane(git, lane, target_head=target_head, worktrees=worktrees)
        for lane in manifest.lanes
    ]

    pairwise_path_overlaps: list[dict[str, Any]] = []
    for left, right in combinations(lanes, 2):
        paths = sorted(set(left["effective_paths"]) & set(right["effective_paths"]))
        if paths:
            pairwise_path_overlaps.append(
                {"lanes": [left["name"], right["name"]], "paths": paths}
            )

    pairwise_declared_path_overlaps: list[dict[str, Any]] = []
    for left, right in combinations(manifest.lanes, 2):
        patterns = [
            {"left": left_pattern, "right": right_pattern}
            for left_pattern in left.allowed_paths
            for right_pattern in right.allowed_paths
            if _patterns_overlap(left_pattern, right_pattern)
        ]
        if patterns:
            pairwise_declared_path_overlaps.append(
                {"lanes": [left.name, right.name], "patterns": patterns}
            )

    report = {
        "protocol": AUDIT_PROTOCOL,
        "manifest_protocol": manifest.protocol,
        "claim_boundary": CLAIM_BOUNDARY,
        "route_ready": False,
        "release_ready": False,
        "tests_evaluated": False,
        "ownership_safe_to_dispatch": not pairwise_declared_path_overlaps,
        "target": {
            "branch": manifest.target_branch,
            "exists": target_head is not None,
            "head": target_head,
        },
        "lanes": lanes,
        "pairwise_path_overlaps": pairwise_path_overlaps,
        "pairwise_declared_path_overlaps": pairwise_declared_path_overlaps,
        "summary": {
            "lane_count": len(lanes),
            "missing_branch_count": sum(
                lane["structural_state"] == "missing_branch" for lane in lanes
            ),
            "dirty_lane_count": sum(lane["dirty"] for lane in lanes),
            "ownership_violation_count": sum(
                bool(lane["ownership_violations"]) for lane in lanes
            ),
            "target_overlap_count": sum(
                bool(lane["target_path_overlap"]) for lane in lanes
            ),
            "pairwise_overlap_count": len(pairwise_path_overlaps),
            "pairwise_declared_overlap_count": len(
                pairwise_declared_path_overlaps
            ),
            "structurally_reviewable_count": sum(
                lane["structural_state"]
                in {"structurally_reviewable", "reviewable_with_target_overlap"}
                for lane in lanes
            ),
        },
    }
    return report


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
