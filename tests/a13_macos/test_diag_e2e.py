# SPDX-License-Identifier: AGPL-3.0-only
"""PV26 end to end on the real signed protocol: the demo's first failure (a member clock 9 s fast, short-TTL
commands expiring) is now visible in /api/diagnostics' document and named in plain English."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.diagnostics import ensure_installed
from mycelium_a13_macos.diagnostics.collect import collect
from mycelium_a13_macos.diagnostics.state import state_for

from test_diag_clock import SKEW, _poll, _skewed_client
from test_managed_control import _command, _world


def test_member_clock_9_seconds_fast_and_expired_short_ttl_commands_are_diagnosed(tmp_path):
    with _world(tmp_path, mailbox_options={"long_poll_hold": 0.0}) as world:
        app = SimpleNamespace(
            installation_role="owner", root=tmp_path,
            authority=SimpleNamespace(coordinator=world["authority"].coordinator, control_mailbox=world["mailbox"]))
        ensure_installed(app)
        client = _skewed_client(world, SKEW)
        for n in range(1, 4):  # the member polls: its signed requests carry its (fast) clock
            _poll(world, client, n)
        for n in range(1, 8):  # the owner mints short-lived commands the member does not claim in time
            world["owner_client"].enqueue(
                target=world["member"], native_command=_command(f"short-{n}", "hello"), sequence=n,
                nonce=f"short-nonce-{n}", request_id=f"short-request-{n}", ttl_seconds=0.2)
        time.sleep(0.4)
        _poll(world, client, 4)  # the next poll fences the stale ones as expired

        doc = collect(app)
        node = world["member"]["node_id"]
        peer = next(p for p in doc["peers"] if p["node_id"] == node)
        assert peer["clock"]["direction"] == "ahead"
        assert peer["clock"]["offset_s"] == pytest.approx(SKEW, abs=0.6)
        assert peer["mailbox"]["available"] is True and peer["mailbox"]["counts"]["expired"] == 7
        assert peer["mailbox"]["short_ttl"]["expired"] == 7 and peer["mailbox"]["short_ttl"]["completed"] == 0
        ids = [f["id"] for f in doc["findings"]]
        assert "peer_clock_skew" in ids and "peer_commands_expire" in ids
        skew = next(f for f in doc["findings"] if f["id"] == "peer_clock_skew")
        assert "sudo sntp -sS time.apple.com" in skew["fix"] and "ahead of this Mac" in skew["title"]
        # reading the mailbox left it exactly as the owner wrote it
        assert world["mailbox"].command_receipt("short-1")["state"] == "expired"
        assert state_for(app).clock.estimate(node)["samples"] >= 4
