"""PV29 L1: signed, digest-bound route record and its tamper tests (L0 unit)."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from mycelium_platform_neutral import backend_parity as bp
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 1_800_000_000.0
DIG = "sha256:" + "ab" * 32


def _route(quant0: str = "none", quant1: str = "int8-weight-only") -> dict:
    stage = {
        "backend_version": "0.31.1",
        "build_digest": DIG,
        "device_class": "metal",
    }
    return {
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "revision": "7ae557604adf67be50417f59c2c2f167def9a775",
        "source_artifact_digest": DIG,
        "n_layer": 24,
        "wire_dtype": "float32",
        "stages": [
            {**stage, "stage_index": 0, "layer_start": 0, "layer_end": 12, "backend": "mlx", "quantization": quant0},
            {
                **stage,
                "stage_index": 1,
                "layer_start": 12,
                "layer_end": 24,
                "backend": "numpy",
                "device_class": "cpu",
                "quantization": quant1,
            },
        ],
    }


def _corpus() -> dict:
    return bp.validate_corpus(
        {
            "protocol": bp.CORPUS_PROTOCOL,
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "revision": "7ae557604adf67be50417f59c2c2f167def9a775",
            "cases": [{"case_id": "c0", "kind": "chat", "text": "x", "token_ids": [1, 2, 3, 4], "prefill_len": 2}],
        }
    )


def _gates(policy: dict) -> dict:
    rng = np.random.default_rng(1)
    logits = rng.normal(size=(3, 64)) * 3
    hop = rng.normal(size=(5, 16))
    cap = lambda l, h: bp.RouteCapture(  # noqa: E731
        cases={
            "c0": bp.CaseCapture(
                "c0", l, {12: h}, {12: {"dtype": "float32", "shape": (1, 5, 16)}}, 0
            )
        }
    )
    route = cap(logits + 1e-7, hop + 1e-7)
    mono = cap(logits, hop)
    return bp.evaluate_route(route, mono, mono, policy, ["none", "int8-weight-only"])


def _issue(signer):
    policy = bp.validate_policy(bp.default_policy_v2())
    gates = _gates(policy)
    record = bp.build_route_record(
        route=_route(),
        corpus=_corpus(),
        policy=policy,
        oracle={"implementation": "test", "runtime_digest": DIG, "archive_digest": DIG, "dtype": "float32"},
        gates=gates,
        created_at=NOW,
        ttl_seconds=3600,
    )
    return policy, bp.sign_route_record(signer, record)


def _verify(envelope, policy, **kw):
    kw.setdefault("now", NOW + 10)
    kw.setdefault("expected_key_digest", None)
    kw.setdefault("unanchored_ok", True)
    return bp.verify_route_record(envelope, policy=policy, **kw)


def test_record_round_trip_partial_pass_with_g_p_unevaluated() -> None:
    signer = generate_ed25519_signer(endpoint_id="t1")
    policy, envelope = _issue(signer)
    record = _verify(envelope, policy, corpus=_corpus(), expected_route=_route())
    assert record["verdict"] == "partial_pass"
    assert record["unevaluated_gates"] == ["G", "P"]
    assert record["quantization_class"] == "Q0"
    assert set(record["gates"]) == {"H", "Q0", "N", "G", "P"}
    assert record["policy_status"] == "provisional"
    assert "not platform support" in record["claim_boundary"]


def test_anchor_is_required_unless_explicitly_waived() -> None:
    signer = generate_ed25519_signer(endpoint_id="t1")
    policy, envelope = _issue(signer)
    with pytest.raises(bp.BackendParityError, match="parity_record_key_unanchored"):
        bp.verify_route_record(envelope, policy=policy, now=NOW, expected_key_digest=None)
    anchored = bp.verify_route_record(
        envelope, policy=policy, now=NOW, expected_key_digest=signer.verification_key_digest
    )
    assert anchored["verdict"] == "partial_pass"
    with pytest.raises(bp.BackendParityError, match="parity_record_key_mismatch"):
        bp.verify_route_record(envelope, policy=policy, now=NOW, expected_key_digest=DIG)


def _tamper(envelope: dict, mutate) -> dict:
    other = copy.deepcopy(envelope)
    mutate(other["record"])
    return other


@pytest.mark.parametrize(
    "name,mutate",
    [
        ("verdict_flip", lambda r: r.update(verdict="pass")),
        ("metric_edit", lambda r: r["gates"]["Q0"]["metrics"].update(kl_mean=0.0)),
        ("status_flip", lambda r: r["gates"]["H"].update(status="fail")),
        ("backend_swap", lambda r: r["route"]["stages"][1].update(backend="mlx")),
        ("range_edit", lambda r: r["route"]["stages"][0].update(layer_end=11)),
        ("quantization_edit", lambda r: r["route"]["stages"][1].update(quantization="none")),
        ("wire_dtype_edit", lambda r: r["route"].update(wire_dtype="bfloat16")),
        ("policy_digest_edit", lambda r: r.update(policy_digest=DIG)),
        ("corpus_digest_edit", lambda r: r.update(corpus_digest=DIG)),
        ("expiry_edit", lambda r: r.update(expires_at=NOW + 10**9)),
        ("extra_field", lambda r: r.update(extra=1)),
        ("drop_field", lambda r: r.pop("claim_boundary")),
        ("unevaluated_edit", lambda r: r.update(unevaluated_gates=[])),
    ],
)
def test_any_edit_without_resigning_is_rejected(name, mutate) -> None:
    signer = generate_ed25519_signer(endpoint_id="t1")
    policy, envelope = _issue(signer)
    with pytest.raises(bp.BackendParityError, match="parity_record_signature_invalid"):
        _verify(_tamper(envelope, mutate), policy)


def _resign(signer, record: dict) -> dict:
    record["record_digest"] = bp.record_body_digest(record)
    return bp.sign_route_record(signer, record)


@pytest.mark.parametrize(
    "mutate,code",
    [
        # a dishonest (but key-holding) issuer re-signs an edited record
        (lambda r: r["gates"]["Q0"]["metrics"].update(kl_mean=9.0), "parity_gate_recompute_mismatch"),
        (lambda r: r["gates"]["H"].update(status="fail"), "parity_gate_recompute_mismatch"),
        (lambda r: r.update(verdict="pass"), "parity_verdict_mismatch"),
        (lambda r: r["gates"]["G"].update(status="pass"), "parity_gate_status_invalid"),
        (lambda r: r.update(quantization_class="Q1"), "parity_quantization_class_mismatch"),
        (lambda r: r["route"]["stages"][0].update(layer_end=11), "parity_route_ranges_invalid"),
        (
            lambda r: (r["route"].update(wire_dtype="bfloat16"), r.update(route_digest=bp.digest_of(r["route"]))),
            "parity_wire_dtype_mismatch",
        ),
        (lambda r: r.update(policy_digest=DIG), "parity_policy_digest_mismatch"),
        (lambda r: r.update(route_digest=DIG), "parity_route_digest_mismatch"),
    ],
)
def test_resigned_inconsistent_records_are_rejected(mutate, code) -> None:
    signer = generate_ed25519_signer(endpoint_id="t1")
    policy, envelope = _issue(signer)
    record = copy.deepcopy(envelope["record"])
    mutate(record)
    with pytest.raises(bp.BackendParityError, match=code):
        _verify(_resign(signer, record), policy)


def test_record_digest_is_checked_independently_of_the_signature() -> None:
    signer = generate_ed25519_signer(endpoint_id="t1")
    policy, envelope = _issue(signer)
    record = copy.deepcopy(envelope["record"])
    record["verdict"] = "partial_pass"
    record["claim_boundary"] = "edited"  # digest left stale, then signed over the stale digest
    forged = {
        "protocol": bp.RECORD_ENVELOPE_PROTOCOL,
        "record": record,
        "signature": signer.sign(record),
        "verification_key": signer.public_key_record(),
    }
    with pytest.raises(bp.BackendParityError, match="parity_record_digest_mismatch"):
        _verify(forged, policy)


def test_foreign_key_cannot_pass_an_anchor() -> None:
    signer = generate_ed25519_signer(endpoint_id="t1")
    attacker = generate_ed25519_signer(endpoint_id="t2")
    policy, envelope = _issue(signer)
    forged = bp.sign_route_record(attacker, copy.deepcopy(envelope["record"]))
    with pytest.raises(bp.BackendParityError, match="parity_record_key_mismatch"):
        bp.verify_route_record(
            forged, policy=policy, now=NOW, expected_key_digest=signer.verification_key_digest
        )
    swapped = copy.deepcopy(envelope)
    swapped["verification_key"] = attacker.public_key_record()
    with pytest.raises(bp.BackendParityError, match="parity_record_key_mismatch"):
        bp.verify_route_record(
            swapped, policy=policy, now=NOW, expected_key_digest=signer.verification_key_digest
        )
    swapped_unanchored = copy.deepcopy(envelope)
    swapped_unanchored["verification_key"] = attacker.public_key_record()
    with pytest.raises(bp.BackendParityError, match="parity_record_signature_invalid"):
        _verify(swapped_unanchored, policy)


def test_component_change_invalidates_the_record() -> None:
    signer = generate_ed25519_signer(endpoint_id="t1")
    policy, envelope = _issue(signer)
    other_route = _route(quant1="none")
    with pytest.raises(bp.BackendParityError, match="parity_route_digest_mismatch"):
        _verify(envelope, policy, expected_route=other_route)
    other_corpus = _corpus()
    other_corpus["cases"][0]["token_ids"][0] = 99
    with pytest.raises(bp.BackendParityError, match="parity_corpus_digest_mismatch"):
        _verify(envelope, policy, corpus=other_corpus)
    other_policy = copy.deepcopy(policy)
    other_policy["gates"]["H"]["thresholds"]["kl_mean"]["value"] = 1.0
    other_policy["policy_digest"] = bp.policy_digest(other_policy)
    with pytest.raises(bp.BackendParityError, match="parity_policy_digest_mismatch"):
        _verify(envelope, other_policy)


def test_expired_and_future_records_are_rejected() -> None:
    signer = generate_ed25519_signer(endpoint_id="t1")
    policy, envelope = _issue(signer)
    with pytest.raises(bp.BackendParityError, match="parity_record_expired"):
        _verify(envelope, policy, now=NOW + 7200)
    with pytest.raises(bp.BackendParityError, match="parity_record_from_the_future"):
        _verify(envelope, policy, now=NOW - 3600)


def test_failed_gate_gives_fail_verdict_that_verifies() -> None:
    signer = generate_ed25519_signer(endpoint_id="t1")
    policy = bp.validate_policy(bp.default_policy_v2())
    rng = np.random.default_rng(2)
    logits = rng.normal(size=(3, 64)) * 3
    hop = rng.normal(size=(5, 16))
    cap = lambda l, h: bp.RouteCapture(  # noqa: E731
        cases={"c0": bp.CaseCapture("c0", l, {12: h}, {12: {"dtype": "float32", "shape": (1, 5, 16)}}, 0)}
    )
    bad = cap(logits + rng.normal(size=logits.shape), hop)
    gates = bp.evaluate_route(bad, cap(logits, hop), cap(logits, hop), policy, ["none", "none"])
    record = bp.build_route_record(
        route=_route("none", "none"),
        corpus=_corpus(),
        policy=policy,
        oracle={"implementation": "t", "runtime_digest": DIG, "archive_digest": DIG, "dtype": "float32"},
        gates=gates,
        created_at=NOW,
    )
    assert record["verdict"] == "fail"
    assert _verify(bp.sign_route_record(signer, record), policy)["verdict"] == "fail"


def test_route_ranges_must_tile_the_model() -> None:
    gap = _route()
    gap["stages"][1]["layer_start"] = 13
    with pytest.raises(bp.BackendParityError, match="parity_route_ranges_invalid"):
        bp.validate_route(gap)
    short = _route()
    short["n_layer"] = 25
    with pytest.raises(bp.BackendParityError, match="parity_route_ranges_invalid"):
        bp.validate_route(short)


def test_unknown_stage_representation_is_refused_at_build() -> None:
    policy = bp.validate_policy(bp.default_policy_v2())
    gates = _gates(policy)
    with pytest.raises(bp.BackendParityError, match="parity_representation_unknown"):
        bp.build_route_record(
            route=_route(quant1="int4-weight-only"),
            corpus=_corpus(),
            policy=policy,
            oracle={"implementation": "t", "runtime_digest": DIG, "archive_digest": DIG, "dtype": "float32"},
            gates=gates,
            created_at=NOW,
        )


def test_battery_summary_requires_every_defect_to_fail_and_every_control_to_pass() -> None:
    rows = [
        {"name": "d1", "kind": "defect", "verdict": "fail"},
        {"name": "c1", "kind": "control", "verdict": "partial_pass"},
    ]
    good = bp.summarize_battery(rows)
    assert good["all_defects_fail"] and good["all_controls_pass"] and not good["problems"]
    bad = bp.summarize_battery(
        [{"name": "d1", "kind": "defect", "verdict": "partial_pass"}, {"name": "c1", "kind": "control", "verdict": "fail"}]
    )
    assert not bad["all_defects_fail"] and not bad["all_controls_pass"]
    assert {p["problem"] for p in bad["problems"]} == {"defect_not_detected", "legitimate_noise_rejected"}
