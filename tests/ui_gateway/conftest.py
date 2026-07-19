from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any

import pytest

from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application

REQUEST_TOKEN = "request-upstream-bearer-canary-7f93a2"
OBSERVATORY_TOKEN = "observatory-upstream-bearer-canary-8c04b3"
PROMPT_CANARY = "private prompt that must not leave the inference stream"
ERROR_CANARY = "private exception and filesystem /Users/operator/secret"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def qualification(*, route_ready: bool = False) -> dict[str, Any]:
    return {
        "protocol": "mycelium.request_gateway.v1",
        "issued_at_unix_ms": 1_000,
        "evidence_class": "physical_qualification",
        "route_ready": route_ready,
        "reason_codes": [] if route_ready else ["route_not_ready"],
        "binding": {
            "qualification_id": "qualification-1",
            "qualification_digest": DIGEST_A,
            "deployment_id": "deployment-1",
            "deployment_epoch": 1,
            "topology_version": 2,
            "model_id": "model-1",
            "resolved_commit": "revision-1",
            "manifest_digest": DIGEST_B,
            "path_manifest_digest": DIGEST_C,
            "stage_load_proof_digests": [DIGEST_A],
        },
    }


def submission(prompt: str = PROMPT_CANARY) -> dict[str, Any]:
    projection = qualification(route_ready=True)
    return {
        "protocol": "mycelium.request_gateway.v1",
        "prompt": prompt,
        "max_new_tokens": 4,
        "qualification": projection["binding"],
    }


def observatory_snapshot(*, generation: int = 7) -> dict[str, Any]:
    return {
        "protocol": "mycelium.observatory_stream.v1",
        "generation": generation,
        "bundle": {
            "snapshot": {
                "protocol": "mycelium.observatory.request_projection.v1",
                "source_cursor": generation,
                "observed_at_unix_ms": 1_000,
                "qualification": None,
                "sessions": [],
            },
            "incidents": [],
            "provisioning": {
                "protocol": "mycelium.observatory.event_adapter_status.v1",
                "route_ready": False,
                "source_cursor": generation,
                "buffered_sessions": 0,
                "quarantine_capacity": 4,
                "dropped_quarantine_count": 0,
            },
        },
    }


@dataclass
class ASGIResult:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes
    messages: list[dict[str, Any]]

    def header(self, name: str) -> str | None:
        wanted = name.encode("ascii").lower()
        values = [value.decode("latin-1") for key, value in self.headers if key.lower() == wanted]
        if not values:
            return None
        assert len(values) == 1, (name, values)
        return values[0]

    def json(self) -> dict[str, Any]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


async def asgi_request(
    app,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 49152),
    scheme: str = "http",
    server: tuple[str, int] = ("127.0.0.1", 8080),
    query_string: bytes = b"",
) -> ASGIResult:
    request_headers = [(b"host", f"{server[0]}:{server[1]}".encode("ascii"))]
    request_headers.extend(headers or [])
    incoming = asyncio.Queue()
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

    await app(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": scheme,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "headers": request_headers,
            "client": client,
            "server": server,
        },
        receive,
        send,
    )
    starts = [message for message in sent if message.get("type") == "http.response.start"]
    assert len(starts) == 1, sent
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message.get("type") == "http.response.body"
    )
    return ASGIResult(
        status=starts[0]["status"],
        headers=list(starts[0].get("headers", [])),
        body=response_body,
        messages=sent,
    )


def request(app, path: str, **kwargs: Any) -> ASGIResult:
    return asyncio.run(asgi_request(app, path, **kwargs))


def json_body(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def cookie_pair(result: ASGIResult) -> bytes:
    cookie = result.header("set-cookie")
    assert cookie is not None
    return cookie.split(";", 1)[0].encode("ascii")


def session_headers(
    bootstrap: ASGIResult,
    *,
    csrf: bool = False,
    origin: bytes = b"http://127.0.0.1:8080",
) -> list[tuple[bytes, bytes]]:
    result = [(b"cookie", cookie_pair(bootstrap))]
    if csrf:
        result.extend(
            [
                (b"origin", origin),
                (b"x-mycelium-csrf", bootstrap.json()["session"]["csrf_token"].encode("ascii")),
            ]
        )
    return result


class FakeUpstream:
    def __init__(
        self,
        responder: Callable[[Mapping[str, Any], bytes], Awaitable[tuple[int, list[tuple[bytes, bytes]], list[bytes]]]],
    ) -> None:
        self.responder = responder
        self.calls: list[tuple[Mapping[str, Any], bytes]] = []
        self.cancelled = False

    async def __call__(self, scope, receive, send) -> None:
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                self.cancelled = True
                return
            if message.get("type") != "http.request":
                raise RuntimeError("unexpected fake upstream receive")
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        immutable_scope = dict(scope)
        immutable_scope["headers"] = list(scope.get("headers", []))
        self.calls.append((immutable_scope, bytes(body)))
        status, headers, chunks = await self.responder(scope, bytes(body))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        if not chunks:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        for index, chunk in enumerate(chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": index != len(chunks) - 1,
                }
            )


async def default_observatory(scope, _body):
    assert scope["path"].startswith("/v1/observatory/")
    return (
        200,
        [(b"content-type", b"application/json"), (b"cache-control", b"no-store")],
        [json_body(observatory_snapshot())],
    )


async def default_request(scope, body):
    path = scope["path"]
    if path == "/v1/qualification/current":
        return 200, [(b"content-type", b"application/json")], [json_body(qualification())]
    if path == "/v1/inference" and scope["method"] == "POST":
        assert json.loads(body)["prompt"] == PROMPT_CANARY
        return (
            202,
            [(b"content-type", b"application/json")],
            [
                json_body(
                    {
                        "request_id": "request-1",
                        "stream_path": "/v1/inference/request-1/events",
                        "cancel_path": "/v1/inference/request-1",
                    }
                )
            ],
        )
    if path == "/v1/inference/request-1/events":
        events = [
            {
                "protocol": "mycelium.request_event.v1",
                "request_id": "request-1",
                "sequence": 0,
                "type": "accepted",
            },
            {
                "protocol": "mycelium.request_event.v1",
                "request_id": "request-1",
                "sequence": 1,
                "type": "token",
                "token_index": 0,
                "text": "hello",
            },
            {
                "protocol": "mycelium.request_event.v1",
                "request_id": "request-1",
                "sequence": 2,
                "type": "completed",
            },
        ]
        frames = [
            f"id: {event['sequence']}\nevent: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'), sort_keys=True)}\n\n".encode()
            for event in events
        ]
        return 200, [(b"content-type", b"text/event-stream; charset=utf-8")], frames
    if path == "/v1/inference/request-1" and scope["method"] == "DELETE":
        return (
            202,
            [(b"content-type", b"application/json")],
            [json_body({"request_id": "request-1", "status": "cancelling"})],
        )
    return 404, [(b"content-type", b"application/json")], [json_body({"error": "not_found"})]


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def status(self):
        self.calls.append(("status", None))
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "native_nodes": [
                {
                    "member_id": "node-1",
                    "capability": "native_inference_node",
                    "membership_state": "reachable",
                    "connectivity": "direct",
                    "endpoint_id": "endpoint-1",
                }
            ],
            "browser_workers": [],
        }

    def create_invite(self, document):
        self.calls.append(("create_invite", dict(document)))
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "invite_id": "invite-1",
            "invite_code": "invite-code-secret-1234",
            "capability": document["capability"],
            "expires_at_unix_ms": 60_000,
        }

    async def join(self, document):
        self.calls.append(("join", dict(document)))
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "joined": True,
            "member_id": "node-joined",
            "capability": "native_inference_node",
        }

    def leave(self, document):
        self.calls.append(("leave", dict(document)))
        return {
            "protocol": "mycelium.product_ui.swarm.v1",
            "member_id": document["member_id"],
            "left": True,
        }


@pytest.fixture
def upstreams():
    return FakeUpstream(default_observatory), FakeUpstream(default_request)


@pytest.fixture
def coordinator():
    return FakeCoordinator()


@pytest.fixture
def app(upstreams, coordinator):
    observatory, request_gateway = upstreams
    return create_product_gateway_application(
        config=GatewayConfig(),
        observatory_app=observatory,
        request_gateway_app=request_gateway,
        swarm_coordinator=coordinator,
        request_gateway_bearer_token=REQUEST_TOKEN,
        observatory_bearer_token=OBSERVATORY_TOKEN,
    )


@pytest.fixture
def bootstrap(app):
    result = request(app, "/api/v1/bootstrap")
    assert result.status == 200
    return result
