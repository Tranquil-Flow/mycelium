# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_remote_probe_measures_staged_artifacts_and_private_runtime_state(tmp_path: Path) -> None:
    from mycelium_physical_runner.remote_probe import (
        derive_run_scoped_identity,
        probe_request,
    )

    run_id = "run-w8-001"
    root = tmp_path / "mycelium-physical-run" / run_id
    files = {
        "source/runtime_loader.py": b"source\n",
        "model/model.safetensors": b"model\n",
        "tokenizer/gpt2-tokenizer": b"tokenizer\n",
        "sidecar/mycelium-iroh-sidecar": b"sidecar\n",
        "dependencies/python-lock-w8": b"deps\n",
        "identities/node-0-endpoint-key": b"private-key-material\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    os.chmod(root / "identities/node-0-endpoint-key", 0o600)

    request: dict[str, Any] = {
        "protocol": "mycelium.physical_runner_remote_probe_request.v1",
        "run_id": run_id,
        "host_alias": "m4pro",
        "node_id": "node-0",
        "credential_path_alias": "node-0-endpoint-key",
        "coordinator_port": 43127,
        "runtime": "mlx-mac-arm64",
        "source_manifest": [
            {"path": "runtime_loader.py", "digest": _digest(root / "source/runtime_loader.py")}
        ],
        "model_assets": [
            {
                "public_alias": "openai-community/gpt2:model.safetensors",
                "digest": _digest(root / "model/model.safetensors"),
                "size_bytes": len(files["model/model.safetensors"]),
            }
        ],
        "tokenizer": {
            "public_alias": "openai-community/gpt2-tokenizer",
            "digest": _digest(root / "tokenizer/gpt2-tokenizer"),
        },
        "sidecar": {
            "public_alias": "mycelium-iroh-sidecar",
            "digest": _digest(root / "sidecar/mycelium-iroh-sidecar"),
        },
        "dependencies": {
            "public_alias": "python-lock-w8",
            "digest": _digest(root / "dependencies/python-lock-w8"),
        },
    }

    result = probe_request(
        request,
        home=tmp_path,
        host_id="host-m4pro",
        boot_id="boot-m4pro",
        runtime_supported=True,
        port_available=True,
        process_conflict=False,
    )

    assert result["protocol"] == "mycelium.physical_runner_live_probe.v1"
    assert result["host_alias"] == "m4pro"
    assert result["node_id"] == "node-0"
    expected_host_id, expected_boot_id = derive_run_scoped_identity(
        run_id=run_id,
        observed_host_id="host-m4pro",
        observed_boot_id="boot-m4pro",
    )
    assert result["host_id"] == expected_host_id
    assert result["boot_id"] == expected_boot_id
    assert result["unknowns"] == []
    assert result["route_ready"] is False
    assert result["public_network_required"] is False
    assert type(result["public_network_bytes"]) is int
    assert result["public_network_bytes"] == 0
    assert result["credential_file"] == {
        "path_alias": "node-0-endpoint-key",
        "regular": True,
        "owner_matches_ssh_user": True,
        "no_symlink": True,
        "mode": "0600",
    }
    assert result["source_manifest"] == request["source_manifest"]
    assert result["model_assets"][0]["present"] is True
    assert result["model_assets"][0]["digest"] == request["model_assets"][0]["digest"]
    assert result["tokenizer"] == {"digest": request["tokenizer"]["digest"], "present": True}
    assert result["sidecar"]["digest"] == request["sidecar"]["digest"]
    assert result["sidecar"]["identity"] == "mycelium-iroh-sidecar"
    assert result["dependencies"] == {"digest": request["dependencies"]["digest"]}
    assert "private-key-material" not in repr(result)
    assert "host-m4pro" not in repr(result)
    assert "boot-m4pro" not in repr(result)


def test_run_scoped_host_identity_is_deterministic_but_not_cross_run_linkable() -> None:
    from mycelium_physical_runner.remote_probe import derive_run_scoped_identity

    observed_host_id = "raw-platform-uuid"
    observed_boot_id = "raw-boot-identity"
    first = derive_run_scoped_identity(
        run_id="run-w8-001",
        observed_host_id=observed_host_id,
        observed_boot_id=observed_boot_id,
    )
    repeated = derive_run_scoped_identity(
        run_id="run-w8-001",
        observed_host_id=observed_host_id,
        observed_boot_id=observed_boot_id,
    )
    next_run = derive_run_scoped_identity(
        run_id="run-w8-002",
        observed_host_id=observed_host_id,
        observed_boot_id=observed_boot_id,
    )

    assert first == repeated
    assert first != next_run
    assert first[0].startswith("host-")
    assert first[1].startswith("boot-")
    assert observed_host_id not in repr(first)
    assert observed_boot_id not in repr(first)


def test_remote_probe_supports_numpy_on_linux_x86_64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mycelium_physical_runner import remote_probe

    monkeypatch.setattr(remote_probe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(remote_probe.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        remote_probe,
        "_module_available",
        lambda name: name == "numpy",
        raising=False,
    )

    assert remote_probe._runtime_supported("numpy-linux-x86_64") is True


@pytest.mark.parametrize(
    ("system", "machine", "dependency_available"),
    [
        ("Darwin", "arm64", True),
        ("Linux", "arm64", True),
        ("Linux", "x86_64", False),
    ],
)
def test_remote_probe_rejects_numpy_linux_runtime_when_platform_or_dependency_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    dependency_available: bool,
) -> None:
    from mycelium_physical_runner import remote_probe

    monkeypatch.setattr(remote_probe.platform, "system", lambda: system)
    monkeypatch.setattr(remote_probe.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        remote_probe,
        "_module_available",
        lambda _name: dependency_available,
        raising=False,
    )

    assert remote_probe._runtime_supported("numpy-linux-x86_64") is False


def test_remote_probe_rejects_path_traversal_aliases(tmp_path: Path) -> None:
    from mycelium_physical_runner.errors import RunnerError
    from mycelium_physical_runner.remote_probe import probe_request

    request = {
        "protocol": "mycelium.physical_runner_remote_probe_request.v1",
        "run_id": "run-w8-001",
        "host_alias": "m4pro",
        "node_id": "node-0",
        "credential_path_alias": "../private-key",
        "coordinator_port": 43127,
        "runtime": "mlx-mac-arm64",
        "source_manifest": [],
        "model_assets": [],
        "tokenizer": {"public_alias": "tokenizer", "digest": "sha256:" + "a" * 64},
        "sidecar": {"public_alias": "sidecar", "digest": "sha256:" + "b" * 64},
        "dependencies": {"public_alias": "deps", "digest": "sha256:" + "c" * 64},
    }
    try:
        probe_request(
            request,
            home=tmp_path,
            host_id="host-m4pro",
            boot_id="boot-m4pro",
            runtime_supported=True,
            port_available=True,
            process_conflict=False,
        )
    except RunnerError as exc:
        assert exc.code == "remote_probe_request_invalid"
    else:
        raise AssertionError("path traversal alias accepted")


def _fake_sysctl(values: dict[str, object]):
    import subprocess as _subprocess

    def run(argv, **_kwargs):
        name = argv[-1]
        value = values.get(name)
        if callable(value):
            value = value()
        if value is None:
            return _subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return _subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")

    return run


def test_darwin_boot_identity_survives_a_wall_clock_step(monkeypatch) -> None:
    from mycelium_physical_runner import remote_probe

    boottimes = iter(
        (
            "{ sec = 1791001291, usec = 757260 } Sat Oct  3 06:21:31 2026",
            "{ sec = 1791001291, usec = 845113 } Sat Oct  3 06:21:31 2026",
        )
    )
    monkeypatch.setattr(remote_probe.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        remote_probe.subprocess,
        "run",
        _fake_sysctl(
            {
                "kern.bootsessionuuid": "9A98D86A-02FA-4F54-A383-3F0F36ACA7DC",
                "kern.boottime": lambda: next(boottimes),
            }
        ),
    )

    assert remote_probe._boot_identity("host-a") == remote_probe._boot_identity("host-a")


def test_darwin_boot_identity_falls_back_to_boottime(monkeypatch) -> None:
    from mycelium_physical_runner import remote_probe

    monkeypatch.setattr(remote_probe.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        remote_probe.subprocess,
        "run",
        _fake_sysctl({"kern.bootsessionuuid": None, "kern.boottime": "{ sec = 1 }"}),
    )
    fallback = remote_probe._boot_identity("host-a")
    monkeypatch.setattr(
        remote_probe.subprocess, "run", _fake_sysctl({"kern.bootsessionuuid": None, "kern.boottime": None})
    )

    assert fallback.startswith("boot-")
    assert fallback != remote_probe._boot_identity("host-a")


def test_route_builder_identity_program_matches_local_preflight() -> None:
    import subprocess
    import sys

    from scripts.build_qwen_live_route import _remote_identity_program
    from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity

    run_id = "run-identity-parity"
    completed = subprocess.run(
        (sys.executable, "-c", _remote_identity_program(run_id)),
        capture_output=True,
        text=True,
        timeout=30.0,
        check=True,
    )

    assert tuple(json.loads(completed.stdout)) == derive_local_run_scoped_identity(run_id)


def test_numpy_windows_runtime_is_supported_only_on_windows_x86_64(monkeypatch) -> None:
    from mycelium_physical_runner import remote_probe

    monkeypatch.setattr(remote_probe.platform, "system", lambda: "Windows")
    monkeypatch.setattr(remote_probe.platform, "machine", lambda: "AMD64")
    assert remote_probe._runtime_supported("numpy-windows-x86_64") is True
    monkeypatch.setattr(remote_probe.platform, "machine", lambda: "ARM64")
    assert remote_probe._runtime_supported("numpy-windows-x86_64") is False
    monkeypatch.setattr(remote_probe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(remote_probe.platform, "machine", lambda: "x86_64")
    assert remote_probe._runtime_supported("numpy-windows-x86_64") is False
