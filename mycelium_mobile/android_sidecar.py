"""Software-only Android sidecar packaging for the Termux deployment lane."""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
from typing import Any, Protocol, Sequence

PACKAGE_PROTOCOL = "mycelium.android_sidecar_package.v1"
ANDROID_TARGET = "aarch64-linux-android"
MAX_SIDECAR_BYTES = 256 * 1024 * 1024
CLAIM_BOUNDARY = (
    "software-only Android sidecar package; no Pixel execution, physical "
    "qualification, route readiness, or release readiness"
)
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)


class AndroidSidecarError(RuntimeError):
    """Stable package validation or construction error."""


@dataclass(frozen=True, slots=True)
class AndroidSidecarPackage:
    root: Path
    binary_path: Path
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class TermuxNodeLaunch:
    argv: tuple[str, ...]
    cwd: str


@dataclass(frozen=True, slots=True)
class TermuxNodeHandle:
    pid: int
    launch: TermuxNodeLaunch


class TermuxBridgePort(Protocol):
    def health(self) -> dict[str, Any]: ...

    def unauthenticated_rejected(self) -> bool: ...

    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_seconds: float = 10.0,
        detach: bool = False,
    ) -> dict[str, Any]: ...


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise AndroidSidecarError(code)


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_mode,
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _read_aarch64_elf(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AndroidSidecarError("android_sidecar_binary_invalid") from exc
    try:
        before = os.fstat(descriptor)
        _require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and 64 <= before.st_size <= MAX_SIDECAR_BYTES,
            "android_sidecar_binary_invalid",
        )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            _require(bool(chunk), "android_sidecar_binary_invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        _require(
            _fingerprint(os.fstat(descriptor)) == _fingerprint(before),
            "android_sidecar_binary_changed",
        )
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    _require(
        payload[:4] == b"\x7fELF"
        and payload[4] == 2
        and payload[5] == 1
        and payload[6] == 1
        and int.from_bytes(payload[16:18], "little") in {2, 3}
        and int.from_bytes(payload[18:20], "little") == 183,
        "android_sidecar_architecture_invalid",
    )
    return payload


def _write_new(path: Path, payload: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            _require(written > 0, "android_sidecar_package_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def build_android_sidecar_package(
    *,
    sidecar_binary: Path,
    output_root: Path,
    source_commit: str,
    cargo_lock_digest: str,
    android_api_level: int = 21,
) -> AndroidSidecarPackage:
    """Validate and package one Android AArch64 sidecar without claiming execution."""
    _require(
        type(android_api_level) is int and 21 <= android_api_level <= 35,
        "android_api_level_invalid",
    )
    _require(
        isinstance(sidecar_binary, Path) and isinstance(output_root, Path),
        "android_sidecar_path_invalid",
    )
    _require(
        isinstance(source_commit, str)
        and _SOURCE_COMMIT.fullmatch(source_commit) is not None,
        "android_sidecar_source_commit_invalid",
    )
    _require(
        isinstance(cargo_lock_digest, str)
        and _SHA256_DIGEST.fullmatch(cargo_lock_digest) is not None,
        "android_sidecar_cargo_lock_digest_invalid",
    )
    payload = _read_aarch64_elf(sidecar_binary)
    try:
        output_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError as exc:
        raise AndroidSidecarError("android_sidecar_output_root_invalid") from exc
    output_identity = output_root.lstat()
    try:
        output_root.chmod(0o700)
        binary_root = output_root / "bin"
        binary_root.mkdir(mode=0o700)
        binary_root.chmod(0o700)
        packaged_binary = binary_root / "mycelium-iroh-sidecar"
        manifest_path = output_root / "manifest.json"
        _write_new(packaged_binary, payload, 0o700)
        manifest = {
            "android_api_level": android_api_level,
            "binary": {
                "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "path": "bin/mycelium-iroh-sidecar",
                "size_bytes": len(payload),
            },
            "claim_boundary": CLAIM_BOUNDARY,
            "protocol": PACKAGE_PROTOCOL,
            "release_ready": False,
            "route_ready": False,
            "source": {
                "cargo_lock_digest": cargo_lock_digest,
                "commit": source_commit,
            },
            "target": ANDROID_TARGET,
        }
        manifest_bytes = (
            json.dumps(
                manifest,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
        _write_new(manifest_path, manifest_bytes, 0o600)
        return AndroidSidecarPackage(
            root=output_root,
            binary_path=packaged_binary,
            manifest_path=manifest_path,
        )
    except BaseException:
        try:
            current = output_root.lstat()
        except OSError:
            current = None
        if (
            current is not None
            and stat.S_ISDIR(current.st_mode)
            and not stat.S_ISLNK(current.st_mode)
            and (current.st_dev, current.st_ino)
            == (output_identity.st_dev, output_identity.st_ino)
        ):
            shutil.rmtree(output_root)
        raise


_TERMUX_PREFIX = PurePosixPath("/data/data/com.termux/files")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)


def _termux_path(value: PurePosixPath, code: str) -> PurePosixPath:
    _require(isinstance(value, PurePosixPath), code)
    lexical = str(value)
    _require(
        value.is_absolute()
        and value != _TERMUX_PREFIX
        and value.is_relative_to(_TERMUX_PREFIX)
        and ".." not in value.parts
        and all(0x20 < ord(character) < 0x7F for character in lexical),
        code,
    )
    return value


def _identifier(value: str, code: str) -> str:
    _require(isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None, code)
    return value


def build_termux_node_launch(
    *,
    python_executable: PurePosixPath,
    staged_repo_root: PurePosixPath,
    package_root: PurePosixPath,
    run_id: str,
    deployment_id: str,
    node_id: str,
    artifact_root: PurePosixPath,
    socket_root: PurePosixPath,
    endpoint_secret_file: PurePosixPath | None = None,
    command_timeout_seconds: float = 30.0,
) -> TermuxNodeLaunch:
    """Build public process argv while keeping bridge credentials out-of-band."""
    run = _identifier(run_id, "termux_run_id_invalid")
    deployment = _identifier(deployment_id, "termux_deployment_id_invalid")
    node = _identifier(node_id, "termux_node_id_invalid")
    python = _termux_path(python_executable, "termux_python_invalid")
    repo = _termux_path(staged_repo_root, "termux_repo_root_invalid")
    package = _termux_path(package_root, "termux_package_root_invalid")
    artifacts = _termux_path(artifact_root, "termux_artifact_root_invalid")
    sockets = _termux_path(socket_root, "termux_socket_root_invalid")
    endpoint_secret = (
        None
        if endpoint_secret_file is None
        else _termux_path(endpoint_secret_file, "termux_endpoint_secret_file_invalid")
    )
    _require(
        run in artifacts.parts
        and run in sockets.parts
        and (endpoint_secret is None or run in endpoint_secret.parts),
        "termux_run_path_binding_invalid",
    )
    _require(
        not isinstance(command_timeout_seconds, bool)
        and isinstance(command_timeout_seconds, (int, float))
        and math.isfinite(float(command_timeout_seconds))
        and 0.1 <= float(command_timeout_seconds) <= 300.0,
        "termux_command_timeout_invalid",
    )
    argv = [
        str(python),
        str(repo / "physical_inference_node.py"),
        "--run-id",
        run,
        "--deployment-id",
        deployment,
        "--node-id",
        node,
        "--artifact-root",
        str(artifacts),
        "--socket-root",
        str(sockets),
        "--sidecar-binary",
        str(package / "bin/mycelium-iroh-sidecar"),
    ]
    if endpoint_secret is not None:
        argv.extend(("--endpoint-secret-file", str(endpoint_secret)))
    argv.extend(("--command-timeout", str(float(command_timeout_seconds))))
    return TermuxNodeLaunch(argv=tuple(argv), cwd=str(repo))


def launch_termux_node(
    *,
    bridge: TermuxBridgePort,
    launch: TermuxNodeLaunch,
) -> TermuxNodeHandle:
    """Launch through the authenticated argv-only bridge after negative auth proof."""
    _require(isinstance(launch, TermuxNodeLaunch), "termux_launch_invalid")
    health = bridge.health()
    _require(
        isinstance(health, dict)
        and health.get("status") == "ok"
        and health.get("allow_shell") is False
        and health.get("claim") == "authenticated argv command bridge for Termux",
        "termux_bridge_health_invalid",
    )
    _require(
        bridge.unauthenticated_rejected() is True,
        "termux_bridge_auth_boundary_invalid",
    )
    result = bridge.run_argv(
        launch.argv,
        cwd=launch.cwd,
        timeout_seconds=10.0,
        detach=True,
    )
    pid = result.get("pid") if isinstance(result, dict) else None
    _require(
        isinstance(result, dict)
        and result.get("shell") is False
        and result.get("detached") is True
        and type(pid) is int,
        "termux_node_launch_invalid",
    )
    if type(pid) is not int or pid <= 1:
        raise AndroidSidecarError("termux_node_launch_invalid")
    handle = TermuxNodeHandle(pid=pid, launch=launch)
    _require(
        _probe_termux_process(bridge, handle) == "expected",
        "termux_node_process_identity_invalid",
    )
    return handle


def _run_command(
    bridge: TermuxBridgePort,
    argv: tuple[str, ...],
) -> dict[str, Any]:
    result = bridge.run_argv(
        argv,
        timeout_seconds=5.0,
        detach=False,
    )
    code = result.get("exit_code") if isinstance(result, dict) else None
    _require(
        isinstance(result, dict)
        and result.get("shell") is False
        and type(code) is int
        and isinstance(result.get("stdout"), str)
        and isinstance(result.get("stderr"), str),
        "termux_cleanup_response_invalid",
    )
    return result


def _run_exit_code(
    bridge: TermuxBridgePort,
    argv: tuple[str, ...],
) -> int:
    result = _run_command(bridge, argv)
    code = result["exit_code"]
    if type(code) is not int:
        raise AndroidSidecarError("termux_cleanup_response_invalid")
    return code


def _probe_termux_process(
    bridge: TermuxBridgePort,
    handle: TermuxNodeHandle,
) -> str:
    cat = "/data/data/com.termux/files/usr/bin/cat"
    result = _run_command(bridge, (cat, f"/proc/{handle.pid}/cmdline"))
    if result["exit_code"] != 0:
        return "absent"
    expected = "\0".join(handle.launch.argv) + "\0"
    if result["stdout"] == expected:
        return "expected"
    return "reused"


def cleanup_termux_node(
    *,
    bridge: TermuxBridgePort,
    handle: TermuxNodeHandle,
    poll_attempts: int = 10,
    poll_interval_seconds: float = 0.1,
) -> dict[str, Any]:
    """Stop one bridge-launched PID with a bounded TERM-to-KILL fallback."""
    _require(isinstance(handle, TermuxNodeHandle), "termux_node_handle_invalid")
    _require(
        type(poll_attempts) is int and 1 <= poll_attempts <= 50,
        "termux_cleanup_poll_attempts_invalid",
    )
    _require(
        not isinstance(poll_interval_seconds, bool)
        and isinstance(poll_interval_seconds, (int, float))
        and math.isfinite(float(poll_interval_seconds))
        and 0.0 <= float(poll_interval_seconds) <= 1.0,
        "termux_cleanup_poll_interval_invalid",
    )
    kill = "/data/data/com.termux/files/usr/bin/kill"
    pid = str(handle.pid)
    initial_status = _probe_termux_process(bridge, handle)
    if initial_status != "expected":
        return {
            "forced": False,
            "graceful": True,
            "pid_reused": initial_status == "reused",
            "physical_execution": False,
            "process_absent": True,
            "protocol": "mycelium.termux_node_cleanup.v1",
            "release_ready": False,
            "route_ready": False,
        }
    _require(
        _run_exit_code(bridge, (kill, "-TERM", pid)) == 0,
        "termux_node_term_failed",
    )
    forced = False
    process_absent = False
    pid_reused = False
    for attempt in range(poll_attempts):
        status = _probe_termux_process(bridge, handle)
        if status != "expected":
            process_absent = True
            pid_reused = status == "reused"
            break
        if attempt + 1 < poll_attempts and poll_interval_seconds:
            time.sleep(float(poll_interval_seconds))
    if not process_absent:
        forced = True
        _require(
            _run_exit_code(bridge, (kill, "-KILL", pid)) == 0,
            "termux_node_kill_failed",
        )
        final_status = _probe_termux_process(bridge, handle)
        process_absent = final_status != "expected"
        pid_reused = final_status == "reused"
    _require(process_absent, "termux_node_cleanup_failed")
    return {
        "forced": forced,
        "graceful": not forced,
        "pid_reused": pid_reused,
        "physical_execution": False,
        "process_absent": True,
        "protocol": "mycelium.termux_node_cleanup.v1",
        "release_ready": False,
        "route_ready": False,
    }
