from __future__ import annotations

import json

import pytest

from conftest import ROOT, canonical_bytes, refresh_authorization


def _api():
    from mycelium_physical_preflight import PreflightValidationError, validate_and_generate

    return PreflightValidationError, validate_and_generate


def test_accepts_only_exact_canonical_duplicate_free_json(encoded_plan: bytes) -> None:
    error, validate = _api()

    generated = validate(encoded_plan, source_tree_root=ROOT)
    assert generated["protocol"] == "mycelium.physical_qualification_execution_plan.v1"

    spaced = json.dumps(json.loads(encoded_plan), indent=2).encode()
    with pytest.raises(error, match="noncanonical_json"):
        validate(spaced, source_tree_root=ROOT)

    duplicate = b'{"plan_id":"shadow",' + encoded_plan[1:]
    with pytest.raises(error, match="duplicate_json_key"):
        validate(duplicate, source_tree_root=ROOT)

    with pytest.raises(error, match="invalid_json"):
        validate(b'{"value":NaN}', source_tree_root=ROOT)

    with pytest.raises(error, match="invalid_unicode"):
        validate(b'{"value":"\\ud800"}', source_tree_root=ROOT)


def test_rejects_missing_unknown_and_wrong_exact_types(plan: dict[str, object]) -> None:
    error, validate = _api()

    missing = dict(plan)
    missing.pop("cleanup")
    with pytest.raises(error, match="missing_field"):
        validate(canonical_bytes(missing), source_tree_root=ROOT)

    unknown = dict(plan, surprise=True)
    with pytest.raises(error, match="unknown_field"):
        validate(canonical_bytes(unknown), source_tree_root=ROOT)

    bool_generation = json.loads(canonical_bytes(plan))
    bool_generation["hosts"][0]["expected_generation"] = True
    refresh_authorization(bool_generation)
    with pytest.raises(error, match="invalid_expected_generation"):
        validate(canonical_bytes(bool_generation), source_tree_root=ROOT)


def test_output_is_byte_deterministic_and_never_contains_source_tree(
    encoded_plan: bytes,
) -> None:
    _, validate = _api()
    from mycelium_physical_preflight import canonical_json_bytes

    first = canonical_json_bytes(validate(encoded_plan, source_tree_root=ROOT))
    second = canonical_json_bytes(validate(encoded_plan, source_tree_root=ROOT))

    assert first == second
    assert str(ROOT).encode() not in first
    assert b"physical_qualification_executed" in first
    assert first.endswith(b"\n")


def test_error_rendering_exposes_code_and_pointer_not_rejected_path(
    plan: dict[str, object],
) -> None:
    error, validate = _api()
    from mycelium_physical_preflight import canonical_error_bytes

    hosts = plan["hosts"]
    assert isinstance(hosts, list)
    hosts[0]["staging_root"] = str(ROOT / "secret-worktree-location")
    refresh_authorization(plan)

    with pytest.raises(error) as caught:
        validate(canonical_bytes(plan), source_tree_root=ROOT)
    rendered = canonical_error_bytes(caught.value)

    assert str(ROOT).encode() not in rendered
    assert json.loads(rendered)["error"]["code"] == "source_tree_path"
    assert json.loads(rendered)["error"]["pointer"] == "/hosts/0/staging_root"


def test_inline_secret_error_never_echoes_secret(plan: dict[str, object]) -> None:
    error, validate = _api()
    from mycelium_physical_preflight import canonical_error_bytes

    secret = "gh" + "p_" + "Z" * 36
    plan["identities"]["model_id"] = secret
    with pytest.raises(error) as caught:
        validate(canonical_bytes(plan), source_tree_root=ROOT)

    rendered = canonical_error_bytes(caught.value)
    assert secret.encode() not in rendered
    assert json.loads(rendered)["error"] == {
        "code": "inline_credential",
        "pointer": "/identities/model_id",
    }
