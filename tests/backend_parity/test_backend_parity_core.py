"""PV29 L1: backend parity gate v0 - metrics, thresholds, policy, gate engine (L0 unit)."""

from __future__ import annotations

import copy
import math

import numpy as np
import pytest

from mycelium_platform_neutral import backend_parity as bp


def _rng(seed: int = 7) -> np.random.Generator:
    return np.random.default_rng(seed)


# --------------------------------------------------------------------------- metrics


def test_cosine_identical_scaled_and_orthogonal() -> None:
    a = _rng().normal(size=(5, 16))
    assert np.allclose(bp.cosine_rows(a, a), 1.0)
    assert np.allclose(bp.cosine_rows(a, 3.0 * a), 1.0)
    x = np.array([[1.0, 0.0]])
    y = np.array([[0.0, 2.0]])
    assert bp.cosine_rows(x, y)[0] == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero_not_nan() -> None:
    z = np.zeros((1, 4))
    assert bp.cosine_rows(z, z)[0] == 0.0


def test_rel_l2_rows() -> None:
    ref = np.array([[3.0, 4.0]])
    test = np.array([[3.0, 4.0 + 0.5]])
    assert bp.rel_l2_rows(ref, test)[0] == pytest.approx(0.5 / 5.0)


def test_kl_rows_known_value_and_zero_for_identical() -> None:
    ref = np.array([[0.0, 0.0]])
    test = np.array([[math.log(3.0), 0.0]])
    expected = 0.5 * math.log(0.5 / 0.75) + 0.5 * math.log(0.5 / 0.25)
    assert bp.kl_rows(ref, test)[0] == pytest.approx(expected, rel=1e-9)
    big = _rng().normal(size=(3, 50)) * 30.0
    assert np.allclose(bp.kl_rows(big, big), 0.0, atol=1e-12)


def test_kl_is_shift_invariant_and_nonnegative() -> None:
    a = _rng(1).normal(size=(4, 40))
    b = a + _rng(2).normal(size=(4, 40)) * 0.1
    assert (bp.kl_rows(a, b) >= -1e-12).all()
    assert np.allclose(bp.kl_rows(a, b), bp.kl_rows(a + 100.0, b - 50.0))


def test_top1_agreement_with_margin_floor_excludes_near_ties() -> None:
    ref = np.array([[5.0, 1.0, 0.0], [2.0, 2.0004, 0.0], [0.0, 3.0, 1.0]])
    test = np.array([[5.0, 1.0, 0.0], [2.0004, 2.0, 0.0], [3.0, 0.0, 1.0]])
    agree, scored, excluded = bp.top1_agreement(ref, test, margin_floor=1e-3)
    assert (scored, excluded) == (2, 1)
    assert agree == pytest.approx(0.5)
    agree_all, scored_all, excluded_all = bp.top1_agreement(ref, test, margin_floor=0.0)
    assert (scored_all, excluded_all) == (3, 0)
    assert agree_all == pytest.approx(1 / 3)


def test_topk_overlap() -> None:
    ref = np.array([[9.0, 8.0, 7.0, 0.0, 0.0]])
    test = np.array([[9.0, 0.0, 7.0, 8.0, 0.0]])
    assert bp.topk_overlap(ref, test, 2) == pytest.approx(0.5)
    assert bp.topk_overlap(ref, test, 3) == pytest.approx(2 / 3)


def test_max_abs_delta_over_reference_top_k() -> None:
    ref = np.array([[10.0, 9.0, 0.0, 0.0]])
    test = np.array([[10.0, 9.5, 100.0, 0.0]])
    assert bp.max_abs_delta(ref, test, top_k=2) == pytest.approx(0.5)
    assert bp.max_abs_delta(ref, test, top_k=None) == pytest.approx(100.0)


def test_metrics_reject_shape_mismatch_and_nonfinite() -> None:
    with pytest.raises(bp.BackendParityError, match="parity_shape_mismatch"):
        bp.cosine_rows(np.zeros((2, 3)), np.zeros((2, 4)))
    with pytest.raises(bp.BackendParityError, match="parity_nonfinite_input"):
        bp.kl_rows(np.array([[0.0, np.nan]]), np.array([[0.0, 1.0]]))


# --------------------------------------------------------------------------- thresholds


def test_thresholds_pass_fail_and_missing_metric() -> None:
    spec = {
        "top1": {"op": ">=", "value": 0.99},
        "kl": {"op": "<=", "value": 0.005},
    }
    ok = bp.evaluate_thresholds({"top1": 1.0, "kl": 0.001}, spec)
    assert all(check["pass"] for check in ok)
    bad = bp.evaluate_thresholds({"top1": 0.9, "kl": 0.001}, spec)
    assert [c["metric"] for c in bad if not c["pass"]] == ["top1"]
    missing = bp.evaluate_thresholds({"top1": 1.0}, spec)
    assert [c["metric"] for c in missing if not c["pass"]] == ["kl"]
    nan = bp.evaluate_thresholds({"top1": float("nan"), "kl": 0.0}, spec)
    assert [c["metric"] for c in nan if not c["pass"]] == ["top1"]


def test_threshold_spec_rejects_unknown_op_and_nonfinite_value() -> None:
    with pytest.raises(bp.BackendParityError, match="parity_threshold_invalid"):
        bp.validate_threshold_spec({"m": {"op": "==", "value": 1.0}})
    with pytest.raises(bp.BackendParityError, match="parity_threshold_invalid"):
        bp.validate_threshold_spec({"m": {"op": "<=", "value": float("inf")}})
    with pytest.raises(bp.BackendParityError, match="parity_threshold_invalid"):
        bp.validate_threshold_spec({"m": {"op": "<=", "value": 1.0, "extra": 1}})


# --------------------------------------------------------------------------- policy


def _policy() -> dict:
    return bp.default_policy_v2()


def test_default_policy_is_valid_provisional_and_digest_bound() -> None:
    policy = _policy()
    checked = bp.validate_policy(policy)
    assert checked["protocol"] == bp.POLICY_PROTOCOL
    assert checked["status"] == "provisional"
    assert checked["frozen_at"] is None
    assert checked["policy_digest"] == bp.policy_digest(checked)
    assert checked["wire"]["dtype"] == "float32"


def test_policy_digest_changes_with_any_threshold() -> None:
    policy = _policy()
    other = copy.deepcopy(policy)
    other["gates"]["H"]["thresholds"]["logits_cosine_min"]["value"] = 0.5
    assert bp.policy_digest(policy) != bp.policy_digest(other)


def test_policy_tamper_is_rejected() -> None:
    policy = _policy()
    policy["gates"]["Q0"]["thresholds"]["kl_mean"]["value"] = 5.0  # digest not refreshed
    with pytest.raises(bp.BackendParityError, match="parity_policy_digest_mismatch"):
        bp.validate_policy(policy)


def test_policy_rejects_unknown_field_and_unknown_representation_class() -> None:
    policy = _policy()
    policy["surprise"] = 1
    with pytest.raises(bp.BackendParityError, match="parity_policy_invalid"):
        bp.validate_policy(policy)
    policy = _policy()
    policy["representations"]["int4-weight-only"] = {"class": "Q9"}
    policy["policy_digest"] = bp.policy_digest(policy)
    with pytest.raises(bp.BackendParityError, match="parity_policy_invalid"):
        bp.validate_policy(policy)


def test_frozen_policy_requires_frozen_at_and_not_calibration_provisional() -> None:
    policy = _policy()
    policy["status"] = "frozen"
    policy["policy_digest"] = bp.policy_digest(policy)
    with pytest.raises(bp.BackendParityError, match="parity_policy_invalid"):
        bp.validate_policy(policy)


def test_quantization_class_takes_the_worst_stage_and_refuses_unknown() -> None:
    policy = _policy()
    assert bp.route_quantization_class(policy, ["none", "int8-weight-only"]) == "Q0"
    assert bp.route_quantization_class(policy, ["none", "gguf-q4_k_m"]) == "Q1"
    with pytest.raises(bp.BackendParityError, match="parity_representation_unknown"):
        bp.route_quantization_class(policy, ["none", "int4-weight-only"])


# --------------------------------------------------------------------------- corpus


def _corpus() -> dict:
    return {
        "protocol": bp.CORPUS_PROTOCOL,
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "revision": "7ae557604adf67be50417f59c2c2f167def9a775",
        "cases": [
            {"case_id": "short_chat", "kind": "chat", "text": "hi there", "token_ids": [1, 2, 3, 4, 5, 6], "prefill_len": 3},
            {"case_id": "repeat", "kind": "repeat", "text": "a a a a", "token_ids": [7, 7, 7, 7, 7], "prefill_len": 2},
        ],
    }


def test_corpus_validates_and_digest_is_stable() -> None:
    corpus = bp.validate_corpus(_corpus())
    assert bp.corpus_digest(corpus) == bp.corpus_digest(bp.validate_corpus(_corpus()))
    other = _corpus()
    other["cases"][0]["token_ids"][0] = 9
    assert bp.corpus_digest(bp.validate_corpus(other)) != bp.corpus_digest(corpus)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["cases"][0].update(prefill_len=0),
        lambda c: c["cases"][0].update(prefill_len=6),
        lambda c: c["cases"][0].update(token_ids=[1, "x", 3, 4]),
        lambda c: c["cases"][1].update(case_id="short_chat"),
        lambda c: c["cases"][0].update(case_id="Bad Id"),
        lambda c: c.update(extra=1),
    ],
)
def test_corpus_rejects_invalid(mutate) -> None:
    corpus = _corpus()
    mutate(corpus)
    with pytest.raises(bp.BackendParityError, match="parity_corpus_invalid"):
        bp.validate_corpus(corpus)


def test_scored_positions_follow_prefill_split() -> None:
    corpus = bp.validate_corpus(_corpus())
    # prefill 3 of 6 tokens: logits at positions 2,3,4 predict tokens 3,4,5
    assert bp.scored_positions(corpus["cases"][0]) == [2, 3, 4]
    assert bp.scored_positions(corpus["cases"][1]) == [1, 2, 3]


# --------------------------------------------------------------------------- gates


def _capture(logits: np.ndarray, hops: dict[int, np.ndarray] | None = None, wire: dict | None = None) -> bp.RouteCapture:
    hops = hops or {}
    return bp.RouteCapture(
        cases={
            "c0": bp.CaseCapture(
                case_id="c0",
                logits=logits,
                hops=hops,
                hop_wire=wire or {b: {"dtype": "float32", "shape": (1, int(h.shape[0]), int(h.shape[1]))} for b, h in hops.items()},
                extra_outputs=0,
            )
        }
    )


def _base() -> tuple[np.ndarray, np.ndarray]:
    rng = _rng(3)
    return rng.normal(size=(6, 64)) * 3.0, rng.normal(size=(9, 16)) * 2.0


def test_gate_h_passes_for_tiny_noise_and_fails_for_perturbation() -> None:
    policy = bp.validate_policy(_policy())
    logits, hop = _base()
    ref = _capture(logits, {12: hop})
    noisy = _capture(logits + 1e-7, {12: hop + 1e-7})
    assert bp.gate_h(noisy, ref, policy)["status"] == "pass"
    broken = _capture(logits + _rng(5).normal(size=logits.shape) * 0.5, {12: hop})
    result = bp.gate_h(broken, ref, policy)
    assert result["status"] == "fail"
    assert any(not c["pass"] for c in result["checks"])


def test_gate_h_fails_on_a_hop_only_error() -> None:
    policy = bp.validate_policy(_policy())
    logits, hop = _base()
    ref = _capture(logits, {12: hop})
    bad = _capture(logits, {12: hop + _rng(9).normal(size=hop.shape) * 0.2})
    result = bp.gate_h(bad, ref, policy)
    assert result["status"] == "fail"
    failed = {c["metric"] for c in result["checks"] if not c["pass"]}
    assert failed & {"hop_cosine_min", "hop_rel_l2_max"}


def test_gate_h_requires_the_same_hop_boundaries() -> None:
    policy = bp.validate_policy(_policy())
    logits, hop = _base()
    result = bp.gate_h(_capture(logits, {11: hop}), _capture(logits, {12: hop}), policy)
    assert result["status"] == "fail"
    assert "hop_boundary_mismatch" in result["reason"]


def test_gate_q_uses_class_thresholds() -> None:
    policy = bp.validate_policy(_policy())
    logits, _ = _base()
    oracle = _capture(logits)
    assert bp.gate_q(_capture(logits + 1e-6), oracle, policy, "Q0")["status"] == "pass"
    bad = _capture(logits + _rng(11).normal(size=logits.shape) * 3.0)
    assert bp.gate_q(bad, oracle, policy, "Q0")["status"] == "fail"


def test_gate_q_unknown_class_is_refused() -> None:
    policy = bp.validate_policy(_policy())
    logits, _ = _base()
    with pytest.raises(bp.BackendParityError, match="parity_gate_class_invalid"):
        bp.gate_q(_capture(logits), _capture(logits), policy, "Q7")


def test_gate_n_wire_dtype_shape_finite_magnitude_and_extra_outputs() -> None:
    policy = bp.validate_policy(_policy())
    logits, hop = _base()
    assert bp.gate_n(_capture(logits, {12: hop}), policy)["status"] == "pass"
    wrong_dtype = _capture(logits, {12: hop}, wire={12: {"dtype": "bfloat16", "shape": (1, 9, 16)}})
    assert bp.gate_n(wrong_dtype, policy)["status"] == "fail"
    bad_shape = _capture(logits, {12: hop}, wire={12: {"dtype": "float32", "shape": (2, 9, 16)}})
    assert bp.gate_n(bad_shape, policy)["status"] == "fail"
    nonfinite = hop.copy()
    nonfinite[0, 0] = np.inf
    assert bp.gate_n(_capture(logits, {12: nonfinite}), policy)["status"] == "fail"
    huge = hop.copy()
    huge[0, 0] = 1e9
    assert bp.gate_n(_capture(logits, {12: huge}), policy)["status"] == "fail"
    leaking = _capture(logits, {12: hop})
    leaking.cases["c0"].extra_outputs = 1
    assert bp.gate_n(leaking, policy)["status"] == "fail"


def test_gates_g_and_p_are_not_evaluated_with_a_reason() -> None:
    policy = bp.validate_policy(_policy())
    for gate in (bp.gate_g(policy), bp.gate_p(policy)):
        assert gate["status"] == "not_evaluated"
        assert gate["reason"]
        assert gate["checks"] == []


def test_verdict_is_fail_partial_pass_or_pass() -> None:
    passed = {"status": "pass"}
    failed = {"status": "fail"}
    skipped = {"status": "not_evaluated"}
    assert bp.route_verdict({"H": passed, "Q0": passed, "N": passed, "G": skipped, "P": skipped}) == "partial_pass"
    assert bp.route_verdict({"H": passed, "Q0": failed, "N": passed, "G": skipped, "P": skipped}) == "fail"
    assert bp.route_verdict({"H": passed, "Q0": passed, "N": passed, "G": passed, "P": passed}) == "pass"
    # a required gate that was not evaluated can never yield a pass
    assert bp.route_verdict({"H": skipped, "Q0": passed, "N": passed, "G": passed, "P": passed}) == "partial_pass"


# --------------------------------------------------------------------------- calibration


def _h_row(name: str, **over) -> dict:
    metrics = {
        "top1_agreement": 1.0,
        "logits_cosine_min": 1 - 1e-12,
        "logits_max_abs_delta_top": 2e-5,
        "kl_mean": 1e-12,
        "kl_max": 1e-11,
        "hop_cosine_min": 1 - 1e-12,
        "hop_rel_l2_max": 1.5e-6,
        "activation_abs_max": 300.0,
    }
    metrics.update(over)
    return {"name": name, "metrics": metrics}


def _q_row(name: str, **over) -> dict:
    metrics = {"top1_agreement": 0.995, "topk_overlap_mean": 0.97, "logits_cosine_min": 0.9995, "kl_mean": 1e-3, "kl_max": 4e-3}
    metrics.update(over)
    return {"name": name, "metrics": metrics}


def _calibrate(h, q, **kw):
    return bp.calibrate_policy(
        bp.default_policy_v2(),
        h_samples=h,
        q_samples=q,
        context={"calibrated_on": "mac-node", "date": "2026-10-21"},
        policy_id="calibrated-test",
        **kw,
    )


def test_calibration_tightens_h_to_measured_noise_times_margin_and_stays_provisional() -> None:
    policy = _calibrate([_h_row("a"), _h_row("b", logits_max_abs_delta_top=4e-5)], [_q_row("a"), _q_row("b", kl_mean=2e-3)])
    h = policy["gates"]["H"]["thresholds"]
    assert h["logits_max_abs_delta_top"]["value"] == pytest.approx(4e-4)
    assert h["top1_agreement"]["value"] == 1.0
    assert 1 - 1e-6 < h["logits_cosine_min"]["value"] < 1 - 1e-9
    assert h["hop_rel_l2_max"]["value"] == pytest.approx(1.5e-5)
    q = policy["gates"]["Q0"]["thresholds"]
    assert q["kl_mean"]["value"] == pytest.approx(5e-3)  # 3 x 2e-3 = 6e-3 is looser than the prior, so the prior holds
    assert policy["status"] == "provisional" and policy["frozen_at"] is None
    assert policy["calibration"]["calibrated_on"] == "mac-node"
    assert policy["calibration"]["samples"]["H"] == ["a", "b"]
    assert policy["policy_digest"] == bp.policy_digest(policy)


def test_calibrated_thresholds_are_never_looser_than_the_prior_when_legit_samples_pass() -> None:
    prior = bp.validate_policy(bp.default_policy_v2())
    policy = _calibrate([_h_row("a", kl_mean=3e-6)], [_q_row("a")])
    for gate in ("H", "Q0"):
        for metric, spec in policy["gates"][gate]["thresholds"].items():
            base = prior["gates"][gate]["thresholds"][metric]
            if spec["op"] == "<=":
                assert spec["value"] <= base["value"] + 1e-18
            else:
                assert spec["value"] >= base["value"] - 1e-18


def test_every_legit_sample_passes_its_own_calibrated_gate() -> None:
    h = [_h_row("a"), _h_row("b", logits_max_abs_delta_top=3e-5, hop_rel_l2_max=2e-6)]
    q = [_q_row("a"), _q_row("b", top1_agreement=0.99, kl_mean=2e-3)]
    policy = _calibrate(h, q)
    for row in h:
        assert all(c["pass"] for c in bp.evaluate_thresholds(row["metrics"], policy["gates"]["H"]["thresholds"]))
    for row in q:
        assert all(c["pass"] for c in bp.evaluate_thresholds(row["metrics"], policy["gates"]["Q0"]["thresholds"]))


def test_a_legit_sample_that_violates_the_prior_relaxes_and_is_flagged() -> None:
    q = [_q_row("a", top1_agreement=0.97)]
    policy = _calibrate([_h_row("a")], q)
    note = policy["calibration"]["per_metric"]["Q0"]["top1_agreement"]
    assert note["prior_exceeded"] is True
    assert policy["gates"]["Q0"]["thresholds"]["top1_agreement"]["value"] <= 0.97


def test_calibration_requires_samples_and_complete_metrics() -> None:
    with pytest.raises(bp.BackendParityError, match="parity_calibration_empty"):
        _calibrate([], [_q_row("a")])
    incomplete = _h_row("a")
    del incomplete["metrics"]["kl_mean"]
    with pytest.raises(bp.BackendParityError, match="parity_calibration_metric_missing"):
        _calibrate([incomplete], [_q_row("a")])
