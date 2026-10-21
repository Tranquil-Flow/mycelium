# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV8: when does the swarm run more than one copy of the model (pure).

Two gates must both pass for k >= 2 copies: k disjoint host groups must each be
able to hold a complete copy with headroom, and the planner's robust
throughput gain of the k-way layout over today's single route (all hosts) must
exceed the planner's minimum gain. Otherwise there is exactly one copy.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from mycelium_replica_decision import (
    HostMemory,
    ModelFootprint,
    ReplicaDecisionError,
    ReplicaPolicy,
    RouteEstimate,
    choose_replica_layout,
    group_fits,
    max_memory_copies,
)

GIB = 1 << 30

# usable = 1/2 RAM, no fixed overhead, 25% headroom: a 4 GiB model with the
# load-peak factor 2 needs 4*2*1.25 = 10 GiB usable, i.e. exactly 20 GiB RAM.
EXACT = ReplicaPolicy(
    usable_memory_fraction=Fraction(1, 2),
    fixed_overhead_bytes=0,
    headroom_fraction=Fraction(1, 4),
    min_hosts_per_route=1,
)
MODEL = ModelFootprint(resident_bytes=4 * GIB, kv_bytes=0, load_peak_factor=2)


def hosts(*pairs):
    return [HostMemory(name, memory) for name, memory in pairs]


def flat_capacity(group):
    """Pipeline depth does not add throughput; each route serves 10 rps."""
    return RouteEstimate(capacity_rps=10.0, calibration_confidence=1.0)


# --- memory: does a copy fit -------------------------------------------------

def test_a_host_exactly_at_the_headroomed_need_fits_and_one_byte_less_does_not():
    assert group_fits(hosts(("a", 20 * GIB)), MODEL, EXACT) is True
    assert group_fits(hosts(("a", 20 * GIB - 1)), MODEL, EXACT) is False


def test_kv_bytes_count_toward_the_need():
    with_kv = ModelFootprint(resident_bytes=4 * GIB, kv_bytes=GIB, load_peak_factor=2)
    # (8 + 1) * 1.25 = 11.25 GiB usable -> 22.5 GiB RAM
    assert group_fits(hosts(("a", 22 * GIB)), with_kv, EXACT) is False
    assert group_fits(hosts(("a", 23 * GIB)), with_kv, EXACT) is True


def test_a_group_may_pool_memory_across_hosts_but_each_host_pays_the_fixed_overhead():
    policy = ReplicaPolicy(
        usable_memory_fraction=Fraction(1, 2), fixed_overhead_bytes=GIB,
        headroom_fraction=Fraction(1, 4), min_hosts_per_route=1,
    )
    # need 10 GiB; each host contributes usable(12 GiB RAM -> 6) - 1 = 5 GiB.
    assert group_fits(hosts(("a", 12 * GIB), ("b", 12 * GIB)), MODEL, policy) is True
    assert group_fits(hosts(("a", 12 * GIB), ("b", 12 * GIB - 2)), MODEL, policy) is False


def test_a_host_smaller_than_its_fixed_overhead_contributes_nothing_not_a_negative():
    policy = ReplicaPolicy(
        usable_memory_fraction=Fraction(1, 2), fixed_overhead_bytes=GIB,
        headroom_fraction=Fraction(1, 4), min_hosts_per_route=1,
    )
    # a: 20 GiB -> 10 - 1 = 9; tiny host must not subtract from it.
    assert group_fits(hosts(("a", 22 * GIB), ("tiny", GIB // 4)), MODEL, policy) is True
    assert group_fits(hosts(("a", 20 * GIB), ("tiny", GIB // 4)), MODEL, policy) is False


def test_group_size_bounds_are_part_of_fitting():
    two = ReplicaPolicy(min_hosts_per_route=2, max_hosts_per_route=2,
                        usable_memory_fraction=Fraction(1, 2), fixed_overhead_bytes=0,
                        headroom_fraction=Fraction(1, 4))
    assert group_fits(hosts(("a", 40 * GIB)), MODEL, two) is False        # below min
    assert group_fits(hosts(("a", 20 * GIB), ("b", 20 * GIB)), MODEL, two) is True
    assert group_fits(hosts(("a", 20 * GIB), ("b", 20 * GIB), ("c", 20 * GIB)),
                      MODEL, two) is False                                # above max


def test_two_big_hosts_give_two_copies_when_a_route_may_be_one_host():
    assert max_memory_copies(hosts(("a", 32 * GIB), ("b", 32 * GIB)), MODEL, EXACT) == 2


def test_two_big_hosts_give_one_copy_when_a_route_needs_two_hosts():
    # today's MIN_HOSTS = 2: two Macs can only form one route.
    today = ReplicaPolicy(
        usable_memory_fraction=Fraction(1, 2), fixed_overhead_bytes=0,
        headroom_fraction=Fraction(1, 4), min_hosts_per_route=2,
    )
    assert max_memory_copies(hosts(("a", 32 * GIB), ("b", 32 * GIB)), MODEL, today) == 1


def test_four_small_hosts_pair_up_into_two_copies():
    today = ReplicaPolicy(
        usable_memory_fraction=Fraction(1, 2), fixed_overhead_bytes=0,
        headroom_fraction=Fraction(1, 4), min_hosts_per_route=2,
    )
    four = hosts(("a", 12 * GIB), ("b", 12 * GIB), ("c", 12 * GIB), ("d", 12 * GIB))
    assert max_memory_copies(four, MODEL, today) == 2       # 6+6 >= 10 per pair
    assert max_memory_copies(four[:3], MODEL, today) == 1


def test_a_model_that_fits_nowhere_has_zero_copies():
    assert max_memory_copies(hosts(("a", 8 * GIB), ("b", 8 * GIB)), MODEL, EXACT) == 0


def test_unused_hosts_may_sit_out_when_a_group_size_cap_blocks_merging_them():
    one_host = ReplicaPolicy(
        usable_memory_fraction=Fraction(1, 2), fixed_overhead_bytes=0,
        headroom_fraction=Fraction(1, 4), min_hosts_per_route=1, max_hosts_per_route=1,
    )
    mixed = hosts(("a", 20 * GIB), ("b", 4 * GIB), ("c", 20 * GIB))
    assert max_memory_copies(mixed, MODEL, one_host) == 2   # b sits out


def test_the_memory_answer_does_not_depend_on_host_order():
    a = hosts(("a", 12 * GIB), ("b", 30 * GIB), ("c", 12 * GIB), ("d", 20 * GIB))
    assert max_memory_copies(a, MODEL, EXACT) == max_memory_copies(list(reversed(a)), MODEL, EXACT)


# --- the decision ------------------------------------------------------------

FOUR = hosts(("a", 32 * GIB), ("b", 32 * GIB), ("c", 32 * GIB), ("d", 32 * GIB))


def test_four_hosts_with_demand_to_spare_run_four_copies():
    decision = choose_replica_layout(FOUR, MODEL, flat_capacity, EXACT)
    assert decision.copies == 4
    assert decision.reason == "accepted_positive_robust_gain"
    assert sorted(h for group in decision.groups for h in group) == ["a", "b", "c", "d"]
    assert decision.single_route_admitted_rps == 10.0
    assert decision.layout_admitted_rps == 40.0
    assert decision.robust_gain_rps == pytest.approx(27.0)   # 30 * (1 - 0.1)


def test_bounded_demand_that_one_route_already_meets_keeps_one_copy():
    decision = choose_replica_layout(FOUR, MODEL, flat_capacity, EXACT, offered_demand_rps=10.0)
    assert decision.copies == 1
    assert decision.reason == "gain_below_threshold"
    assert decision.groups == (("a", "b", "c", "d"),)
    assert decision.robust_gain_rps == 0.0


def test_the_smallest_copy_count_that_meets_the_demand_wins_ties():
    decision = choose_replica_layout(FOUR, MODEL, flat_capacity, EXACT, offered_demand_rps=25.0)
    # k=3 already reaches the 25 rps ceiling; k=4 adds nothing.
    assert decision.copies == 3
    assert decision.layout_admitted_rps == 25.0


def test_low_planner_confidence_removes_the_robust_gain():
    def unsure(group):
        return RouteEstimate(capacity_rps=10.0, calibration_confidence=0.0)
    decision = choose_replica_layout(FOUR, MODEL, unsure, EXACT)
    assert decision.copies == 1
    assert decision.reason == "gain_below_threshold"


def test_the_minimum_gain_fraction_is_the_planners_threshold():
    two = hosts(("a", 32 * GIB), ("b", 32 * GIB))
    # k=2: gain 10, robust 9. Threshold = single(10) * fraction.
    accepted = choose_replica_layout(
        two, MODEL, flat_capacity, ReplicaPolicy(**{**_fields(EXACT), "minimum_gain_fraction": 0.5}))
    rejected = choose_replica_layout(
        two, MODEL, flat_capacity, ReplicaPolicy(**{**_fields(EXACT), "minimum_gain_fraction": 0.95}))
    assert (accepted.copies, rejected.copies) == (2, 1)
    assert accepted.minimum_required_gain_rps == pytest.approx(5.0)
    assert rejected.minimum_required_gain_rps == pytest.approx(9.5)


def test_uncertainty_is_the_larger_of_the_policy_floor_and_one_minus_confidence():
    def middling(group):
        return RouteEstimate(capacity_rps=10.0, calibration_confidence=0.5)
    two = hosts(("a", 32 * GIB), ("b", 32 * GIB))
    decision = choose_replica_layout(two, MODEL, middling, EXACT)
    assert decision.uncertainty_fraction == pytest.approx(0.5)
    assert decision.robust_gain_rps == pytest.approx(5.0)        # 10 * (1 - 0.5)


def test_memory_for_only_one_copy_means_one_copy_without_asking_the_planner():
    calls = []

    def estimator(group):
        calls.append(group)
        return RouteEstimate(10.0, 1.0)
    today = ReplicaPolicy(
        usable_memory_fraction=Fraction(1, 2), fixed_overhead_bytes=0,
        headroom_fraction=Fraction(1, 4), min_hosts_per_route=2,
    )
    two = hosts(("a", 32 * GIB), ("b", 32 * GIB))
    decision = choose_replica_layout(two, MODEL, estimator, today)
    assert decision.copies == 1
    assert decision.reason == "single_copy_only_memory"
    assert decision.groups == (("a", "b"),)
    assert calls == []


def test_a_model_that_does_not_fit_is_zero_copies_and_no_groups():
    small = hosts(("a", 8 * GIB), ("b", 8 * GIB))
    decision = choose_replica_layout(small, MODEL, flat_capacity, EXACT)
    assert decision.copies == 0
    assert decision.reason == "model_does_not_fit"
    assert decision.groups == ()


def test_no_planner_estimate_means_no_replicas():
    decision = choose_replica_layout(FOUR, MODEL, lambda group: None, EXACT)
    assert decision.copies == 1
    assert decision.reason == "no_capacity_estimate"
    assert decision.groups == (("a", "b", "c", "d"),)


def test_layouts_the_planner_cannot_estimate_are_skipped_not_guessed():
    def only_pairs_and_all(group):
        return RouteEstimate(10.0, 1.0) if len(group) in (2, 4) else None
    decision = choose_replica_layout(FOUR, MODEL, only_pairs_and_all, EXACT)
    assert decision.copies == 2
    assert all(len(group) == 2 for group in decision.groups)


def test_max_copies_policy_caps_the_count():
    capped = ReplicaPolicy(**{**_fields(EXACT), "max_copies": 2})
    decision = choose_replica_layout(FOUR, MODEL, flat_capacity, capped)
    assert decision.copies == 2


def test_a_host_that_adds_nothing_sits_out_instead_of_being_forced_into_a_copy():
    def by_group(group):
        # any group containing the slow host is useless; fast groups serve 10.
        return RouteEstimate(
            capacity_rps=0.0 if any(node == "slow" for node in group) else 10.0,
            calibration_confidence=1.0,
        )
    trio = hosts(("fast1", 32 * GIB), ("fast2", 32 * GIB), ("slow", 32 * GIB))
    decision = choose_replica_layout(trio, MODEL, by_group, EXACT)
    assert decision.copies == 2
    assert sorted(h for group in decision.groups for h in group) == ["fast1", "fast2"]


def test_the_decision_is_deterministic_and_independent_of_host_order():
    first = choose_replica_layout(FOUR, MODEL, flat_capacity, EXACT, offered_demand_rps=25.0)
    second = choose_replica_layout(list(reversed(FOUR)), MODEL, flat_capacity, EXACT,
                                   offered_demand_rps=25.0)
    assert first == second
    assert first.groups == tuple(sorted(first.groups))
    assert all(group == tuple(sorted(group)) for group in first.groups)


def test_every_group_in_an_accepted_layout_fits_a_copy_on_its_own():
    mixed = hosts(("a", 20 * GIB), ("b", 12 * GIB), ("c", 12 * GIB), ("d", 20 * GIB))
    today = ReplicaPolicy(**{**_fields(EXACT), "min_hosts_per_route": 1})
    decision = choose_replica_layout(mixed, MODEL, flat_capacity, today)
    by_name = {h.node_id: h for h in mixed}
    assert decision.copies >= 2
    for group in decision.groups:
        assert group_fits([by_name[n] for n in group], MODEL, today)


def test_the_decision_document_is_plain_data():
    document = choose_replica_layout(FOUR, MODEL, flat_capacity, EXACT).to_dict()
    assert document["copies"] == 4
    assert document["groups"] == [["a"], ["b"], ["c"], ["d"]]
    assert set(document) >= {"copies", "groups", "reason", "robust_gain_rps",
                             "minimum_required_gain_rps", "single_route_admitted_rps",
                             "layout_admitted_rps", "uncertainty_fraction"}


# --- bounds and validation ---------------------------------------------------

def test_more_hosts_than_the_exact_search_or_a_route_may_use_is_refused():
    nine = [HostMemory(f"h{i}", 32 * GIB) for i in range(9)]
    with pytest.raises(ReplicaDecisionError) as caught:
        choose_replica_layout(nine, MODEL, flat_capacity,
                              ReplicaPolicy(**{**_fields(EXACT), "max_hosts_per_route": 8}))
    assert caught.value.code == "hosts_out_of_bounds"
    eleven = [HostMemory(f"h{i}", 32 * GIB) for i in range(11)]
    with pytest.raises(ReplicaDecisionError) as caught:
        max_memory_copies(eleven, MODEL, ReplicaPolicy(**{**_fields(EXACT), "max_hosts_per_route": 11}))
    assert caught.value.code == "hosts_out_of_bounds"


def test_duplicate_or_malformed_hosts_are_refused():
    with pytest.raises(ReplicaDecisionError) as caught:
        choose_replica_layout(hosts(("a", GIB), ("a", GIB)), MODEL, flat_capacity, EXACT)
    assert caught.value.code == "duplicate_host"
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ReplicaDecisionError) as caught:
            HostMemory("a", bad)
        assert caught.value.code == "invalid_host"
    with pytest.raises(ReplicaDecisionError):
        HostMemory("", GIB)


def test_empty_hosts_are_zero_copies_not_a_crash():
    decision = choose_replica_layout([], MODEL, flat_capacity, EXACT)
    assert (decision.copies, decision.reason, decision.groups) == (0, "model_does_not_fit", ())


@pytest.mark.parametrize("kwargs", [
    dict(resident_bytes=0), dict(resident_bytes=True), dict(kv_bytes=-1),
    dict(load_peak_factor=0), dict(load_peak_factor=1.5),
])
def test_footprint_is_validated(kwargs):
    base = dict(resident_bytes=GIB, kv_bytes=0, load_peak_factor=2)
    with pytest.raises(ReplicaDecisionError) as caught:
        ModelFootprint(**{**base, **kwargs})
    assert caught.value.code == "invalid_footprint"


@pytest.mark.parametrize("kwargs", [
    dict(usable_memory_fraction=Fraction(0)), dict(usable_memory_fraction=Fraction(3, 2)),
    dict(headroom_fraction=Fraction(-1, 10)), dict(fixed_overhead_bytes=-1),
    dict(min_hosts_per_route=0), dict(min_hosts_per_route=3, max_hosts_per_route=2),
    dict(max_copies=0), dict(minimum_gain_fraction=1.5), dict(uncertainty_fraction=1.0),
])
def test_policy_is_validated(kwargs):
    with pytest.raises(ReplicaDecisionError) as caught:
        ReplicaPolicy(**{**_fields(EXACT), **kwargs})
    assert caught.value.code == "invalid_policy"


@pytest.mark.parametrize("estimate", [
    dict(capacity_rps=-1.0), dict(capacity_rps=float("nan")),
    dict(capacity_rps=1.0, calibration_confidence=1.5),
    dict(capacity_rps=1.0, calibration_confidence=-0.1),
])
def test_route_estimates_are_validated(estimate):
    with pytest.raises(ReplicaDecisionError) as caught:
        RouteEstimate(**estimate)
    assert caught.value.code == "invalid_estimate"


def test_defaults_match_the_planner_and_the_live_resource_model():
    policy = ReplicaPolicy()
    assert policy.usable_memory_fraction == Fraction(3, 4)      # MEMBER_PEAK_MEMORY_FRACTION
    assert policy.fixed_overhead_bytes == GIB                   # GROUP_FIXED_BYTES
    assert policy.headroom_fraction == Fraction(15, 100)        # GUARD_HEADROOM
    assert policy.min_hosts_per_route == 2                      # layer_split.MIN_HOSTS
    assert policy.max_hosts_per_route == 8                      # layer_split.MAX_HOSTS
    assert policy.minimum_gain_fraction == 0.05                 # PlanningPolicy
    assert policy.uncertainty_fraction == 0.1                   # PlanningPolicy


def _fields(policy):
    return {name: getattr(policy, name) for name in policy.__dataclass_fields__}
