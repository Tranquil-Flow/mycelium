"""ASGI token-stream and cancellation contract tests; local fixtures only."""
from __future__ import annotations

import asyncio
import json
import time

from mycelium_request_gateway.asgi import RequestGatewayASGIApplication
from mycelium_request_gateway.auth import StaticBearerAuthenticator
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from test_core import ASGIHarness, MutableQualificationSource, _synthetic_qualification
from test_stream import ControlledBackend, ScriptedBackend


async def _run_stream(app, path: str, headers=()):
    messages = []
    first = True
    never = asyncio.Event()

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await never.wait()
        raise AssertionError("unreachable")

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": list(headers),
        },
        receive,
        send,
    )
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    response_headers = dict(
        next(item["headers"] for item in messages if item["type"] == "http.response.start")
    )
    body = b"".join(
        item.get("body", b"") for item in messages if item["type"] == "http.response.body"
    )
    return status, response_headers, body


def _sse_documents(body: bytes):
    documents = []
    for block in body.decode("utf-8").strip().split("\n\n"):
        if not block:
            continue
        fields = {}
        for line in block.splitlines():
            key, value = line.split(":", 1)
            fields[key] = value.lstrip()
        documents.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return documents


def test_sse_resume_uses_last_event_id_and_never_replays_older_tokens():
    qualification = _synthetic_qualification()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=ScriptedBackend(("zero", "one")),
        request_id_source=lambda: "sse-resume-001",
        max_buffered_events=8,
    )
    app = RequestGatewayASGIApplication(
        service=service,
        authenticator=StaticBearerAuthenticator("request-secret"),
    )
    try:
        request_id = service.submit(
            InferenceSubmission(
                prompt="private prompt",
                max_new_tokens=2,
                qualification=qualification_binding(qualification),
            )
        )
        deadline = time.monotonic() + 1
        while service.terminal_event_count(request_id) == 0 and time.monotonic() < deadline:
            time.sleep(0.005)

        status, headers, body = asyncio.run(
            _run_stream(
                app,
                f"/v1/inference/{request_id}/events",
                headers=[
                    (b"authorization", b"Bearer request-secret"),
                    (b"last-event-id", b"1"),
                ],
            )
        )
        documents = _sse_documents(body)

        assert status == 200
        assert headers[b"content-type"] == b"text/event-stream; charset=utf-8"
        assert [(sequence, kind) for sequence, kind, _ in documents] == [
            (2, "token"),
            (3, "completed"),
        ]
        assert documents[0][2]["text"] == "one"
        assert b"zero" not in body
    finally:
        service.close()


def test_stream_and_cancel_require_request_gateway_authentication():
    qualification = _synthetic_qualification()
    backend = ControlledBackend()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "sse-cancel-001",
        max_buffered_events=4,
    )
    app = RequestGatewayASGIApplication(
        service=service,
        authenticator=StaticBearerAuthenticator("request-secret"),
    )
    try:
        request_id = service.submit(
            InferenceSubmission(
                prompt="private prompt",
                max_new_tokens=2,
                qualification=qualification_binding(qualification),
            )
        )
        assert backend.first_emitted.wait(timeout=1)

        status, _, body = asyncio.run(
            ASGIHarness.request(
                app,
                f"/v1/inference/{request_id}",
                method="DELETE",
            )
        )
        assert status == 401
        assert body["error"] == "unauthorized"
        assert backend.cancelled == []

        status, _, body = asyncio.run(
            ASGIHarness.request(
                app,
                f"/v1/inference/{request_id}",
                method="DELETE",
                token="request-secret",
            )
        )
        assert status == 202
        assert body == {
            "request_id": request_id,
            "status": "cancelling",
        }
        assert backend.cancelled == [request_id]
    finally:
        service.close()
