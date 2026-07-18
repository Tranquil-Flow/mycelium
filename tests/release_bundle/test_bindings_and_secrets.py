from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from mycelium_release_bundle import canonical_output, verify_bundle

from .conftest import (
    REQUIRED_GATES,
    canonical_bytes,
    replace_artifact,
    rewrite_manifest,
)

SUMMARY_PATH = "qualification/synthetic-summary.json"
LOCK_PATH = "provenance/synthetic-dependencies.lock"


def _body(manifest: dict[str, Any]) -> dict[str, Any]:
    body = manifest["body"]
    assert isinstance(body, dict)
    return body


def _summary(root: Path) -> dict[str, Any]:
    return json.loads((root / SUMMARY_PATH).read_text(encoding="utf-8"))


def test_declared_gates_and_exact_bindings_are_verified(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, _manifest = synthetic_bundle

    result = verify_bundle(root)

    assert result["ok"] is True
    assert result["checks"]["content_policy"] is True
    assert result["checks"]["declared_gate_presence"] is True
    assert result["checks"]["bindings"] is True
    assert result["observed"]["declared_gates"] == len(REQUIRED_GATES)
    assert result["observed"]["declared_bindings"] == len(REQUIRED_GATES)
    assert result["synthetic_fixture"] is True
    assert result["physical_evidence_accepted"] is False
    assert result["route_ready"] is False
    assert result["release_ready"] is False


@pytest.mark.parametrize("kind", REQUIRED_GATES)
def test_every_declared_binding_kind_is_checked_exactly(
    synthetic_bundle: tuple[Path, dict[str, Any]], kind: str
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    binding = next(item for item in body["bindings"] if item["kind"] == kind)
    expected = binding["expected"]
    if isinstance(expected, int):
        binding["expected"] = expected + 1
    elif kind == "source_commit":
        binding["expected"] = "1" * 40
    elif isinstance(expected, str) and expected.startswith("sha256:"):
        binding["expected"] = "sha256:" + "f" * 64
    else:
        binding["expected"] = f"{expected}-mismatch"
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["ok"] is False
    assert result["findings"][0]["code"] == "binding_mismatch"
    assert result["route_ready"] is False
    assert result["release_ready"] is False


def test_declared_gate_requires_listed_evidence_path(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    body["declared_gates"][0]["evidence_paths"] = [
        "qualification/not-in-inventory.json"
    ]
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "declared_gate_path_missing", "subject": "manifest"}
    ]


def test_declared_gate_requires_binding_for_each_evidence_path(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    body["bindings"].pop(0)
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "declared_gate_binding_missing", "subject": "manifest"}
    ]


def test_unknown_gate_name_is_rejected(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    body["declared_gates"][0]["gate"] = "unreviewed_gate"
    body["bindings"][0]["kind"] = "unreviewed_gate"
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "invalid_gate_name", "subject": "manifest"}
    ]


def test_missing_json_pointer_target_fails_closed(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    binding = next(item for item in body["bindings"] if item["kind"] == "assignment")
    binding["json_pointer"] = "/missing"
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "binding_target_missing"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("private_key", "-----BEGIN PRIVATE KEY-----\nforbidden\n-----END PRIVATE KEY-----"),
        ("credential", "forbidden-user-password"),
        ("prompt", "forbidden private prompt"),
        ("token_ids", [101, 102]),
        ("activations", [0.25, 0.5]),
        ("kv_cache", {"key": [1], "value": [2]}),
        ("runtime_endpoint", "http://127.0.0.1:9123"),
    ],
)
def test_sensitive_model_and_access_material_is_rejected(
    synthetic_bundle: tuple[Path, dict[str, Any]], key: str, value: object
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    summary[key] = value
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "forbidden_bundle_content", "subject": "artifact:0002"}
    ]
    assert result["route_ready"] is False
    assert result["release_ready"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_prompt", "summarize private material"),
        ("tokens", [101, 102]),
        ("activation_values", [0.1, 0.2]),
        ("kv_values", {"layer": [1, 2]}),
        ("client_secret", "forbidden-value"),
        ("private_endpoint_uri", "http://10.0.0.8:9000"),
        ("url", "http://127.0.0.1:9000/private"),
    ],
)
def test_sensitive_aliases_and_private_endpoint_values_are_rejected(
    synthetic_bundle: tuple[Path, dict[str, Any]], field: str, value: Any
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    summary[field] = value
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "forbidden_bundle_content", "subject": "artifact:0002"}
    ]


def test_sensitive_digest_alias_requires_an_actual_digest(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    summary["prompt_digest"] = "this is raw prompt text, not a digest"
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "forbidden_bundle_content", "subject": "artifact:0002"}
    ]


def test_sensitive_digest_fields_accept_only_digest_material(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    digest = "sha256:" + "d" * 64
    summary.update(
        {
            "activation_digests": [digest],
            "kv_ownership_digest": digest,
            "prompt_digest": digest,
            "token_parity_digest": digest,
        }
    )
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    result = verify_bundle(root)

    assert result["ok"] is True
    assert result["route_ready"] is False
    assert result["release_ready"] is False


def test_unknown_artifact_field_cannot_hide_private_model_material(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    summary["data"] = "raw prompt payload hidden under a generic key"
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "unsupported_artifact_field", "subject": "artifact:0002"}
    ]


def test_bundle_id_is_a_bounded_identifier_not_a_payload_channel(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    _body(manifest)["bundle_id"] = "synthetic-test-fixture:raw prompt payload"
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "invalid_bundle_id", "subject": "manifest"}
    ]


@pytest.mark.parametrize(
    "endpoint_id",
    [
        "fd00::1",
        "fe80::1",
        "::1",
        "[fd00::1]:9000",
        "[fd00::1]:https",
        "[::1]:9000",
        "node.internal:9000",
        "db.internal:https",
        "localhost:9000",
        "localhost:http",
        "https://api.example.com",
    ],
)
def test_endpoint_id_cannot_encode_an_endpoint_address(
    synthetic_bundle: tuple[Path, dict[str, Any]], endpoint_id: str
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    summary["endpoint_id"] = endpoint_id
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))
    binding = next(
        item for item in _body(manifest)["bindings"] if item["kind"] == "endpoint_id"
    )
    binding["expected"] = endpoint_id
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "forbidden_bundle_content"


def test_bound_identifier_cannot_be_used_as_prompt_payload(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    payload = "summarize this private transcript verbatim"
    summary = _summary(root)
    summary["assignment_id"] = payload
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))
    binding = next(
        item for item in _body(manifest)["bindings"] if item["kind"] == "assignment"
    )
    binding["expected"] = payload
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "forbidden_bundle_content"


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("reason_codes", ["summarize private transcript"]),
        ("claim_boundary", "summarize private transcript"),
    ],
)
def test_free_text_contract_fields_cannot_be_used_as_prompt_payload(
    synthetic_bundle: tuple[Path, dict[str, Any]], field: str, payload: Any
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    summary[field] = payload
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "forbidden_bundle_content"


def test_nondigest_gate_cannot_substitute_file_hash_for_identifier(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    summary_entry = next(item for item in body["files"] if item["path"] == SUMMARY_PATH)
    assignment = next(item for item in body["bindings"] if item["kind"] == "assignment")
    assignment["json_pointer"] = None
    assignment["expected"] = summary_entry["sha256"]
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "invalid_null_binding_pointer"


def test_synthetic_artifacts_cannot_be_reclassified_as_physical(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    body["bundle_id"] = "synthetic-test-fixture:adversarial-physical-claim"
    body["evidence_class"] = "physical_qualification"
    body["synthetic_fixture"] = False
    for entry in body["files"]:
        entry["synthetic_fixture"] = False
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["ok"] is False
    assert result["physical_input_inventory_complete"] is False
    assert result["findings"][0]["code"] == "physical_bundle_contains_synthetic_marker"
    assert result["physical_evidence_accepted"] is False
    assert result["route_ready"] is False
    assert result["release_ready"] is False


def test_physical_claim_rejects_synthetic_json_artifact_markers(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    old_path = SUMMARY_PATH
    new_path = "qualification/adversarial-test-summary.json"
    summary_entry = next(item for item in body["files"] if item["path"] == old_path)
    summary_entry["path"] = new_path
    summary_entry["synthetic_fixture"] = False
    body["files"] = [summary_entry]
    body["declared_gates"] = [
        declaration
        for declaration in body["declared_gates"]
        if declaration["gate"] != "dependency_lock"
    ]
    for declaration in body["declared_gates"]:
        declaration["evidence_paths"] = [new_path]
    body["bindings"] = [
        binding for binding in body["bindings"] if binding["kind"] != "dependency_lock"
    ]
    for binding in body["bindings"]:
        binding["path"] = new_path
    body["bundle_id"] = "test-only:adversarial-physical-artifact-claim"
    body["evidence_class"] = "physical_qualification"
    body["synthetic_fixture"] = False
    body["file_count"] = 1
    body["total_size_bytes"] = summary_entry["size_bytes"]
    (root / old_path).rename(root / new_path)
    (root / LOCK_PATH).unlink()
    (root / "provenance").rmdir()
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {
            "code": "physical_bundle_contains_synthetic_marker",
            "subject": "artifact:0001",
        }
    ]
    assert result["physical_input_inventory_complete"] is False


def test_physical_claim_rejects_opaque_non_json_artifact(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    lock_entry = next(item for item in body["files"] if item["path"] == LOCK_PATH)
    lock_entry["path"] = "provenance/adversarial-test.lock"
    lock_entry["synthetic_fixture"] = False
    body["files"] = [lock_entry]
    body["declared_gates"] = [
        {
            "evidence_paths": ["provenance/adversarial-test.lock"],
            "gate": "dependency_lock",
        }
    ]
    dependency = next(
        binding for binding in body["bindings"] if binding["kind"] == "dependency_lock"
    )
    dependency["path"] = "provenance/adversarial-test.lock"
    body["bindings"] = [dependency]
    body["bundle_id"] = "test-only:adversarial-opaque-physical-claim"
    body["evidence_class"] = "physical_qualification"
    body["synthetic_fixture"] = False
    body["file_count"] = 1
    body["total_size_bytes"] = lock_entry["size_bytes"]
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "opaque_physical_input_forbidden", "subject": "artifact:0001"}
    ]
    assert result["physical_input_inventory_complete"] is False


def test_private_key_bytes_in_non_json_input_are_rejected(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    private_bytes = (
        b"-----BEGIN PRIVATE KEY-----\n"
        b"synthetic-fixture-secret-bytes\n"
        b"-----END PRIVATE KEY-----\n"
    )
    replace_artifact(root, manifest, LOCK_PATH, private_bytes)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "forbidden_bundle_content", "subject": "artifact:0001"}
    ]
    assert b"synthetic-fixture-secret-bytes" not in canonical_output(result)


def test_static_output_never_echoes_secret_bytes(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    secret = "never-echo-this-credential-value"
    summary = _summary(root)
    summary["credential"] = secret
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    first = canonical_output(verify_bundle(root))
    second = canonical_output(verify_bundle(root))

    assert first == second
    assert secret.encode("utf-8") not in first
    assert str(root).encode("utf-8") not in first


def test_synthetic_fixture_cannot_claim_route_readiness(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    summary["route_ready"] = True
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "synthetic_acceptance_forbidden", "subject": "artifact:0002"}
    ]
    assert result["physical_evidence_accepted"] is False
    assert result["route_ready"] is False
    assert result["release_ready"] is False


@pytest.mark.parametrize("alias", ["route-ready", "release.ready"])
def test_synthetic_readiness_aliases_are_not_accepted_as_schema_fields(
    synthetic_bundle: tuple[Path, dict[str, Any]], alias: str
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    summary[alias] = True
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "unsupported_artifact_field", "subject": "artifact:0002"}
    ]
    assert result["physical_evidence_accepted"] is False
    assert result["route_ready"] is False
    assert result["release_ready"] is False


def test_every_synthetic_file_requires_prominent_marker(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    body["files"][0]["synthetic_fixture"] = False
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "invalid_synthetic_fixture_marker"


def test_noncanonical_and_duplicate_key_artifact_json_is_rejected(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    summary = _summary(root)
    noncanonical = json.dumps(summary, indent=2, sort_keys=True).encode("utf-8")
    replace_artifact(root, manifest, SUMMARY_PATH, noncanonical)
    noncanonical_result = verify_bundle(root)
    assert noncanonical_result["findings"] == [
        {"code": "noncanonical_artifact_json", "subject": "artifact:0002"}
    ]

    duplicate = b'{"evidence_class":"synthetic_test_fixture","x":1,"x":2}'
    replace_artifact(root, manifest, SUMMARY_PATH, duplicate)
    duplicate_result = verify_bundle(root)
    assert duplicate_result["findings"] == [
        {"code": "duplicate_artifact_json_key", "subject": "artifact:0002"}
    ]


@pytest.mark.parametrize(
    "word",
    ["activation", "credential", "password", "secret", "token"],
)
def test_singular_sensitive_path_terms_are_rejected(
    synthetic_bundle: tuple[Path, dict[str, Any]], word: str
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    body["files"][0]["path"] = f"qualification/{word}.json"
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "forbidden_bundle_path"


def test_sensitive_material_in_manifest_metadata_is_rejected(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    _body(manifest)["bundle_id"] = (
        "synthetic-test-fixture:private key material must not be bundled"
    )
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"] == [
        {"code": "forbidden_bundle_content", "subject": "manifest"}
    ]
    assert b"private key material" not in canonical_output(result)


def test_sensitive_path_name_is_rejected_even_before_content_read(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    forbidden_path = "qualification/prompts.json"
    (root / SUMMARY_PATH).rename(root / forbidden_path)
    entry = next(item for item in body["files"] if item["path"] == SUMMARY_PATH)
    entry["path"] = forbidden_path
    body["files"].sort(key=lambda item: item["path"])
    for gate in body["declared_gates"]:
        gate["evidence_paths"] = [
            forbidden_path if path == SUMMARY_PATH else path
            for path in gate["evidence_paths"]
        ]
    for binding in body["bindings"]:
        if binding["path"] == SUMMARY_PATH:
            binding["path"] = forbidden_path
    body["bindings"].sort(
        key=lambda item: (item["kind"], item["path"], item["json_pointer"] or "")
    )
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "forbidden_bundle_path"


def test_manifest_binding_value_cannot_hide_sensitive_material(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    body = _body(manifest)
    binding = next(item for item in body["bindings"] if item["kind"] == "endpoint_id")
    binding["json_pointer"] = "/runtime_endpoint"
    binding["expected"] = "http://192.168.1.7:9000"
    rewrite_manifest(root, manifest)

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "forbidden_bundle_content"
    serialized = canonical_output(result)
    assert b"192.168.1.7" not in serialized


def test_manifest_lists_remain_semantically_canonical(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    reordered = copy.deepcopy(manifest)
    _body(reordered)["declared_gates"].reverse()
    rewrite_manifest(root, reordered)

    result = verify_bundle(root)

    assert result["findings"][0]["code"] == "noncanonical_gate_order"
