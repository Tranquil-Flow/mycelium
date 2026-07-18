"""Core request-gateway contract tests.

All RouteQualificationV1 values in this directory come from the qualifier's
in-memory synthetic fixture. They prove local contract behavior only; they do
not establish physical, distributed, or route_ready evidence.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import threading
from typing import Any

import pytest

from mycelium_qualification.evidence import sha256_bytes
from mycelium_qualification.qualifier import qualify_route
from mycelium_request_gateway.asgi import RequestGatewayASGIApplication
from mycelium_request_gateway.auth import StaticBearerAuthenticator
from mycelium_request_gateway.contracts import (
    AdmissionError,
    InferenceSubmission,
    QualificationBinding,
    REQUEST_GATEWAY_PROTOCOL,
    qualification_binding,
)
from mycelium_request_gateway.service import RequestGatewayService


ROOT = Path(__file__).resolve().parents[2]
AUTH_TOKEN = "gateway-test-credential"


def _synthetic_qualification():
    spec = importlib.util.spec_from_file_location(
        "request_gateway_qualification_fixture",
        ROOT / "tests" / "qualification" / "conftest.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    case = module.make_case()
    files, manifest = case.render()

    def verify(statement: bytes, signature: dict[str, Any]) -> bool:
        return (
            signature.get("algorithm") == "ed25519"
            and signature.get("signature")
            == "synthetic-test-signature-never-production"
            and signature.get("signed_statement_digest") == sha256_bytes(statement)
        )

    return qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms,
        verify_gossip_signature=verify,
        verify_load_proof_signature=verify,
    )


class MutableQualificationSource:
    def __init__(self, current):
        self.value = current

    def current(self):
        return self.value


class BlockingBackend:
    def __init__(self) -> None:
        self.started: list[InferenceSubmission] = []
        self.cancelled: list[str] = []
        self.started_event = threading.Event()
        self.release_event = threading.Event()

    def run(self, request_id, submission, emit_token, is_cancelled):
        assert request_id == "request-001"
        self.started.append(submission)
        self.started_event.set()
        self.release_event.wait(timeout=2)
        return "cancelled" if is_cancelled() else "completed"

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)
        self.release_event.set()


@pytest.fixture(scope="module")
def qualification():
    return _synthetic_qualification()


@pytest.fixture
def runtime(qualification):
    source = MutableQualificationSource(qualification)
    backend = BlockingBackend()
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "request-001",
        max_buffered_events=8,
    )
    yield source, backend, service
    backend.release_event.set()
    service.close()


def _submission(current_qualification, **changes: Any) -> InferenceSubmission:
    binding = qualification_binding(current_qualification)
    values: dict[str, Any] = {
        "prompt": "private prompt moonlight",
        "max_new_tokens": 3,
        "qualification": binding,
    }
    values.update(changes)
    return InferenceSubmission(**values)


def _clone_binding(binding: QualificationBinding, **changes: Any) -> QualificationBinding:
    document = binding.to_dict()
    document.update(changes)
    return QualificationBinding.from_dict(document)


class ASGIHarness:
    @staticmethod
    async def request(
        app: RequestGatewayASGIApplication,
        path: str,
        *,
        method: str = "GET",
        token: str | None = None,
        document: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        body = b"" if document is None else json.dumps(document).encode("utf-8")
        headers = [(b"content-type", b"application/json")]
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode("ascii")))
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": headers,
        }
        incoming = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": body, "more_body": False})
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(scope, receive, send)
        start = next(message for message in sent if message["type"] == "http.response.start")
        payload = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        response_headers = {
            key.decode("ascii").lower(): value.decode("ascii")
            for key, value in start.get("headers", [])
        }
        return start["status"], response_headers, json.loads(payload or b"{}")


def test_health_is_minimal_and_does_not_consult_qualification(runtime):
    source, _backend, service = runtime
    source.value = None
    app = RequestGatewayASGIApplication(
        service,
        authenticator=StaticBearerAuthenticator(AUTH_TOKEN),
    )

    status, headers, body = asyncio.run(ASGIHarness.request(app, "/healthz"))

    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert body == {"service": "mycelium-request-gateway", "status": "ok"}


def test_current_qualification_requires_separate_bearer_auth_and_is_safe(runtime):
    _source, _backend, service = runtime
    app = RequestGatewayASGIApplication(
        service,
        authenticator=StaticBearerAuthenticator(AUTH_TOKEN),
    )

    missing = asyncio.run(ASGIHarness.request(app, "/v1/qualification/current"))
    wrong = asyncio.run(
        ASGIHarness.request(
            app,
            "/v1/qualification/current",
            token="observatory-or-other-credential",
        )
    )
    accepted = asyncio.run(
        ASGIHarness.request(app, "/v1/qualification/current", token=AUTH_TOKEN)
    )

    assert missing[0] == 401
    assert wrong[0] == 401
    assert accepted[0] == 200
    assert missing[1]["www-authenticate"] == "Bearer"
    projection = accepted[2]
    assert projection["protocol"] == REQUEST_GATEWAY_PROTOCOL
    assert projection["route_ready"] is True
    assert projection["binding"]["qualification_id"]
    assert projection["binding"]["qualification_digest"].startswith("sha256:")
    serialized = json.dumps(projection, sort_keys=True)
    for forbidden in (
        "stage_bindings",
        "endpoint_id",
        "process_id",
        "reservation_id",
        "source_endpoint",
        "destination_endpoint",
    ):
        assert forbidden not in serialized


def test_exact_current_qualification_binding_admits(runtime, qualification):
    _source, backend, service = runtime

    request_id = service.submit(_submission(qualification))

    assert request_id == "request-001"
    assert backend.started_event.wait(timeout=1)
    assert backend.started == [_submission(qualification)]


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ({"qualification_id": "stale-qualification"}, "stale_qualification"),
        ({"deployment_epoch": 999}, "deployment_epoch_changed"),
        ({"topology_version": 999}, "path_changed"),
        ({"path_manifest_digest": "sha256:" + "1" * 64}, "path_changed"),
        ({"resolved_commit": "wrong-model-revision"}, "qualification_mismatch"),
        ({"manifest_digest": "sha256:" + "2" * 64}, "qualification_mismatch"),
        ({"stage_load_proof_digests": ("sha256:" + "3" * 64,)}, "qualification_mismatch"),
        ({"qualification_digest": "sha256:" + "4" * 64}, "qualification_mismatch"),
    ],
)
def test_stale_or_mismatched_qualification_is_rejected(
    runtime,
    qualification,
    change,
    expected_code,
):
    _source, backend, service = runtime
    bad = _clone_binding(qualification_binding(qualification), **change)

    with pytest.raises(AdmissionError) as raised:
        service.submit(_submission(qualification, qualification=bad))

    assert raised.value.code == expected_code
    assert backend.started == []


def test_dropped_route_and_revoked_readiness_are_rejected(runtime, qualification):
    source, backend, service = runtime
    source.value = None
    with pytest.raises(AdmissionError) as dropped:
        service.submit(_submission(qualification))
    assert dropped.value.code == "route_dropped"

    source.value = qualification
    submission = _submission(qualification)
    object.__setattr__(qualification, "route_ready", False)
    try:
        with pytest.raises(AdmissionError) as revoked:
            service.submit(submission)
        assert revoked.value.code == "readiness_revoked"
    finally:
        object.__setattr__(qualification, "route_ready", True)
    assert backend.started == []


def test_post_inference_uses_authenticated_service_contract(runtime, qualification):
    _source, backend, service = runtime
    app = RequestGatewayASGIApplication(
        service,
        authenticator=StaticBearerAuthenticator(AUTH_TOKEN),
    )
    document = _submission(qualification).to_dict()
    assert document["protocol"] == REQUEST_GATEWAY_PROTOCOL

    denied = asyncio.run(
        ASGIHarness.request(app, "/v1/inference", method="POST", document=document)
    )
    accepted = asyncio.run(
        ASGIHarness.request(
            app,
            "/v1/inference",
            method="POST",
            token=AUTH_TOKEN,
            document=document,
        )
    )

    assert denied[0] == 401
    assert accepted[0] == 202
    assert accepted[2] == {
        "cancel_path": "/v1/inference/request-001",
        "request_id": "request-001",
        "stream_path": "/v1/inference/request-001/events",
    }
    assert backend.started_event.wait(timeout=1)


def test_authenticator_denies_malformed_asgi_header_shapes():
    authenticator = StaticBearerAuthenticator(AUTH_TOKEN)

    for headers in (
        None,
        "not-a-header-list",
        [(b"authorization",)],
        [(b"authorization", b"Bearer " + AUTH_TOKEN.encode(), b"extra")],
        [("authorization", b"Bearer " + AUTH_TOKEN.encode())],
    ):
        assert authenticator.is_authorized({"headers": headers}) is False
