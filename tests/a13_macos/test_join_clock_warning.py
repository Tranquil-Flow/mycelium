# SPDX-License-Identifier: AGPL-3.0-only
"""C3 at the app layer: a skewed clock is an advisory warning on the join response."""
from __future__ import annotations

import time

from tests.a13_macos.test_join_window_flow import CONSENT, STANDING, _owner, _target


def _join(tmp_path, monkeypatch, *, offset_seconds):
    owner = _owner(tmp_path, monkeypatch, "https://relay-fixture.invalid")
    target = _target(tmp_path, owner)
    original = target._join_client

    def skewed(origin):
        client = original(origin)
        client._clock = lambda: time.time() + offset_seconds
        return client

    target._join_client = skewed
    opened = owner.action("/api/pair/window/open", {"ttl_seconds": 600})
    waiting = target.action("/api/join/window", {"consent": CONSENT, "standing_consent": STANDING,
                                                 "join": opened["join_link"]})
    return owner, target, waiting


def test_a_skewed_clock_adds_an_advisory_warning_and_still_joins(tmp_path, monkeypatch):
    owner, target, waiting = _join(tmp_path, monkeypatch, offset_seconds=-120)  # local behind by 120 s
    try:
        assert waiting["state"] == "waiting_for_owner"             # never blocks
        [warning] = waiting["warnings"]
        assert warning["code"] == "clock_skew" and 118 <= warning["skew_seconds"] <= 122
    finally:
        target.close(); owner.close()


def test_an_accurate_clock_has_no_warnings_key(tmp_path, monkeypatch):
    owner, target, waiting = _join(tmp_path, monkeypatch, offset_seconds=0)
    try:
        assert "warnings" not in waiting
    finally:
        target.close(); owner.close()
