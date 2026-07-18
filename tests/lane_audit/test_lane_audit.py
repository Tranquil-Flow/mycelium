from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mycelium_lane_audit.audit import audit_repository, canonical_json
from mycelium_lane_audit.manifest import (
    AuditManifest,
    ManifestError,
    manifest_from_dict,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PROTOCOL = "mycelium.lane_audit_manifest.v1"
AUDIT_PROTOCOL = "mycelium.lane_topology_audit.v1"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Lane Audit Tests",
            "GIT_AUTHOR_EMAIL": "lane-audit@example.invalid",
            "GIT_COMMITTER_NAME": "Lane Audit Tests",
            "GIT_COMMITTER_EMAIL": "lane-audit@example.invalid",
        },
    )


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    return repo, base


def _add_lane_worktree(
    repo: Path,
    tmp_path: Path,
    *,
    branch: str,
    base: str,
) -> Path:
    worktree = tmp_path / branch.replace("/", "-")
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    return worktree


def _manifest(base: str, lanes: list[dict[str, object]]) -> AuditManifest:
    return manifest_from_dict(
        {
            "protocol": MANIFEST_PROTOCOL,
            "target_branch": "main",
            "lanes": lanes,
        }
    )


def _lane(*, name: str, branch: str, base: str, allowed: list[str]) -> dict[str, object]:
    return {
        "name": name,
        "branch": branch,
        "expected_base": base,
        "allowed_paths": allowed,
    }


def test_manifest_rejects_duplicate_lanes_and_unsafe_or_ambiguous_paths() -> None:
    base = "a" * 40
    duplicate = {
        "protocol": MANIFEST_PROTOCOL,
        "target_branch": "main",
        "lanes": [
            _lane(name="lane-a", branch="feature/a", base=base, allowed=["owned/**"]),
            _lane(name="lane-a", branch="feature/b", base=base, allowed=["other/**"]),
        ],
    }
    with pytest.raises(ManifestError, match="duplicate lane name"):
        manifest_from_dict(duplicate)

    for unsafe in (
        "/absolute/**",
        "../escape/**",
        "safe/../escape.py",
        "./relative.py",
        "safe//double.py",
        "safe/\u0000control.py",
    ):
        payload = {
            "protocol": MANIFEST_PROTOCOL,
            "target_branch": "main",
            "lanes": [
                _lane(name="lane-a", branch="feature/a", base=base, allowed=[unsafe])
            ],
        }
        with pytest.raises(ManifestError, match="allowed path"):
            manifest_from_dict(payload)


def test_audit_reports_missing_branch_without_inflating_any_readiness(tmp_path: Path) -> None:
    repo, base = _init_repo(tmp_path)
    report = audit_repository(
        repo,
        _manifest(
            base,
            [
                _lane(
                    name="missing",
                    branch="feature/missing",
                    base=base,
                    allowed=["owned/**"],
                )
            ],
        ),
    )

    assert report["protocol"] == AUDIT_PROTOCOL
    assert report["route_ready"] is False
    assert report["release_ready"] is False
    assert report["tests_evaluated"] is False
    assert "structural Git topology only" in report["claim_boundary"]
    assert report["lanes"][0]["structural_state"] == "missing_branch"
    assert report["summary"]["missing_branch_count"] == 1


def test_dirty_paths_are_audited_read_only_and_ownership_violations_fail_closed(
    tmp_path: Path,
) -> None:
    repo, base = _init_repo(tmp_path)
    worktree = _add_lane_worktree(
        repo, tmp_path, branch="feature/lane-a", base=base
    )
    (worktree / "owned").mkdir()
    (worktree / "owned" / "new.py").write_text("OWNED = True\n", encoding="utf-8")
    (worktree / "outside.py").write_text("OUTSIDE = True\n", encoding="utf-8")
    before = _git(worktree, "status", "--porcelain=v1", "-z").stdout

    report = audit_repository(
        repo,
        _manifest(
            base,
            [
                _lane(
                    name="lane-a",
                    branch="feature/lane-a",
                    base=base,
                    allowed=["owned/**"],
                )
            ],
        ),
    )

    after = _git(worktree, "status", "--porcelain=v1", "-z").stdout
    lane = report["lanes"][0]
    assert after == before
    assert lane["dirty"] is True
    assert lane["dirty_paths"] == ["outside.py", "owned/new.py"]
    assert lane["ownership_violations"] == ["outside.py"]
    assert lane["structural_state"] == "ownership_violation"
    assert report["summary"]["ownership_violation_count"] == 1


def test_clean_commits_report_target_and_pairwise_path_overlap(tmp_path: Path) -> None:
    repo, base = _init_repo(tmp_path)
    lane_a = _add_lane_worktree(repo, tmp_path, branch="feature/a", base=base)
    lane_b = _add_lane_worktree(repo, tmp_path, branch="feature/b", base=base)

    for worktree, value in ((lane_a, "A"), (lane_b, "B")):
        (worktree / "owned").mkdir()
        (worktree / "owned" / "shared.py").write_text(
            f"VALUE = {value!r}\n", encoding="utf-8"
        )
        _git(worktree, "add", "owned/shared.py")
        _git(worktree, "commit", "-m", f"lane {value}")

    (repo / "owned").mkdir()
    (repo / "owned" / "shared.py").write_text("VALUE = 'target'\n", encoding="utf-8")
    _git(repo, "add", "owned/shared.py")
    _git(repo, "commit", "-m", "target change")

    report = audit_repository(
        repo,
        _manifest(
            base,
            [
                _lane(
                    name="lane-b",
                    branch="feature/b",
                    base=base,
                    allowed=["owned/**"],
                ),
                _lane(
                    name="lane-a",
                    branch="feature/a",
                    base=base,
                    allowed=["owned/**"],
                ),
            ],
        ),
    )

    assert [lane["name"] for lane in report["lanes"]] == ["lane-a", "lane-b"]
    for lane in report["lanes"]:
        assert lane["dirty"] is False
        assert lane["commits_ahead"] == 1
        assert lane["committed_paths"] == ["owned/shared.py"]
        assert lane["target_path_overlap"] == ["owned/shared.py"]
        assert lane["structural_state"] == "reviewable_with_target_overlap"
    assert report["pairwise_path_overlaps"] == [
        {"lanes": ["lane-a", "lane-b"], "paths": ["owned/shared.py"]}
    ]
    assert report["summary"]["target_overlap_count"] == 2
    assert report["summary"]["pairwise_overlap_count"] == 1


def test_lane_that_does_not_descend_from_declared_base_is_not_reviewable(
    tmp_path: Path,
) -> None:
    repo, original_base = _init_repo(tmp_path)
    _add_lane_worktree(repo, tmp_path, branch="feature/old-base", base=original_base)
    (repo / "new-base.txt").write_text("new base\n", encoding="utf-8")
    _git(repo, "add", "new-base.txt")
    _git(repo, "commit", "-m", "advance declared base")
    declared_base = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()

    report = audit_repository(
        repo,
        _manifest(
            declared_base,
            [
                _lane(
                    name="old-base",
                    branch="feature/old-base",
                    base=declared_base,
                    allowed=["owned/**"],
                )
            ],
        ),
    )

    lane = report["lanes"][0]
    assert lane["base_is_ancestor"] is False
    assert lane["structural_state"] == "base_mismatch"
    assert report["summary"]["structurally_reviewable_count"] == 0


def test_cli_emits_deterministic_canonical_json_without_absolute_paths(
    tmp_path: Path,
) -> None:
    repo, base = _init_repo(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "protocol": MANIFEST_PROTOCOL,
                "target_branch": "main",
                "lanes": [
                    _lane(
                        name="missing",
                        branch="feature/missing",
                        base=base,
                        allowed=["owned/**"],
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        "-m",
        "mycelium_lane_audit",
        "--repo-root",
        str(repo),
        "--manifest",
        str(manifest_path),
    ]
    first = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    report = json.loads(first.stdout)
    assert first.stdout == second.stdout == canonical_json(report) + "\n"
    assert str(repo) not in first.stdout
    assert str(tmp_path) not in first.stdout


def test_checked_in_active_lane_manifest_and_runbook_preserve_claim_boundary() -> None:
    manifest = manifest_from_dict(
        json.loads(
            (ROOT / "docs/integration/2026-07-18-active-lanes.json").read_text(
                encoding="utf-8"
            )
        )
    )
    assert manifest.target_branch == "integration/mycelium-manual-driver"
    assert {lane.name for lane in manifest.lanes} == {
        "independent-numerical-oracle",
        "release-doctor-hardening",
        "request-token-stream",
        "route-qualification-authority",
        "router-iroh-adapter",
        "router-lifecycle-conformance",
        "stage-local-kv",
    }

    runbook = (ROOT / "docs/integration/lane-topology-audit.md").read_text(
        encoding="utf-8"
    )
    assert "does not run or verify tests" in runbook
    assert "route_ready=false" in runbook
    assert "never `git add -A`" in runbook
