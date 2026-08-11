from __future__ import annotations

import hashlib
import json

import pytest

from mycelium_m23_kv import PROTOCOL, validate_m23_kv_evidence


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _evidence() -> dict[str, object]:
    gates = {
        "same_route_model_stages_hosts": True,
        "same_prompt_and_budget": True,
        "exact_output_parity": True,
        "one_token_decode_every_stage": True,
        "all_stages_advanced_physical_counters": True,
        "kv_active_then_terminally_released": True,
        "no_fatal_or_cleanup_failure": True,
        "measured_tpot_improvement": True,
    }
    document: dict[str, object] = {
        "protocol": PROTOCOL,
        "generated_at_unix_ms": 1,
        "replay_capture_digest": "sha256:" + "a" * 64,
        "kv_capture_digest": "sha256:" + "b" * 64,
        "gates": gates,
        "implemented": True,
        "performance_qualified": True,
        "promotion_state": "qualified",
        "measurements": {
            "replay_tpot_ms": 100.0,
            "kv_tpot_ms": 10.0,
            "tpot_delta_ms": -90.0,
            "tpot_improvement_ratio": 0.9,
            "replay_activation_output_bytes": 500,
            "kv_activation_output_bytes": 100,
            "activation_byte_delta": -400,
            "replay_total_ms": 500.0,
            "kv_total_ms": 100.0,
        },
        "claim_boundary": "One fixed physical route and prompt; no wider claim.",
    }
    document["evidence_digest"] = _digest(document)
    return document


def test_m23_kv_evidence_recomputes_the_qualified_gate() -> None:
    document = _evidence()

    assert validate_m23_kv_evidence(document) == document


@pytest.mark.parametrize(
    ("field", "value"),
    (("implemented", False), ("promotion_state", "withheld")),
)
def test_m23_kv_evidence_rejects_claim_drift(field: str, value: object) -> None:
    document = _evidence()
    document[field] = value
    unsigned = {key: item for key, item in document.items() if key != "evidence_digest"}
    document["evidence_digest"] = _digest(unsigned)

    with pytest.raises(ValueError, match="m23_kv_evidence_invalid"):
        validate_m23_kv_evidence(document)


def test_m23_kv_evidence_rejects_measurement_or_digest_tampering() -> None:
    document = _evidence()
    document["measurements"]["kv_tpot_ms"] = 11.0

    with pytest.raises(ValueError, match="m23_kv_evidence_invalid"):
        validate_m23_kv_evidence(document)
