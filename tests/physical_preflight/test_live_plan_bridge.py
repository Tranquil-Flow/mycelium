"""RED tests for W8 live preflight bridge safety.

The live bridge is allowed to use only injected probes and a bounded argv-only
command runner.  These tests never open SSH, launch processes, bind ports, load
models, or touch a network; runner responses are deterministic fake captures.
"""

from __future__ import annotations

import copy
import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest


def _sha(label: str) -> str:
    return "sha256:" + label[0] * 64


COMMIT = "1" * 40
SHA_SOURCE_A = _sha("a")
SHA_SOURCE_B = _sha("b")
SHA_MODEL_BLOB = _sha("c")
SHA_TOKENIZER = _sha("d")
SHA_SIDECAR = _sha("e")
SHA_DEPENDENCIES = _sha("f")
HOST_M4PRO = "host-" + "1" * 32
BOOT_M4PRO = "boot-" + "2" * 32
HOST_LAPTOP = "host-" + "3" * 32
BOOT_LAPTOP = "boot-" + "4" * 32
SECRET_VALUE = "sk-" + "S" * 36
PRIVATE_CACHE = "/Users/operator/Library/Caches/huggingface/hub/private-snapshot"
PRIVATE_SSH_IDENTITY = "/Users/operator/.ssh/id_ed25519_mycelium"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


@dataclass(frozen=True)
class FakeCommandCapture:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class RecordingRunner:
    """Fail-fast fake that proves live preflight uses argv-only execution."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[tuple[str, ...], float, bytes | None, Path | None]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_bytes: bytes | None = None,
        cwd: Path | None = None,
    ) -> FakeCommandCapture:
        assert type(argv) is tuple, "live preflight must pass an argv tuple, not a shell string"
        assert argv and all(type(arg) is str and arg for arg in argv)
        assert type(timeout_seconds) in {int, float}
        assert 0.0 < float(timeout_seconds) <= 30.0
        assert isinstance(stdin_bytes, bytes) and stdin_bytes.endswith(b"\n")
        request = json.loads(stdin_bytes)
        assert request["protocol"] == "mycelium.physical_runner_remote_probe_request.v1"
        payload = self.payloads.pop(0)
        self.calls.append((argv, float(timeout_seconds), stdin_bytes, cwd))
        return FakeCommandCapture(
            argv=argv,
            returncode=0,
            stdout=_canonical_bytes(payload),
            stderr=b"",
        )


class FakeLocalProbes:
    """Local-only facts used by the bridge; no real ports/models/network."""

    def __init__(self) -> None:
        self.git_dirty = False
        self.expected_commit = COMMIT
        self.public_network_bytes = 0
        self.private_cache_root = PRIVATE_CACHE
        self.ssh_identities = {
            "wave8-ssh-identity": {
                "path": PRIVATE_SSH_IDENTITY,
                "regular": True,
                "owner_matches_local_user": True,
                "no_symlink": True,
                "mode": "0600",
            }
        }


def _safe_plan() -> dict[str, Any]:
    return {
        "protocol": "mycelium.physical_runner_safe_plan.v1",
        "run_id": "run-w8-001",
        "deployment_id": "deployment-w8-001",
        "route_ready": False,
        "release_ready": False,
        "source_manifest": [
            {"path": "mycelium_router/transports/iroh.py", "digest": SHA_SOURCE_A},
            {"path": "runtime_loader.py", "digest": SHA_SOURCE_B},
        ],
        "model_assets": [
            {
                "public_alias": "openai-community/gpt2:model.safetensors",
                "digest": SHA_MODEL_BLOB,
                "size_bytes": 1024,
            }
        ],
        "tokenizer": {
            "public_alias": "openai-community/gpt2-tokenizer",
            "digest": SHA_TOKENIZER,
        },
        "sidecar": {
            "public_alias": "mycelium-iroh-sidecar",
            "digest": SHA_SIDECAR,
        },
        "dependencies": {
            "public_alias": "python-lock-w8",
            "digest": SHA_DEPENDENCIES,
        },
        "hosts": [
            {
                "alias": "m4pro",
                "role": "coordinator",
                "node_id": "node-0",
                "ssh_target": "operator@m4pro.example",
                "ssh_user": "operator",
                "probe_transport": "local",
                "host_id": HOST_M4PRO,
                "boot_id": BOOT_M4PRO,
                "runtime": "mlx-mac-arm64",
                "coordinator_port": 43127,
                "credential_path_alias": "node-0-endpoint-key",
                "ssh_identity_path_alias": "wave8-ssh-identity",
            },
            {
                "alias": "laptop",
                "role": "peer",
                "node_id": "node-1",
                "ssh_target": "operator@laptop.example",
                "ssh_user": "operator",
                "probe_transport": "ssh",
                "host_id": HOST_LAPTOP,
                "boot_id": BOOT_LAPTOP,
                "runtime": "mlx-mac-arm64",
                "coordinator_port": 43128,
                "credential_path_alias": "node-1-endpoint-key",
                "ssh_identity_path_alias": "wave8-ssh-identity",
            },
        ],
    }


def _live_payload(host: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": "mycelium.physical_runner_live_probe.v1",
        "host_alias": host["alias"],
        "node_id": host["node_id"],
        "host_id": host["host_id"],
        "boot_id": host["boot_id"],
        "unknowns": [],
        "route_ready": False,
        "public_network_required": False,
        "public_network_bytes": 0,
        "credential_file": {
            "path_alias": host["credential_path_alias"],
            "regular": True,
            "owner_matches_ssh_user": True,
            "no_symlink": True,
            "mode": "0600",
        },
        "port": {"port": host["coordinator_port"], "available": True},
        "process": {"conflict": False},
        "runtime": {"name": host["runtime"], "supported": True},
        "source_manifest": [
            {"path": "mycelium_router/transports/iroh.py", "digest": SHA_SOURCE_A},
            {"path": "runtime_loader.py", "digest": SHA_SOURCE_B},
        ],
        "model_assets": [
            {
                "public_alias": "openai-community/gpt2:model.safetensors",
                "digest": SHA_MODEL_BLOB,
                "present": True,
            }
        ],
        "tokenizer": {"digest": SHA_TOKENIZER, "present": True},
        "sidecar": {
            "public_alias": "mycelium-iroh-sidecar",
            "digest": SHA_SIDECAR,
            "identity": "mycelium-iroh-sidecar",
        },
        "dependencies": {"digest": SHA_DEPENDENCIES},
    }


def _payloads_for(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [_live_payload(host) for host in plan["hosts"]]


def _api() -> Any:
    return importlib.import_module("mycelium_physical_runner.live_preflight")


def _run_bridge(
    plan: dict[str, Any],
    payloads: list[dict[str, Any]],
    *,
    probes: FakeLocalProbes | None = None,
) -> tuple[dict[str, Any], RecordingRunner]:
    live_preflight = _api()
    runner = RecordingRunner(payloads)
    result = live_preflight.run_live_preflight(
        copy.deepcopy(plan),
        probes=probes or FakeLocalProbes(),
        runner=runner,
    )
    return result, runner


def _blocker_codes(result: dict[str, Any]) -> set[str]:
    return {
        blocker["code"]
        for blocker in result.get("blockers", [])
        if isinstance(blocker, dict) and isinstance(blocker.get("code"), str)
    }


def test_live_preflight_uses_local_probe_for_controller_and_bounded_strict_ssh_for_peer() -> None:
    plan = _safe_plan()
    result, runner = _run_bridge(plan, _payloads_for(plan))

    assert result["protocol"] == "mycelium.physical_runner_live_preflight.v1"
    assert result["preflight_ready"] is True
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert runner.payloads == []
    assert len(runner.calls) == len(plan["hosts"])

    for host, (argv, timeout_seconds, stdin_bytes, cwd) in zip(
        plan["hosts"], runner.calls, strict=True
    ):
        assert isinstance(stdin_bytes, bytes)
        request = json.loads(stdin_bytes)
        assert request["protocol"] == "mycelium.physical_runner_remote_probe_request.v1"
        assert request["host_alias"] == host["alias"]
        assert PRIVATE_CACHE not in stdin_bytes.decode("utf-8")
        assert PRIVATE_SSH_IDENTITY not in stdin_bytes.decode("utf-8")
        assert SECRET_VALUE not in stdin_bytes.decode("utf-8")
        assert 0.0 < timeout_seconds <= 30.0
        assert SECRET_VALUE not in "\x00".join(argv)

        if host["probe_transport"] == "local":
            assert argv == (
                "/opt/homebrew/bin/python3.14",
                "-m",
                "mycelium_physical_runner.remote_probe",
                "--canonical-json",
            )
            assert cwd == Path.home() / "mycelium-physical-run/run-w8-001/source"
        else:
            assert cwd is None
            assert argv[0] == "ssh"
            assert host["ssh_target"] in argv
            assert "BatchMode=yes" in argv
            assert "IdentitiesOnly=yes" in argv
            assert "StrictHostKeyChecking=yes" in argv
            assert "-i" in argv
            assert argv[argv.index("-i") + 1] == PRIVATE_SSH_IDENTITY
            assert any(value.startswith("ConnectTimeout=") for value in argv)
            assert "--" in argv
            assert argv[-1] == (
                'cd "$HOME/mycelium-physical-run/run-w8-001/source" && '
                "exec /opt/homebrew/bin/python3.14 -m "
                "mycelium_physical_runner.remote_probe --canonical-json"
            )

    rendered = _canonical_bytes(result)
    assert PRIVATE_CACHE.encode("utf-8") not in rendered
    assert PRIVATE_SSH_IDENTITY.encode("utf-8") not in rendered
    assert SECRET_VALUE.encode("utf-8") not in rendered


@pytest.mark.parametrize("field", ["alias", "node_id", "ssh_target", "probe_transport"])
def test_live_preflight_rejects_tampered_shell_bearing_host_identity_before_output(field: str) -> None:
    plan = _safe_plan()
    plan["hosts"][0][field] = "safe; printf secret"
    with pytest.raises(Exception) as caught:
        _run_bridge(plan, _payloads_for(_safe_plan()))
    assert getattr(caught.value, "code", None) == "live_preflight_plan_invalid"
    assert "printf secret" not in str(caught.value)


@pytest.mark.parametrize("field", ["host_id", "boot_id"])
def test_live_preflight_rejects_wrong_but_unique_physical_identity(field: str) -> None:
    plan = _safe_plan()
    payloads = _payloads_for(plan)
    payloads[0][field] = f"wrong-{field}"
    result, _runner = _run_bridge(plan, payloads)
    assert "remote_probe_identity_mismatch" in _blocker_codes(result)
    assert result["preflight_ready"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("host_id", "raw-platform-uuid"), ("boot_id", "raw-boot-identity")],
)
def test_live_preflight_rejects_non_run_scoped_plan_identity_before_remote_calls(
    field: str,
    value: str,
) -> None:
    plan = _safe_plan()
    plan["hosts"][0][field] = value

    with pytest.raises(Exception) as caught:
        _run_bridge(plan, _payloads_for(_safe_plan()))

    assert getattr(caught.value, "code", None) == "live_preflight_plan_invalid"


def test_live_preflight_rejects_boolean_public_network_byte_counts() -> None:
    plan = _safe_plan()
    probes = FakeLocalProbes()
    probes.public_network_bytes = False
    result, runner = _run_bridge(plan, _payloads_for(plan), probes=probes)
    assert runner.calls == []
    assert "public_network_required" in _blocker_codes(result)

    probes.public_network_bytes = 0
    payloads = _payloads_for(plan)
    payloads[0]["public_network_bytes"] = False
    result, _runner = _run_bridge(plan, payloads, probes=probes)
    assert "public_network_required" in _blocker_codes(result)


def test_live_preflight_rejects_unsafe_local_ssh_identity_before_remote_calls() -> None:
    plan = _safe_plan()
    probes = FakeLocalProbes()
    probes.ssh_identities["wave8-ssh-identity"]["mode"] = "0644"

    result, runner = _run_bridge(plan, _payloads_for(plan), probes=probes)

    assert runner.calls == []
    assert result["preflight_ready"] is False
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert "ssh_identity_path_invalid" in _blocker_codes(result)
    assert PRIVATE_SSH_IDENTITY.encode("utf-8") not in _canonical_bytes(result)


def test_live_preflight_constructs_production_dependencies_when_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _safe_plan()
    runner = RecordingRunner(_payloads_for(plan))
    probes = FakeLocalProbes()
    calls: list[dict[str, Any]] = []

    def dependencies(value: dict[str, Any]) -> tuple[FakeLocalProbes, RecordingRunner]:
        calls.append(value)
        return probes, runner

    monkeypatch.setattr(_api(), "_production_dependencies", dependencies)
    result = _api().run_live_preflight(copy.deepcopy(plan))
    assert result["preflight_ready"] is True
    assert calls == [plan]
    assert len(runner.calls) == 2


def test_ssh_identity_alias_selects_private_key_without_exposing_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = tmp_path / ".ssh" / "mycelium-peer-key"
    identity.parent.mkdir(mode=0o700)
    identity.write_bytes(b"private-key-placeholder")
    identity.chmod(0o600)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def forbidden_subprocess(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ssh -G fallback must not run when the private alias exists")

    monkeypatch.setattr(_api().subprocess, "run", forbidden_subprocess)

    assert _api()._ssh_identity_for(
        "operator@laptop.example",
        "mycelium-peer-key",
    ) == str(identity)


def test_live_preflight_treats_any_unknown_as_blocker_and_keeps_readiness_false() -> None:
    plan = _safe_plan()
    payloads = _payloads_for(plan)
    payloads[0]["unknowns"] = ["runtime.supported", "port.available"]
    payloads[0]["runtime"]["supported"] = None
    payloads[0]["port"]["available"] = None

    result, runner = _run_bridge(plan, payloads)

    assert runner.payloads == []
    assert result["preflight_ready"] is False
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert "unknown_blocker" in _blocker_codes(result)


@pytest.mark.parametrize(
    ("expected_code", "mutate"),
    [
        (
            "credential_path_invalid",
            lambda payloads: payloads[0]["credential_file"].__setitem__("regular", False),
        ),
        (
            "credential_path_invalid",
            lambda payloads: payloads[0]["credential_file"].__setitem__(
                "owner_matches_ssh_user", False
            ),
        ),
        (
            "credential_path_invalid",
            lambda payloads: payloads[0]["credential_file"].__setitem__("no_symlink", False),
        ),
        (
            "credential_path_invalid",
            lambda payloads: payloads[0]["credential_file"].__setitem__("mode", "0644"),
        ),
        (
            "port_conflict",
            lambda payloads: payloads[0]["port"].__setitem__("available", False),
        ),
        (
            "process_conflict",
            lambda payloads: payloads[0]["process"].__setitem__("conflict", True),
        ),
        (
            "duplicate_host_id",
            lambda payloads: payloads[1].__setitem__("host_id", payloads[0]["host_id"]),
        ),
        (
            "duplicate_boot_id",
            lambda payloads: payloads[1].__setitem__("boot_id", payloads[0]["boot_id"]),
        ),
        (
            "unsupported_runtime",
            lambda payloads: payloads[0]["runtime"].__setitem__("supported", False),
        ),
        (
            "source_digest_mismatch",
            lambda payloads: payloads[0]["source_manifest"][0].__setitem__(
                "digest", _sha("6")
            ),
        ),
        (
            "model_digest_mismatch",
            lambda payloads: payloads[0]["model_assets"][0].__setitem__(
                "digest", _sha("5")
            ),
        ),
        (
            "model_blob_missing",
            lambda payloads: payloads[0]["model_assets"][0].__setitem__("present", False),
        ),
        (
            "tokenizer_digest_mismatch",
            lambda payloads: payloads[0]["tokenizer"].__setitem__("digest", _sha("4")),
        ),
        (
            "sidecar_digest_mismatch",
            lambda payloads: payloads[0]["sidecar"].__setitem__("digest", _sha("3")),
        ),
        (
            "sidecar_identity_mismatch",
            lambda payloads: payloads[0]["sidecar"].__setitem__(
                "identity", "unexpected-sidecar"
            ),
        ),
        (
            "dependency_digest_mismatch",
            lambda payloads: payloads[0]["dependencies"].__setitem__("digest", _sha("2")),
        ),
        (
            "public_network_required",
            lambda payloads: payloads[0].__setitem__("public_network_required", True),
        ),
    ],
    ids=[
        "credential-not-regular",
        "credential-owner",
        "credential-symlink",
        "credential-mode",
        "port-conflict",
        "process-conflict",
        "duplicate-host-id",
        "duplicate-boot-id",
        "unsupported-runtime",
        "source-digest",
        "model-digest",
        "missing-model-blob",
        "tokenizer-digest",
        "sidecar-digest",
        "wrong-sidecar",
        "dependency-digest",
        "public-network",
    ],
)
def test_live_preflight_blocks_conflicts_and_mismatches_with_stable_codes(
    expected_code: str,
    mutate: Callable[[list[dict[str, Any]]], None],
) -> None:
    plan = _safe_plan()
    payloads = _payloads_for(plan)
    mutate(payloads)

    result, _runner = _run_bridge(plan, payloads)

    assert result["preflight_ready"] is False
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert expected_code in _blocker_codes(result)
