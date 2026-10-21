# SPDX-License-Identifier: AGPL-3.0-only
"""M0: real gateway, real tokenizer, real streaming, simulated route."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mycelium_live.codec import GPT2PromptCodec
from mycelium_live.health import RouteHealthSource
from mycelium_live.route import FakeLiveRoute
from mycelium_live.router_port import LiveRouterPort
from mycelium_request_gateway import create_request_gateway_application


AUTH_TOKEN = "m0-request-credential"


async def _asgi_request(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    document: dict[str, Any] | None = None,
    session_token: str | None = None,
) -> tuple[int, bytes]:
    body = b"" if document is None else json.dumps(document).encode("utf-8")
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": body, "more_body": False})
    never_disconnect = asyncio.Event()
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if not incoming.empty():
            return await incoming.get()
        await never_disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [
        (b"authorization", f"Bearer {AUTH_TOKEN}".encode("ascii")),
        (b"content-type", b"application/json"),
    ]
    if session_token is not None:
        headers.append((b"x-mycelium-session", session_token.encode("ascii")))

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": headers,
        },
        receive,
        send,
    )
    start = next(item for item in sent if item["type"] == "http.response.start")
    payload = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    return start["status"], payload


async def _shutdown(app: Any) -> None:
    async def receive() -> dict[str, str]:
        return {"type": "lifespan.shutdown"}

    async def send(_message: dict[str, Any]) -> None:
        return None

    await app({"type": "lifespan"}, receive, send)


def _sse_documents(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    documents = []
    for block in body.decode("utf-8").strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        documents.append((fields["event"], json.loads(fields["data"])))
    return documents


def test_prompt_string_streams_decoded_text_through_the_real_gateway(
    deployment_dir, qualified_route
):
    qualification, graph = qualified_route
    codec = GPT2PromptCodec.from_deployment(deployment_dir)
    route = FakeLiveRoute(scripted_tokens=(4599, 3329, 2506, 5145))
    route.open()
    health = RouteHealthSource(route=route)
    health.publish(qualification)
    port = LiveRouterPort(route=route, execution_graph=graph)
    app = create_request_gateway_application(
        qualification_source=health,
        router=port,
        codec=codec,
        bearer_token=AUTH_TOKEN,
        request_id_source=lambda: "m0-request-001",
    )
    try:
        current_status, current_body = asyncio.run(
            _asgi_request(app, "/v1/qualification/current")
        )
        assert current_status == 200
        binding = json.loads(current_body)["binding"]

        prompt = "Tell me about mycelium"
        submission_status, submission_body = asyncio.run(
            _asgi_request(
                app,
                "/v1/inference",
                method="POST",
                document={
                    "protocol": "mycelium.request_gateway.v1",
                    "prompt": prompt,
                    "max_new_tokens": 4,
                    "qualification": binding,
                },
            )
        )
        assert submission_status == 202
        accepted = json.loads(submission_body)
        stream_path = accepted["stream_path"]
        session_token = accepted["session_token"]

        # The gateway locks the session to the token its session-token source
        # minted and advertises in the 202 body (A4 owner scoping, documented
        # in docs/operations/m1-gateway-control-runtime.md §6). A subscriber
        # that does not present that token is refused before a single event is
        # streamed, so the harness must carry it exactly like the product
        # client does (mycelium_ui_gateway/app.py forwards the owner token as
        # X-Mycelium-Session).
        refused_status, refused_body = asyncio.run(_asgi_request(app, stream_path))
        assert refused_status == 409
        assert json.loads(refused_body)["error"] == "session_owner_mismatch"

        stream_status, stream_body = asyncio.run(
            _asgi_request(app, stream_path, session_token=session_token)
        )
        events = _sse_documents(stream_body)
        token_text = "".join(
            document["text"] for kind, document in events if kind == "token"
        )

        assert stream_status == 200
        assert [kind for kind, _document in events] == [
            "accepted",
            "token",
            "token",
            "token",
            "token",
            "completed",
        ]
        assert token_text == "".join(
            codec.decode_token(token_id) for token_id in (4599, 3329, 2506, 5145)
        )
        assert token_text.strip() != ""
        assert codec.encode(prompt)
        assert route.counters().frames_sent == 4
    finally:
        asyncio.run(_shutdown(app))
