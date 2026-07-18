from __future__ import annotations

import json
import os
import shutil
import socket
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

PROTOCOL = "mycelium.release_doctor_preflight.v1"
DEFAULT_COMMANDS = ("python3.14", "git", "cargo", "node", "npm")
DEFAULT_REQUIRED_FILES = (
    "contracts/contract-manifest.v1.json",
    "scripts/contract_audit.py",
    "native/iroh_transport/Cargo.lock",
    "ui/web/package-lock.json",
    "docs/automation/2026-07-18-manual-driver-handover.md",
)
CLAIM_BOUNDARY = (
    "read-only local environment preflight only; no process start, provisioning, "
    "inference, transport, qualification, or physical-host evidence"
)
RELEASE_BLOCKERS = [
    "RouteQualificationV1 is not consumed by this preflight tranche",
    "physical two-host inference and transport are not evaluated",
    "request streaming, live Observatory, recovery, and evidence sealing are not evaluated",
]

Which = Callable[[str], str | None]
PortProbe = Callable[[int], bool]


def canonical_json(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def local_tcp_port_available(port: int) -> bool:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _file_check(root: Path, relative: str) -> dict[str, Any]:
    name = f"file:{relative}"
    rel = Path(relative)
    if (
        not isinstance(relative, str)
        or not relative
        or rel.is_absolute()
        or ".." in rel.parts
        or rel.as_posix() != relative
        or any(part in {"", "."} for part in rel.parts)
    ):
        return _check(name, False, "required file path must be canonical and repository-relative")

    candidate = root / rel
    resolved = candidate.resolve(strict=False)
    if not _inside(resolved, root):
        return _check(name, False, "required file escapes repository root")
    if not candidate.is_file():
        return _check(name, False, f"required file missing: {relative}")
    return _check(name, True, f"required file present: {relative}")


def run_preflight(
    *,
    repo_root: Path | str,
    state_dir: Path | str,
    commands: Iterable[str] = DEFAULT_COMMANDS,
    required_files: Iterable[str] = DEFAULT_REQUIRED_FILES,
    ports: Iterable[int] = (),
    which: Which = shutil.which,
    port_available: PortProbe = local_tcp_port_available,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve(strict=False)
    state = Path(state_dir).expanduser().resolve(strict=False)
    checks: list[dict[str, Any]] = []

    checks.append(
        _check(
            "repository_root",
            root.is_dir(),
            "repository root exists" if root.is_dir() else "repository root is not a directory",
        )
    )

    state_outside = not _inside(state, root)
    checks.append(
        _check(
            "state_directory_outside_source",
            state_outside,
            "state directory resolves outside repository root"
            if state_outside
            else "state directory must resolve outside repository root",
        )
    )
    state_parent = _existing_parent(state)
    state_parent_writable = state_parent.is_dir() and os.access(state_parent, os.W_OK | os.X_OK)
    checks.append(
        _check(
            "state_directory_parent_writable",
            state_parent_writable,
            "existing state-directory parent is writable"
            if state_parent_writable
            else "existing state-directory parent is not writable",
        )
    )

    for command in commands:
        found = which(command)
        checks.append(
            _check(
                f"command:{command}",
                found is not None,
                f"required command found: {command}"
                if found is not None
                else f"required command not found: {command}",
            )
        )

    for relative in required_files:
        checks.append(_file_check(root, relative))

    for port in ports:
        try:
            available = port_available(port)
        except (OSError, ValueError):
            available = False
        checks.append(
            _check(
                f"port:{port}",
                available,
                f"local TCP port is available: {port}"
                if available
                else f"local TCP port is unavailable: {port}",
            )
        )

    return {
        "protocol": PROTOCOL,
        "local_preflight_ok": all(check["ok"] for check in checks),
        "route_ready": False,
        "release_ready": False,
        "qualification_evaluated": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "release_blockers": list(RELEASE_BLOCKERS),
        "checks": checks,
    }
