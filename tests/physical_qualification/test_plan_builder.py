"""RED tests for W8 deterministic safe-plan construction.

These tests define the public contract of ``mycelium_physical_runner.plan_builder``
before the implementation exists.  The builder must work from an explicit
inventory plus injected probe facts only: no SSH, subprocesses, ports, model
loading, or public network access are allowed in this test surface.
"""

from __future__ import annotations

import copy
import importlib
import json
from dataclasses import dataclass
from typing import Any, Callable

import pytest


def _sha(label: str) -> str:
    return "sha256:" + label[0] * 64


COMMIT = "1" * 40
SHA_SOURCE_A = _sha("a")
SHA_SOURCE_B = _sha("b")
SHA_SOURCE_C = _sha("c")
SHA_MODEL_MANIFEST = _sha("d")
SHA_MODEL_BLOB = _sha("e")
SHA_TOKENIZER = _sha("f")
SHA_SIDECAR = _sha("9")
SHA_DEPENDENCIES = _sha("8")
HOST_M4PRO = "host-" + "1" * 32
BOOT_M4PRO = "boot-" + "2" * 32
HOST_LAPTOP = "host-" + "3" * 32
BOOT_LAPTOP = "boot-" + "4" * 32

PRIVATE_MODEL_CACHE = "/Users/operator/Library/Caches/huggingface/hub/models--openai-community--gpt2"
PRIVATE_TOKENIZER_CACHE = "/Users/operator/Library/Caches/huggingface/tokenizers/openai-community/gpt2"
SECRET_VALUE = "hf_" + "S" * 40


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass
class FakePathFact:
    kind: str = "regular"
    owner: str = "operator"
    mode: int = 0o600
    symlink: bool = False
    canonical: bool = True


class FakePlanProbes:
    """Static probe fixture consumed by the desired public builder API."""

    def __init__(self, inventory: dict[str, Any]) -> None:
        self.git_dirty = False
        self.resolved_commit = COMMIT
        self.source_files = {
            record["path"]: {
                "kind": "regular",
                "digest": record["digest"],
                "symlink": False,
            }
            for record in inventory["source_tree"]["files"]
        }
        self.paths: dict[str, FakePathFact] = {}
        for host in inventory["hosts"]:
            self.paths[host["staging_root"]] = FakePathFact(kind="directory")
            self.paths[host["credential_path"]] = FakePathFact(
                owner=host["ssh_user"],
                mode=0o600,
            )
            self.paths[host["ssh_identity_path"]] = FakePathFact(
                owner=host["ssh_identity_owner"],
                mode=0o600,
            )
            self.paths[host["socket_root"]] = FakePathFact(kind="directory")
            self.paths[host["evidence_root"]] = FakePathFact(kind="directory")
        self.model_blobs = {
            record["alias"]: {"digest": record["digest"], "present": True}
            for record in inventory["model"]["blobs"]
        }
        self.model_manifest_digest = inventory["model"]["manifest_digest"]
        self.tokenizer_digest = inventory["tokenizer"]["digest"]
        self.sidecar = {
            host["sidecar_binary"]: {
                "digest": host["sidecar_digest"],
                "identity": "mycelium-iroh-sidecar",
            }
            for host in inventory["hosts"]
        }
        self.dependency_digest = inventory["dependency_lock"]["digest"]
        self.port_conflicts: set[tuple[str, int]] = set()
        self.process_conflicts: set[str] = set()
        self.supported_runtimes = {host["runtime"]: True for host in inventory["hosts"]}
        self.public_network_required = False


def _base_inventory() -> dict[str, Any]:
    return {
        "protocol": "mycelium.physical_runner_inventory.v1",
        "run_id": "run-w8-001",
        "deployment_id": "deployment-w8-001",
        "source_tree": {
            "public_alias": "mycelium-wave8-g4",
            "private_root": "/Users/operator/private/worktrees/mycelium-wave8-g4",
            "expected_commit": COMMIT,
            "files": [
                {"path": "physical_inference_node.py", "digest": SHA_SOURCE_C},
                {"path": "runtime_loader.py", "digest": SHA_SOURCE_B},
                {"path": "mycelium_router/transports/iroh.py", "digest": SHA_SOURCE_A},
            ],
        },
        "model": {
            "public_alias": "openai-community/gpt2",
            "resolved_commit": COMMIT,
            "manifest_digest": SHA_MODEL_MANIFEST,
            "private_cache_path": PRIVATE_MODEL_CACHE,
            "blobs": [
                {
                    "alias": "model.safetensors",
                    "digest": SHA_MODEL_BLOB,
                    "size_bytes": 1024,
                    "private_cache_path": PRIVATE_MODEL_CACHE + "/snapshots/" + COMMIT + "/model.safetensors",
                }
            ],
        },
        "tokenizer": {
            "public_alias": "openai-community/gpt2-tokenizer",
            "digest": SHA_TOKENIZER,
            "private_cache_path": PRIVATE_TOKENIZER_CACHE,
        },
        "dependency_lock": {
            "public_alias": "python-lock-w8",
            "path": "requirements.lock",
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
                "python_executable": "/opt/mycelium/python/bin/python3",
                "sidecar_binary": "/opt/mycelium/bin/mycelium-iroh-sidecar",
                "sidecar_digest": SHA_SIDECAR,
                "staging_root": "/Users/operator/mycelium-physical-run/run-w8-001/node-0",
                "socket_root": "/private/tmp/mycelium-physical-run/run-w8-001/node-0/socket",
                "credential_path": "/Users/operator/.mycelium/identities/node-0.key",
                "ssh_identity_path": "/Users/operator/.ssh/id_ed25519_mycelium",
                "ssh_identity_owner": "operator",
                "coordinator_port": 43127,
                "evidence_root": "/Users/operator/mycelium-physical-evidence/run-w8-001/node-0",
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
                "python_executable": "/opt/mycelium/python/bin/python3",
                "sidecar_binary": "/opt/mycelium/bin/mycelium-iroh-sidecar",
                "sidecar_digest": SHA_SIDECAR,
                "staging_root": "/Users/operator/mycelium-physical-run/run-w8-001/node-1",
                "socket_root": "/private/tmp/mycelium-physical-run/run-w8-001/node-1/socket",
                "credential_path": "/Users/operator/.mycelium/identities/node-1.key",
                "ssh_identity_path": "/Users/operator/.ssh/id_ed25519_mycelium",
                "ssh_identity_owner": "operator",
                "coordinator_port": 43128,
                "evidence_root": "/Users/operator/mycelium-physical-evidence/run-w8-001/node-1",
            },
        ],
        "request": {
            "request_id": "request-w8-001",
            "prompt_token_ids": [1, 2, 3],
            "max_new_tokens": 1,
            "expected_new_tokens": 1,
            "qos_class": "interactive",
            "admitted_at": 0.0,
            "target_ttft_ms": 1000.0,
            "target_tpot_ms": 1000.0,
            "target_tokens_per_second": 1.0,
            "sampling_seed": 17,
            "generation_config_digest": _sha("7"),
        },
        "decode_count": 1,
        "expected_token_ids": [11, 12],
    }


def _api() -> Any:
    return importlib.import_module("mycelium_physical_runner.plan_builder")


def _build(inventory: dict[str, Any], probes: FakePlanProbes | None = None) -> dict[str, Any]:
    plan_builder = _api()
    return plan_builder.build_safe_plan(
        copy.deepcopy(inventory),
        probes=probes or FakePlanProbes(inventory),
    )


def _assert_build_error(
    expected_code: str,
    mutate: Callable[[dict[str, Any], FakePlanProbes], None],
) -> None:
    inventory = _base_inventory()
    probes = FakePlanProbes(inventory)
    mutate(inventory, probes)
    plan_builder = _api()

    with pytest.raises(plan_builder.PlanBuildError) as captured:
        plan_builder.build_safe_plan(copy.deepcopy(inventory), probes=probes)

    assert captured.value.code == expected_code
    rendered_error = str(captured.value).encode("utf-8")
    assert SECRET_VALUE.encode("utf-8") not in rendered_error
    assert PRIVATE_MODEL_CACHE.encode("utf-8") not in rendered_error


def test_build_safe_plan_is_deterministic_sorted_and_redacted() -> None:
    inventory = _base_inventory()
    probes = FakePlanProbes(inventory)

    first = _build(inventory, probes)
    second = _build(inventory, probes)

    assert _canonical_bytes(first) == _canonical_bytes(second)
    assert first["protocol"] == "mycelium.physical_runner_safe_plan.v1"
    assert first["route_ready"] is False
    assert first["release_ready"] is False

    operator_plan = first["operator_plan"]
    controller_run_plan = first["controller_run_plan"]
    assert operator_plan["source_files"] == sorted(
        record["path"] for record in inventory["source_tree"]["files"]
    )
    assert [node["node_id"] for node in controller_run_plan["nodes"]] == [
        "node-0",
        "node-1",
    ]
    assert [host["probe_transport"] for host in first["hosts"]] == ["local", "ssh"]

    rendered = _canonical_bytes(first)
    assert b"openai-community/gpt2" in rendered
    assert SHA_MODEL_BLOB.encode("ascii") in rendered
    assert SHA_SIDECAR.encode("ascii") in rendered
    assert PRIVATE_MODEL_CACHE.encode("utf-8") not in rendered
    assert PRIVATE_TOKENIZER_CACHE.encode("utf-8") not in rendered
    assert b"/Users/operator/.mycelium/identities" not in rendered
    assert b"/Users/operator/.ssh" not in rendered
    assert all("ssh_identity_path_alias" in host for host in first["hosts"])
    assert SECRET_VALUE.encode("utf-8") not in rendered


def test_build_safe_plan_declares_local_only_cold_and_warm_cache_contracts() -> None:
    safe_plan = _build(_base_inventory())

    run_matrix = safe_plan["operator_plan"]["run_matrix"]
    assert run_matrix["cold"] == {
        "cache_precondition": "fresh_run_scoped_stage_cache",
        "local_files_only": True,
        "public_network_bytes": 0,
        "public_downloads": "forbidden",
    }
    assert run_matrix["warm"] == {
        "cache_precondition": "verified_compatible_local_cache",
        "local_files_only": True,
        "public_network_bytes": 0,
        "public_downloads": "forbidden",
    }

    rendered = _canonical_bytes(safe_plan).lower()
    assert b"expected_network_bytes\":\"positive" not in rendered
    assert b"download" in rendered and b"forbidden" in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [("host_id", "raw-platform-uuid"), ("boot_id", "raw-boot-identity")],
)
def test_build_safe_plan_rejects_non_run_scoped_host_identity(field: str, value: str) -> None:
    inventory = _base_inventory()
    probes = FakePlanProbes(inventory)
    inventory["hosts"][0][field] = value

    with pytest.raises(_api().PlanBuildError) as captured:
        _api().build_safe_plan(inventory, probes=probes)

    assert captured.value.code == "host_invalid"


def test_build_safe_plan_accepts_controller_owned_ssh_key_for_different_remote_user() -> None:
    inventory = _base_inventory()
    inventory["hosts"][1]["ssh_user"] = "peer-user"
    inventory["hosts"][1]["ssh_identity_path"] = "/Users/controller/.ssh/mycelium-peer-key"
    inventory["hosts"][1]["ssh_identity_owner"] = "controller-user"
    probes = FakePlanProbes(inventory)

    safe_plan = _api().build_safe_plan(inventory, probes=probes)

    assert safe_plan["hosts"][1]["ssh_user"] == "peer-user"
    assert "ssh_identity_owner" not in safe_plan["hosts"][1]


def test_build_safe_plan_binds_ssh_identity_alias_to_validated_key_basename() -> None:
    inventory = _base_inventory()
    inventory["hosts"][1]["ssh_identity_path"] = "/Users/controller/.ssh/mycelium-peer-key"
    inventory["hosts"][1]["ssh_identity_owner"] = "controller-user"
    probes = FakePlanProbes(inventory)

    safe_plan = _api().build_safe_plan(inventory, probes=probes)

    assert safe_plan["hosts"][1]["ssh_identity_path_alias"] == "mycelium-peer-key"
    assert b"/Users/controller/.ssh" not in _canonical_bytes(safe_plan)


@pytest.mark.parametrize(
    ("expected_code", "mutate"),
    [
        (
            "credential_value_forbidden",
            lambda inventory, probes: inventory["hosts"][0].__setitem__(
                "credential_value", SECRET_VALUE
            ),
        ),
        (
            "credential_path_invalid",
            lambda inventory, probes: probes.paths[
                inventory["hosts"][0]["credential_path"]
            ].__setattr__("mode", 0o644),
        ),
        (
            "credential_path_invalid",
            lambda inventory, probes: probes.paths[
                inventory["hosts"][0]["credential_path"]
            ].__setattr__("owner", "other-user"),
        ),
        (
            "credential_path_invalid",
            lambda inventory, probes: probes.paths[
                inventory["hosts"][0]["credential_path"]
            ].__setattr__("symlink", True),
        ),
        (
            "ssh_identity_path_invalid",
            lambda inventory, probes: probes.paths[
                inventory["hosts"][0]["ssh_identity_path"]
            ].__setattr__("mode", 0o644),
        ),
        (
            "unsafe_path",
            lambda inventory, probes: inventory["hosts"][0].__setitem__(
                "staging_root", "/Users/operator/mycelium-physical-run/run-w8-001/../escape"
            ),
        ),
        (
            "unsafe_path",
            lambda inventory, probes: probes.paths[
                inventory["hosts"][0]["staging_root"]
            ].__setattr__("symlink", True),
        ),
        (
            "git_dirty",
            lambda inventory, probes: setattr(probes, "git_dirty", True),
        ),
        (
            "source_file_invalid",
            lambda inventory, probes: inventory["source_tree"]["files"].append(
                {"path": "*.py", "digest": _sha("6")}
            ),
        ),
        (
            "source_digest_mismatch",
            lambda inventory, probes: probes.source_files["runtime_loader.py"].__setitem__(
                "digest", _sha("6")
            ),
        ),
        (
            "model_digest_mismatch",
            lambda inventory, probes: probes.model_blobs["model.safetensors"].__setitem__(
                "digest", _sha("5")
            ),
        ),
        (
            "tokenizer_digest_mismatch",
            lambda inventory, probes: setattr(probes, "tokenizer_digest", _sha("4")),
        ),
        (
            "sidecar_digest_mismatch",
            lambda inventory, probes: probes.sidecar[
                inventory["hosts"][0]["sidecar_binary"]
            ].__setitem__("digest", _sha("3")),
        ),
        (
            "dependency_digest_mismatch",
            lambda inventory, probes: setattr(probes, "dependency_digest", _sha("2")),
        ),
        (
            "model_blob_missing",
            lambda inventory, probes: probes.model_blobs.__delitem__("model.safetensors"),
        ),
        (
            "sidecar_identity_mismatch",
            lambda inventory, probes: probes.sidecar[
                inventory["hosts"][0]["sidecar_binary"]
            ].__setitem__("identity", "unexpected-sidecar"),
        ),
        (
            "process_conflict",
            lambda inventory, probes: probes.process_conflicts.add("mycelium-node:node-0"),
        ),
        (
            "port_conflict",
            lambda inventory, probes: probes.port_conflicts.add(("m4pro", 43127)),
        ),
        (
            "duplicate_host_id",
            lambda inventory, probes: inventory["hosts"][1].__setitem__(
                "host_id", inventory["hosts"][0]["host_id"]
            ),
        ),
        (
            "duplicate_boot_id",
            lambda inventory, probes: inventory["hosts"][1].__setitem__(
                "boot_id", inventory["hosts"][0]["boot_id"]
            ),
        ),
        (
            "unsupported_runtime",
            lambda inventory, probes: probes.supported_runtimes.__setitem__(
                "mlx-mac-arm64", False
            ),
        ),
        (
            "public_network_required",
            lambda inventory, probes: setattr(probes, "public_network_required", True),
        ),
    ],
    ids=[
        "inline-credential-value",
        "credential-mode",
        "credential-owner",
        "credential-symlink",
        "ssh-identity-mode",
        "noncanonical-path",
        "path-component-symlink",
        "dirty-git",
        "source-glob",
        "source-digest",
        "model-digest",
        "tokenizer-digest",
        "sidecar-digest",
        "dependency-digest",
        "missing-model-blob",
        "wrong-sidecar",
        "process-conflict",
        "port-conflict",
        "duplicate-host-id",
        "duplicate-boot-id",
        "unsupported-runtime",
        "public-network",
    ],
)
def test_build_safe_plan_rejects_blockers_with_stable_error_codes(
    expected_code: str,
    mutate: Callable[[dict[str, Any], FakePlanProbes], None],
) -> None:
    _assert_build_error(expected_code, mutate)


@pytest.mark.parametrize(
    ("section", "value"),
    [
        ("source_tree", "/Users/operator/private/source"),
        ("model", "hf_" + "S" * 40),
        ("tokenizer", "safe\nsecret"),
        ("dependency_lock", "../private-lock"),
    ],
)
def test_build_safe_plan_rejects_private_or_control_bearing_public_aliases(
    section: str,
    value: str,
) -> None:
    _assert_build_error(
        "public_alias_invalid",
        lambda inventory, _probes: inventory[section].__setitem__("public_alias", value),
    )
