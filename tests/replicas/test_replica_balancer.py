# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV8: request balancing across independent whole-model routes (pure).

The balancer picks which route a NEW request goes to. It never moves an
admitted request: a request stays bound to its route until it is released, and
a route that goes unhealthy only stops receiving new requests.
"""
from __future__ import annotations

import math
from collections import Counter

import pytest

from mycelium_replica_balancer import (
    NO_HEALTHY_REPLICA,
    REPLICA_CAPACITY_EXHAUSTED,
    REQUEST_TOO_LARGE,
    ReplicaBalancer,
    ReplicaRejected,
    RouteHealth,
    RouteLoad,
    RouteSpec,
    select_route,
)


def load(route_id, *, tokens=0, requests=0, weight=1.0, health=RouteHealth.READY,
         max_requests=8, max_tokens=None, last=0):
    return RouteLoad(
        route_id=route_id, health=health, weight=weight,
        outstanding_requests=requests, outstanding_tokens=tokens,
        max_outstanding_requests=max_requests, max_outstanding_tokens=max_tokens,
        last_selected_sequence=last,
    )


def spec(route_id, *, weight=1.0, max_requests=8, max_tokens=None):
    return RouteSpec(route_id=route_id, weight=weight,
                     max_outstanding_requests=max_requests,
                     max_outstanding_tokens=max_tokens)


# --- selection policy -------------------------------------------------------

def test_picks_the_route_with_the_fewest_outstanding_tokens():
    loads = [load("a", tokens=500, requests=2), load("b", tokens=100, requests=2),
             load("c", tokens=300, requests=2)]
    assert select_route(loads, 64) == "b"


def test_weight_normalizes_load_so_a_faster_route_takes_proportionally_more():
    # a is twice as fast: 300/2 = 150 per unit beats b's 200 per unit.
    loads = [load("a", tokens=300, weight=2.0), load("b", tokens=200, weight=1.0)]
    assert select_route(loads, 0 + 1) == "a"
    # but not when a's normalized load is higher.
    loads = [load("a", tokens=500, weight=2.0), load("b", tokens=200, weight=1.0)]
    assert select_route(loads, 1) == "b"


def test_request_cost_is_part_of_the_comparison():
    # b has fewer tokens now, but a request that big is no worse on a with
    # the weight taken into account: (100+400)/1 vs (200+400)/4.
    loads = [load("a", tokens=200, weight=4.0), load("b", tokens=100, weight=1.0)]
    assert select_route(loads, 400) == "a"


def test_tie_breaks_on_fewer_outstanding_requests_then_least_recently_used_then_id():
    base = dict(tokens=100)
    assert select_route([load("a", requests=3, **base), load("b", requests=1, **base)], 10) == "b"
    assert select_route([load("a", requests=2, last=9, **base),
                         load("b", requests=2, last=4, **base)], 10) == "b"
    assert select_route([load("b", requests=2, last=4, **base),
                         load("a", requests=2, last=4, **base)], 10) == "a"


def test_selection_does_not_depend_on_input_order():
    loads = [load("a", tokens=10, last=3), load("b", tokens=10, last=1),
             load("c", tokens=10, last=2)]
    results = {select_route(list(p), 5) for p in
               ([loads[0], loads[1], loads[2]], [loads[2], loads[1], loads[0]],
                [loads[1], loads[2], loads[0]])}
    assert results == {"b"}


# --- health exclusion -------------------------------------------------------

@pytest.mark.parametrize("health", [RouteHealth.RECOVERING, RouteHealth.DOWN])
def test_a_route_that_is_not_ready_gets_no_new_requests_even_when_idle(health):
    loads = [load("a", health=health), load("b", tokens=10_000, requests=7)]
    assert select_route(loads, 64) == "b"


def test_no_ready_route_is_rejected_as_no_healthy_replica():
    loads = [load("a", health=RouteHealth.DOWN), load("b", health=RouteHealth.RECOVERING)]
    with pytest.raises(ReplicaRejected) as caught:
        select_route(loads, 64)
    assert caught.value.code == NO_HEALTHY_REPLICA
    assert caught.value.retry_after_s > 0


def test_an_empty_route_list_is_no_healthy_replica():
    with pytest.raises(ReplicaRejected) as caught:
        select_route([], 64)
    assert caught.value.code == NO_HEALTHY_REPLICA


# --- per-route admission ----------------------------------------------------

def test_a_full_route_is_skipped_even_when_another_is_far_more_loaded():
    loads = [load("a", tokens=0, requests=8, max_requests=8),
             load("b", tokens=9_000, requests=7, max_requests=8)]
    assert select_route(loads, 64) == "b"


def test_token_budget_is_a_per_route_admission_limit():
    loads = [load("a", tokens=900, max_tokens=1000), load("b", tokens=5000, max_tokens=8000)]
    assert select_route(loads, 200) == "b"        # 900+200 > 1000 on a
    assert select_route(loads, 100) == "a"        # 900+100 == 1000 fits


def test_all_ready_routes_full_is_replica_capacity_exhausted_with_retry_after():
    loads = [load("a", requests=8, max_requests=8), load("b", tokens=1000, max_tokens=1000)]
    with pytest.raises(ReplicaRejected) as caught:
        select_route(loads, 10, retry_after_s=0.5)
    assert caught.value.code == REPLICA_CAPACITY_EXHAUSTED
    assert caught.value.retry_after_s == 0.5


def test_a_request_bigger_than_every_route_budget_can_never_fit_and_says_so():
    loads = [load("a", max_tokens=1000), load("b", max_tokens=2000)]
    with pytest.raises(ReplicaRejected) as caught:
        select_route(loads, 2001)
    assert caught.value.code == REQUEST_TOO_LARGE


def test_a_down_routes_larger_budget_is_ignored_when_judging_request_size():
    loads = [load("a", health=RouteHealth.DOWN, max_tokens=10_000), load("b", max_tokens=1000)]
    with pytest.raises(ReplicaRejected) as caught:
        select_route(loads, 5000)
    assert caught.value.code == REQUEST_TOO_LARGE


# --- fairness / no starvation ----------------------------------------------

def test_equal_idle_routes_alternate_instead_of_always_picking_the_lowest_id():
    balancer = ReplicaBalancer([spec("a"), spec("b")])
    picks = []
    for index in range(6):
        route = balancer.admit(f"r{index}", 10)
        picks.append(route)
        balancer.release(f"r{index}")
    assert picks == ["a", "b", "a", "b", "a", "b"]


def test_equal_routes_share_a_burst_evenly():
    balancer = ReplicaBalancer([spec("a", max_requests=64), spec("b", max_requests=64),
                                spec("c", max_requests=64)])
    counts = Counter(balancer.admit(f"r{i}", 10) for i in range(60))
    assert counts == {"a": 20, "b": 20, "c": 20}


def test_weighted_routes_share_a_burst_in_proportion_to_weight():
    balancer = ReplicaBalancer([spec("a", weight=2.0, max_requests=400),
                                spec("b", weight=1.0, max_requests=400)])
    counts = Counter(balancer.admit(f"r{i}", 10) for i in range(300))
    assert counts == {"a": 200, "b": 100}


def test_a_route_holding_a_long_request_still_gets_work_once_the_others_catch_up():
    balancer = ReplicaBalancer([spec("a", max_requests=100), spec("b", max_requests=100)])
    assert balancer.admit("long", 10) == "a"
    routes = [balancer.admit(f"s{index}", 10) for index in range(40)]
    assert routes[0] == "b"                       # a is busier at first
    assert "a" in routes[:3]                      # but not starved for long
    counts = Counter(routes + ["a"])
    assert abs(counts["a"] - counts["b"]) <= 1    # 41 equal requests end level


# --- stateful balancer ------------------------------------------------------

def test_admit_binds_the_request_to_its_route_until_release():
    balancer = ReplicaBalancer([spec("a"), spec("b")])
    route = balancer.admit("r1", 32)
    assert balancer.route_of("r1") == route
    assert balancer.requests_on(route) == ("r1",)
    assert balancer.release("r1") is True
    assert balancer.route_of("r1") is None
    assert balancer.requests_on(route) == ()


def test_duplicate_request_id_is_refused_and_does_not_change_state():
    balancer = ReplicaBalancer([spec("a"), spec("b")])
    balancer.admit("r1", 32)
    before = balancer.status()
    with pytest.raises(ValueError, match="duplicate_request_id"):
        balancer.admit("r1", 32)
    assert balancer.status() == before


def test_release_is_idempotent_and_unknown_ids_return_false():
    balancer = ReplicaBalancer([spec("a")])
    balancer.admit("r1", 32)
    assert balancer.release("r1") is True
    assert balancer.release("r1") is False
    assert balancer.release("never") is False


def test_a_rejected_admission_leaves_no_binding_and_no_rotation_change():
    balancer = ReplicaBalancer([spec("a", max_requests=1)])
    balancer.admit("r1", 10)
    before = balancer.status()
    with pytest.raises(ReplicaRejected) as caught:
        balancer.admit("r2", 10)
    assert caught.value.code == REPLICA_CAPACITY_EXHAUSTED
    assert balancer.route_of("r2") is None
    assert balancer.status() == before


def test_release_returns_capacity_so_a_full_set_admits_again():
    balancer = ReplicaBalancer([spec("a", max_requests=1), spec("b", max_requests=1)])
    balancer.admit("r1", 10)
    balancer.admit("r2", 10)
    with pytest.raises(ReplicaRejected):
        balancer.admit("r3", 10)
    balancer.release("r1")
    assert balancer.admit("r3", 10) in {"a", "b"}


def test_token_accounting_follows_each_request_cost():
    balancer = ReplicaBalancer([spec("a", max_tokens=100), spec("b", max_tokens=100)])
    balancer.admit("r1", 90)                       # -> a
    assert balancer.admit("r2", 60) == "b"         # a would exceed 100
    balancer.release("r1")
    assert balancer.admit("r3", 90) == "a"


def test_unhealthy_route_stops_new_work_but_keeps_its_bound_requests():
    balancer = ReplicaBalancer([spec("a"), spec("b")])
    on_a = [rid for rid in ("r1", "r2", "r3", "r4") if balancer.admit(rid, 10) == "a"]
    assert on_a                                        # a took some of them
    balancer.set_health("a", RouteHealth.DOWN)
    assert balancer.requests_on("a") == tuple(sorted(on_a))   # not migrated
    for index in range(5):
        assert balancer.admit(f"n{index}", 10) == "b"
    # the dead route's own recovery ends the requests; release still works
    assert balancer.release(on_a[0]) is True
    balancer.set_health("a", RouteHealth.READY)
    assert "a" in {balancer.admit(f"m{index}", 10) for index in range(4)}


def test_all_routes_down_is_no_healthy_replica_and_back_up_recovers():
    balancer = ReplicaBalancer([spec("a"), spec("b")])
    balancer.set_health("a", RouteHealth.DOWN)
    balancer.set_health("b", RouteHealth.RECOVERING)
    with pytest.raises(ReplicaRejected) as caught:
        balancer.admit("r1", 10)
    assert caught.value.code == NO_HEALTHY_REPLICA
    balancer.set_health("b", RouteHealth.READY)
    assert balancer.admit("r1", 10) == "b"


def test_set_health_rejects_unknown_route_and_bad_value():
    balancer = ReplicaBalancer([spec("a")])
    with pytest.raises(ValueError, match="unknown_route"):
        balancer.set_health("zzz", RouteHealth.DOWN)
    with pytest.raises(ValueError, match="invalid_route_health"):
        balancer.set_health("a", "up")


# --- status -----------------------------------------------------------------

def test_status_line_for_two_serving_copies():
    balancer = ReplicaBalancer([spec("a"), spec("b")])
    status = balancer.status()
    assert status["copies_configured"] == 2
    assert status["copies_serving"] == 2
    assert status["summary"] == "2 copies of the model are serving; balancing requests"


def test_status_line_for_a_degraded_set_and_a_single_copy_and_none():
    balancer = ReplicaBalancer([spec("a"), spec("b")])
    balancer.set_health("b", RouteHealth.RECOVERING)
    status = balancer.status()
    assert status["copies_serving"] == 1
    assert status["summary"] == "1 of 2 copies of the model is serving; the rest are recovering"
    balancer.set_health("a", RouteHealth.DOWN)
    assert balancer.status()["summary"] == "no copy of the model is serving"
    assert ReplicaBalancer([spec("only")]).status()["summary"] == "1 copy of the model is serving"


def test_status_reports_per_route_load_and_no_request_identities():
    balancer = ReplicaBalancer([spec("a"), spec("b")])
    balancer.admit("secret-request-id", 40)
    status = balancer.status()
    rows = {row["route_id"]: row for row in status["routes"]}
    assert sum(row["outstanding_requests"] for row in rows.values()) == 1
    assert sum(row["outstanding_tokens"] for row in rows.values()) == 40
    assert rows["a"]["health"] == "ready"
    assert "secret-request-id" not in repr(status)


# --- validation -------------------------------------------------------------

def test_balancer_requires_at_least_one_route_and_unique_ids():
    with pytest.raises(ValueError, match="routes_required"):
        ReplicaBalancer([])
    with pytest.raises(ValueError, match="duplicate_route_id"):
        ReplicaBalancer([spec("a"), spec("a")])


@pytest.mark.parametrize("weight", [0, -1.0, math.nan, math.inf, True, "1"])
def test_route_weight_must_be_finite_and_positive(weight):
    with pytest.raises(ValueError, match="invalid_route_weight"):
        spec("a", weight=weight)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, None])
def test_request_cost_must_be_a_positive_integer(bad):
    balancer = ReplicaBalancer([spec("a")])
    with pytest.raises(ValueError, match="invalid_request_cost"):
        balancer.admit("r1", bad)
    with pytest.raises(ValueError, match="invalid_request_cost"):
        select_route([load("a")], bad)


@pytest.mark.parametrize("kwargs", [dict(max_requests=0), dict(max_requests=True),
                                    dict(max_tokens=0), dict(max_tokens=-5)])
def test_route_limits_must_be_positive_integers(kwargs):
    with pytest.raises(ValueError, match="invalid_route_limit"):
        spec("a", **kwargs)


def test_request_id_must_be_a_bounded_non_empty_string():
    balancer = ReplicaBalancer([spec("a")])
    for bad in ("", None, 7, "x" * 257):
        with pytest.raises(ValueError, match="invalid_request_id"):
            balancer.admit(bad, 10)
