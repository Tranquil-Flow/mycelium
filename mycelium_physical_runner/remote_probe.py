"""Remote, read-only physical facts probe for live preflight."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import socket
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import RunnerError

REQUEST_PROTOCOL = "mycelium.physical_runner_remote_probe_request.v1"
RESULT_PROTOCOL = "mycelium.physical_runner_live_probe.v1"
MAX_REQUEST_BYTES = 256 * 1024
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXPECTED_KEYS = {
    "protocol",
    "run_id",
    "host_alias",
    "node_id",
    "credential_path_alias",
    "coordinator_port",
    "runtime",
    "source_manifest",
    "model_assets",
    "tokenizer",
    "sidecar",
    "dependencies",
}


def _invalid() -> None:
    raise RunnerError("remote_probe_request_invalid")


def _segment(value: Any) -> str:
    if not isinstance(value, str) or _SEGMENT_RE.fullmatch(value) is None:
        _invalid()
    return value


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _invalid()
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _invalid()
    return value


def _artifact_name(value: Any) -> str:
    alias = _relative(value)
    name = PurePosixPath(alias.rsplit(":", 1)[-1]).name
    return _segment(name)


def _digest(path: Path) -> str | None:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        return "sha256:" + hasher.hexdigest()
    except OSError:
        return None


def _host_identity() -> str:
    if platform.system() == "Darwin":
        try:
            completed = subprocess.run(
                ("ioreg", "-rd1", "-c", "IOPlatformExpertDevice"),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
                text=True,
            )
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([A-Fa-f0-9-]+)"', completed.stdout)
            if completed.returncode == 0 and match is not None:
                return match.group(1).lower()
        except (OSError, subprocess.SubprocessError):
            pass
    machine_id = Path("/etc/machine-id")
    try:
        value = machine_id.read_text(encoding="ascii").strip()
        if value:
            return value
    except OSError:
        pass
    return "host-" + hashlib.sha256(platform.node().encode("utf-8")).hexdigest()[:32]


def _boot_identity(host_id: str) -> str:
    source = ""
    if platform.system() == "Darwin":
        try:
            completed = subprocess.run(
                ("sysctl", "-n", "kern.boottime"),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
                text=True,
            )
            if completed.returncode == 0:
                source = completed.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            source = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError:
            pass
    if not source:
        source = "unknown"
    return "boot-" + hashlib.sha256(f"{host_id}\x00{source}".encode()).hexdigest()[:32]


def derive_run_scoped_identity(
    *,
    run_id: str,
    observed_host_id: str,
    observed_boot_id: str,
) -> tuple[str, str]:
    """Derive unlinkable run-scoped aliases from local host observations."""

    run_id = _segment(run_id)
    observations = (observed_host_id, observed_boot_id)
    if any(
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1024
        or any(character in value for character in "\x00\n\r\t")
        for value in observations
    ):
        _invalid()
    host_digest = hashlib.sha256(
        b"mycelium.physical_runner.host_identity.v1\x00"
        + run_id.encode("utf-8")
        + b"\x00"
        + observed_host_id.encode("utf-8")
    ).hexdigest()
    boot_digest = hashlib.sha256(
        b"mycelium.physical_runner.boot_identity.v1\x00"
        + run_id.encode("utf-8")
        + b"\x00"
        + observed_boot_id.encode("utf-8")
    ).hexdigest()
    return "host-" + host_digest[:32], "boot-" + boot_digest[:32]


def _runtime_supported(runtime: str) -> bool:
    if runtime != "mlx-mac-arm64" or platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        __import__("mlx.core")
    except ImportError:
        return False
    return True


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
            handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            handle.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _process_conflict(node_id: str) -> bool:
    try:
        completed = subprocess.run(
            ("pgrep", "-f", f"mycelium-node.*{node_id}"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return completed.returncode == 0


def _credential_record(path: Path, alias: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError:
        metadata = None
    return {
        "path_alias": alias,
        "regular": metadata is not None and stat.S_ISREG(metadata.st_mode),
        "owner_matches_ssh_user": metadata is not None and metadata.st_uid == os.getuid(),
        "no_symlink": metadata is not None and not stat.S_ISLNK(metadata.st_mode),
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}" if metadata is not None else None,
    }


def _validate_request(request: Mapping[str, Any]) -> None:
    if set(request) != _EXPECTED_KEYS or request.get("protocol") != REQUEST_PROTOCOL:
        _invalid()
    for key in ("run_id", "host_alias", "node_id", "credential_path_alias", "runtime"):
        _segment(request.get(key))
    port = request.get("coordinator_port")
    if type(port) is not int or not 1024 <= port <= 65535:
        _invalid()
    source = request.get("source_manifest")
    models = request.get("model_assets")
    if not isinstance(source, list) or not isinstance(models, list):
        _invalid()
    for item in source:
        if not isinstance(item, Mapping) or set(item) != {"path", "digest"}:
            _invalid()
        _relative(item.get("path"))
        if not isinstance(item.get("digest"), str) or _SHA256_RE.fullmatch(item["digest"]) is None:
            _invalid()
    for item in models:
        if not isinstance(item, Mapping) or set(item) != {"public_alias", "digest", "size_bytes"}:
            _invalid()
        _artifact_name(item.get("public_alias"))
        if not isinstance(item.get("digest"), str) or _SHA256_RE.fullmatch(item["digest"]) is None:
            _invalid()
        if type(item.get("size_bytes")) is not int or item["size_bytes"] < 0:
            _invalid()
    for section in ("tokenizer", "sidecar", "dependencies"):
        item = request.get(section)
        if not isinstance(item, Mapping) or set(item) != {"public_alias", "digest"}:
            _invalid()
        _artifact_name(item.get("public_alias"))
        if not isinstance(item.get("digest"), str) or _SHA256_RE.fullmatch(item["digest"]) is None:
            _invalid()


def probe_request(
    request: Mapping[str, Any],
    *,
    home: Path | None = None,
    host_id: str | None = None,
    boot_id: str | None = None,
    runtime_supported: bool | None = None,
    port_available: bool | None = None,
    process_conflict: bool | None = None,
) -> dict[str, Any]:
    _validate_request(request)
    run_id = _segment(request["run_id"])
    alias = _segment(request["host_alias"])
    node_id = _segment(request["node_id"])
    credential_alias = _segment(request["credential_path_alias"])
    runtime = _segment(request["runtime"])
    port = request["coordinator_port"]
    root = (home or Path.home()) / "mycelium-physical-run" / run_id

    observed_host_id = host_id or _host_identity()
    observed_boot_id = boot_id or _boot_identity(observed_host_id)
    actual_host_id, actual_boot_id = derive_run_scoped_identity(
        run_id=run_id,
        observed_host_id=observed_host_id,
        observed_boot_id=observed_boot_id,
    )
    source = [
        {"path": item["path"], "digest": _digest(root / "source" / _relative(item["path"]))}
        for item in request["source_manifest"]
    ]
    models = []
    for item in request["model_assets"]:
        digest = _digest(root / "model" / _artifact_name(item["public_alias"]))
        models.append(
            {
                "public_alias": item["public_alias"],
                "digest": digest,
                "present": digest is not None,
            }
        )
    tokenizer_digest = _digest(
        root / "tokenizer" / _artifact_name(request["tokenizer"]["public_alias"])
    )
    sidecar_digest = _digest(
        root / "sidecar" / _artifact_name(request["sidecar"]["public_alias"])
    )
    dependency_digest = _digest(
        root / "dependencies" / _artifact_name(request["dependencies"]["public_alias"])
    )

    return {
        "protocol": RESULT_PROTOCOL,
        "host_alias": alias,
        "node_id": node_id,
        "host_id": actual_host_id,
        "boot_id": actual_boot_id,
        "unknowns": [],
        "route_ready": False,
        "public_network_required": False,
        "public_network_bytes": 0,
        "credential_file": _credential_record(root / "identities" / credential_alias, credential_alias),
        "port": {"port": port, "available": _port_available(port) if port_available is None else port_available},
        "process": {
            "conflict": _process_conflict(node_id) if process_conflict is None else process_conflict
        },
        "runtime": {
            "name": runtime,
            "supported": _runtime_supported(runtime) if runtime_supported is None else runtime_supported,
        },
        "source_manifest": source,
        "model_assets": models,
        "tokenizer": {"digest": tokenizer_digest, "present": tokenizer_digest is not None},
        "sidecar": {
            "public_alias": request["sidecar"]["public_alias"],
            "digest": sidecar_digest,
            "identity": request["sidecar"]["public_alias"] if sidecar_digest is not None else None,
        },
        "dependencies": {"digest": dependency_digest},
    }


def _load_request() -> Mapping[str, Any]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        _invalid()

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                _invalid()
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerError("remote_probe_request_invalid") from exc
    if not isinstance(value, Mapping):
        _invalid()
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments != ["--canonical-json"]:
        return 2
    try:
        result = probe_request(_load_request())
    except RunnerError:
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "REQUEST_PROTOCOL",
    "RESULT_PROTOCOL",
    "derive_run_scoped_identity",
    "main",
    "probe_request",
]
