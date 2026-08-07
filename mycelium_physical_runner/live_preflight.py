"""Bounded strict-SSH bridge from redacted safe plans to live probe facts."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RunnerError
from .plan_builder import SAFE_PLAN_PROTOCOL

LIVE_PREFLIGHT_PROTOCOL = "mycelium.physical_runner_live_preflight.v1"
LIVE_PROBE_PROTOCOL = "mycelium.physical_runner_live_probe.v1"
MAX_PROBE_BYTES = 256 * 1024
MAX_SAFE_PLAN_BYTES = 2 * 1024 * 1024
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_RUN_SCOPED_HOST_RE = re.compile(r"^host-[0-9a-f]{32}$")
_RUN_SCOPED_BOOT_RE = re.compile(r"^boot-[0-9a-f]{32}$")
_REMOTE_PROBE_MODULE_COMMAND = (
    "/opt/homebrew/bin/python3.14 -m "
    "mycelium_physical_runner.remote_probe --canonical-json"
)
_REMOTE_REQUEST_PROTOCOL = "mycelium.physical_runner_remote_probe_request.v1"


@dataclass(frozen=True, slots=True)
class _Capture:
    returncode: int
    stdout: bytes
    stderr: bytes


class _SubprocessRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_bytes: bytes | None,
        cwd: Path | None = None,
    ) -> _Capture:
        completed = subprocess.run(
            argv,
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            cwd=cwd,
        )
        return _Capture(completed.returncode, completed.stdout, completed.stderr)


@dataclass(slots=True)
class _ProductionLocalProbes:
    git_dirty: bool
    expected_commit: str | None
    public_network_bytes: int
    ssh_identities: dict[str, dict[str, Any]]


def _git_output(*arguments: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ("git", *arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _ssh_identity_for(target: str) -> str | None:
    try:
        completed = subprocess.run(
            ("ssh", "-G", "--", target),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.decode("utf-8", errors="strict").splitlines():
        key, separator, value = line.partition(" ")
        if separator and key == "identityfile":
            return str(Path(value).expanduser().resolve(strict=False))
    return None


def _identity_record(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError:
        return {"path": path}
    return {
        "path": path,
        "regular": stat.S_ISREG(metadata.st_mode),
        "owner_matches_local_user": metadata.st_uid == os.getuid(),
        "no_symlink": not stat.S_ISLNK(metadata.st_mode),
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _production_dependencies(plan: Mapping[str, Any]) -> tuple[_ProductionLocalProbes, _SubprocessRunner]:
    status = _git_output("status", "--porcelain", "--untracked-files=no")
    commit = _git_output("rev-parse", "HEAD")
    records: dict[str, dict[str, Any]] = {}
    for host in plan.get("hosts", []):
        if host.get("probe_transport") != "ssh":
            continue
        alias = host.get("ssh_identity_path_alias")
        target = host.get("ssh_target")
        if isinstance(alias, str) and isinstance(target, str) and alias not in records:
            records[alias] = _identity_record(_ssh_identity_for(target))
    return (
        _ProductionLocalProbes(
            git_dirty=status is None or bool(status.strip()),
            expected_commit=commit.decode("ascii").strip() if commit is not None else None,
            public_network_bytes=0,
            ssh_identities=records,
        ),
        _SubprocessRunner(),
    )


def _remote_request(plan: Mapping[str, Any], host: Mapping[str, Any]) -> bytes:
    request = {
        "protocol": _REMOTE_REQUEST_PROTOCOL,
        "run_id": plan.get("run_id"),
        "host_alias": host.get("alias"),
        "node_id": host.get("node_id"),
        "credential_path_alias": host.get("credential_path_alias"),
        "coordinator_port": host.get("coordinator_port"),
        "runtime": host.get("runtime"),
        "source_manifest": plan.get("source_manifest"),
        "model_assets": plan.get("model_assets"),
        "tokenizer": plan.get("tokenizer"),
        "sidecar": plan.get("sidecar"),
        "dependencies": plan.get("dependencies"),
    }
    return json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def load_safe_plan(path: str | os.PathLike[str]) -> dict[str, Any]:
    candidate = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(candidate, flags)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_SAFE_PLAN_BYTES:
                raise RunnerError("live_preflight_plan_invalid")
            raw = os.read(fd, MAX_SAFE_PLAN_BYTES + 1)
        finally:
            os.close(fd)
    except RunnerError:
        raise
    except OSError as exc:
        raise RunnerError("live_preflight_plan_unavailable") from exc
    if not raw or len(raw) > MAX_SAFE_PLAN_BYTES:
        raise RunnerError("live_preflight_plan_invalid")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise RunnerError("live_preflight_plan_invalid")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerError("live_preflight_plan_invalid") from exc
    if not isinstance(value, dict) or value.get("protocol") != SAFE_PLAN_PROTOCOL:
        raise RunnerError("live_preflight_plan_invalid")
    return value


def _block(blockers: list[dict[str, str]], code: str, host_alias: str | None = None) -> None:
    value = {"code": code}
    if host_alias is not None:
        value["host_alias"] = host_alias
    if value not in blockers:
        blockers.append(value)


def _result(plan: Mapping[str, Any], blockers: list[dict[str, str]], hosts: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(blockers, key=lambda item: (item["code"], item.get("host_alias", "")))
    return {
        "protocol": LIVE_PREFLIGHT_PROTOCOL,
        "run_id": plan.get("run_id"),
        "deployment_id": plan.get("deployment_id"),
        "preflight_ready": not ordered,
        "route_ready": False,
        "release_ready": False,
        "blockers": ordered,
        "hosts": sorted(hosts, key=lambda item: str(item.get("node_id", ""))),
    }


def _identity_path(host: Mapping[str, Any], probes: Any) -> str | None:
    alias = host.get("ssh_identity_path_alias")
    records = getattr(probes, "ssh_identities", None)
    if not isinstance(alias, str) or not isinstance(records, Mapping):
        return None
    record = records.get(alias)
    if not isinstance(record, Mapping):
        return None
    path = record.get("path")
    if (
        record.get("regular") is not True
        or record.get("owner_matches_local_user") is not True
        or record.get("no_symlink") is not True
        or record.get("mode") != "0600"
        or not isinstance(path, str)
        or not Path(path).is_absolute()
        or "\x00" in path
        or any(character in path for character in "\n\r\t")
        or ".." in Path(path).parts
    ):
        return None
    return path


def _ssh_argv(
    host: Mapping[str, Any],
    identity_path: str,
    run_id: Any,
) -> tuple[str, ...]:
    target = host.get("ssh_target")
    alias = host.get("alias")
    node_id = host.get("node_id")
    if (
        not isinstance(target, str)
        or _SSH_TARGET_RE.fullmatch(target) is None
        or not isinstance(alias, str)
        or _SEGMENT_RE.fullmatch(alias) is None
        or not isinstance(node_id, str)
        or _SEGMENT_RE.fullmatch(node_id) is None
        or not isinstance(run_id, str)
        or _SEGMENT_RE.fullmatch(run_id) is None
    ):
        raise RunnerError("live_preflight_plan_invalid")
    remote_command = (
        f'cd "$HOME/mycelium-physical-run/{run_id}/source" && '
        f"exec {_REMOTE_PROBE_MODULE_COMMAND}"
    )
    return (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-i",
        identity_path,
        "--",
        target,
        remote_command,
    )


def _local_argv(run_id: Any) -> tuple[tuple[str, ...], Path]:
    if not isinstance(run_id, str) or _SEGMENT_RE.fullmatch(run_id) is None:
        raise RunnerError("live_preflight_plan_invalid")
    return (
        (
            "/opt/homebrew/bin/python3.14",
            "-m",
            "mycelium_physical_runner.remote_probe",
            "--canonical-json",
        ),
        Path.home() / "mycelium-physical-run" / run_id / "source",
    )


def _decode_capture(capture: Any) -> Mapping[str, Any] | None:
    if (
        getattr(capture, "returncode", None) != 0
        or getattr(capture, "stderr", None) != b""
        or not isinstance(getattr(capture, "stdout", None), bytes)
    ):
        return None
    raw = capture.stdout
    if not raw or len(raw) > MAX_PROBE_BYTES or not raw.endswith(b"\n"):
        return None

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate")
            value[key] = item
        return value

    try:
        payload = json.loads(
            raw[:-1].decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
    except (UnicodeDecodeError, ValueError, TypeError):
        return None
    if raw != canonical or not isinstance(payload, Mapping):
        return None
    return payload


def _expected_map(records: Any, key: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if isinstance(record, Mapping) and isinstance(record.get(key), str):
            result[str(record[key])] = record
    return result


def _validate_payload(
    *,
    plan: Mapping[str, Any],
    host: Mapping[str, Any],
    payload: Mapping[str, Any],
    blockers: list[dict[str, str]],
) -> dict[str, Any]:
    alias = str(host.get("alias", ""))
    if (
        payload.get("protocol") != LIVE_PROBE_PROTOCOL
        or payload.get("host_alias") != alias
        or payload.get("node_id") != host.get("node_id")
        or payload.get("host_id") != host.get("host_id")
        or payload.get("boot_id") != host.get("boot_id")
    ):
        _block(blockers, "remote_probe_identity_mismatch", alias)
    unknowns = payload.get("unknowns")
    if not isinstance(unknowns, list) or unknowns:
        _block(blockers, "unknown_blocker", alias)
    if payload.get("route_ready") is not False:
        _block(blockers, "remote_probe_contract_invalid", alias)
    public_network_bytes = payload.get("public_network_bytes")
    if (
        payload.get("public_network_required") is not False
        or type(public_network_bytes) is not int
        or public_network_bytes != 0
    ):
        _block(blockers, "public_network_required", alias)

    credential = payload.get("credential_file")
    if not isinstance(credential, Mapping) or (
        credential.get("path_alias") != host.get("credential_path_alias")
        or credential.get("regular") is not True
        or credential.get("owner_matches_ssh_user") is not True
        or credential.get("no_symlink") is not True
        or credential.get("mode") != "0600"
    ):
        _block(blockers, "credential_path_invalid", alias)

    port = payload.get("port")
    if not isinstance(port, Mapping) or port.get("port") != host.get("coordinator_port"):
        _block(blockers, "remote_probe_contract_invalid", alias)
    elif port.get("available") is not True:
        _block(blockers, "unknown_blocker" if port.get("available") is None else "port_conflict", alias)
    process = payload.get("process")
    if not isinstance(process, Mapping) or process.get("conflict") is not False:
        _block(blockers, "process_conflict", alias)
    runtime = payload.get("runtime")
    if not isinstance(runtime, Mapping) or runtime.get("name") != host.get("runtime") or runtime.get("supported") is not True:
        _block(blockers, "unknown_blocker" if isinstance(runtime, Mapping) and runtime.get("supported") is None else "unsupported_runtime", alias)

    expected_sources = _expected_map(plan.get("source_manifest"), "path")
    observed_sources = _expected_map(payload.get("source_manifest"), "path")
    if set(observed_sources) != set(expected_sources) or any(
        observed_sources[path].get("digest") != expected_sources[path].get("digest")
        for path in expected_sources
    ):
        _block(blockers, "source_digest_mismatch", alias)

    expected_models = _expected_map(plan.get("model_assets"), "public_alias")
    observed_models = _expected_map(payload.get("model_assets"), "public_alias")
    if set(observed_models) != set(expected_models):
        _block(blockers, "model_blob_missing", alias)
    else:
        for model_alias, expected in expected_models.items():
            observed = observed_models[model_alias]
            if observed.get("present") is not True:
                _block(blockers, "model_blob_missing", alias)
            if observed.get("digest") != expected.get("digest"):
                _block(blockers, "model_digest_mismatch", alias)

    expected_tokenizer = plan.get("tokenizer")
    observed_tokenizer = payload.get("tokenizer")
    if not isinstance(expected_tokenizer, Mapping) or not isinstance(observed_tokenizer, Mapping):
        _block(blockers, "tokenizer_digest_mismatch", alias)
    elif observed_tokenizer.get("present") is not True or observed_tokenizer.get("digest") != expected_tokenizer.get("digest"):
        _block(blockers, "tokenizer_digest_mismatch", alias)

    expected_sidecar = plan.get("sidecar")
    observed_sidecar = payload.get("sidecar")
    if not isinstance(expected_sidecar, Mapping) or not isinstance(observed_sidecar, Mapping):
        _block(blockers, "sidecar_digest_mismatch", alias)
    else:
        if observed_sidecar.get("digest") != expected_sidecar.get("digest"):
            _block(blockers, "sidecar_digest_mismatch", alias)
        if observed_sidecar.get("identity") != expected_sidecar.get("public_alias"):
            _block(blockers, "sidecar_identity_mismatch", alias)

    expected_dependencies = plan.get("dependencies")
    observed_dependencies = payload.get("dependencies")
    if not isinstance(expected_dependencies, Mapping) or not isinstance(observed_dependencies, Mapping) or observed_dependencies.get("digest") != expected_dependencies.get("digest"):
        _block(blockers, "dependency_digest_mismatch", alias)

    return {
        "host_alias": alias,
        "node_id": host.get("node_id"),
        "host_id": payload.get("host_id"),
        "boot_id": payload.get("boot_id"),
        "observed": True,
    }


def run_live_preflight(
    plan: Mapping[str, Any],
    *,
    probes: Any | None = None,
    runner: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or plan.get("protocol") != SAFE_PLAN_PROTOCOL:
        raise RunnerError("live_preflight_plan_invalid")
    if plan.get("route_ready") is not False or plan.get("release_ready") is not False:
        raise RunnerError("live_preflight_plan_invalid")
    hosts_raw = plan.get("hosts")
    if not isinstance(hosts_raw, list) or len(hosts_raw) < 2 or not all(isinstance(host, Mapping) for host in hosts_raw):
        raise RunnerError("live_preflight_plan_invalid")
    for host in hosts_raw:
        for field in ("alias", "node_id", "host_id", "boot_id"):
            value = host.get(field)
            if not isinstance(value, str) or _SEGMENT_RE.fullmatch(value) is None:
                raise RunnerError("live_preflight_plan_invalid")
        if _RUN_SCOPED_HOST_RE.fullmatch(host["host_id"]) is None:
            raise RunnerError("live_preflight_plan_invalid")
        if _RUN_SCOPED_BOOT_RE.fullmatch(host["boot_id"]) is None:
            raise RunnerError("live_preflight_plan_invalid")
        target = host.get("ssh_target")
        if not isinstance(target, str) or _SSH_TARGET_RE.fullmatch(target) is None:
            raise RunnerError("live_preflight_plan_invalid")
        if host.get("probe_transport") not in {"local", "ssh"}:
            raise RunnerError("live_preflight_plan_invalid")
    if probes is None and runner is None:
        probes, runner = _production_dependencies(plan)
    elif probes is None or runner is None:
        raise RunnerError("live_preflight_dependencies_invalid")

    blockers: list[dict[str, str]] = []
    observations: list[dict[str, Any]] = []
    if getattr(probes, "git_dirty", None) is not False:
        _block(blockers, "git_dirty")
    expected_commit = plan.get("expected_commit")
    probe_commit = getattr(probes, "expected_commit", None)
    if expected_commit is not None and probe_commit != expected_commit:
        _block(blockers, "source_commit_mismatch")
    local_network_bytes = getattr(probes, "public_network_bytes", None)
    if type(local_network_bytes) is not int or local_network_bytes != 0:
        _block(blockers, "public_network_required")

    identities: dict[str, str] = {}
    for host in hosts_raw:
        if host.get("probe_transport") != "ssh":
            continue
        alias = str(host.get("alias", ""))
        path = _identity_path(host, probes)
        if path is None:
            _block(blockers, "ssh_identity_path_invalid", alias)
        else:
            identities[alias] = path
    if blockers:
        return _result(plan, blockers, observations)

    for host in hosts_raw:
        alias = str(host.get("alias", ""))
        if host.get("probe_transport") == "local":
            argv, cwd = _local_argv(plan.get("run_id"))
        else:
            argv = _ssh_argv(host, identities[alias], plan.get("run_id"))
            cwd = None
        try:
            capture = runner.run(
                argv,
                timeout_seconds=30.0,
                stdin_bytes=_remote_request(plan, host),
                cwd=cwd,
            )
        except Exception:
            _block(blockers, "remote_probe_failed", alias)
            continue
        payload = _decode_capture(capture)
        if payload is None:
            _block(blockers, "remote_probe_failed", alias)
            continue
        observations.append(
            _validate_payload(plan=plan, host=host, payload=payload, blockers=blockers)
        )

    host_ids = [item.get("host_id") for item in observations]
    boot_ids = [item.get("boot_id") for item in observations]
    if len(host_ids) != len(set(host_ids)):
        _block(blockers, "duplicate_host_id")
    if len(boot_ids) != len(set(boot_ids)):
        _block(blockers, "duplicate_boot_id")
    return _result(plan, blockers, observations)


__all__ = ["LIVE_PREFLIGHT_PROTOCOL", "LIVE_PROBE_PROTOCOL", "load_safe_plan", "run_live_preflight"]
