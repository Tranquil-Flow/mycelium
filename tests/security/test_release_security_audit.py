from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import TracebackType
from typing import Any, IO

import pytest

import scripts.release_security_audit as security_audit
from scripts.release_security_audit import PROTOCOL, audit_repository, canonical_json

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_security_audit.py"
RUNBOOK = ROOT / "docs" / "security" / "release-security-audit.md"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"GIT_OPTIONAL_LOCKS": "0"},
    )


def _repo(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        _git(repo, "add", "--", name)
    return repo


def _codes(result: dict[str, object]) -> set[str]:
    findings = result["findings"]
    assert isinstance(findings, list)
    return {finding["code"] for finding in findings}


def test_honest_tracked_tree_passes_without_inflating_readiness(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "app.py": "print('moonlight')\n",
            "cli.py": "parser.add_argument('--token-file')\n",
            "descriptor.py": "parser.add_argument('--token-fd')\n",
            ".env.example": "MYCELIUM_TOKEN_FILE=/run/secrets/token\n",
            "reference.py": "parser.add_argument('--secret-ref')\n",
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
        "dependency_vulnerabilities_evaluated": False,
        "history_evaluated": False,
        "physical_qualification_evaluated": False,
        "runtime_security_evaluated": False,
        "scope": "tracked working-tree files and Python CLI declarations only",
        "untracked_files_evaluated": False,
    }


def test_detects_secret_material_without_echoing_it(tmp_path: Path) -> None:
    token = "gh" + "p_" + ("A" * 36)
    private_key = (
        "-----BEGIN "
        + "OPENSSH PRIVATE KEY-----\n"
        + ("A" * 64)
        + "\n"
        + ("B" * 64)
        + "\n-----END "
        + "OPENSSH PRIVATE KEY-----\n"
    )
    encrypted_key = (
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\n"
        + "Proc-Type: 4,ENCRYPTED\n"
        + "DEK-Info: AES-256-CBC,0123456789ABCDEF\n\n"
        + ("C" * 64)
        + "\n"
        + ("D" * 8)
        + "\n-----END RSA "
        + "PRIVATE KEY-----\n"
    )
    repo = _repo(
        tmp_path,
        {
            "src/token.txt": token + "\n",
            "keys/peer.key": private_key,
            "keys/encrypted.key": encrypted_key,
        },
    )

    result = audit_repository(repo)
    rendered = canonical_json(result)

    assert result["ok"] is False
    assert {"private_key_material", "prefixed_access_token"} <= _codes(result)
    assert token not in rendered
    assert "redacted" not in rendered


def test_rejects_direct_secret_cli_values_but_allows_file_or_env_references(
    tmp_path: Path,
) -> None:
    repo = _repo(
        tmp_path,
        {
            "unsafe.py": "parser.add_argument('--api-key')\n",
            "safe.py": (
                "parser.add_argument('--token-file')\n"
                "parser.add_argument('--password-env')\n"
                "parser.add_argument('--secret-path')\n"
            ),
        },
    )

    result = audit_repository(repo)

    assert result["ok"] is False
    findings = [item for item in result["findings"] if item["code"] == "secret_cli_argument"]
    assert findings == [
        {
            "code": "secret_cli_argument",
            "line": 1,
            "path": "unsafe.py",
            "subject": "--api-key",
        }
    ]


def test_flags_tracked_credential_paths_and_ignores_untracked_material(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {".env": "MODE=local\n", "app.py": "pass\n"})
    untracked_token = "hf" + "_" + ("B" * 40)
    (repo / "scratch.txt").write_text(untracked_token, encoding="utf-8")

    result = audit_repository(repo)

    assert result["ok"] is False
    assert "tracked_credential_path" in _codes(result)
    assert all(item["path"] != "scratch.txt" for item in result["findings"])
    assert result["claim_boundary"]["untracked_files_evaluated"] is False


def test_rejects_tracked_symlinks_without_following_them(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"outside.txt": "safe\n"})
    target = tmp_path / "external"
    target.write_text("not read\n", encoding="utf-8")
    link = repo / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    _git(repo, "add", "--", "linked.txt")

    result = audit_repository(repo)

    assert result["ok"] is False
    assert [item for item in result["findings"] if item["path"] == "linked.txt"] == [
        {"code": "tracked_symlink", "path": "linked.txt"}
    ]


def test_same_size_mutation_with_restored_mtime_during_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_content = ("S" * 40) + "\n"
    secret_content = "ghp_" + ("A" * 36) + "\n"
    assert len(safe_content) == len(secret_content)
    repo = _repo(tmp_path, {"app.py": safe_content})
    target = repo / "app.py"
    initial = target.stat()
    real_open = Path.open

    class MutatingHandle:
        def __init__(self, handle: IO[Any]) -> None:
            self._handle = handle

        def __enter__(self) -> "MutatingHandle":
            self._handle.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            self._handle.__exit__(exc_type, exc_value, traceback)

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self, size: int = -1) -> bytes:
            with real_open(target, "wb") as mutator:
                mutator.write(secret_content.encode("ascii"))
            os.utime(target, ns=(initial.st_atime_ns, initial.st_mtime_ns))
            return self._handle.read(size)

    def racing_open(
        path: Path, mode: str = "r", *args: Any, **kwargs: Any
    ) -> Any:
        handle = real_open(path, mode, *args, **kwargs)
        if path == target and mode == "rb":
            return MutatingHandle(handle)
        return handle

    monkeypatch.setattr(Path, "open", racing_open)

    result = audit_repository(repo)

    assert result["ok"] is False
    assert [item for item in result["findings"] if item["path"] == "app.py"] == [
        {"code": "tracked_file_changed_during_read", "path": "app.py"}
    ]


def test_audit_is_deterministic_and_does_not_mutate_repository(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            ".env": "MODE=local\n",
            "unsafe.py": "parser.add_argument('--password')\n",
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


def test_cli_emits_canonical_json_and_exit_reflects_bounded_audit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"app.py": "pass\n"})

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert completed.stdout == canonical_json(payload)
    assert payload["ok"] is True
    assert payload["route_ready"] is False
    assert completed.stderr == ""

    token = "gh" + "p_" + ("Q" * 36)
    unsafe_name = f"src/{token}.py"
    unsafe = repo / unsafe_name
    unsafe.parent.mkdir()
    unsafe.write_text(f"parser.add_argument('--api-key={token}')\n", encoding="utf-8")
    _git(repo, "add", "--", unsafe_name)
    rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode == 1
    assert token not in rejected.stdout
    assert token not in rejected.stderr
    rejected_payload = json.loads(rejected.stdout)
    assert rejected_payload["ok"] is False
    assert all(token not in item["path"] for item in rejected_payload["findings"])


def test_missing_oversized_and_unreadable_tracked_files_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(
        tmp_path,
        {
            "missing.txt": "present at index time\n",
            "oversized.bin": b"123456789",
            "unreadable.txt": "locked\n",
        },
    )
    (repo / "missing.txt").unlink()
    unreadable = repo / "unreadable.txt"
    unreadable.chmod(0)
    monkeypatch.setattr(security_audit, "MAX_FILE_BYTES", 8)
    try:
        result = audit_repository(repo)
    finally:
        unreadable.chmod(0o600)

    assert {
        "tracked_file_missing",
        "tracked_file_too_large",
        "tracked_file_read_error",
    } <= _codes(result)


def test_current_checkout_and_runbook_preserve_claim_boundary() -> None:
    result = audit_repository(ROOT)
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert result["ok"] is True, result["findings"]
    assert "route_ready=false" in runbook
    assert "release_ready=false" in runbook
    assert "untracked_files_evaluated=false" in runbook
    assert "does not run inference" in runbook
    assert "does not evaluate authenticated transport" in runbook
