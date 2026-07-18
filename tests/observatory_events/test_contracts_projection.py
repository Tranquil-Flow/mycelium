from __future__ import annotations

import json

from conftest import CANARY, qualification_bytes, request_event_bytes

from mycelium_gateway.event_adapter import (
    OBSERVATORY_EVENT_PROJECTION_PROTOCOL,
    OBSERVATORY_EVENT_STATUS_PROTOCOL,
    ObservatoryEventAdapter,
)


def current_bundle(adapter: ObservatoryEventAdapter) -> dict:
    envelope = adapter.current_envelope()
    assert envelope is not None
    return envelope["bundle"]


def test_exact_v1_contracts_project_only_allowlisted_metadata(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)

    qualification = adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    accepted = adapter.apply(
        1,
        request_event_bytes("request-z", 0, "accepted"),
        observed_at_unix_ms=101,
    )
    token = adapter.apply(
        2,
        request_event_bytes("request-z", 1, "token", token_index=0, text=CANARY),
        observed_at_unix_ms=102,
    )

    assert qualification.applied and accepted.applied and token.applied
    bundle = current_bundle(adapter)
    assert bundle["snapshot"]["protocol"] == OBSERVATORY_EVENT_PROJECTION_PROTOCOL
    assert bundle["provisioning"] == {
        "protocol": OBSERVATORY_EVENT_STATUS_PROTOCOL,
        "route_ready": False,
        "source_cursor": 2,
        "buffered_sessions": 1,
        "quarantine_capacity": 16,
        "dropped_quarantine_count": 0,
    }
    assert bundle["snapshot"]["qualification"]["protocol"] == "mycelium.route_qualification.v1"
    assert bundle["snapshot"]["sessions"] == [
        {
            "request_id": "request-z",
            "state": "streaming",
            "last_sequence": 1,
            "event_count": 2,
            "token_count": 1,
            "terminal": False,
            "qualification_id": "synthetic-test-fixture:route-qualification-v1",
            "started_at_unix_ms": 101,
            "updated_at_unix_ms": 102,
            "quarantine_reason": None,
        }
    ]
    wire = json.dumps(bundle, sort_keys=True)
    assert CANARY not in wire
    for prohibited in (
        "prompt",
        "token_index",
        '"text"',
        "endpoint_id",
        "process_id",
        "reservation_id",
        "tensor_scope_digest",
    ):
        assert prohibited not in wire


def test_unknown_fields_versions_malformed_and_oversized_payloads_fail_closed(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher, max_event_bytes=8_192, quarantine_capacity=8)
    assert adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100).applied

    rejected = [
        adapter.apply(
            1,
            request_event_bytes("request-a", 0, "accepted", prompt=CANARY),
            observed_at_unix_ms=101,
        ),
        adapter.apply(
            2,
            request_event_bytes(
                "request-a",
                0,
                "accepted",
                protocol="mycelium.request_event.v2",
            ),
            observed_at_unix_ms=102,
        ),
        adapter.apply(3, b'{"protocol":', observed_at_unix_ms=103),
        adapter.apply(
            4,
            b'{' + (b'"private":"' + CANARY.encode() + b'x' * 9_000 + b'"}'),
            observed_at_unix_ms=104,
        ),
    ]

    assert [item.applied for item in rejected] == [False, False, False, False]
    assert [item.reason for item in rejected] == [
        "invalid_request_event",
        "unsupported_protocol",
        "invalid_json",
        "event_too_large",
    ]
    bundle = current_bundle(adapter)
    assert [incident["reason"] for incident in bundle["incidents"]] == [
        "invalid_request_event",
        "unsupported_protocol",
        "invalid_json",
        "event_too_large",
    ]
    wire = json.dumps(bundle, sort_keys=True)
    assert CANARY not in wire
    assert "private" not in wire


def test_duplicate_json_keys_and_non_bytes_input_fail_closed(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    duplicate = (
        b'{"protocol":"mycelium.request_event.v1",'
        b'"protocol":"mycelium.request_event.v1",'
        b'"request_id":"request-a","sequence":0,"type":"accepted"}'
    )

    outcome = adapter.apply(0, duplicate, observed_at_unix_ms=100)

    assert not outcome.applied
    assert outcome.reason == "invalid_json"
    assert current_bundle(adapter)["incidents"][0]["reason"] == "invalid_json"


def test_endpoint_and_credential_shaped_qualification_identifiers_are_not_retained(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    private_values = ("localhost:8080", "sk-" + "privateabcdefghijklmnop6789")

    first = adapter.apply(
        0,
        qualification_bytes(deployment_id=private_values[0]),
        observed_at_unix_ms=100,
    )
    second = adapter.apply(
        1,
        qualification_bytes(model_id=private_values[1]),
        observed_at_unix_ms=101,
    )

    assert first.reason == "invalid_qualification_event"
    assert second.reason == "invalid_qualification_event"
    serialized = json.dumps(adapter.current_envelope(), sort_keys=True)
    assert all(value not in serialized for value in private_values)


def test_route_ready_true_is_never_reconstructed_or_promoted(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    document = json.loads(qualification_bytes())
    document.update(
        route_ready=True,
        evidence_class="physical_qualification",
        reason_codes=[],
        qualified_by="mycelium_qualification.qualifier:RouteQualificationV1",
    )

    outcome = adapter.apply(
        0,
        json.dumps(document, separators=(",", ":")).encode(),
        observed_at_unix_ms=100,
    )

    assert not outcome.applied
    assert outcome.reason == "invalid_qualification_event"
    bundle = current_bundle(adapter)
    assert bundle["snapshot"]["qualification"] is None
    assert bundle["provisioning"]["route_ready"] is False


def test_projection_is_deterministic_and_sessions_are_sorted(tmp_path) -> None:
    from mycelium_gateway.observatory import CoherentSnapshotPublisher

    events = [
        (0, qualification_bytes(), 100),
        (1, request_event_bytes("request-z", 0, "accepted"), 101),
        (2, request_event_bytes("request-a", 0, "accepted"), 102),
        (3, request_event_bytes("request-a", 1, "completed"), 103),
    ]
    envelopes = []
    for name in ("first.json", "second.json"):
        adapter = ObservatoryEventAdapter(CoherentSnapshotPublisher(tmp_path / name))
        for cursor, payload, observed_at in events:
            assert adapter.apply(cursor, payload, observed_at_unix_ms=observed_at).publication is not None
        envelopes.append(adapter.current_envelope())

    assert envelopes[0] == envelopes[1]
    assert [
        session["request_id"] for session in envelopes[0]["bundle"]["snapshot"]["sessions"]
    ] == ["request-a", "request-z"]
