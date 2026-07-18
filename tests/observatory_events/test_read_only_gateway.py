from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from conftest import CANARY, qualification_bytes, request_event_bytes

from mycelium_gateway.asgi import ObservatoryASGIApplication
from mycelium_gateway.event_adapter import EventAdapterStateError, ObservatoryEventAdapter
from mycelium_gateway.observatory import CoherentSnapshotPublisher


async def request(
    app: ObservatoryASGIApplication,
    *,
    method: str,
    path: str = "/v1/observatory/snapshot",
) -> list[dict[str, object]]:
    sent: list[dict[str, object]] = []
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 4040),
            "server": ("127.0.0.1", 8080),
        },
        receive,
        send,
    )
    return sent


def response_body(messages: list[dict[str, object]]) -> bytes:
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message.get("type") == "http.response.body"
    )


def test_existing_observatory_surface_serves_adapter_projection_by_get(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    app = ObservatoryASGIApplication(adapter, read_policy=lambda _scope: True)

    messages = asyncio.run(request(app, method="GET"))
    payload = json.loads(response_body(messages))

    assert messages[0]["status"] == 200
    assert payload["protocol"] == "mycelium.observatory_stream.v1"
    assert payload["bundle"]["provisioning"]["route_ready"] is False


def test_observatory_adapter_adds_no_mutating_http_surface(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    app = ObservatoryASGIApplication(adapter, read_policy=lambda _scope: True)

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        messages = asyncio.run(request(app, method=method))
        assert messages[0]["status"] == 405

    for name in ("submit", "cancel", "admit", "dispatch", "qualify", "promote"):
        assert not hasattr(adapter, name)


def test_default_state_and_errors_retain_no_private_event_material(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    qualification = json.loads(qualification_bytes())
    qualification["stage_bindings"] = [
        {
            "stage_id": "stage-0",
            "placement_id": "placement-0",
            "assignment_id": "assignment-0",
            "node_id": "node-0",
            "stage_signature": "sha256:" + "1" * 64,
            "load_proof_digest": "sha256:" + "2" * 64,
            "stage_probe_result_digest": "sha256:" + "3" * 64,
            "endpoint_id": "private-endpoint-canary",
            "process_id": 43210,
            "process_host_id": "private-host-canary",
            "tensor_scope_digest": "sha256:" + "4" * 64,
            "reservation_id": "private-reservation-canary",
        }
    ]
    adapter.apply(
        0,
        json.dumps(qualification, separators=(",", ":")).encode(),
        observed_at_unix_ms=100,
    )
    outcome = adapter.apply(
        1,
        request_event_bytes("request-a", 0, "token", token_index=0, text=CANARY),
        observed_at_unix_ms=101,
    )

    assert not outcome.applied
    assert CANARY not in str(outcome)
    serialized = json.dumps(adapter.current_envelope(), sort_keys=True)
    for private_value in (
        CANARY,
        "private-endpoint-canary",
        "private-host-canary",
        "private-reservation-canary",
        "43210",
    ):
        assert private_value not in serialized


def test_restart_rejects_private_identifier_in_persisted_projection(publisher) -> None:
    adapter = ObservatoryEventAdapter(publisher)
    adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    adapter.apply(
        1,
        request_event_bytes("request-a", 0, "accepted"),
        observed_at_unix_ms=101,
    )
    bundle = adapter.current_envelope()["bundle"]
    bundle["snapshot"]["sessions"][0]["qualification_id"] = "localhost:8080"
    publisher.publish(bundle)

    with pytest.raises(EventAdapterStateError):
        ObservatoryEventAdapter(publisher)


def test_restart_rejects_active_session_bound_to_other_qualification(tmp_path) -> None:
    publisher = CoherentSnapshotPublisher(tmp_path / "incoherent.json")
    adapter = ObservatoryEventAdapter(publisher)
    adapter.apply(0, qualification_bytes(), observed_at_unix_ms=100)
    adapter.apply(
        1,
        request_event_bytes("request-a", 0, "accepted"),
        observed_at_unix_ms=101,
    )
    envelope = adapter.current_envelope()
    assert envelope is not None
    envelope["bundle"]["snapshot"]["sessions"][0]["qualification_id"] = (
        "other-qualification"
    )
    publisher.publish(envelope["bundle"])

    with pytest.raises(EventAdapterStateError):
        ObservatoryEventAdapter(publisher)


def test_adapter_source_contains_no_control_plane_or_qualification_authority_imports() -> None:
    import mycelium_gateway.event_adapter as module

    source = inspect.getsource(module)
    assert "mycelium_qualification.qualifier" not in source
    assert "mycelium_router" not in source
    assert "mycelium_request_gateway.gateway" not in source
    assert "route_ready=True" not in source.replace(" ", "")
