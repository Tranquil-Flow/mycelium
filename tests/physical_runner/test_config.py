"""Operator-plan configuration is strict, canonical, bounded, and JSON-only."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import mycelium_physical_runner.config as config_module
from mycelium_physical_runner.config import (
    MAX_PLAN_BYTES,
    RunnerConfig,
    load_operator_plan,
    parse_operator_plan,
)
from mycelium_physical_runner.errors import RunnerError

from tests.physical_runner.conftest import operator_plan_payload, write_operator_plan


def _plan(workspace: Path, **overrides: Any) -> dict[str, Any]:
    return operator_plan_payload(workspace, **overrides)


def test_valid_plan_parses_into_a_frozen_config(workspace: Path) -> None:
    config = parse_operator_plan(_plan(workspace))

    assert isinstance(config, RunnerConfig)
    assert config.run_id == "run-0001"
    assert config.plan_id == "two-mac-g4"
    assert config.evidence_output_dir == str(workspace / "evidence" / "run-0001")
    assert [peer["process_transport"] for peer in config.controller["peers"]] == [
        "local",
        "ssh",
    ]
    assert config.gossip_verification_keys[0]["algorithm"] == "ed25519"
    with pytest.raises((AttributeError, TypeError)):
        config.run_id = "other"  # type: ignore[misc]


def test_callable_verifier_values_are_rejected(workspace: Path) -> None:
    payload = _plan(workspace)
    payload["verification_keys"] = {
        "gossip": [lambda *_: True],
        "load_proof": [dict(payload["verification_keys"]["load_proof"][0])],
    }

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)
    assert caught.value.code == "plan_value_not_json"


def test_raw_credential_values_are_rejected(workspace: Path) -> None:
    with_key_field = _plan(workspace)
    with_key_field["controller"]["run_plan"]["nodes"][0]["private_key"] = "abc"
    with pytest.raises(RunnerError) as key_error:
        parse_operator_plan(with_key_field)
    assert key_error.value.code == "plan_credential_field"

    with_pem = _plan(workspace)
    with_pem["controller"]["run_plan"]["nodes"][0]["configure"] = (
        "-----BEGIN PRIVATE KEY-----AAAA-----END PRIVATE KEY-----"
    )
    with pytest.raises(RunnerError) as pem_error:
        parse_operator_plan(with_pem)
    assert pem_error.value.code == "plan_credential_material"


def test_secret_file_path_fields_remain_allowed(workspace: Path) -> None:
    config = parse_operator_plan(_plan(workspace))
    node = config.controller["run_plan"]["nodes"][0]
    assert node["endpoint_secret_file"].endswith("/endpoint")


@pytest.mark.parametrize("value", [None, "", "remote", True, 1])
def test_peer_process_transport_is_required_and_bounded(
    workspace: Path,
    value: Any,
) -> None:
    payload = _plan(workspace)
    if value is None:
        payload["controller"]["peers"][0].pop("process_transport", None)
    else:
        payload["controller"]["peers"][0]["process_transport"] = value

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code in {"plan_missing_field", "plan_field_invalid"}


@pytest.mark.parametrize(
    "transports",
    [
        ("local", "local"),
        ("ssh", "ssh"),
        ("ssh", "local"),
    ],
)
def test_entry_peer_must_be_local_and_every_other_peer_must_use_ssh(
    workspace: Path,
    transports: tuple[str, str],
) -> None:
    payload = _plan(workspace)
    ssh_identity_file = payload["controller"]["peers"][1]["ssh_identity_file"]
    for peer, process_transport in zip(
        payload["controller"]["peers"], transports, strict=True
    ):
        peer["process_transport"] = process_transport
        peer["ssh_identity_file"] = (
            None if process_transport == "local" else ssh_identity_file
        )

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_field_invalid"


def test_ssh_peer_requires_explicit_identity_file(workspace: Path) -> None:
    payload = _plan(workspace)
    del payload["controller"]["peers"][1]["ssh_identity_file"]

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_missing_field"


def test_local_peer_rejects_ssh_identity_file(workspace: Path) -> None:
    payload = _plan(workspace)
    payload["controller"]["peers"][0]["ssh_identity_file"] = str(
        workspace / "ssh" / "local.identity"
    )

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_field_invalid"


def test_ssh_identity_file_must_exist(workspace: Path) -> None:
    payload = _plan(workspace)
    payload["controller"]["peers"][1]["ssh_identity_file"] = str(
        workspace / "ssh" / "missing.identity"
    )

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_path_unsafe"


def test_ssh_identity_file_must_be_regular(workspace: Path) -> None:
    payload = _plan(workspace)
    payload["controller"]["peers"][1]["ssh_identity_file"] = str(
        workspace / "ssh"
    )

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_path_unsafe"


def test_ssh_identity_file_must_have_single_hard_link(workspace: Path) -> None:
    payload = _plan(workspace)
    identity_file = Path(
        payload["controller"]["peers"][1]["ssh_identity_file"]
    )
    second_link = identity_file.with_name("second.identity")
    second_link.hardlink_to(identity_file)

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_path_unsafe"


def test_ssh_identity_file_must_not_be_group_or_other_accessible(
    workspace: Path,
) -> None:
    payload = _plan(workspace)
    identity_file = Path(
        payload["controller"]["peers"][1]["ssh_identity_file"]
    )
    identity_file.chmod(0o640)

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_path_unsafe"


def test_ssh_identity_ancestor_must_not_be_group_or_other_writable(
    workspace: Path,
) -> None:
    payload = _plan(workspace)
    identity_file = Path(
        payload["controller"]["peers"][1]["ssh_identity_file"]
    )
    identity_file.parent.chmod(0o777)

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_path_unsafe"


def test_ssh_identity_file_must_not_be_empty(workspace: Path) -> None:
    payload = _plan(workspace)
    identity_file = Path(
        payload["controller"]["peers"][1]["ssh_identity_file"]
    )
    identity_file.write_bytes(b"")

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_path_unsafe"


def test_ssh_identity_file_must_be_owned_by_controller_user(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _plan(workspace)
    monkeypatch.setattr(config_module.os, "geteuid", lambda: -1)

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_path_unsafe"


def test_unknown_peer_fields_fail_closed(workspace: Path) -> None:
    payload = _plan(workspace)
    payload["controller"]["peers"][0]["unexpected"] = "value"

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_unknown_field"


@pytest.mark.parametrize(
    "field",
    [
        "node_id",
        "ssh_target",
        "ssh_identity_file",
        "host_id",
        "boot_id",
        "staging_root",
    ],
)
def test_missing_non_transport_peer_fields_fail_closed(
    workspace: Path,
    field: str,
) -> None:
    payload = _plan(workspace)
    del payload["controller"]["peers"][0][field]

    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)

    assert caught.value.code == "plan_missing_field"


def test_controller_authority_profile_is_bounded_and_exclusive(workspace: Path) -> None:
    payload = _plan(workspace)
    payload["controller"]["authority_profile"] = "physical_frozen_route_inference_v1"
    config = parse_operator_plan(payload)
    assert config.controller["authority_profile"] == "physical_frozen_route_inference_v1"

    invalid = _plan(workspace)
    invalid["controller"]["authority_profile"] = "planner_v2_full_route_v1"
    with pytest.raises(RunnerError) as invalid_error:
        parse_operator_plan(invalid)
    assert invalid_error.value.code == "plan_field_invalid"

    mixed = _plan(workspace)
    mixed["controller"]["authority_profile"] = "physical_frozen_route_inference_v1"
    mixed["controller"]["authority_documents"] = {}
    with pytest.raises(RunnerError) as mixed_error:
        parse_operator_plan(mixed)
    assert mixed_error.value.code == "plan_field_invalid"


def test_controller_accepts_only_typed_node_transfer_manifests(workspace: Path) -> None:
    payload = _plan(workspace)
    payload["controller"]["node_transfer_manifests"] = {
        "protocol": "mycelium.controller_node_transfer_manifests.v1",
        "manifests": {"node-a": payload["controller"]["transfer_manifest"]},
    }
    config = parse_operator_plan(payload)
    assert config.controller["node_transfer_manifests"]["manifests"] == {
        "node-a": payload["controller"]["transfer_manifest"]
    }

    invalid = _plan(workspace)
    invalid["controller"]["node_transfer_manifests"] = {
        "protocol": "mycelium.controller_node_transfer_manifests.v2",
        "manifests": {},
    }
    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(invalid)
    assert caught.value.code == "plan_field_invalid"


def test_controller_accepts_closed_prepositioned_artifact_bindings(
    workspace: Path,
) -> None:
    payload = _plan(workspace)
    payload["controller"]["prepositioned_artifacts"] = {
        "protocol": "mycelium.controller_prepositioned_artifacts.v1",
        "members": {
            node_id: [
                {
                    "destination_path": "deployment/stage.safetensors",
                    "source_path": f"/srv/{node_id}/stage.safetensors",
                    "size_bytes": 17,
                    "content_digest": "sha256:" + "a" * 64,
                }
            ]
            for node_id in ("node-a", "node-b")
        },
    }

    parsed = parse_operator_plan(payload)

    assert set(parsed.controller["prepositioned_artifacts"]["members"]) == {
        "node-a",
        "node-b",
    }

    invalid = _plan(workspace)
    invalid["controller"]["prepositioned_artifacts"] = {
        "protocol": "mycelium.controller_prepositioned_artifacts.v1",
        "members": {"node-a": []},
    }
    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(invalid)
    assert caught.value.code == "plan_field_invalid"


def test_unknown_and_missing_top_level_fields_fail_closed(workspace: Path) -> None:
    extra = _plan(workspace)
    extra["unexpected"] = 1
    with pytest.raises(RunnerError) as extra_error:
        parse_operator_plan(extra)
    assert extra_error.value.code == "plan_unknown_field"

    missing = _plan(workspace)
    del missing["run_id"]
    with pytest.raises(RunnerError) as missing_error:
        parse_operator_plan(missing)
    assert missing_error.value.code == "plan_missing_field"

    wrong_protocol = _plan(workspace, protocol="mycelium.other.v1")
    with pytest.raises(RunnerError) as protocol_error:
        parse_operator_plan(wrong_protocol)
    assert protocol_error.value.code == "plan_protocol_invalid"


@pytest.mark.parametrize(
    "path_value",
    ["relative/path", "/tmp/../etc/passwd", "/etc/mycelium/state.json", "/tmp/a\nb"],
)
def test_unsafe_paths_fail_closed(workspace: Path, path_value: str) -> None:
    payload = _plan(workspace)
    payload["paths"]["state_path"] = path_value
    with pytest.raises(RunnerError) as caught:
        parse_operator_plan(payload)
    assert caught.value.code == "plan_path_unsafe"


def test_duplicate_json_keys_and_non_finite_numbers_are_rejected(
    workspace: Path, tmp_path: Path
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"protocol": "a", "protocol": "b"}', encoding="utf-8")
    with pytest.raises(RunnerError) as duplicate_error:
        load_operator_plan(duplicate)
    assert duplicate_error.value.code == "plan_duplicate_key"

    non_finite = tmp_path / "nonfinite.json"
    non_finite.write_text('{"now_unix_ms": NaN}', encoding="utf-8")
    with pytest.raises(RunnerError) as non_finite_error:
        load_operator_plan(non_finite)
    assert non_finite_error.value.code == "plan_non_finite_number"


def test_plan_file_must_be_a_bounded_regular_non_symlink_file(
    workspace: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "absent.json"
    with pytest.raises(RunnerError) as missing_error:
        load_operator_plan(missing)
    assert missing_error.value.code == "plan_unavailable"

    real = write_operator_plan(tmp_path / "plan.json", _plan(workspace))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(RunnerError) as symlink_error:
        load_operator_plan(link)
    assert symlink_error.value.code == "plan_unavailable"

    oversized = tmp_path / "big.json"
    oversized.write_bytes(b"{" + b" " * (MAX_PLAN_BYTES + 1))
    with pytest.raises(RunnerError) as size_error:
        load_operator_plan(oversized)
    assert size_error.value.code == "plan_too_large"


def test_load_operator_plan_round_trips_a_written_plan(
    workspace: Path, tmp_path: Path
) -> None:
    path = write_operator_plan(tmp_path / "plan.json", _plan(workspace))
    config = load_operator_plan(path)
    assert config.plan_id == "two-mac-g4"
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == config.run_id
