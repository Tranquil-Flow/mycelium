"""Host-level product composition only; this is not physical-route proof.

The authority consumes an in-memory synthetic evidence-shape fixture. No device,
network transport, or physical execution is exercised or established here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

import model_manifest as model_manifest_contract
from mycelium_demo.product_stack import build_loopback_product_stack
from mycelium_qualification import QualificationAuthority
from mycelium_router.serialization import execution_graph_from_dict
from scripts import generate_contract_fixtures


ROOT = Path(__file__).resolve().parents[2]
REQUEST_BEARER_TOKEN = "host-composition-request-bearer"


@dataclass(frozen=True)
class ASGIResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes

    def header(self, name: bytes) -> bytes | None:
        values = [value for key, value in self.headers if key.lower() == name.lower()]
        assert len(values) <= 1
        return values[0] if values else None

    def json(self) -> dict[str, Any]:
        document = json.loads(self.body)
        assert isinstance(document, dict)
        return document


async def _asgi_request(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: tuple[tuple[bytes, bytes], ...] = (),
    scheme: str = "http",
    host: bytes = b"127.0.0.1:8080",
) -> ASGIResponse:
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

    await app(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": scheme,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"host", host), *headers],
            "client": ("127.0.0.1", 49152),
            "server": ("127.0.0.1", 8080),
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
    return ASGIResponse(
        status=starts[0]["status"],
        headers=tuple(starts[0].get("headers", ())),
        body=response_body,
    )


def _request(app: Any, path: str, **kwargs: Any) -> ASGIResponse:
    return asyncio.run(_asgi_request(app, path, **kwargs))


def _published_qualification():
    spec = importlib.util.spec_from_file_location(
        "product_composition_qualification_fixture",
        ROOT / "tests" / "qualification" / "conftest.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    original_model_manifest = generate_contract_fixtures.model_manifest

    def browser_public_model_manifest():
        manifest = original_model_manifest()
        manifest["model_id"] = "org-model"
        unsigned = dict(manifest)
        unsigned.pop("manifest_digest")
        manifest["manifest_digest"] = {
            "algorithm": "sha256",
            "value": hashlib.sha256(
                model_manifest_contract.canonical_json(unsigned).encode("utf-8")
            ).hexdigest(),
        }
        return manifest

    setattr(module, "model_manifest", browser_public_model_manifest)
    generate_contract_fixtures.model_manifest = browser_public_model_manifest
    try:
        case = module.make_case()
    finally:
        generate_contract_fixtures.model_manifest = original_model_manifest
    files, manifest = case.render()
    authority = QualificationAuthority(clock_unix_ms=lambda: case.now_unix_ms)
    record = authority.qualify_and_publish(
        evidence_files=files,
        evidence_manifest=manifest,
        verify_gossip_signature=module.synthetic_signature_verifier,
        verify_load_proof_signature=module.synthetic_signature_verifier,
    )
    graph = execution_graph_from_dict(json.loads(files["router/execution-graph.json"]))
    return authority, record, graph


class DeterministicCodec:
    def __init__(self) -> None:
        self.encoded: list[str] = []

    def encode(self, prompt: str) -> tuple[int, ...]:
        self.encoded.append(prompt)
        return (7, 8)

    def decode_token(self, token_id: int) -> str:
        return f"host-token-{token_id - 700}"


class DeterministicRouter:
    def __init__(self, graph: Any) -> None:
        self.graph = graph
        self.admit_calls = 0
        self._requests: dict[str, dict[str, Any]] = {}

    def current_deployment(self):
        return self.graph

    def admit(self, request, client_sink, *, pinned_deployment=None, **_kwargs):
        assert pinned_deployment == self.graph
        self.admit_calls += 1
        self._requests[request.request_id] = {
            "request": request,
            "sink": client_sink,
            "next_token": 0,
            "status": "DECODING",
        }
        return request.request_id

    def request_status(self, request_id: str) -> str:
        return self._requests[request_id]["status"]

    def decode_one(self, request_id: str) -> bool:
        state = self._requests[request_id]
        token_index = state["next_token"]
        state["sink"].emit(token_index, 700 + token_index)
        state["next_token"] = token_index + 1
        if state["next_token"] >= state["request"].max_new_tokens:
            state["status"] = "COMPLETED"
        return True

    def cancel(self, request_id: str) -> bool:
        state = self._requests.get(request_id)
        if state is None:
            return False
        state["status"] = "CANCELLED"
        return True


async def _unused_observatory(_scope, _receive, _send) -> None:
    raise AssertionError("observatory is outside this focused composition path")


def test_host_level_product_composition_reaches_browser_token_boundary_only() -> None:
    authority, record, graph = _published_qualification()
    router = DeterministicRouter(graph)
    codec = DeterministicCodec()
    app = build_loopback_product_stack(
        qualification_source=authority,
        router=router,
        codec=codec,
        observatory_app=_unused_observatory,
        swarm_coordinator=object(),
        request_bearer_token=REQUEST_BEARER_TOKEN,
    )

    bootstrap = _request(app, "/api/v1/bootstrap")
    assert bootstrap.status == 200
    assert bootstrap.json()["source_mode"] == "live"
    set_cookie = bootstrap.header(b"set-cookie")
    assert set_cookie is not None
    cookie = set_cookie.split(b";", 1)[0]
    session_headers = ((b"cookie", cookie),)

    current = _request(
        app,
        "/api/v1/qualification/current",
        headers=session_headers,
    )
    assert current.status == 200
    assert current.json()["route_ready"] is True
    assert current.json()["binding"]["qualification_id"] == record.qualification_id
    assert authority.current() is record

    prompt = "host-only composition prompt"
    submission = {
        "protocol": "mycelium.request_gateway.v1",
        "prompt": prompt,
        "max_new_tokens": 2,
        "qualification": current.json()["binding"],
    }
    payload = json.dumps(submission, separators=(",", ":"), sort_keys=True).encode()
    rejected = _request(
        app,
        "/api/v1/inference",
        method="POST",
        body=payload,
        headers=(*session_headers, (b"content-type", b"application/json")),
    )
    assert rejected.status == 403
    assert rejected.json()["code"] == "csrf_required"
    assert router.admit_calls == 0
    assert codec.encoded == []

    csrf_token = bootstrap.json()["session"]["csrf_token"].encode("ascii")
    accepted = _request(
        app,
        "/api/v1/inference",
        method="POST",
        body=payload,
        headers=(
            *session_headers,
            (b"content-type", b"application/json"),
            (b"origin", b"http://127.0.0.1:8080"),
            (b"x-mycelium-csrf", csrf_token),
        ),
    )
    assert accepted.status == 202
    assert router.admit_calls == 1
    assert codec.encoded == [prompt]

    event_path = accepted.json()["event_path"]
    stream = _request(app, event_path, headers=session_headers)
    assert stream.status == 200
    assert stream.header(b"content-type") == b"text/event-stream; charset=utf-8"
    assert b"event: accepted" in stream.body
    assert b"event: token" in stream.body
    assert b'"text":"host-token-0"' in stream.body
    assert b'"text":"host-token-1"' in stream.body
    assert b"event: completed" in stream.body

    assert authority.drop(expected_qualification_id=record.qualification_id) is True
    dropped = _request(
        app,
        "/api/v1/inference",
        method="POST",
        body=payload,
        headers=(
            *session_headers,
            (b"content-type", b"application/json"),
            (b"origin", b"http://127.0.0.1:8080"),
            (b"x-mycelium-csrf", csrf_token),
        ),
    )
    assert dropped.status == 409
    assert dropped.json()["code"] == "route_dropped"
    assert router.admit_calls == 1
    assert codec.encoded == [prompt]


def test_trusted_proxy_composition_requires_authenticated_proxy_capability() -> None:
    authority, _record, graph = _published_qualification()
    proxy_capability = b"a" * 64
    app = build_loopback_product_stack(
        qualification_source=authority,
        router=DeterministicRouter(graph),
        codec=DeterministicCodec(),
        observatory_app=_unused_observatory,
        swarm_coordinator=object(),
        request_bearer_token=REQUEST_BEARER_TOKEN,
        public_origin="https://a8.example.test",
        trusted_https_proxy=True,
        trusted_proxy_capability=proxy_capability,
    )

    denied = _request(
        app,
        "/api/v1/bootstrap",
        scheme="https",
        host=b"a8.example.test",
    )
    forged_identity = _request(
        app,
        "/api/v1/bootstrap",
        scheme="https",
        host=b"a8.example.test",
        headers=((b"x-mycelium-authenticated-user", b"owner"),),
    )
    wrong_capability = _request(
        app,
        "/api/v1/bootstrap",
        scheme="https",
        host=b"a8.example.test",
        headers=(
            (b"x-mycelium-authenticated-user", b"owner"),
            (b"x-mycelium-proxy-capability", b"b" * 64),
        ),
    )
    wrong_identity = _request(
        app,
        "/api/v1/bootstrap",
        scheme="https",
        host=b"a8.example.test",
        headers=(
            (b"x-mycelium-authenticated-user", b"administrator"),
            (b"x-mycelium-proxy-capability", proxy_capability),
        ),
    )
    duplicate_identity = _request(
        app,
        "/api/v1/bootstrap",
        scheme="https",
        host=b"a8.example.test",
        headers=(
            (b"x-mycelium-authenticated-user", b"owner"),
            (b"x-mycelium-authenticated-user", b"owner"),
            (b"x-mycelium-proxy-capability", proxy_capability),
        ),
    )
    duplicate_capability = _request(
        app,
        "/api/v1/bootstrap",
        scheme="https",
        host=b"a8.example.test",
        headers=(
            (b"x-mycelium-authenticated-user", b"owner"),
            (b"x-mycelium-proxy-capability", proxy_capability),
            (b"x-mycelium-proxy-capability", b"b" * 64),
        ),
    )
    accepted = _request(
        app,
        "/api/v1/bootstrap",
        scheme="https",
        host=b"a8.example.test",
        headers=(
            (b"x-mycelium-authenticated-user", b"owner"),
            (b"x-mycelium-proxy-capability", proxy_capability),
        ),
    )

    for response in (
        denied,
        forged_identity,
        wrong_capability,
        wrong_identity,
        duplicate_identity,
        duplicate_capability,
    ):
        assert response.status == 403
        assert response.json()["code"] == "access_denied"
    assert accepted.status == 200


def test_trusted_proxy_composition_requires_proxy_capability_configuration() -> None:
    authority, _record, graph = _published_qualification()
    for capability in (None, b"a" * 63, b"a" * 65, b"A" * 64, b"g" * 64):
        with pytest.raises(ValueError, match="invalid_trusted_proxy_capability"):
            build_loopback_product_stack(
                qualification_source=authority,
                router=DeterministicRouter(graph),
                codec=DeterministicCodec(),
                observatory_app=_unused_observatory,
                swarm_coordinator=object(),
                request_bearer_token=REQUEST_BEARER_TOKEN,
                public_origin="https://a8.example.test",
                trusted_https_proxy=True,
                trusted_proxy_capability=capability,
            )

    with pytest.raises(
        ValueError,
        match="trusted_proxy_capability_requires_trusted_https_proxy",
    ):
        build_loopback_product_stack(
            qualification_source=authority,
            router=DeterministicRouter(graph),
            codec=DeterministicCodec(),
            observatory_app=_unused_observatory,
            swarm_coordinator=object(),
            request_bearer_token=REQUEST_BEARER_TOKEN,
            trusted_proxy_capability=b"a" * 64,
        )
