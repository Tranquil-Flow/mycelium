# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: the native node wiring - idle policy file in, idle status file out, commands, health."""
from __future__ import annotations

import json

import pytest

from mycelium_idle import node_link
from mycelium_idle import policy as ip
from mycelium_idle.residency import WeightResidency


class Clock:
    now = 5000.0

    def __call__(self):
        return self.now


class Port:
    def __init__(self):
        self.resident = True
        self.calls = []

    def release_weights(self):
        self.calls.append("release")
        self.resident = False

    def restore_weights(self):
        self.calls.append("restore")
        self.resident = True

    @property
    def weights_resident(self):
        return self.resident

    def active_request_count(self):
        return 0


def link(tmp_path, *, minutes=1):
    ip.write_policy(tmp_path, minutes)
    clock = Clock()
    port = Port()
    node = node_link.NodeIdleLink(capacity_root=tmp_path, port=port, clock=clock,
                                  on_reload_failed=lambda code: port.calls.append(code),
                                  spawn=lambda fn: fn())
    return node, port, clock


def test_tick_reads_the_policy_file_and_unloads_then_writes_the_status_file(tmp_path):
    node, port, clock = link(tmp_path, minutes=1)
    clock.now += 61
    node.tick()
    assert port.calls == ["release"]
    doc = json.loads((tmp_path / node_link.STATUS_FILENAME).read_text())
    assert doc["state"] == "idle" and doc["line"] == "idle (weights unloaded)"
    assert oct((tmp_path / node_link.STATUS_FILENAME).stat().st_mode & 0o777) == "0o600"


def test_a_bad_policy_file_means_never_unload(tmp_path):
    node, port, clock = link(tmp_path, minutes=1)
    (tmp_path / ip.POLICY_FILENAME).write_text("garbage")
    clock.now += 10_000
    node.tick()
    assert port.calls == []


def test_command_idle_reload_starts_the_reload_and_reports_state_quickly(tmp_path):
    node, port, clock = link(tmp_path, minutes=1)
    clock.now += 61
    node.tick()
    reply = node.command("idle_reload", {})
    assert reply["state"] == "loaded" and port.calls == ["release", "restore"]  # spawn ran inline here


def test_command_idle_status_and_unknown_payload_refused(tmp_path):
    node, port, clock = link(tmp_path)
    assert node.command("idle_status", {})["state"] == "loaded"
    with pytest.raises(node_link.IdleCommandError) as error:
        node.command("idle_reload", {"surprise": 1})
    assert error.value.code == "invalid_idle_fields"
    with pytest.raises(node_link.IdleCommandError):
        node.command("idle_bogus", {})


def test_health_detail_is_additive_and_small(tmp_path):
    node, port, clock = link(tmp_path)
    detail = node.health_detail()
    assert set(detail) <= {"state", "line", "unload_after_minutes", "reload_count", "last_reload_ms",
                           "unloaded_for_s", "error_code"}
    assert detail["state"] == "loaded"


def test_work_gate_reloads_or_raises_member_warming_for_an_unloaded_node(tmp_path):
    node, port, clock = link(tmp_path, minutes=1)
    clock.now += 61
    node.tick()
    assert node.work_gate(timeout_s=1.0) == "loaded"  # inline spawn: reload completes
    assert port.resident is True
