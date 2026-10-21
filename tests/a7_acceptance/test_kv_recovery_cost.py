"""PV4: KV transfer vs recompute cost model (pure unit tests, no runtime).

``kv_bytes = layers x kv_heads x head_dim x 2 x seq x dtype bytes``;
``transfer_s = rtt + bytes_to_move / measured bandwidth`` against
``recompute_s = (prompt + committed tokens) / measured prefill rate``.  The
break-even direction is proven with the real Qwen2.5-0.5B and 7B attention
shapes at 512 / 2k / 8k tokens over slow and fast links.
"""

from __future__ import annotations

import math

import pytest

import mycelium_kv_recovery_cost as cost

MiB = 1024 * 1024

# Qwen2.5-0.5B: 24 layers, 2 kv heads (GQA 14:2), head_dim 64.
# Qwen2.5-7B:   28 layers, 4 kv heads (GQA 28:4), head_dim 128.
SHAPE_05B = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="float16")
SHAPE_7B = cost.KVShape(layers=28, kv_heads=4, head_dim=128, dtype="float16")

SLOW = cost.LinkMeasurement(bandwidth_bytes_per_s=350_000.0, rtt_s=0.080, source="probe")  # phone hotspot
MID = cost.LinkMeasurement(bandwidth_bytes_per_s=12_000_000.0, rtt_s=0.010, source="probe")
FAST = cost.LinkMeasurement(bandwidth_bytes_per_s=100_000_000.0, rtt_s=0.003, source="probe")  # good Wi-Fi / LAN

PREFILL_05B = cost.PrefillMeasurement(tokens_per_s=4000.0, source="calibration")
PREFILL_7B = cost.PrefillMeasurement(tokens_per_s=300.0, source="calibration")


# -- bytes ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "seq", "expected"),
    [
        (SHAPE_05B, 512, 24 * 2 * 64 * 2 * 512 * 2),
        (SHAPE_05B, 2048, 25_165_824),
        (SHAPE_05B, 8192, 100_663_296),
        (SHAPE_7B, 512, 29_360_128),
        (SHAPE_7B, 2048, 117_440_512),  # ~112 MiB
        (SHAPE_7B, 8192, 469_762_048),
    ],
)
def test_kv_bytes_is_layers_heads_dim_k_and_v_seq_dtype(shape, seq, expected) -> None:
    assert shape.kv_bytes(seq) == expected
    assert shape.kv_bytes(0) == 0


def test_dtype_widths_and_quantised_kv_shrink_the_bytes() -> None:
    base = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="float16").kv_bytes(2048)
    fp32 = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="float32").kv_bytes(2048)
    bf16 = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="bfloat16").kv_bytes(2048)
    assert fp32 == 2 * base and bf16 == base
    int8 = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="int8").kv_bytes(2048)
    int4 = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="int4").kv_bytes(2048)
    assert int8 == base // 2 and int4 == base // 4
    # Group scales cost something: INT8 with a 2-byte scale per 32 elements.
    grouped = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="int8", quant_group_size=32, quant_scale_bytes=2)
    assert int8 < grouped.kv_bytes(2048) < base
    assert grouped.kv_bytes(2048) == int8 + (int8 // 32) * 2


def test_unknown_dtype_and_bad_shapes_fail_closed() -> None:
    with pytest.raises(cost.KVCostError):
        cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="float7")
    for field in ("layers", "kv_heads", "head_dim"):
        kwargs = {"layers": 24, "kv_heads": 2, "head_dim": 64, field: 0}
        with pytest.raises(cost.KVCostError):
            cost.KVShape(**kwargs)
    with pytest.raises(cost.KVCostError):
        SHAPE_05B.kv_bytes(-1)


def test_sliding_window_and_mla_shrink_the_bytes() -> None:
    full = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="float16")
    # Half the layers keep only a 1024-token window.
    windowed = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="float16", sliding_window=1024, windowed_layers=12)
    per_layer_token = 2 * 64 * 2 * 2
    assert windowed.kv_bytes(512) == full.kv_bytes(512)  # below the window: no saving
    assert windowed.kv_bytes(8192) == 12 * per_layer_token * 8192 + 12 * per_layer_token * 1024
    # MLA caches one latent vector per layer per token instead of K and V heads.
    mla = cost.KVShape(layers=24, kv_heads=2, head_dim=64, dtype="float16", mla_latent_dim=576)
    assert mla.kv_bytes(2048) == 24 * 576 * 2 * 2048


def test_shape_from_the_model_config_matches_the_published_qwen_shapes() -> None:
    qwen_05b = {"hidden_size": 896, "num_attention_heads": 14, "num_key_value_heads": 2, "num_hidden_layers": 24, "use_sliding_window": False, "sliding_window": 32768}
    qwen_7b = {"hidden_size": 3584, "num_attention_heads": 28, "num_key_value_heads": 4, "num_hidden_layers": 28, "use_sliding_window": False}
    assert cost.shape_from_model_config(qwen_05b) == SHAPE_05B
    assert cost.shape_from_model_config(qwen_7b) == SHAPE_7B
    # A stage holding 12 of the 28 layers moves 12/28 of the bytes.
    stage = cost.shape_from_model_config(qwen_7b, layers=12)
    assert stage.kv_bytes(2048) == SHAPE_7B.kv_bytes(2048) * 12 // 28
    # MHA (no num_key_value_heads) means kv_heads == attention heads.
    mha = cost.shape_from_model_config({"hidden_size": 128, "num_attention_heads": 4, "num_hidden_layers": 2})
    assert mha.kv_heads == 4 and mha.head_dim == 32


# -- predictions ---------------------------------------------------------------------


def test_prediction_formulae_are_exact() -> None:
    decision = cost.decide_recovery(
        shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=FAST, prefill=PREFILL_7B, margin=0.25
    )
    assert decision["kv_bytes_total"] == 117_440_512
    assert decision["kv_bytes_to_move"] == 117_440_512  # cold transfer: everything moves
    assert math.isclose(decision["transfer_s"], 0.003 + 117_440_512 / 100_000_000.0)
    assert math.isclose(decision["recompute_s"], 2049 / 300.0)


def test_shadowed_bytes_are_not_moved_again() -> None:
    decision = cost.decide_recovery(
        shape=SHAPE_7B,
        kv_positions=2048,
        replay_tokens=2049,
        link=FAST,
        prefill=PREFILL_7B,
        pending_positions=0,
    )
    assert decision["kv_bytes_total"] == 117_440_512
    assert decision["kv_bytes_to_move"] == 0
    assert math.isclose(decision["transfer_s"], 0.003)  # one RTT, no payload
    assert decision["choice"] == "kv"
    assert decision["kv_bytes_shadowed"] == 117_440_512


# -- the break-even direction: Qwen2.5-0.5B and 7B x {512, 2k, 8k} x link -------------

_CASES = [
    # (shape, prefill, seq, link, expected choice)
    # 0.5B break-even bandwidth = 12288 B/token x 4000 tok/s ~ 49 MB/s
    (SHAPE_05B, PREFILL_05B, 512, SLOW, "replay"),
    (SHAPE_05B, PREFILL_05B, 2048, SLOW, "replay"),
    (SHAPE_05B, PREFILL_05B, 8192, SLOW, "replay"),
    (SHAPE_05B, PREFILL_05B, 512, MID, "replay"),
    (SHAPE_05B, PREFILL_05B, 2048, MID, "replay"),
    (SHAPE_05B, PREFILL_05B, 8192, MID, "replay"),
    (SHAPE_05B, PREFILL_05B, 512, FAST, "kv"),
    (SHAPE_05B, PREFILL_05B, 2048, FAST, "kv"),
    (SHAPE_05B, PREFILL_05B, 8192, FAST, "kv"),
    # 7B break-even bandwidth = 57344 B/token x 300 tok/s ~ 17 MB/s
    (SHAPE_7B, PREFILL_7B, 512, SLOW, "replay"),
    (SHAPE_7B, PREFILL_7B, 2048, SLOW, "replay"),
    (SHAPE_7B, PREFILL_7B, 8192, SLOW, "replay"),
    (SHAPE_7B, PREFILL_7B, 512, MID, "replay"),
    (SHAPE_7B, PREFILL_7B, 2048, MID, "replay"),
    (SHAPE_7B, PREFILL_7B, 8192, MID, "replay"),
    (SHAPE_7B, PREFILL_7B, 512, FAST, "kv"),
    (SHAPE_7B, PREFILL_7B, 2048, FAST, "kv"),
    (SHAPE_7B, PREFILL_7B, 8192, FAST, "kv"),
]


@pytest.mark.parametrize(("shape", "prefill", "seq", "link", "expected"), _CASES)
def test_break_even_direction_cold_transfer(shape, prefill, seq, link, expected) -> None:
    decision = cost.decide_recovery(
        shape=shape, kv_positions=seq, replay_tokens=seq + 1, link=link, prefill=prefill, margin=0.25
    )
    assert decision["choice"] == expected, decision
    assert decision["basis"] == "clear_win", decision
    # The choice is the cheaper predicted side, never the other way round.
    cheaper = "kv" if decision["transfer_s"] < decision["recompute_s"] else "replay"
    assert cheaper == expected


def test_break_even_bandwidth_is_per_token_bytes_times_prefill_rate() -> None:
    """Above ~bytes/token x tok/s the KV wins; below it replay wins (long sequence, tiny RTT)."""

    seq = 8192
    break_even = SHAPE_7B.kv_bytes(1) * PREFILL_7B.tokens_per_s  # ~17.2 MB/s
    above = cost.LinkMeasurement(bandwidth_bytes_per_s=break_even * 1.5, rtt_s=0.001, source="probe")
    below = cost.LinkMeasurement(bandwidth_bytes_per_s=break_even * 0.6, rtt_s=0.001, source="probe")
    assert cost.decide_recovery(shape=SHAPE_7B, kv_positions=seq, replay_tokens=seq + 1, link=above, prefill=PREFILL_7B)["choice"] == "kv"
    assert cost.decide_recovery(shape=SHAPE_7B, kv_positions=seq, replay_tokens=seq + 1, link=below, prefill=PREFILL_7B)["choice"] == "replay"


def test_rtt_dominates_short_sequences_on_a_fast_pipe() -> None:
    # 0.5B at 128 tokens: 1.5 MB moves in ~16 ms but a 300 ms RTT link makes replay (32 ms) cheaper.
    lagging = cost.LinkMeasurement(bandwidth_bytes_per_s=100_000_000.0, rtt_s=0.300, source="probe")
    decision = cost.decide_recovery(shape=SHAPE_05B, kv_positions=128, replay_tokens=129, link=lagging, prefill=PREFILL_05B)
    assert decision["choice"] == "replay" and decision["basis"] == "clear_win"


def test_quantised_kv_moves_the_break_even_toward_transfer() -> None:
    # 12 MB/s sits below the fp16 7B break-even (~17 MB/s): fp16 recomputes, INT4 KV (4x fewer bytes) transfers.
    link = cost.LinkMeasurement(bandwidth_bytes_per_s=12_000_000.0, rtt_s=0.005, source="probe")
    fp16 = cost.decide_recovery(shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=link, prefill=PREFILL_7B)
    int4 = cost.decide_recovery(
        shape=cost.KVShape(layers=28, kv_heads=4, head_dim=128, dtype="int4"),
        kv_positions=2048,
        replay_tokens=2049,
        link=link,
        prefill=PREFILL_7B,
    )
    assert int4["kv_bytes_total"] == fp16["kv_bytes_total"] // 4
    assert int4["transfer_s"] < fp16["transfer_s"]
    assert fp16["choice"] == "replay" and int4["choice"] == "kv"
    assert int4["kv_dtype"] == "int4"


# -- margin, ties and missing inputs --------------------------------------------------


def test_within_the_margin_the_existing_kv_behaviour_stands() -> None:
    # 7B, 2k tokens: recompute 2049/300 = 6.83 s.  Pick a bandwidth that makes transfer ~7.5 s.
    link = cost.LinkMeasurement(bandwidth_bytes_per_s=117_440_512 / 7.49, rtt_s=0.01, source="probe")
    decision = cost.decide_recovery(shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=link, prefill=PREFILL_7B, margin=0.25)
    assert decision["transfer_s"] > decision["recompute_s"]  # replay is nominally cheaper...
    assert decision["choice"] == "kv"  # ...but not by the safety margin
    assert decision["basis"] == "within_margin_default"
    # The same inputs with no margin let the cheaper side win.
    exact = cost.decide_recovery(shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=link, prefill=PREFILL_7B, margin=0.0)
    assert exact["choice"] == "replay" and exact["basis"] == "clear_win"


def test_tie_break_can_default_to_replay() -> None:
    link = cost.LinkMeasurement(bandwidth_bytes_per_s=117_440_512 / 6.5, rtt_s=0.01, source="probe")
    decision = cost.decide_recovery(
        shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=link, prefill=PREFILL_7B, margin=0.25, tie_break="replay"
    )
    assert decision["basis"] == "within_margin_default" and decision["choice"] == "replay"


@pytest.mark.parametrize(
    ("link", "prefill", "missing"),
    [
        (None, PREFILL_7B, "link"),
        (FAST, None, "prefill"),
        (None, None, "link,prefill"),
        (cost.LinkMeasurement(bandwidth_bytes_per_s=0.0, rtt_s=0.0, source="probe"), PREFILL_7B, "link"),
        (FAST, cost.PrefillMeasurement(tokens_per_s=float("nan"), source="recent_timings"), "prefill"),
    ],
)
def test_unmeasured_inputs_keep_the_existing_behaviour_and_say_so(link, prefill, missing) -> None:
    decision = cost.decide_recovery(shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=link, prefill=prefill)
    assert decision["choice"] == "kv"  # A7 continuation is today's behaviour when both are possible
    assert decision["basis"] == "inputs_unavailable"
    assert decision["missing_inputs"] == missing.split(",")
    assert "transfer_s" in decision and "recompute_s" in decision


def test_measurement_values_are_validated() -> None:
    with pytest.raises(cost.KVCostError):
        cost.LinkMeasurement(bandwidth_bytes_per_s=-1.0, rtt_s=0.0, source="probe")
    with pytest.raises(cost.KVCostError):
        cost.LinkMeasurement(bandwidth_bytes_per_s=1.0, rtt_s=-0.1, source="probe")
    with pytest.raises(cost.KVCostError):
        cost.PrefillMeasurement(tokens_per_s=-5.0, source="calibration")
    with pytest.raises(cost.KVCostError):
        cost.decide_recovery(shape=SHAPE_7B, kv_positions=-1, replay_tokens=1, link=FAST, prefill=PREFILL_7B)
    with pytest.raises(cost.KVCostError):
        cost.decide_recovery(shape=SHAPE_7B, kv_positions=10, replay_tokens=11, link=FAST, prefill=PREFILL_7B, margin=-0.1)
    with pytest.raises(cost.KVCostError):
        cost.decide_recovery(shape=SHAPE_7B, kv_positions=10, replay_tokens=11, link=FAST, prefill=PREFILL_7B, tie_break="both")


# -- the recorded receipt ------------------------------------------------------------


def test_record_is_closed_privacy_reduced_and_names_both_predictions() -> None:
    decision = cost.decide_recovery(
        shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=FAST, prefill=PREFILL_7B, margin=0.25
    )
    assert decision["protocol"] == cost.COST_DECISION_PROTOCOL
    assert set(decision) == cost.COST_DECISION_FIELDS
    assert decision["margin"] == 0.25 and decision["tie_break"] == "kv"
    assert decision["link_source"] == "probe" and decision["prefill_source"] == "calibration"
    assert decision["kv_dtype"] == "float16"
    assert decision["kv_positions"] == 2048 and decision["replay_tokens"] == 2049
    assert decision["link_bandwidth_bytes_per_s"] == 100_000_000.0
    assert decision["prefill_tokens_per_s"] == 300.0
    assert all(isinstance(value, (str, int, float, bool, list, type(None))) for value in decision.values())


def test_summary_reads_like_the_owner_asked() -> None:
    kv = cost.decide_recovery(shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=FAST, prefill=PREFILL_7B)
    assert kv["summary"].startswith("continued from KV: 112.0 MiB in ~1.2 s beats recompute ~6.8 s"), kv["summary"]
    replay = cost.decide_recovery(shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=SLOW, prefill=PREFILL_7B)
    assert replay["summary"].startswith("recomputed (replay): KV transfer of 112.0 MiB would take ~336 s vs recompute ~6.8 s"), replay["summary"]
    shadowed = cost.decide_recovery(
        shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=FAST, prefill=PREFILL_7B, pending_positions=0
    )
    assert "112.0 MiB already shadowed" in shadowed["summary"]
    unknown = cost.decide_recovery(shape=SHAPE_7B, kv_positions=2048, replay_tokens=2049, link=None, prefill=PREFILL_7B)
    assert "continued from KV" in unknown["summary"] and "not measured" in unknown["summary"]


# -- measuring: link estimator, bounded probe, prefill tracker -----------------------


def test_link_from_samples_recovers_rtt_and_bandwidth() -> None:
    # t = 0.02 + bytes / 25 MB/s
    samples = [(n, 0.02 + n / 25_000_000.0) for n in (4096, 262_144, 1_048_576)]
    link = cost.link_from_samples(samples, source="probe")
    assert link is not None
    assert math.isclose(link.rtt_s, 0.02, rel_tol=0.02, abs_tol=1e-4)
    assert math.isclose(link.bandwidth_bytes_per_s, 25_000_000.0, rel_tol=0.02)


def test_link_from_samples_needs_two_distinct_sizes() -> None:
    assert cost.link_from_samples([], source="probe") is None
    assert cost.link_from_samples([(4096, 0.01)], source="probe") is None
    assert cost.link_from_samples([(4096, 0.01), (4096, 0.012)], source="probe") is None
    # No size dependence at all (a stalled or cached path): refuse to invent a bandwidth.
    assert cost.link_from_samples([(4096, 0.05), (1_048_576, 0.05)], source="probe") is None


def test_bounded_probe_runs_the_sizes_within_budget() -> None:
    now = [0.0]
    sent: list[int] = []

    def send(nbytes: int) -> None:
        sent.append(nbytes)
        now[0] += 0.01 + nbytes / 50_000_000.0

    link = cost.probe_link(send, clock=lambda: now[0], sizes=(1024, 65_536, 1_048_576), budget_s=5.0)
    assert sent == [1024, 65_536, 1_048_576]
    assert link is not None and math.isclose(link.bandwidth_bytes_per_s, 50_000_000.0, rel_tol=0.05)
    assert link.source == "probe"


def test_bounded_probe_stops_at_the_budget_and_survives_failures() -> None:
    now = [0.0]
    sent: list[int] = []

    def slow(nbytes: int) -> None:
        sent.append(nbytes)
        now[0] += 3.0

    # The first send already spends 3 s of a 4 s budget: the next one is skipped, so no estimate exists.
    assert cost.probe_link(slow, clock=lambda: now[0], sizes=(1024, 1_048_576), budget_s=4.0) is None
    assert sent == [1024]

    def broken(nbytes: int) -> None:
        raise OSError("link down")

    assert cost.probe_link(broken, clock=lambda: now[0], sizes=(1024, 1_048_576), budget_s=4.0) is None


def test_prefill_tracker_uses_recent_timings_then_calibration() -> None:
    tracker = cost.PrefillRateTracker(calibration_tokens_per_s=250.0, window=4)
    assert tracker.measurement().source == "calibration" and tracker.measurement().tokens_per_s == 250.0
    for tokens, seconds in ((1000, 4.0), (1000, 5.0), (1000, 2.0)):
        tracker.observe(tokens, seconds)
    measured = tracker.measurement()
    assert measured.source == "recent_timings"
    assert measured.tokens_per_s == 250.0  # median of 250, 200, 500
    # Bad samples are ignored, the window is bounded.
    tracker.observe(0, 1.0)
    tracker.observe(100, 0.0)
    tracker.observe(100, float("nan"))
    for _ in range(10):
        tracker.observe(1000, 1.0)
    assert tracker.measurement().tokens_per_s == 1000.0


def test_prefill_tracker_without_any_measurement_is_none() -> None:
    assert cost.PrefillRateTracker().measurement() is None


# -- the policy factory the entry controller calls ------------------------------------


def test_policy_factory_builds_the_decision_from_the_cutover_context() -> None:
    policy = cost.make_recovery_cost_policy(
        shape_for=lambda stage_id: SHAPE_7B,
        link_for=lambda context: FAST,
        prefill_for=lambda context: PREFILL_7B,
        margin=0.25,
    )
    decision = policy({"stage_id": "stage-1", "kv_positions": 2048, "replay_tokens": 2049, "pending_positions": 0})
    assert decision["choice"] == "kv" and decision["kv_bytes_to_move"] == 0
    # The replay also needs one round trip to start the prefill on the successor: it is charged to recompute.
    assert math.isclose(decision["recompute_s"], 2049 / 300.0 + 0.003)
    slow_policy = cost.make_recovery_cost_policy(
        shape_for=lambda stage_id: SHAPE_7B,
        link_for=lambda context: SLOW,
        prefill_for=lambda context: PREFILL_7B,
        always_move_all=True,
    )
    cold = slow_policy({"stage_id": "stage-1", "kv_positions": 2048, "replay_tokens": 2049, "pending_positions": 0})
    assert cold["choice"] == "replay" and cold["kv_bytes_to_move"] == 117_440_512


def test_policy_factory_without_a_shape_returns_none() -> None:
    policy = cost.make_recovery_cost_policy(shape_for=lambda stage_id: None, link_for=lambda c: FAST, prefill_for=lambda c: PREFILL_7B)
    assert policy({"stage_id": "x", "kv_positions": 10, "replay_tokens": 11, "pending_positions": 0}) is None


# -- keeping a shadow stream at all ----------------------------------------------------


def test_shadow_stream_overhead_and_break_even_loss_probability() -> None:
    # The entry gates every decode on the standby ack, so a shadow costs one RTT + the token's KV per token.
    committed = 256
    overhead_7b = cost.shadow_stream_overhead_s(SHAPE_7B, committed_tokens=committed, link=FAST)
    assert math.isclose(overhead_7b, committed * (0.003 + SHAPE_7B.kv_bytes(1) / 100_000_000.0))
    recompute_7b = 2300 / PREFILL_7B.tokens_per_s
    p = cost.shadow_break_even_loss_probability(overhead_s=overhead_7b, recompute_s=recompute_7b, resume_s=0.003)
    # The shadow pays when (loss probability x time saved) exceeds its overhead.
    assert 0.0 < p < 1.0
    assert math.isclose(p, overhead_7b / (recompute_7b - 0.003))
    # A cheap-KV, fast-prefill model (0.5B): recompute is so cheap that even a certain loss does not pay for the shadow.
    overhead_05b = cost.shadow_stream_overhead_s(SHAPE_05B, committed_tokens=committed, link=FAST)
    p_05b = cost.shadow_break_even_loss_probability(
        overhead_s=overhead_05b, recompute_s=2300 / PREFILL_05B.tokens_per_s, resume_s=0.003
    )
    assert p_05b > 1.0
    # When resuming from KV is no cheaper than recomputing, a shadow never pays.
    assert cost.shadow_break_even_loss_probability(overhead_s=overhead_7b, recompute_s=0.1, resume_s=0.2) == math.inf
