# SPDX-License-Identifier: AGPL-3.0-only
"""The browser gateway's per-request tables (owner tokens, cancel dispatch, terminal marks) are bounded."""
import asyncio
import json

from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application
from ui_gateway_support import (
    FakeCoordinator, FakeUpstream, asgi_request, default_observatory, default_request, json_body,
    session_headers, submission,
)


def _app(clock=None):
    counter = {"n": 0}

    async def upstream(scope, receive, send):
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        if scope["path"] == "/v1/inference" and scope["method"] == "POST":
            counter["n"] += 1
            identifier = f"request-{counter['n']:06d}"
            document = {"request_id": identifier, "stream_path": f"/v1/inference/{identifier}/events",
                        "cancel_path": f"/v1/inference/{identifier}", "session_token": f"owner-token-{identifier}-0000000000"}
            await send({"type": "http.response.start", "status": 202, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps(document).encode(), "more_body": False})
            return
        status, headers, chunks = await default_request(scope, bytes(body))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        for index, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk, "more_body": index + 1 < len(chunks)})

    kwargs = {} if clock is None else {"clock": clock}
    return create_product_gateway_application(
        config=GatewayConfig(max_sessions=2, max_requests_per_session=2, session_ttl_seconds=60),
        observatory_app=FakeUpstream(default_observatory), request_gateway_app=upstream,
        swarm_coordinator=FakeCoordinator(), request_gateway_bearer_token="private-request-bearer", **kwargs)


async def _submit_many(app, sessions):
    for _ in range(sessions):
        boot = await asgi_request(app, "/api/v1/bootstrap")
        for _ in range(2):
            response = await asgi_request(app, "/api/v1/inference", method="POST", body=json_body(submission()),
                                          headers=session_headers(boot, csrf=True) + [(b"content-type", b"application/json")])
            assert response.status == 202
    return boot


def _inner(app):
    for candidate in (app, getattr(app, "__self__", None), getattr(app, "_app", None)):
        if candidate is not None and hasattr(candidate, "_request_owner_tokens"):
            return candidate
    raise AssertionError("gateway application object not reachable")


def test_owner_tokens_cancel_marks_and_terminal_marks_stay_bounded():
    async def scenario():
        app = _app()
        await _submit_many(app, 400)  # 800 accepted requests across recycled sessions
        gateway = _inner(app)
        for index in range(2_000):
            gateway._cancel_dispatched.add(f"c{index}")
            gateway._terminal_requests.add(f"t{index}")
        assert len(gateway._request_owner_tokens) <= 256
        assert len(gateway._cancel_dispatched) <= 256
        assert len(gateway._terminal_requests) <= 256
        assert gateway._request_owner_tokens.get("request-000800") is not None
        assert gateway._request_owner_tokens.get("request-000001") is None
    asyncio.run(scenario())


def test_owner_tokens_outlive_their_session_only_by_a_short_grace():
    async def scenario():
        now = {"t": 1_000.0}
        app = _app(clock=lambda: now["t"])
        await _submit_many(app, 1)
        gateway = _inner(app)
        assert len(gateway._request_owner_tokens) == 2
        now["t"] += 60 + 600 + 1  # session ttl + stream grace
        assert gateway._request_owner_tokens.get("request-000001") is None
        assert gateway._request_owner_tokens.get("request-000002") is None
    asyncio.run(scenario())
