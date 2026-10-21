"""PV29 L1: route executors, oracle and the seeded-defect battery on a tiny Qwen2-shaped model.

Runs the *production* NumPy and MLX per-block kernels in process (no 0.5B weights); the
real-checkpoint calibration is `scripts/run_backend_parity.py` (see the calibrated policy
test in test_backend_parity_calibrated_policy.py).
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

pytest.importorskip("mlx.core")

from mycelium_platform_neutral import backend_parity as bp  # noqa: E402
from mycelium_platform_neutral import backend_parity_route as rt  # noqa: E402

REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"


def _corpus(vocab: int = 128) -> dict:
    rng = np.random.default_rng(99)
    cases = []
    for index, (length, prefill) in enumerate([(14, 4), (12, 3), (16, 6), (13, 5)]):
        ids = rng.integers(0, vocab, size=length).tolist()
        if index == 2:
            ids = [7] * length  # repeated-token case
        cases.append({"case_id": f"case{index}", "kind": "synthetic", "text": "", "token_ids": ids, "prefill_len": prefill})
    return bp.validate_corpus(
        {"protocol": bp.CORPUS_PROTOCOL, "model_id": "Qwen/Qwen2.5-0.5B-Instruct", "revision": REVISION, "cases": cases}
    )


@pytest.fixture(scope="module")
def ctx() -> rt.Context:
    config, tensors = rt.synthetic_model()
    return rt.Context(config=config, base=tensors, corpus=_corpus())


@pytest.fixture(scope="module")
def policy() -> dict:
    return bp.validate_policy(bp.default_policy_v2())


@pytest.fixture(scope="module")
def oracle(ctx: rt.Context) -> bp.RouteCapture:
    return rt.generate_numpy_oracle(ctx, boundaries=(2,))


@pytest.fixture(scope="module")
def evaluator(ctx, policy, oracle) -> rt.Evaluator:
    return rt.Evaluator(ctx, policy, oracle)


def _plan(ctx: rt.Context, first: str = "mlx", second: str = "numpy") -> rt.RoutePlan:
    return rt.two_stage_plan(ctx.config["n_layer"], first, second)


def test_monolith_with_kv_matches_the_independent_full_context_oracle(ctx, oracle) -> None:
    mono = rt.run_monolith(ctx, ["none", "none"], (2,), (2,))
    for case_id, case in mono.cases.items():
        ref = oracle.cases[case_id]
        assert case.logits.shape == ref.logits.shape
        assert np.abs(case.logits - ref.logits).max() < 1e-4
        assert np.abs(case.hops[2] - ref.hops[2]).max() < 1e-4


@pytest.mark.parametrize("first,second", [("mlx", "numpy"), ("numpy", "mlx"), ("numpy", "numpy"), ("mlx", "mlx")])
def test_clean_routes_pass_h_q0_and_n(evaluator, ctx, first, second) -> None:
    gates, capture = evaluator.evaluate(_plan(ctx, first, second))
    assert gates["H"]["status"] == "pass", gates["H"]["checks"]
    assert gates["Q0"]["status"] == "pass", gates["Q0"]["checks"]
    assert gates["N"]["status"] == "pass", gates["N"]["metrics"]
    assert gates["G"]["status"] == gates["P"]["status"] == "not_evaluated"
    assert bp.route_verdict(gates) == "partial_pass"
    for case in capture.cases.values():
        assert set(case.hops) == {2}
        assert case.hop_wire[2]["dtype"] == "float32"
        assert case.hop_wire[2]["shape"][0] == 1


def test_hop_rows_cover_every_position_prefill_then_decode(evaluator, ctx) -> None:
    _, capture = evaluator.evaluate(_plan(ctx))
    for case in ctx.corpus["cases"]:
        assert capture.cases[case["case_id"]].hops[2].shape == (len(case["token_ids"]) - 1, ctx.config["n_embd"])
        assert capture.cases[case["case_id"]].logits.shape[0] == len(bp.scored_positions(case))


def test_wire_modes_round_trip_through_the_real_codec() -> None:
    x = np.random.default_rng(0).normal(size=(1, 3, 8)).astype(np.float32)
    exact, meta = rt._wire_hop(x, "float32")
    assert np.array_equal(exact, x) and meta == {"dtype": "float32", "shape": (1, 3, 8)}
    rounded, meta = rt._wire_hop(x, "bfloat16_declared")
    assert meta["dtype"] == "bfloat16"
    assert 0 < np.abs(rounded - x).max() <= np.abs(x).max() * 2**-8
    assert np.array_equal(rounded, rt._bf16_round(x))
    wrong, meta = rt._wire_hop(x, "bf16_bits_as_fp16")
    assert meta["dtype"] == "float16" and not np.allclose(wrong, x, rtol=0.1)


def test_bf16_round_is_nearest_even_and_idempotent() -> None:
    x = np.array([1.0, 1.00390625, 1.005859375, -3.14159, 0.0], dtype=np.float32)
    r = rt._bf16_round(x)
    assert np.array_equal(rt._bf16_round(r), r)
    assert r[1] == np.float32(1.0)  # exact tie rounds to even
    assert r[2] == np.float32(1.0078125)


def test_synthetic_weights_are_bf16_representable(ctx) -> None:
    for array in ctx.base.values():
        assert np.array_equal(rt._bf16_round(array), array)


@pytest.fixture(scope="module")
def calibrated(ctx, policy, oracle) -> dict:
    """Policy calibrated from the controls on the tiny model (the prior is real-model scaled)."""

    rows = rt.run_scenarios(ctx, rt.Evaluator(ctx, policy, oracle), lambda: _plan(ctx), [s for s in rt.default_scenarios() if s.kind == "control"])
    h, q = rt.legit_samples(rows)
    return bp.calibrate_policy(policy, h_samples=h, q_samples=q, context={"calibrated_on": "unit-test"}, policy_id="synthetic-tiny")


def test_seeded_defect_battery_every_defect_fails_every_control_passes(ctx, oracle, calibrated) -> None:
    rows = rt.run_scenarios(ctx, rt.Evaluator(ctx, calibrated, oracle), lambda: _plan(ctx), rt.default_scenarios())
    summary = bp.summarize_battery(rows)
    assert summary["defects_total"] >= 20 and summary["controls_total"] >= 8
    assert not summary["problems"], summary["problems"]
    assert summary["all_defects_fail"] and summary["all_controls_pass"]
    # the stage-1-only eps change is below the float noise floor: reported, not claimed detected
    assert summary["observations_total"] == 1 and summary["observations_detected"] == 0


def test_defects_name_the_metric_that_catches_them(ctx, oracle, calibrated) -> None:
    rows = {r["name"]: r for r in rt.run_scenarios(ctx, rt.Evaluator(ctx, calibrated, oracle), lambda: _plan(ctx), [s for s in rt.default_scenarios() if s.kind == "defect"])}
    assert "logits_max_abs_delta_top" in rows["defect_hop_bf16_rounded_on_f32_wire"]["failed"]["H"]
    assert "hop_rel_l2_max" in rows["defect_hop_bf16_rounded_on_f32_wire"]["failed"]["H"]
    assert rows["defect_hop_bfloat16_frame"]["failed"]["N"] == ["wire_dtype_mismatch_count"]
    assert "hop_cosine_min" in rows["defect_layer_range_short_stage0"]["failed"]["H"]
    assert "top1_agreement" in rows["defect_layer_range_skip"]["failed"]["H"]
    assert "Q0" in rows["defect_int4_rowwise_claimed_q0"]["failed"]


def test_each_wire_defect_is_caught_by_the_gate_that_owns_it(ctx, oracle, calibrated) -> None:
    by_name = {s.name: s for s in rt.default_scenarios()}
    rows = {r["name"]: r for r in rt.run_scenarios(ctx, rt.Evaluator(ctx, calibrated, oracle), lambda: _plan(ctx), [by_name["defect_hop_bfloat16_frame"], by_name["defect_non_final_stage_leaks_logits"]])}
    assert rows["defect_hop_bfloat16_frame"]["gate_status"]["N"] == "fail"
    assert rows["defect_non_final_stage_leaks_logits"]["gate_status"]["N"] == "fail"


def test_a_nan_producing_route_fails_instead_of_crashing(ctx, evaluator) -> None:
    plan = _plan(ctx)
    plan.stages[1].config_overrides = {"rope_theta": 0.0}  # inf frequencies -> nan
    gates, _ = evaluator.evaluate(plan)
    assert bp.route_verdict(gates) == "fail"


def test_oracle_round_trip_and_digest_binding(tmp_path, ctx, oracle) -> None:
    info = rt.save_oracle(tmp_path / "oracle.npz", oracle, {"implementation": "numpy-full-context", "runtime_digest": "sha256:" + "0" * 64})
    loaded = rt.load_oracle(tmp_path / "oracle.npz", ctx.corpus, expected_digest=info["archive_digest"])
    for case_id in oracle.cases:
        assert np.array_equal(loaded.cases[case_id].logits, oracle.cases[case_id].logits)
        assert np.array_equal(loaded.cases[case_id].hops[2], oracle.cases[case_id].hops[2])
    with pytest.raises(bp.BackendParityError, match="parity_oracle_digest_mismatch"):
        rt.load_oracle(tmp_path / "oracle.npz", ctx.corpus, expected_digest="sha256:" + "1" * 64)
    other = _corpus()
    other["cases"][0]["token_ids"] = other["cases"][0]["token_ids"] + [3, 4]
    with pytest.raises(bp.BackendParityError, match="parity_oracle_shape_mismatch"):
        rt.load_oracle(tmp_path / "oracle.npz", other)


def test_read_safetensors_f32_decodes_bf16_f16_and_f32(tmp_path) -> None:
    import json

    a = np.array([[1.0, -2.5], [0.15625, 3.0]], dtype=np.float32)
    bf16 = (a.view(np.uint32) >> 16).astype("<u2").tobytes()
    f16 = a.astype("<f2").tobytes()
    f32 = a.astype("<f4").tobytes()
    blobs = {"a.bf16": ("BF16", bf16), "b.f16": ("F16", f16), "c.f32": ("F32", f32)}
    header, offset, payload = {}, 0, b""
    for name, (dtype, blob) in blobs.items():
        header[name] = {"dtype": dtype, "shape": [2, 2], "data_offsets": [offset, offset + len(blob)]}
        offset += len(blob)
        payload += blob
    raw = json.dumps(header).encode()
    path = tmp_path / "m.safetensors"
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + payload)
    tensors = rt.read_safetensors_f32(path)
    assert set(tensors) == set(blobs)
    for value in tensors.values():
        assert np.array_equal(value, a)


def test_route_document_is_a_valid_digest_bound_route(ctx) -> None:
    document = rt.route_document(ctx, _plan(ctx), model_id="Qwen/Qwen2.5-0.5B-Instruct", revision=REVISION, source_digest="sha256:" + "a" * 64)
    assert bp.validate_route(document)["stages"][0]["backend"] == "mlx"
    assert document["stages"][1]["device_class"] == "cpu"
    swapped = rt.route_document(ctx, _plan(ctx, "numpy", "mlx"), model_id="Qwen/Qwen2.5-0.5B-Instruct", revision=REVISION, source_digest="sha256:" + "a" * 64)
    assert bp.route_digest(swapped) != bp.route_digest(document)
