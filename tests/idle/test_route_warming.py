# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: the owner treats a reloading member as warming (bounded wait), never as lost."""
from __future__ import annotations

import pytest

from mycelium_idle.residency import MemberWarming
from mycelium_idle.route_warming import WarmingCoordinator, idle_state_of


class Fleet:
    """Fake peers: each has a residency state that flips to loaded after N health polls."""

    def __init__(self, states, polls_to_load=2):
        self.states = dict(states)
        self.polls = {n: 0 for n in states}
        self.polls_to_load = polls_to_load
        self.reload_sent = []
        self.clock = 0.0

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def send_reload(self, node_id):
        self.reload_sent.append(node_id)
        if self.states[node_id] == "idle":
            self.states[node_id] = "warming"

    def health(self, node_id):
        if self.states[node_id] == "warming":
            self.polls[node_id] += 1
            if self.polls[node_id] >= self.polls_to_load:
                self.states[node_id] = "loaded"
        return {"details": {"idle_residency": {"state": self.states[node_id]}}}


def coordinator(fleet, **kwargs):
    return WarmingCoordinator(send_reload=fleet.send_reload, health=fleet.health, clock=fleet.now,
                              sleep=fleet.sleep, deadline_s=kwargs.pop("deadline_s", 10.0), poll_s=0.5, **kwargs)


def test_idle_state_of_reads_the_node_health_detail():
    assert idle_state_of({"details": {"idle_residency": {"state": "idle"}}}) == "idle"
    assert idle_state_of({"details": {}}) is None
    assert idle_state_of({"details": {"idle_residency": {"state": "bogus"}}}) is None
    assert idle_state_of(None) is None
    assert idle_state_of({"details": "x"}) is None


def test_loaded_members_cost_one_health_read_and_no_reload():
    fleet = Fleet({"a": "loaded", "b": "loaded"})
    result = coordinator(fleet).ensure_warm(["a", "b"])
    assert fleet.reload_sent == [] and result.reload_ms == {}


def test_idle_member_is_woken_and_waited_for_and_reload_time_is_recorded():
    fleet = Fleet({"a": "loaded", "b": "idle"}, polls_to_load=3)
    result = coordinator(fleet).ensure_warm(["a", "b"])
    assert fleet.reload_sent == ["b"] and fleet.states["b"] == "loaded"
    assert result.reload_ms["b"] >= 1000  # 2 polls * 0.5 s of fake time


def test_a_member_still_warming_at_the_deadline_raises_member_warming():
    fleet = Fleet({"a": "idle"}, polls_to_load=10**6)
    with pytest.raises(MemberWarming) as error:
        coordinator(fleet, deadline_s=3.0).ensure_warm(["a"])
    assert error.value.code == "member_warming"
    assert error.value.node_ids == ("a",)
    assert fleet.states["a"] == "warming"  # not failed, not lost: left warming


def test_a_member_whose_health_fails_is_not_swallowed_into_warming():
    fleet = Fleet({"a": "idle"})
    def broken(node_id):
        raise TimeoutError("command_deadline_exceeded")
    with pytest.raises(TimeoutError):
        WarmingCoordinator(send_reload=fleet.send_reload, health=broken, clock=fleet.now,
                           sleep=fleet.sleep, deadline_s=3.0, poll_s=0.5).ensure_warm(["a"])


def test_a_failed_reload_state_surfaces_as_the_member_error_not_warming():
    fleet = Fleet({"a": "failed"})
    from mycelium_idle.residency import ResidencyError
    with pytest.raises(ResidencyError) as error:
        coordinator(fleet).ensure_warm(["a"])
    assert error.value.code == "member_reload_failed"


def test_recently_used_members_are_skipped_without_any_probe():
    fleet = Fleet({"a": "idle"})
    coord = coordinator(fleet, recent_activity_s=30.0)
    coord.note_used(["a"])
    fleet.clock += 10
    fleet.states["a"] = "loaded"
    seen = []
    coord._health = lambda n: seen.append(n) or fleet.health(n)
    coord.ensure_warm(["a"])
    assert seen == []
    fleet.clock += 60
    coord.ensure_warm(["a"])
    assert seen == ["a"]
