from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.claim_boundary_audit import (
    MAX_SOURCE_BYTES,
    PROTOCOL,
    audit_repository,
    canonical_json,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "claim_boundary_audit.py"
RUNBOOK = ROOT / "docs" / "security" / "claim-boundary-audit.md"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"GIT_OPTIONAL_LOCKS": "0"},
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(repo, "add", "--", name)
    return repo


def _codes(result: dict[str, object]) -> set[str]:
    findings = result["findings"]
    assert isinstance(findings, list)
    return {str(item["code"]) for item in findings}


def test_honest_tree_preserves_fixed_false_readiness_claims(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "app.py": "state = {'route_ready': False, 'release_ready': False}\n",
            "mycelium_qualification/qualifier.py": (
                "def accepted_record():\n"
                "    return {'route_ready': True}\n"
            ),
            "mycelium_gateway/asgi.py": (
                "ALLOWED_METHOD = 'GET'\n"
                "REJECTION = 'POST requests remain rejected'\n"
            ),
            "ui/web/src/data/observatorySource.ts": (
                "export const load = (url: string) => fetch(url);\n"
                "export const events = (url: string) => new EventSource(url);\n"
            ),
        },
    )

    result = audit_repository(repo)

    assert result["protocol"] == PROTOCOL
    assert result["ok"] is True
    assert result["findings"] == []
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert result["claim_boundary"] == {
        "authenticated_transport_evaluated": False,
        "dynamic_dispatch_evaluated": False,
        "physical_qualification_evaluated": False,
        "runtime_semantics_evaluated": False,
        "scope": "tracked production source literal claims, read-only Observatory, and allowlisted product action clients only",
        "semantic_qualification_evaluated": False,
    }
    scan = result["scan"]
    assert isinstance(scan, dict)
    assert scan["allowed_route_ready_literals"] == 1


def test_route_and_release_true_literals_fail_outside_authority(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "worker.py": (
                "state = {'route_ready': True}\n"
                "record = Result(route_ready=True)\n"
                "state['route_ready'] = True\n"
            ),
            "mycelium_qualification/qualifier.py": "release_ready = True\n",
            "release.py": "release_ready = True\n",
        },
    )

    result = audit_repository(repo)

    assert result["ok"] is False
    findings = result["findings"]
    assert isinstance(findings, list)
    assert [item for item in findings if item["code"] == "route_ready_true_outside_authority"] == [
        {"code": "route_ready_true_outside_authority", "line": 1, "path": "worker.py"},
        {"code": "route_ready_true_outside_authority", "line": 2, "path": "worker.py"},
        {"code": "route_ready_true_outside_authority", "line": 3, "path": "worker.py"},
    ]
    assert [item for item in findings if item["code"] == "release_ready_true_literal"] == [
        {
            "code": "release_ready_true_literal",
            "line": 1,
            "path": "mycelium_qualification/qualifier.py",
        },
        {"code": "release_ready_true_literal", "line": 1, "path": "release.py"},
    ]


def test_observatory_backend_and_ui_write_surfaces_fail(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "mycelium_gateway/asgi.py": (
                "MUTATING_METHOD = 'POST'\n"
                "@app.delete('/records')\n"
                "def remove():\n"
                "    pass\n"
            ),
            "ui/web/src/data/observatorySource.ts": (
                "fetch(url, { method: 'PATCH', body: payload });\n"
                "client.post('/control', payload);\n"
            ),
            "ui/web/src/data/observatorySource.test.ts": (
                "fetch(url, { method: 'DELETE' });\n"
            ),
        },
    )

    result = audit_repository(repo)

    assert result["ok"] is False
    findings = result["findings"]
    assert isinstance(findings, list)
    assert [item for item in findings if item["code"] == "observatory_backend_write_surface"] == [
        {
            "code": "observatory_backend_write_surface",
            "line": 1,
            "path": "mycelium_gateway/asgi.py",
            "subject": "POST",
        },
        {
            "code": "observatory_backend_write_surface",
            "line": 2,
            "path": "mycelium_gateway/asgi.py",
            "subject": "delete",
        },
    ]
    assert [item for item in findings if item["code"] == "observatory_ui_write_surface"] == [
        {
            "code": "observatory_ui_write_surface",
            "line": 1,
            "path": "ui/web/src/data/observatorySource.ts",
            "subject": "PATCH",
        },
        {
            "code": "observatory_ui_write_surface",
            "line": 2,
            "path": "ui/web/src/data/observatorySource.ts",
            "subject": "POST",
        },
    ]


def test_allowlisted_product_action_clients_do_not_weaken_observatory_boundary(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "ui/web/src/features/inference/deploymentClient.ts": "fetch('/__mycelium/deployments/select', { method: 'POST' });\n",
            "ui/web/src/features/inference/requestClient.ts": "fetch('/api/v1/inference/requests', { method: 'POST' });\n",
            "ui/web/src/features/swarm/SwarmClient.ts": "fetch('/api/v1/swarm/join', { method: 'POST' });\n",
            "ui/web/src/features/membership/membershipClient.ts": "fetch('/api/v1/membership/join', { method: 'POST' });\n",
            "ui/web/src/features/deviceLab/deviceLabClient.ts": "fetch('/api/interactive/infer', { method: 'POST' });\n",
            "ui/web/src/features/deviceLab/arbitraryClient.ts": "fetch('/api/interactive/infer', { method: 'POST' });\n",
            "ui/web/src/data/observatorySource.ts": "fetch('/api/v1/observatory', { method: 'POST' });\n",
        },
    )

    result = audit_repository(repo)
    findings = result["findings"]
    assert isinstance(findings, list)

    assert [item["path"] for item in findings] == [
        "ui/web/src/data/observatorySource.ts",
        "ui/web/src/features/deviceLab/arbitraryClient.ts",
    ]


def test_unreadable_source_shapes_fail_closed(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "broken.py": "def broken(:\n",
            "large.py": "#" + ("x" * MAX_SOURCE_BYTES),
            "missing.py": "present at index time\n",
            "safe.py": "pass\n",
        },
    )
    (repo / "missing.py").unlink()
    target = tmp_path / "external.py"
    target.write_text("route_ready = True\n", encoding="utf-8")
    link = repo / "linked.py"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    _git(repo, "add", "--", "linked.py")

    result = audit_repository(repo)

    assert result["ok"] is False
    assert {
        "python_parse_error",
        "tracked_file_missing",
        "tracked_source_too_large",
        "tracked_symlink",
    } <= _codes(result)


def test_output_redacts_secret_shaped_tracked_paths(tmp_path: Path) -> None:
    token = "ghp_" + ("a" * 36)
    repo = _repo(tmp_path, {f"{token}.py": "route_ready = True\n"})

    result = audit_repository(repo)
    rendered = canonical_json(result)

    assert result["ok"] is False
    assert token not in rendered
    findings = result["findings"]
    assert isinstance(findings, list)
    assert findings[0]["path"].startswith("<redacted:sha256:")


def test_output_is_deterministic_and_repository_is_unchanged(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "z.py": "release_ready = True\n",
            "a.py": "route_ready = True\n",
        },
    )
    before_index = (repo / ".git" / "index").read_bytes()
    before_status = _git(repo, "status", "--porcelain=v1", "-z").stdout

    first = canonical_json(audit_repository(repo))
    second = canonical_json(audit_repository(repo))

    assert first == second
    assert first.endswith("\n")
    findings = json.loads(first)["findings"]
    order = [(item["path"], item.get("line", 0), item["code"]) for item in findings]
    assert order == sorted(order)
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert _git(repo, "status", "--porcelain=v1", "-z").stdout == before_status


def test_cli_json_is_canonical_and_exit_matches_findings(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"app.py": "route_ready = False\n"})

    accepted = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert accepted.returncode == 0
    accepted_payload = json.loads(accepted.stdout)
    assert accepted.stdout == canonical_json(accepted_payload)
    assert accepted_payload["ok"] is True
    assert accepted_payload["route_ready"] is False
    assert accepted.stderr == ""

    unsafe = repo / "unsafe.py"
    unsafe.write_text("route_ready = True\n", encoding="utf-8")
    _git(repo, "add", "--", "unsafe.py")
    rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode == 1
    rejected_payload = json.loads(rejected.stdout)
    assert rejected.stdout == canonical_json(rejected_payload)
    assert rejected_payload["ok"] is False
    assert rejected_payload["route_ready"] is False
    assert rejected_payload["release_ready"] is False
    assert rejected.stderr == ""


def test_current_checkout_and_runbook_preserve_claim_boundary() -> None:
    result = audit_repository(ROOT)
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert result["ok"] is True, result["findings"]
    assert "route_ready=false" in runbook
    assert "release_ready=false" in runbook
    assert "semantic_qualification_evaluated=false" in runbook
    assert "does not run inference" in runbook
    assert "does not authorize the request gateway" in runbook
    assert "Observatory" in runbook
