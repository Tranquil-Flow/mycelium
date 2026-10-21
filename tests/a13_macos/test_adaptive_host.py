# SPDX-License-Identifier: AGPL-3.0-only
"""A16 optional adaptive host-resource mode: deterministic cases only.

Every test uses a fake clock and fake samples.  Nothing here reads real host
memory pressure, launches an application or touches a device; the two-Mac
physical cases in the A16 draft remain unproven by this file.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from mycelium_a13_macos import adaptive_host as ah

GIB = 1 << 30


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def on_policy(**changes):
    policy = dict(ah.DEFAULT_POLICY)
    policy["timings"] = {}
    policy["enabled"] = True
    policy.update(changes)
    return policy


def sample(clock, pressure="normal", **extra):
    value = {"observed_at": clock.now, "pressure": pressure}
    value.update(extra)
    return value


def make(clock=None, policy=None, footprint=0, inflight=0):
    clock = clock or Clock()
    state = {"inflight": inflight}
    ctl = ah.AdaptiveHostController(
        policy or on_policy(),
        clock=clock,
        footprint_bytes=lambda: footprint,
        inflight=lambda: state["inflight"],
    )
    return ctl, clock, state


def tick(ctl, clock, seconds=2.0, pressure="normal", **extra):
    clock.advance(seconds)
    return ctl.observe(sample(clock, pressure, **extra))


def settle_authority(ctl, clock, count=2):
    for _ in range(count):
        clock.advance(1.0)
        ctl.record_authority_observation(ctl.generation, observed_at=clock.now)


# ---------------------------------------------------------------- case 1
def test_default_policy_is_off_and_defaults_match_draft_option_a():
    assert ah.DEFAULT_POLICY["enabled"] is False
    assert ah.DEFAULT_POLICY["game_mode"] is False
    assert ah.DEFAULT_TIMINGS == {
        "sample_interval_s": 2,
        "stale_after_s": 6,
        "game_drain_s": 10,
        "pressure_drain_s": 15,
        "zero_capacity_s": 30,
        "withdraw_s": 120,
        "reduced_dwell_s": 30,
        "drain_dwell_s": 60,
        "recovery_s": 120,
        "cpu_sustain_s": 15,
        "authority_observations": 2,
    }


def test_existing_consent_without_adaptive_file_migrates_to_off(tmp_path):
    from mycelium_a13_macos.app import consent_valid

    old_consent = {
        "peer_visibility": True, "power": "ac_only", "storage_bytes": 10**9,
        "network": "unmetered_only", "background": False, "diagnostics": False,
        "thermal": "stop_on_serious",
    }
    assert consent_valid(old_consent)  # A13 consent schema is unchanged
    policy = ah.read_adaptive_policy(tmp_path)
    assert policy["enabled"] is False
    assert ah.AdaptiveHostController(policy, clock=Clock()).admission() is None


def test_opt_in_needs_authorized_owner_and_is_stored_private(tmp_path):
    for bad in (False, None, 1, "yes", object()):
        with pytest.raises(ah.AdaptiveHostError) as raised:
            ah.write_adaptive_policy(tmp_path, on_policy(), owner_authorized=bad)
        assert raised.value.code == "adaptive_toggle_unauthorized"
    assert not (tmp_path / ah.POLICY_FILENAME).exists()
    ah.write_adaptive_policy(tmp_path, on_policy(), owner_authorized=True)
    path = tmp_path / ah.POLICY_FILENAME
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ah.read_adaptive_policy(tmp_path)["enabled"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(extra=1),
        lambda p: p.update(enabled="true"),
        lambda p: p.update(enabled=1),
        lambda p: p.update(game_mode=None),
        lambda p: p.update(schema=2),
        lambda p: p.update(reserve_bytes=-1),
        lambda p: p.update(reserve_bytes=True),
        lambda p: p.update(cpu_saturation_fraction=1.5),
        lambda p: p.update(cpu_saturation_fraction=float("nan")),
        lambda p: p.update(timings={"recovery_s": 0}),
        lambda p: p.update(timings={"recovery_s": float("inf")}),
        lambda p: p.update(timings={"recovery_s": True}),
        lambda p: p.update(timings={"unknown_key": 5}),
        lambda p: p.update(timings={"authority_observations": 1}),
        lambda p: p.update(timings={"game_drain_s": 20, "pressure_drain_s": 15}),
        lambda p: p.update(timings={"pressure_drain_s": 40, "zero_capacity_s": 30}),
        lambda p: p.update(timings={"stale_after_s": 1}),
    ],
)
def test_malformed_policy_is_refused_on_write_and_read(tmp_path, mutate):
    policy = on_policy()
    mutate(policy)
    with pytest.raises(ah.AdaptiveHostError) as raised:
        ah.write_adaptive_policy(tmp_path, policy, owner_authorized=True)
    assert raised.value.code == "adaptive_policy_invalid"
    path = tmp_path / ah.POLICY_FILENAME
    path.write_text(json.dumps(policy))
    path.chmod(0o600)
    with pytest.raises(ah.AdaptiveHostError) as raised:
        ah.read_adaptive_policy(tmp_path)
    assert raised.value.code == "adaptive_policy_invalid"


def test_changed_file_permissions_symlink_and_size_are_refused(tmp_path):
    ah.write_adaptive_policy(tmp_path, on_policy(), owner_authorized=True)
    path = tmp_path / ah.POLICY_FILENAME
    path.chmod(0o644)
    with pytest.raises(ah.AdaptiveHostError):
        ah.read_adaptive_policy(tmp_path)
    path.unlink()
    target = tmp_path / "elsewhere"
    target.write_text(json.dumps(on_policy()))
    os.symlink(target, path)
    with pytest.raises(ah.AdaptiveHostError):
        ah.read_adaptive_policy(tmp_path)
    path.unlink()
    path.write_bytes(b" " * 5000)
    path.chmod(0o600)
    with pytest.raises(ah.AdaptiveHostError):
        ah.read_adaptive_policy(tmp_path)


def test_owner_slower_timings_are_accepted_and_resolved(tmp_path):
    slower = on_policy(timings={"pressure_drain_s": 25, "zero_capacity_s": 60,
                                "withdraw_s": 300, "recovery_s": 240})
    ah.write_adaptive_policy(tmp_path, slower, owner_authorized=True)
    resolved = ah.resolve_timings(ah.read_adaptive_policy(tmp_path))
    assert resolved["pressure_drain_s"] == 25 and resolved["recovery_s"] == 240
    assert resolved["game_drain_s"] == 10  # unspecified keys keep draft defaults


# ---------------------------------------------------------------- case 2
@pytest.mark.parametrize(
    "key",
    ["process_name", "bundle_id", "window_title", "foreground_app", "apps", "processes"],
)
def test_sample_with_app_identity_is_refused(key):
    with pytest.raises(ah.AdaptiveHostError) as raised:
        ah.parse_sample({"observed_at": 1.0, "pressure": "normal", key: "Game"})
    assert raised.value.code == "adaptive_sample_invalid"


def test_classification_never_reports_healthy_for_bad_or_stale_probes():
    t = ah.resolve_timings(on_policy())
    classify = lambda raw, now=1000.0, fp=0, reserve=0: ah.classify_sample(
        raw, now=now, timings=t, footprint_bytes=fp, reserve_bytes=reserve)
    ok = {"observed_at": 1000.0, "pressure": "normal"}
    assert classify(ok) == "healthy"
    assert classify(None) == "unknown"
    assert classify("garbage") == "unknown"
    assert classify({**ok, "observed_at": 990.0}) == "unknown"          # stale
    assert classify({**ok, "observed_at": 1010.0}) == "unknown"         # from the future
    assert classify({**ok, "pressure": "unknown"}) == "unknown"
    assert classify({**ok, "pressure": "bogus"}) == "unknown"
    assert classify({**ok, "pressure_crosscheck": "critical"}) == "unknown"  # contradiction
    assert classify({**ok, "pressure": "critical", "pressure_crosscheck": "normal"}) == "unknown"
    assert classify({**ok, "pressure": "warn", "pressure_crosscheck": "normal"}) == "moderate"
    assert classify({**ok, "pressure": "warn"}) == "moderate"
    assert classify({**ok, "pressure": "critical"}) == "critical"
    # headroom is judged against the admitted footprint plus owner reserve
    assert classify(ok, fp=8 * GIB) == "unknown"                         # headroom unmeasured
    assert classify({**ok, "available_bytes": 9 * GIB}, fp=8 * GIB) == "healthy"
    assert classify({**ok, "available_bytes": 7 * GIB}, fp=8 * GIB) == "moderate"
    assert classify({**ok, "available_bytes": 9 * GIB}, fp=8 * GIB, reserve=2 * GIB) == "moderate"
    assert classify({**ok, "available_bytes": -1}) == "unknown"


def test_unknown_signal_fences_new_admission_but_not_existing_work():
    ctl, clock, state = make(inflight=2)
    tick(ctl, clock)
    assert ctl.admission() is None
    clock.advance(2)
    ctl.observe(None)  # probe failure
    assert ctl.admission() == "adaptive_signal_unknown"
    assert ctl.mode == "normal"  # no unilateral kill / mode change
    assert ctl.publication()["new_admissions_allowed"] is False
    tick(ctl, clock)
    assert ctl.admission() is None  # a fresh good sample clears a pure signal fence


def test_disabled_controller_never_monitors_or_acts():
    probes = []
    ctl, clock, _ = make(policy=dict(ah.DEFAULT_POLICY))
    status = ctl.observe(sample(clock, "critical"))
    assert status["adaptive_enabled"] is False
    assert ctl.mode == "normal" and ctl.admission() is None
    runtime = ah.AdaptiveHostRuntime(
        policy_loader=lambda: dict(ah.DEFAULT_POLICY),
        sampler=lambda: probes.append(1) or {},
        clock=clock,
    )
    assert runtime.check() is None and probes == []


def test_public_publication_has_only_allowlisted_coarse_fields():
    ctl, clock, _ = make()
    tick(ctl, clock, pressure="critical")
    publication = ctl.publication()
    assert set(publication) <= ah.PUBLICATION_KEYS
    assert publication["reason"] in ah.REASON_CATEGORIES
    blob = json.dumps(publication)
    for word in ("process", "bundle", "window", "foreground", "pid"):
        assert word not in blob


# ---------------------------------------------------------------- case 3
def test_normal_capacity_is_min_of_all_bounds_and_never_inflated():
    cap = ah.effective_capacity("normal", qualified=4, loaded=3, consent=8,
                                available_bytes=None, reserve_bytes=0,
                                per_member_kv_bytes=GIB, committed_members=1)
    assert cap["concurrency"] == 3 and cap["new_admissions"] == 2
    cap = ah.effective_capacity("normal", qualified=2, loaded=9, consent=9,
                                available_bytes=None, reserve_bytes=0,
                                per_member_kv_bytes=GIB, committed_members=0)
    assert cap["concurrency"] == 2


def test_reduced_capacity_follows_measured_headroom_not_a_made_up_fraction():
    cap = ah.effective_capacity("reduced", qualified=8, loaded=8, consent=8,
                                available_bytes=int(3.5 * GIB), reserve_bytes=GIB // 2,
                                per_member_kv_bytes=GIB, committed_members=2)
    assert cap["new_admissions"] == 3 and cap["concurrency"] == 5
    assert cap["kv_budget_bytes"] == 3 * GIB
    # no headroom: committed reservations stay published, nothing new
    cap = ah.effective_capacity("reduced", qualified=8, loaded=8, consent=8,
                                available_bytes=0, reserve_bytes=0,
                                per_member_kv_bytes=GIB, committed_members=2)
    assert cap["new_admissions"] == 0 and cap["concurrency"] == 2
    # unknown headroom while reduced fails closed for new members
    cap = ah.effective_capacity("reduced", qualified=8, loaded=8, consent=8,
                                available_bytes=None, reserve_bytes=0,
                                per_member_kv_bytes=GIB, committed_members=1)
    assert cap["new_admissions"] == 0


def test_drain_and_withdraw_admit_nothing_new_and_withdraw_publishes_zero():
    kwargs = dict(qualified=8, loaded=8, consent=8, available_bytes=64 * GIB,
                  reserve_bytes=0, per_member_kv_bytes=GIB, committed_members=2)
    drain = ah.effective_capacity("drain", **kwargs)
    assert drain["new_admissions"] == 0 and drain["concurrency"] == 2
    withdraw = ah.effective_capacity("withdraw", **kwargs)
    assert withdraw["new_admissions"] == 0 and withdraw["concurrency"] == 0


def test_capacity_cannot_exceed_loaded_stage_or_consent_ceiling_after_recovery():
    cap = ah.effective_capacity("normal", qualified=8, loaded=1, consent=8,
                                available_bytes=64 * GIB, reserve_bytes=0,
                                per_member_kv_bytes=GIB, committed_members=0)
    assert cap["concurrency"] == 1


def test_kv_isolation_committed_prefixes_are_never_mutated_or_truncated():
    committed = {"req-a": {"prefix_tokens": 900, "kv_reserved_bytes": 2 * GIB},
                 "req-b": {"prefix_tokens": 40, "kv_reserved_bytes": GIB}}
    before = copy.deepcopy(committed)
    cap = ah.effective_capacity("reduced", qualified=8, loaded=8, consent=8,
                                available_bytes=GIB, reserve_bytes=0,
                                per_member_kv_bytes=GIB, committed_members=len(committed))
    plan = ah.plan_new_admissions(
        cap, [{"id": "n1", "kv_bytes": GIB}, {"id": "n2", "kv_bytes": GIB}],
        committed=committed)
    assert committed == before
    assert plan["committed_floor_bytes"] == 3 * GIB
    assert [r["id"] for r in plan["accepted"]] == ["n1"]
    assert plan["refused"] == [{"id": "n2", "reason": "adaptive_capacity_reduced"}]


def test_request_whose_full_declared_context_cannot_fit_is_refused_not_truncated():
    cap = ah.effective_capacity("reduced", qualified=8, loaded=8, consent=8,
                                available_bytes=2 * GIB, reserve_bytes=0,
                                per_member_kv_bytes=GIB, committed_members=0)
    plan = ah.plan_new_admissions(cap, [{"id": "big", "kv_bytes": 3 * GIB}], committed={})
    assert plan["accepted"] == []
    assert plan["refused"] == [{"id": "big", "reason": "adaptive_context_exceeds_budget"}]


def test_colocated_nodes_do_not_each_count_the_same_free_ram():
    shares = ah.allocate_shared_headroom(10 * GIB, {"node-b": 8 * GIB, "node-a": 6 * GIB})
    assert shares == {"node-a": 6 * GIB, "node-b": 4 * GIB}
    assert sum(shares.values()) <= 10 * GIB


# ---------------------------------------------------------------- case 4
def test_game_mode_drains_immediately_and_publishes_zero_within_bound():
    ctl, clock, _ = make()
    tick(ctl, clock)
    start = clock.now
    ctl.apply_policy(on_policy(game_mode=True))
    assert ctl.mode == "drain" and ctl.reason == "owner_game_mode"
    publication = ctl.publication()
    assert publication["new_admissions_allowed"] is False
    assert publication["capacity_scale"] in {"committed_only", "zero"}
    assert clock.now - start <= ah.DEFAULT_TIMINGS["game_drain_s"]
    assert ctl.admission() == "adaptive_host_draining"


def test_critical_pressure_bypasses_reduced():
    ctl, clock, _ = make()
    tick(ctl, clock)
    tick(ctl, clock, pressure="critical")
    assert ctl.mode == "drain" and ctl.reason == "host_memory_pressure"


def test_moderate_pressure_reduces_first_then_drains_when_sustained():
    ctl, clock, _ = make()
    tick(ctl, clock)
    tick(ctl, clock, pressure="warn")
    assert ctl.mode == "reduced"
    assert ctl.admission() is None
    for _ in range(6):
        tick(ctl, clock, pressure="warn")
    assert ctl.mode == "reduced"  # 12 s < 15 s
    tick(ctl, clock, pressure="warn")
    tick(ctl, clock, pressure="warn")
    assert ctl.mode == "drain"


def test_owner_slower_pressure_drain_bound_is_honoured():
    ctl, clock, _ = make(policy=on_policy(timings={"pressure_drain_s": 40, "zero_capacity_s": 60}))
    tick(ctl, clock, pressure="warn")
    for _ in range(15):
        tick(ctl, clock, pressure="warn")
    assert ctl.mode == "reduced"  # 30 s < 40 s
    for _ in range(6):
        tick(ctl, clock, pressure="warn")
    assert ctl.mode == "drain"


def test_drain_publishes_committed_only_then_zero_after_zero_capacity_bound():
    ctl, clock, state = make(inflight=1)
    tick(ctl, clock, pressure="critical")
    assert ctl.publication()["capacity_scale"] == "committed_only"
    for _ in range(14):
        tick(ctl, clock, pressure="critical")
    assert ctl.publication()["capacity_scale"] == "committed_only"  # 28 s
    for _ in range(2):
        tick(ctl, clock, pressure="critical")
    assert ctl.publication()["capacity_scale"] == "zero"            # >= 30 s


def test_withdraw_needs_no_inflight_work_and_confirmed_cleanup():
    ctl, clock, state = make(inflight=1)
    tick(ctl, clock, pressure="critical")
    tick(ctl, clock, pressure="critical")
    assert ctl.mode == "drain"
    ctl.mark_cleanup_complete()
    tick(ctl, clock, pressure="critical")
    assert ctl.mode == "drain"  # a request is still unresolved
    state["inflight"] = 0
    tick(ctl, clock, pressure="critical")
    assert ctl.mode == "withdraw"
    assert ctl.publication()["capacity_scale"] == "zero"


def test_unresolved_inflight_work_past_withdraw_bound_is_visible_not_hidden():
    ctl, clock, state = make(inflight=1)
    tick(ctl, clock, pressure="critical")
    for _ in range(61):
        tick(ctl, clock, pressure="critical")
    assert ctl.mode == "drain"
    assert ctl.status()["withdraw_overdue"] is True


def test_owner_stop_fences_immediately_and_survives_healthy_samples():
    ctl, clock, _ = make()
    tick(ctl, clock)
    ctl.stop_serving()
    assert ctl.mode == "drain" and ctl.reason == "owner_stop"
    for _ in range(100):
        tick(ctl, clock)
    settle_authority(ctl, clock)
    assert ctl.mode == "drain"
    assert ctl.admission() == "adaptive_host_draining"
    ctl.resume_serving()
    assert ctl.stopped is False


def test_flapping_pressure_cannot_restore_capacity():
    ctl, clock, _ = make()
    tick(ctl, clock, pressure="warn")
    assert ctl.mode == "reduced"
    entry_generation = ctl.generation
    for _ in range(30):  # alternate healthy / warn every 2 s for 60 s
        tick(ctl, clock)
        settle_authority(ctl, clock)
        tick(ctl, clock, pressure="warn")
    assert ctl.mode in {"reduced", "drain"}
    assert ctl.mode != "normal" and ctl.generation >= entry_generation


def test_readmission_needs_recovery_window_dwell_and_two_authority_observations():
    ctl, clock, _ = make()
    tick(ctl, clock, pressure="critical")
    assert ctl.mode == "drain"
    first_healthy = tick(ctl, clock)  # healthy window starts here
    start = clock.now
    clock.now = start + 117
    ctl.record_authority_observation(ctl.generation, observed_at=clock.now)
    clock.now = start + 118
    ctl.record_authority_observation(ctl.generation, observed_at=clock.now)
    clock.now = start + 119
    ctl.observe(sample(clock))
    assert ctl.mode == "drain"  # 119 s < 120 s
    clock.now = start + 120
    ctl.observe(sample(clock))
    assert ctl.mode == "normal" and first_healthy["mode"] == "drain"
    assert ctl.admission() is None
    assert ctl.reason == "none"


def test_single_authority_observation_or_wrong_generation_is_not_enough():
    ctl, clock, _ = make()
    tick(ctl, clock, pressure="critical")
    for _ in range(70):
        tick(ctl, clock)
    clock.advance(1)
    ctl.record_authority_observation(ctl.generation, observed_at=clock.now)
    ctl.record_authority_observation(ctl.generation - 1, observed_at=clock.now + 1)   # stale generation
    ctl.record_authority_observation(ctl.generation, observed_at=clock.now - 100)     # stale observation
    ctl.record_authority_observation(ctl.generation, observed_at=clock.now)           # duplicate time
    tick(ctl, clock)
    assert ctl.mode != "normal"


def test_withdrawn_stage_is_not_revived_without_requalification():
    ctl, clock, state = make()
    tick(ctl, clock, pressure="critical")
    ctl.mark_cleanup_complete()
    tick(ctl, clock, pressure="critical")
    assert ctl.mode == "withdraw"
    for _ in range(80):
        tick(ctl, clock)
    settle_authority(ctl, clock, 3)
    tick(ctl, clock)
    assert ctl.mode == "withdraw"
    ctl.mark_requalified(ctl.generation)
    tick(ctl, clock)
    assert ctl.mode == "normal"


def test_dwell_applies_but_escalation_and_stop_bypass_it():
    ctl, clock, _ = make(policy=on_policy(timings={"recovery_s": 2, "reduced_dwell_s": 30}))
    tick(ctl, clock, pressure="warn")
    assert ctl.mode == "reduced"
    tick(ctl, clock)
    tick(ctl, clock)
    settle_authority(ctl, clock)
    tick(ctl, clock)
    assert ctl.mode == "reduced"  # still inside the 30 s dwell
    tick(ctl, clock, pressure="critical")
    assert ctl.mode == "drain"    # escalation is immediate


def test_stale_generation_and_late_positive_publication_are_rejected():
    ctl, clock, _ = make()
    tick(ctl, clock)
    old = ctl.publication()
    tick(ctl, clock, pressure="critical")
    current = ctl.publication()
    assert ah.publication_admissible(current, current, now=clock.now) is None
    assert ah.publication_admissible(current, old, now=clock.now) == "adaptive_publication_stale"
    late_positive = dict(current, new_admissions_allowed=True, capacity_scale="full")
    assert ah.publication_admissible(current, late_positive, now=clock.now) == "adaptive_publication_stale"
    future = dict(current, generation=current["generation"] + 5)
    assert ah.publication_admissible(current, future, now=clock.now) == "adaptive_publication_stale"
    expired = dict(current)
    assert ah.publication_admissible(current, expired, now=current["expires_at"] + 1) == "adaptive_publication_stale"


def test_stalled_controller_fences_new_admission_locally_and_lease_expires():
    ctl, clock, _ = make()
    tick(ctl, clock)
    assert ctl.admission() is None
    publication = ctl.publication()
    clock.advance(ah.DEFAULT_TIMINGS["stale_after_s"] + 1)  # controller stopped sampling
    assert ctl.admission() == "adaptive_controller_stale"
    assert ctl.publication()["new_admissions_allowed"] is False
    assert publication["expires_at"] <= clock.now


def test_cpu_saturation_requires_sustained_aggregate_signal_and_is_opt_in():
    ctl, clock, _ = make(policy=on_policy(cpu_saturation_fraction=0.9))
    tick(ctl, clock, cpu_utilization=0.99)       # one transient spike
    tick(ctl, clock, cpu_utilization=0.2)
    assert ctl.mode == "normal"
    for _ in range(9):
        tick(ctl, clock, cpu_utilization=0.99)   # 16+ s sustained
    assert ctl.mode == "reduced" and ctl.reason == "host_cpu_saturation"
    off, off_clock, _ = make()                    # fraction unset => CPU never acted on
    for _ in range(20):
        tick(off, off_clock, cpu_utilization=1.0)
    assert off.mode == "normal"


# ---------------------------------------------------------------- case 8
def test_inflight_disposition_never_fabricates_success():
    good = {"qualified": True, "compatible": True, "fence_ready": True, "checkpoint_committed": True}
    d = ah.inflight_disposition
    assert d(mode="drain", deadline_remaining_s=30, cancelled=False, successor=None, can_finish=True) == "finish"
    assert d(mode="drain", deadline_remaining_s=30, cancelled=True, successor=good, can_finish=True) == "cancel"
    assert d(mode="drain", deadline_remaining_s=30, cancelled=False, successor=good, can_finish=False) == "a6_handoff"
    assert d(mode="drain", deadline_remaining_s=30, cancelled=False, successor=None, can_finish=False) == "abort_terminal"
    for field in good:
        partial = dict(good, **{field: False})
        assert d(mode="drain", deadline_remaining_s=30, cancelled=False, successor=partial, can_finish=False) == "abort_terminal"
    assert d(mode="drain", deadline_remaining_s=0, cancelled=False, successor=None, can_finish=True) == "abort_terminal"
    # critical pressure does not wait for a long decode
    assert d(mode="drain", deadline_remaining_s=30, cancelled=False, successor=None, can_finish=True, critical=True) == "abort_terminal"
    assert d(mode="drain", deadline_remaining_s=30, cancelled=False, successor=good, can_finish=True, critical=True) == "a6_handoff"


# ---------------------------------------------------------------- case 10
def test_a13_thermal_and_power_gates_take_precedence_over_healthy_a16():
    ctl, clock, _ = make()
    tick(ctl, clock)
    assert ctl.admission() is None
    assert ctl.admission(hard_gate_reason="resource_thermal_policy_violation") == "resource_thermal_policy_violation"
    off, _, _ = make(policy=dict(ah.DEFAULT_POLICY))
    assert off.admission(hard_gate_reason="resource_power_policy_violation") == "resource_power_policy_violation"


class _AppStub:
    def __init__(self, tmp_path, observation):
        from mycelium_a13_macos.app import DemoApp  # noqa: F401
        self.root = tmp_path
        self._policy_observer = lambda: dict(observation)


def _app_resource_check(tmp_path, observation, sampler=None, clock=None):
    from mycelium_a13_macos.app import DemoApp

    app = _AppStub(tmp_path, observation)
    if sampler is not None:
        app._adaptive_sampler = sampler
    if clock is not None:
        app._adaptive_clock = clock
    return DemoApp._resource_check(app), app


def _write_a13_policy(root, **changes):
    policy = {"peer_visibility": True, "power": "battery_allowed", "storage_bytes": 10**9,
              "network": "metered_allowed", "background": False, "diagnostics": False,
              "thermal": "stop_on_serious"}
    policy.update(changes)
    path = root / "resource-policy.json"
    path.write_text(json.dumps(policy))
    path.chmod(0o600)


SAFE = {"power": "external", "thermal": "nominal", "network": "unmetered"}


def test_app_resource_check_is_unchanged_when_adaptive_is_off(tmp_path):
    _write_a13_policy(tmp_path)
    reason, _ = _app_resource_check(tmp_path, SAFE, sampler=lambda: pytest.fail("sampled while off"))
    assert reason is None


def test_app_resource_check_refuses_new_work_under_host_pressure_when_opted_in(tmp_path):
    _write_a13_policy(tmp_path)
    ah.write_adaptive_policy(tmp_path, on_policy(), owner_authorized=True)
    clock = Clock()
    pressure = {"level": "critical"}
    sampler = lambda: {"observed_at": clock.now, "pressure": pressure["level"]}
    reason, app = _app_resource_check(tmp_path, SAFE, sampler=sampler, clock=clock)
    assert reason == "adaptive_host_draining"
    from mycelium_a13_macos.app import DemoApp
    app._policy_observer = lambda: dict(SAFE, thermal="serious")
    assert DemoApp._resource_check(app) == "resource_thermal_policy_violation"  # A13 wins


def test_app_resource_check_fails_closed_on_corrupt_adaptive_policy(tmp_path):
    _write_a13_policy(tmp_path)
    path = tmp_path / ah.POLICY_FILENAME
    path.write_text("{not json")
    path.chmod(0o600)
    reason, _ = _app_resource_check(tmp_path, SAFE, sampler=lambda: {})
    assert reason == "adaptive_policy_invalid"


# ---------------------------------------------------------------- probe
def test_memory_probe_parses_strictly_and_degrades_to_unknown():
    outputs = {
        ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): "2\n",
        ("sysctl", "-n", "hw.memsize"): "17179869184\n",
        ("vm_stat",): ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                       "Pages free:                                    100.\n"
                       "Pages inactive:                                 50.\n"
                       "Pages speculative:                              10.\n"),
    }
    runner = lambda argv: outputs[tuple(argv)]
    result = ah.sample_host_memory(runner=runner, clock=lambda: 5.0)
    assert result == {"observed_at": 5.0, "pressure": "warn", "available_bytes": 160 * 16384}
    outputs[("sysctl", "-n", "kern.memorystatus_vm_pressure_level")] = "7\n"
    assert ah.sample_host_memory(runner=runner, clock=lambda: 5.0)["pressure"] == "unknown"

    def broken(argv):
        raise OSError("no")

    broken_result = ah.sample_host_memory(runner=broken, clock=lambda: 5.0)
    assert broken_result == {"observed_at": 5.0, "pressure": "unknown"}
