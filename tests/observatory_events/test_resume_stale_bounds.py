from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import qualification_bytes, request_event_bytes

from mycelium_gateway.event_adapter import ObservatoryEventAdapter
from mycelium_gateway.observatory import CoherentSnapshotPublisher


def snapshot(adapter: ObservatoryEventAdapter) -> dict:
    envelope = adapter.current_envelope()
    assert envelope is not None
    return envelope["bundle"]["snapshot"]


def incidents(adapter: ObservatoryEventAdapter) -> list[dict]:
    envelope = adapter.current_envelope()
    assert envelope is not None
    return envelope["bundle"]["incidents"]


def test_cursor_resume_is_persistent_and_does_not_duplicate_applied_output(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    publisher = CoherentSnapshotPublisher(state_path, replay_capacity=8)
    adapter = ObservatoryEventAdapter(publisher)
    inputs = [
        (0, qualification_bytes(), 100),
        (1, request_event_bytes("request-a", 0, "accepted"), 101),
        (2, request_event_bytes("request-a", 1, "token", token_index=0, text="private"), 102),
        (3, request_event_bytes("request-a", 2, "completed"), 103),
    ]
    for cursor, payload, observed_at in inputs:
        assert adapter.apply(cursor, payload, observed_at_unix_ms=observed_at).applied
    assert publisher.current_publication().generation == 4
    replay = adapter.subscribe(last_event_id=2)
    try:
        assert [publication.generation for publication in replay.replay] == [3, 4]
    finally:
        replay.close()

    restarted_publisher = CoherentSnapshotPublisher(state_path, replay_capacity=8)
    restarted = ObservatoryEventAdapter(restarted_publisher)
    duplicate = restarted.apply(3, inputs[-1][1], observed_at_unix_ms=103)

    assert not duplicate.applied
    assert duplicate.reason == "duplicate_cursor"
    assert duplicate.publication is None
    assert restarted_publisher.current_publication().generation == 4
    assert snapshot(restarted)["sessions"][0]["event_count"] == 3
    assert snapshot(restarted)["sessions"][0]["terminal"] is True


def test_terminal_event_is_applied_exactly_once_even_with_new_source_cursor(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    adapter.apply(1, request_event_bytes("request-a", 0, "accepted"), observed_at_unix_ms=101)
    terminal = request_event_bytes("request-a", 1, "completed")
    first = adapter.apply(2, terminal, observed_at_unix_ms=102)
    duplicate = adapter.apply(3, terminal, observed_at_unix_ms=103)
    late = adapter.apply(
        4,
        request_event_bytes("request-a", 2, "failed", code="late_failure"),
        observed_at_unix_ms=104,
    )

    session = snapshot(adapter)["sessions"][0]
    assert first.applied
    assert not duplicate.applied and duplicate.reason == "duplicate_session_event"
    assert not late.applied and late.reason == "event_after_terminal"
    assert session["state"] == "completed"
    assert session["event_count"] == 2
    assert session["last_sequence"] == 1
    assert [item["reason"] for item in incidents(adapter)] == ["event_after_terminal"]


def test_conflicting_terminal_at_same_sequence_is_quarantined(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    adapter.apply(1, request_event_bytes("request-a", 0, "accepted"), observed_at_unix_ms=101)
    adapter.apply(
        2,
        request_event_bytes("request-a", 1, "completed"),
        observed_at_unix_ms=102,
    )

    conflict = adapter.apply(
        3,
        request_event_bytes("request-a", 1, "failed", code="conflicting_terminal"),
        observed_at_unix_ms=103,
    )

    assert not conflict.applied
    assert conflict.reason == "session_sequence_conflict"
    assert incidents(adapter)[-1]["reason"] == "session_sequence_conflict"
    assert snapshot(adapter)["sessions"][0]["state"] == "completed"


def test_qualification_expiry_quarantines_recent_session_before_next_event(publisher) -> None:
    adapter = ObservatoryEventAdapter(
        publisher,
        max_qualification_age_ms=300_000,
        max_session_idle_ms=300_000,
    )
    adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    adapter.apply(1, request_event_bytes("request-a", 0, "accepted"), observed_at_unix_ms=101)

    expired = adapter.apply(
        2,
        request_event_bytes("request-a", 1, "token", token_index=0, text="private"),
        observed_at_unix_ms=300_001,
    )

    assert not expired.applied
    assert expired.reason == "stale_qualification"
    session = snapshot(adapter)["sessions"][0]
    assert session["state"] == "quarantined"
    assert session["quarantine_reason"] == "stale_qualification"
    assert session["token_count"] == 0


def test_cross_session_gap_and_stale_session_events_are_quarantined(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher, max_session_idle_ms=300_000)
    adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    adapter.apply(1, request_event_bytes("request-a", 0, "accepted"), observed_at_unix_ms=101)

    unknown = adapter.apply(
        2,
        request_event_bytes("request-b", 1, "token", token_index=0, text="x"),
        observed_at_unix_ms=102,
    )
    gap = adapter.apply(
        3,
        request_event_bytes("request-a", 2, "token", token_index=1, text="y"),
        observed_at_unix_ms=103,
    )
    stale = adapter.apply(
        4,
        request_event_bytes("request-a", 1, "token", token_index=0, text="z"),
        observed_at_unix_ms=300_102,
    )

    assert [unknown.reason, gap.reason, stale.reason] == [
        "cross_session_event",
        "session_sequence_gap",
        "stale_session",
    ]
    session = snapshot(adapter)["sessions"][0]
    assert session["state"] == "quarantined"
    assert session["quarantine_reason"] == "stale_session"
    assert session["event_count"] == 1


def test_stale_epoch_rejected_and_new_path_quarantines_bound_sessions(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    current = qualification_bytes(
        issued_at_unix_ms=100,
        deployment_epoch=2,
        path_manifest_digest="sha256:" + "a" * 64,
    )
    adapter.apply(0, current, observed_at_unix_ms=100)
    adapter.apply(1, request_event_bytes("request-a", 0, "accepted"), observed_at_unix_ms=101)

    stale_epoch = adapter.apply(
        2,
        qualification_bytes(
            qualification_id="qualification-stale-epoch",
            issued_at_unix_ms=102,
            deployment_epoch=1,
            path_manifest_digest="sha256:" + "b" * 64,
        ),
        observed_at_unix_ms=102,
    )
    changed_path = adapter.apply(
        3,
        qualification_bytes(
            qualification_id="qualification-new-path",
            issued_at_unix_ms=103,
            deployment_epoch=2,
            path_manifest_digest="sha256:" + "c" * 64,
        ),
        observed_at_unix_ms=103,
    )

    assert not stale_epoch.applied and stale_epoch.reason == "stale_deployment_epoch"
    assert changed_path.applied
    current_snapshot = snapshot(adapter)
    assert current_snapshot["qualification"]["qualification_id"] == "qualification-new-path"
    assert current_snapshot["sessions"][0]["state"] == "quarantined"
    assert current_snapshot["sessions"][0]["quarantine_reason"] == "path_changed"


def test_old_and_expired_qualification_events_fail_closed(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher, max_qualification_age_ms=300_000)
    adapter.apply(
        0,
        qualification_bytes(qualification_id="qualification-current", issued_at_unix_ms=200),
        observed_at_unix_ms=200,
    )
    older = adapter.apply(
        1,
        qualification_bytes(qualification_id="qualification-old", issued_at_unix_ms=199),
        observed_at_unix_ms=201,
    )
    expired_request = adapter.apply(
        2,
        request_event_bytes("request-a", 0, "accepted"),
        observed_at_unix_ms=300_201,
    )

    assert not older.applied and older.reason == "stale_qualification"
    assert not expired_request.applied and expired_request.reason == "stale_qualification"
    assert snapshot(adapter)["sessions"] == []


def test_bounds_limit_sessions_quarantine_and_slow_subscriber_buffer(tmp_path: Path) -> None:
    publisher = CoherentSnapshotPublisher(
        tmp_path / "bounded.json",
        subscriber_queue_size=1,
        replay_capacity=4,
    )
    adapter = ObservatoryEventAdapter(publisher, max_sessions=2, quarantine_capacity=2)
    adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    adapter.apply(1, request_event_bytes("request-a", 0, "accepted"), observed_at_unix_ms=101)
    adapter.apply(2, request_event_bytes("request-b", 0, "accepted"), observed_at_unix_ms=102)
    full = adapter.apply(
        3,
        request_event_bytes("request-c", 0, "accepted"),
        observed_at_unix_ms=103,
    )
    adapter.apply(4, b"not-json", observed_at_unix_ms=104)
    adapter.apply(5, b"still-not-json", observed_at_unix_ms=105)

    assert not full.applied and full.reason == "session_capacity"
    assert len(snapshot(adapter)["sessions"]) == 2
    assert [item["source_cursor"] for item in incidents(adapter)] == [4, 5]
    status = adapter.current_envelope()["bundle"]["provisioning"]
    assert status["dropped_quarantine_count"] == 1

    subscription = adapter.subscribe(last_event_id=publisher.current_publication().generation)
    adapter.apply(
        6,
        request_event_bytes("request-a", 1, "completed"),
        observed_at_unix_ms=106,
    )
    adapter.apply(
        7,
        request_event_bytes("request-b", 1, "completed"),
        observed_at_unix_ms=107,
    )
    assert subscription.closed
    assert subscription.disconnect_reason == "slow_consumer"


def test_source_cursor_gap_does_not_poison_missing_cursor_observation_time(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    gap = adapter.apply(1, qualification_bytes(), observed_at_unix_ms=1_000)
    resumed = adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)

    assert not gap.applied
    assert gap.reason == "source_cursor_gap"
    assert resumed.applied
    assert adapter.source_cursor == 0
    assert adapter.current_projection()["snapshot"]["observed_at_unix_ms"] == 100


def test_json_integers_outside_javascript_safe_range_fail_closed(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    document = json.loads(qualification_bytes())
    document["deployment_epoch"] = 9_007_199_254_740_992

    outcome = adapter.apply(
        0,
        json.dumps(document, separators=(",", ":")).encode(),
        observed_at_unix_ms=100,
    )

    assert not outcome.applied
    assert outcome.reason == "invalid_json"
    assert adapter.source_cursor == 0


def test_source_cursor_gap_is_visible_but_does_not_skip_missing_event(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    gap = adapter.apply(1, qualification_bytes(), observed_at_unix_ms=100)
    first = adapter.apply(0, qualification_bytes(), observed_at_unix_ms=101)

    assert not gap.applied and gap.reason == "source_cursor_gap"
    assert first.applied
    assert adapter.source_cursor == 0


def test_projection_bounds_are_compatible_with_ui_decoder(publisher) -> None:
    for option in (
        {"max_sessions": 257},
        {"quarantine_capacity": 257},
        {"max_event_bytes": 2 * 1024 * 1024 + 1},
    ):
        with pytest.raises(ValueError):
            ObservatoryEventAdapter(publisher, **option)
