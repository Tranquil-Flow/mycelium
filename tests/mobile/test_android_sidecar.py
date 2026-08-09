from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

import pytest


def _aarch64_elf() -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # little endian
    header[6] = 1  # ELF version
    header[16:18] = (3).to_bytes(2, "little")  # ET_DYN / PIE
    header[18:20] = (183).to_bytes(2, "little")  # EM_AARCH64
    header[20:24] = (1).to_bytes(4, "little")
    return bytes(header) + b"mycelium-android-sidecar-test"


def test_android_sidecar_package_binds_aarch64_elf_digest_and_permissions(
    tmp_path: Path,
) -> None:
    from mycelium_mobile.android_sidecar import build_android_sidecar_package

    source = tmp_path / "mycelium-iroh-sidecar"
    payload = _aarch64_elf()
    source.write_bytes(payload)
    source.chmod(0o700)

    package = build_android_sidecar_package(
        sidecar_binary=source,
        output_root=tmp_path / "package",
        android_api_level=21,
        source_commit="a" * 40,
        cargo_lock_digest="sha256:" + "b" * 64,
    )

    manifest = json.loads(package.manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "android_api_level": 21,
        "binary": {
            "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "path": "bin/mycelium-iroh-sidecar",
            "size_bytes": len(payload),
        },
        "claim_boundary": (
            "software-only Android sidecar package; no Pixel execution, physical "
            "qualification, route readiness, or release readiness"
        ),
        "protocol": "mycelium.android_sidecar_package.v1",
        "release_ready": False,
        "route_ready": False,
        "source": {
            "cargo_lock_digest": "sha256:" + "b" * 64,
            "commit": "a" * 40,
        },
        "target": "aarch64-linux-android",
    }
    assert package.binary_path.read_bytes() == payload
    assert stat.S_IMODE(package.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(package.binary_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(package.manifest_path.stat().st_mode) == 0o600


def test_termux_launch_argv_contains_paths_and_public_identity_but_no_credentials() -> None:
    from mycelium_mobile.android_sidecar import build_termux_node_launch

    prefix = PurePosixPath("/data/data/com.termux/files")
    launch = build_termux_node_launch(
        python_executable=prefix / "usr/bin/python",
        staged_repo_root=prefix / "home/mycelium/releases/d42a692",
        package_root=prefix / "home/mycelium/packages/sidecar-d42a692",
        run_id="m9-03-pixel-run",
        deployment_id="m9-03-pixel-deployment",
        node_id="pixel-8-pro",
        artifact_root=prefix / "home/mycelium/runs/m9-03-pixel-run/artifacts",
        socket_root=prefix / "usr/tmp/m/m9-03-pixel-run",
        endpoint_secret_file=(
            prefix / "home/mycelium/runs/m9-03-pixel-run/private/endpoint.key"
        ),
        command_timeout_seconds=30.0,
    )

    assert launch.cwd == str(prefix / "home/mycelium/releases/d42a692")
    assert launch.argv == (
        str(prefix / "usr/bin/python"),
        str(prefix / "home/mycelium/releases/d42a692/physical_inference_node.py"),
        "--run-id",
        "m9-03-pixel-run",
        "--deployment-id",
        "m9-03-pixel-deployment",
        "--node-id",
        "pixel-8-pro",
        "--artifact-root",
        str(prefix / "home/mycelium/runs/m9-03-pixel-run/artifacts"),
        "--socket-root",
        str(prefix / "usr/tmp/m/m9-03-pixel-run"),
        "--sidecar-binary",
        str(prefix / "home/mycelium/packages/sidecar-d42a692/bin/mycelium-iroh-sidecar"),
        "--endpoint-secret-file",
        str(prefix / "home/mycelium/runs/m9-03-pixel-run/private/endpoint.key"),
        "--command-timeout",
        "30.0",
    )
    assert "--sidecar-local-only" not in launch.argv
    rendered = "\n".join(launch.argv).lower()
    assert "authorization" not in rendered
    assert "bearer" not in rendered
    assert "token" not in rendered


def test_termux_launch_rejects_cross_run_artifact_paths() -> None:
    from mycelium_mobile.android_sidecar import (
        AndroidSidecarError,
        build_termux_node_launch,
    )

    prefix = PurePosixPath("/data/data/com.termux/files")
    with pytest.raises(AndroidSidecarError):
        build_termux_node_launch(
            python_executable=prefix / "usr/bin/python",
            staged_repo_root=prefix / "home/mycelium/releases/d42a692",
            package_root=prefix / "home/mycelium/packages/sidecar-d42a692",
            run_id="m9-03-pixel-run",
            deployment_id="m9-03-pixel-deployment",
            node_id="pixel-8-pro",
            artifact_root=prefix / "home/mycelium/runs/other-run/artifacts",
            socket_root=prefix / "usr/tmp/m/m9-03-pixel-run",
        )


class _RecordingBridge:
    def __init__(self) -> None:
        self.health_calls = 0
        self.unauthenticated_calls = 0
        self.run_calls: list[dict[str, Any]] = []
        self.active_cmdline: str | None = None
        self.stop_on_term = True

    def health(self) -> dict[str, Any]:
        self.health_calls += 1
        return {
            "allow_shell": False,
            "claim": "authenticated argv command bridge for Termux",
            "status": "ok",
        }

    def unauthenticated_rejected(self) -> bool:
        self.unauthenticated_calls += 1
        return True

    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_seconds: float = 10.0,
        detach: bool = False,
    ) -> dict[str, Any]:
        call = {
            "argv": tuple(argv),
            "cwd": cwd,
            "timeout_seconds": timeout_seconds,
            "detach": detach,
        }
        self.run_calls.append(call)
        if detach:
            self.active_cmdline = "\0".join(argv) + "\0"
            return {"detached": True, "pid": 4321, "shell": False}
        command = tuple(argv)
        if command[:1] == ("/data/data/com.termux/files/usr/bin/cat",):
            return {
                "exit_code": 0 if self.active_cmdline is not None else 1,
                "stdout": self.active_cmdline or "",
                "stderr": "",
                "shell": False,
            }
        if command[1:2] == ("-TERM",) and self.stop_on_term:
            self.active_cmdline = None
        if command[1:2] == ("-KILL",):
            self.active_cmdline = None
        return {"exit_code": 0, "stdout": "", "stderr": "", "shell": False}


def _termux_launch():
    from mycelium_mobile.android_sidecar import build_termux_node_launch

    prefix = PurePosixPath("/data/data/com.termux/files")
    return build_termux_node_launch(
        python_executable=prefix / "usr/bin/python",
        staged_repo_root=prefix / "home/mycelium/releases/d42a692",
        package_root=prefix / "home/mycelium/packages/sidecar-d42a692",
        run_id="m9-03-pixel-run",
        deployment_id="m9-03-pixel-deployment",
        node_id="pixel-8-pro",
        artifact_root=prefix / "home/mycelium/runs/m9-03-pixel-run/artifacts",
        socket_root=prefix / "usr/tmp/m/m9-03-pixel-run",
    )


def test_authenticated_bridge_launch_and_graceful_process_cleanup() -> None:
    from mycelium_mobile.android_sidecar import (
        cleanup_termux_node,
        launch_termux_node,
    )

    bridge = _RecordingBridge()
    launch = _termux_launch()
    handle = launch_termux_node(bridge=bridge, launch=launch)
    report = cleanup_termux_node(bridge=bridge, handle=handle)

    assert bridge.health_calls == 1
    assert bridge.unauthenticated_calls == 1
    assert bridge.run_calls == [
        {
            "argv": launch.argv,
            "cwd": launch.cwd,
            "timeout_seconds": 10.0,
            "detach": True,
        },
        {
            "argv": (
                "/data/data/com.termux/files/usr/bin/cat",
                "/proc/4321/cmdline",
            ),
            "cwd": None,
            "timeout_seconds": 5.0,
            "detach": False,
        },
        {
            "argv": (
                "/data/data/com.termux/files/usr/bin/cat",
                "/proc/4321/cmdline",
            ),
            "cwd": None,
            "timeout_seconds": 5.0,
            "detach": False,
        },
        {
            "argv": (
                "/data/data/com.termux/files/usr/bin/kill",
                "-TERM",
                "4321",
            ),
            "cwd": None,
            "timeout_seconds": 5.0,
            "detach": False,
        },
        {
            "argv": (
                "/data/data/com.termux/files/usr/bin/cat",
                "/proc/4321/cmdline",
            ),
            "cwd": None,
            "timeout_seconds": 5.0,
            "detach": False,
        },
    ]
    assert report == {
        "forced": False,
        "graceful": True,
        "pid_reused": False,
        "physical_execution": False,
        "process_absent": True,
        "protocol": "mycelium.termux_node_cleanup.v1",
        "release_ready": False,
        "route_ready": False,
    }


def test_launch_rejects_health_without_exact_argv_only_boundary() -> None:
    from mycelium_mobile.android_sidecar import AndroidSidecarError, launch_termux_node

    bridge = _RecordingBridge()
    bridge.health = lambda: {"status": "ok"}  # type: ignore[method-assign]

    with pytest.raises(AndroidSidecarError):
        launch_termux_node(bridge=bridge, launch=_termux_launch())

    assert bridge.run_calls == []


@pytest.mark.parametrize(
    ("mutation", "source_commit", "cargo_lock_digest", "code"),
    [
        pytest.param("x86", "a" * 40, "sha256:" + "b" * 64, "architecture", id="x86"),
        pytest.param("none", "A" * 40, "sha256:" + "b" * 64, "source", id="commit"),
        pytest.param("none", "a" * 40, "sha256:" + "B" * 64, "lock", id="lock"),
    ],
)
def test_invalid_binary_or_provenance_fails_before_output(
    tmp_path: Path,
    mutation: str,
    source_commit: str,
    cargo_lock_digest: str,
    code: str,
) -> None:
    from mycelium_mobile.android_sidecar import (
        AndroidSidecarError,
        build_android_sidecar_package,
    )

    payload = bytearray(_aarch64_elf())
    if mutation == "x86":
        payload[18:20] = (62).to_bytes(2, "little")
    source = tmp_path / "sidecar"
    source.write_bytes(payload)
    output = tmp_path / "package"

    with pytest.raises(AndroidSidecarError):
        build_android_sidecar_package(
            sidecar_binary=source,
            output_root=output,
            android_api_level=21,
            source_commit=source_commit,
            cargo_lock_digest=cargo_lock_digest,
        )

    assert code
    assert not output.exists()


def test_existing_output_root_is_rejected_without_deleting_caller_data(
    tmp_path: Path,
) -> None:
    from mycelium_mobile.android_sidecar import (
        AndroidSidecarError,
        build_android_sidecar_package,
    )

    source = tmp_path / "sidecar"
    source.write_bytes(_aarch64_elf())
    output = tmp_path / "package"
    output.mkdir()
    sentinel = output / "keep-me"
    sentinel.write_text("caller-owned\n", encoding="utf-8")

    with pytest.raises((AndroidSidecarError, FileExistsError)):
        build_android_sidecar_package(
            sidecar_binary=source,
            output_root=output,
            android_api_level=21,
            source_commit="a" * 40,
            cargo_lock_digest="sha256:" + "b" * 64,
        )

    assert sentinel.read_text(encoding="utf-8") == "caller-owned\n"


class _StubbornBridge(_RecordingBridge):
    def __init__(self) -> None:
        super().__init__()
        self.stop_on_term = False

    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_seconds: float = 10.0,
        detach: bool = False,
    ) -> dict[str, Any]:
        result = super().run_argv(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            detach=detach,
        )
        return result


def test_cleanup_uses_bounded_kill_fallback_when_term_does_not_stop_node() -> None:
    from mycelium_mobile.android_sidecar import (
        cleanup_termux_node,
        launch_termux_node,
    )

    bridge = _StubbornBridge()
    handle = launch_termux_node(bridge=bridge, launch=_termux_launch())
    report = cleanup_termux_node(
        bridge=bridge,
        handle=handle,
        poll_attempts=2,
        poll_interval_seconds=0.0,
    )

    assert report["forced"] is True
    assert report["graceful"] is False
    assert report["process_absent"] is True
    cleanup_argv = [call["argv"] for call in bridge.run_calls[2:]]
    assert cleanup_argv[-2:] == [
        ("/data/data/com.termux/files/usr/bin/kill", "-KILL", "4321"),
        ("/data/data/com.termux/files/usr/bin/cat", "/proc/4321/cmdline"),
    ]


def test_cleanup_never_signals_a_reused_pid() -> None:
    from mycelium_mobile.android_sidecar import cleanup_termux_node, launch_termux_node

    bridge = _RecordingBridge()
    handle = launch_termux_node(bridge=bridge, launch=_termux_launch())
    bridge.active_cmdline = "/data/data/com.termux/files/usr/bin/unrelated\0"

    report = cleanup_termux_node(bridge=bridge, handle=handle)

    assert report["pid_reused"] is True
    assert report["process_absent"] is True
    cleanup_argv = [call["argv"] for call in bridge.run_calls[2:]]
    assert cleanup_argv == [
        ("/data/data/com.termux/files/usr/bin/cat", "/proc/4321/cmdline")
    ]
