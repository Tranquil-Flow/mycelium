# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded canonical-JSON HTTP transport for the seed coordinator."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mycelium_invite import InviteError, verify_invite_bundle
from mycelium_membership import MembershipContractError
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import build_ed25519_verifier

from .coordinator import (
    SEED_IDENTITY_PROTOCOL,
    SEED_RECEIPT_PROTOCOL,
    SEED_SIGNED_ENVELOPE_PROTOCOL,
    SeedCoordinator,
    SeedCoordinatorError,
)


SEED_JOIN_HTTP_PROTOCOL = "mycelium.seed.join_http.v1"
SEED_MEMBER_HTTP_PROTOCOL = "mycelium.seed.member_http.v1"
SEED_HTTP_ERROR_PROTOCOL = "mycelium.seed.http_error.v1"
MAX_HTTP_FRAME_BYTES = 1024 * 1024
_BASE_STATEMENT_FIELDS = frozenset(
    {
        "protocol",
        "swarm_id",
        "seed_node_id",
        "seed_endpoint_id",
        "seed_url",
        "issued_at",
        "expires_at",
    }
)


class SeedHTTPError(RuntimeError):
    """Stable HTTP transport or remote seed error."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: Any,
        _file: Any,
        _code: Any,
        _message: Any,
        _headers: Any,
        _new_url: Any,
    ) -> None:
        return None


def _reject_constant(_value: str) -> None:
    raise SeedHTTPError("seed_http_json_invalid")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SeedHTTPError("seed_http_json_invalid")
        result[key] = value
    return result


def _canonical_json_loads(raw: bytes) -> Any:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except SeedHTTPError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SeedHTTPError("seed_http_json_invalid") from exc
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise SeedHTTPError("seed_http_json_invalid") from exc
    if canonical != raw:
        raise SeedHTTPError("seed_http_noncanonical")
    return value


def _error_status(code: str) -> int:
    if code == "seed_http_frame_too_large":
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    if code in {
        "invite_replayed",
        "seed_node_key_conflict",
        "seed_assignment_exists",
        "seed_message_replayed",
    }:
        return HTTPStatus.CONFLICT
    if code in {"seed_member_unknown", "seed_assignment_unknown"}:
        return HTTPStatus.NOT_FOUND
    if "signature" in code or "key_pin" in code:
        return HTTPStatus.UNAUTHORIZED
    return HTTPStatus.BAD_REQUEST


def _error_body(code: str) -> dict[str, Any]:
    return {
        "protocol": SEED_HTTP_ERROR_PROTOCOL,
        "error": {"code": code},
    }


class _SeedRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MyceliumSeed/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def coordinator(self) -> SeedCoordinator:
        return self.server.coordinator  # type: ignore[attr-defined, no-any-return]

    def _send(self, status: int, value: Mapping[str, Any]) -> None:
        body = canonical_json_bytes(dict(value))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code: str, *, status: int | None = None) -> None:
        self._send(_error_status(code) if status is None else status, _error_body(code))

    def _read_body(self) -> Mapping[str, Any]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise SeedHTTPError("seed_http_transfer_encoding_unsupported")
        if self.headers.get_content_type() != "application/json":
            raise SeedHTTPError("seed_http_content_type_invalid")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise SeedHTTPError("seed_http_content_length_required")
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise SeedHTTPError("seed_http_content_length_invalid") from exc
        if content_length < 0:
            raise SeedHTTPError("seed_http_content_length_invalid")
        if content_length > MAX_HTTP_FRAME_BYTES:
            self.close_connection = True
            raise SeedHTTPError("seed_http_frame_too_large")
        raw = self.rfile.read(content_length)
        if len(raw) != content_length:
            raise SeedHTTPError("seed_http_body_truncated")
        value = _canonical_json_loads(raw)
        if not isinstance(value, Mapping):
            raise SeedHTTPError("seed_http_body_invalid")
        return value

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/seed/identity":
            self._fail("seed_http_route_unknown", status=HTTPStatus.NOT_FOUND)
            return
        try:
            self._send(HTTPStatus.OK, self.coordinator.identity_envelope())
        except (SeedCoordinatorError, ValueError) as exc:
            code = getattr(exc, "code", "seed_http_request_invalid")
            self._fail(code)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            body = self._read_body()
            if self.path == "/seed/join":
                if set(body) != {"protocol", "invite_token", "join_envelope"}:
                    raise SeedHTTPError("seed_http_body_invalid")
                if body.get("protocol") != SEED_JOIN_HTTP_PROTOCOL:
                    raise SeedHTTPError("seed_http_protocol_invalid")
                acceptance = self.coordinator.accept_join(
                    invite_token=body["invite_token"],
                    join_envelope=body["join_envelope"],
                )
                self._send(HTTPStatus.OK, acceptance)
                return
            if self.path == "/seed/message":
                if set(body) != {"protocol", "envelope"}:
                    raise SeedHTTPError("seed_http_body_invalid")
                if body.get("protocol") != SEED_MEMBER_HTTP_PROTOCOL:
                    raise SeedHTTPError("seed_http_protocol_invalid")
                envelope = body["envelope"]
                try:
                    expected_protocol = envelope["message"]["protocol"]
                except (KeyError, TypeError) as exc:
                    raise SeedHTTPError("seed_http_body_invalid") from exc
                message = self.coordinator.receive_member_message(
                    envelope,
                    expected_protocol=expected_protocol,
                )
                self._send(
                    HTTPStatus.OK,
                    self.coordinator.receipt_envelope(message["message_id"]),
                )
                return
            raise SeedHTTPError("seed_http_route_unknown", status=HTTPStatus.NOT_FOUND)
        except (SeedHTTPError, InviteError, MembershipContractError, SeedCoordinatorError, ValueError) as exc:
            code = getattr(exc, "code", "seed_http_request_invalid")
            status = exc.status if isinstance(exc, SeedHTTPError) else None
            self._fail(code, status=status)
        except Exception:
            self._fail("seed_http_internal_error", status=HTTPStatus.INTERNAL_SERVER_ERROR)


class SeedHTTPServer:
    """Threaded local seed HTTP server with explicit lifecycle."""

    def __init__(
        self,
        coordinator: SeedCoordinator,
        *,
        host: str,
        port: int,
        advertised_url: str | None = None,
    ) -> None:
        if host in {"0.0.0.0", "::"} and advertised_url is None:
            raise ValueError("advertised_url is required for wildcard binds")
        self._server = ThreadingHTTPServer((host, port), _SeedRequestHandler)
        self._server.daemon_threads = True
        self._server.coordinator = coordinator  # type: ignore[attr-defined]
        bound_host, bound_port = self._server.server_address[:2]
        self.base_url = f"http://{bound_host}:{bound_port}"
        try:
            coordinator.bind_seed_url(
                self.base_url if advertised_url is None else advertised_url
            )
        except Exception:
            self._server.server_close()
            raise
        self._thread: threading.Thread | None = None

    def start(self) -> "SeedHTTPServer":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mycelium-seed-http",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> "SeedHTTPServer":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


class SeedHTTPClient:
    """Pinned-key client for one seed HTTP endpoint."""

    def __init__(
        self,
        *,
        seed_url: str,
        swarm_id: str,
        seed_key_digest: str,
        seed_key_records: list[Mapping[str, Any]],
        timeout: float = 10.0,
    ) -> None:
        parsed = urlsplit(seed_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("seed_url is invalid")
        if parsed.query or parsed.fragment:
            raise ValueError("seed_url is invalid")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("seed_url is invalid")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("timeout is invalid")
        if len(seed_key_records) != 1:
            raise ValueError("seed_key_records is invalid")
        record = dict(seed_key_records[0])
        if record.get("verification_key_digest") != seed_key_digest:
            raise ValueError("seed key pin mismatch")
        self.seed_url = seed_url.rstrip("/")
        self.swarm_id = swarm_id
        self.seed_key_digest = seed_key_digest
        self.seed_key_records = [record]
        self.timeout = float(timeout)
        self._opener = build_opener(_NoRedirect())

    @classmethod
    def from_invite_bundle(
        cls,
        bundle: Mapping[str, Any],
        *,
        now: float,
        timeout: float = 10.0,
    ) -> "SeedHTTPClient":
        verified = verify_invite_bundle(bundle, now=now)
        return cls(
            seed_url=verified["payload"]["seed_url"],
            swarm_id=verified["payload"]["swarm_id"],
            seed_key_digest=verified["seed_key_digest"],
            seed_key_records=list(verified["seed_key_records"]),
            timeout=timeout,
        )

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        raw = None if body is None else canonical_json_bytes(dict(body))
        request = Request(
            self.seed_url + path,
            data=raw,
            method=method,
            headers={} if raw is None else {"Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                response_body = response.read(MAX_HTTP_FRAME_BYTES + 1)
                status = response.status
        except HTTPError as exc:
            response_body = exc.read(MAX_HTTP_FRAME_BYTES + 1)
            try:
                error = _canonical_json_loads(response_body)
                code = error["error"]["code"]
            except Exception:
                code = "seed_http_remote_error"
            raise SeedHTTPError(code, status=exc.code) from exc
        except (OSError, URLError) as exc:
            raise SeedHTTPError("seed_http_unreachable") from exc
        if len(response_body) > MAX_HTTP_FRAME_BYTES:
            raise SeedHTTPError("seed_http_response_too_large", status=status)
        value = _canonical_json_loads(response_body)
        if not isinstance(value, Mapping):
            raise SeedHTTPError("seed_http_response_invalid", status=status)
        return value

    def _verify_seed_envelope(
        self,
        envelope: Mapping[str, Any],
        *,
        expected_protocol: str,
        now: float,
    ) -> dict[str, Any]:
        if set(envelope) != {"protocol", "statement", "signature", "verification_key"}:
            raise SeedHTTPError("seed_http_seed_envelope_invalid")
        if envelope.get("protocol") != SEED_SIGNED_ENVELOPE_PROTOCOL:
            raise SeedHTTPError("seed_http_seed_envelope_invalid")
        statement = envelope.get("statement")
        signature = envelope.get("signature")
        record = envelope.get("verification_key")
        if not isinstance(statement, Mapping) or not isinstance(signature, Mapping) or not isinstance(record, Mapping):
            raise SeedHTTPError("seed_http_seed_envelope_invalid")
        expected_fields = set(_BASE_STATEMENT_FIELDS)
        if expected_protocol == SEED_RECEIPT_PROTOCOL:
            expected_fields.add("accepted_message_id")
        if set(statement) != expected_fields or statement.get("protocol") != expected_protocol:
            raise SeedHTTPError("seed_http_seed_envelope_invalid")
        if (
            record.get("verification_key_digest") != self.seed_key_digest
            or statement.get("swarm_id") != self.swarm_id
            or statement.get("seed_url") != self.seed_url
            or signature.get("signer_endpoint_id") != statement.get("seed_endpoint_id")
        ):
            raise SeedHTTPError("seed_http_seed_pin_mismatch")
        try:
            verify = build_ed25519_verifier([dict(record)])
        except Exception as exc:
            raise SeedHTTPError("seed_http_seed_key_invalid") from exc
        if not verify(canonical_json_bytes(dict(statement)), dict(signature)):
            raise SeedHTTPError("seed_http_seed_signature_invalid")
        issued = statement.get("issued_at")
        expires = statement.get("expires_at")
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not isinstance(issued, (int, float))
            or isinstance(issued, bool)
            or not isinstance(expires, (int, float))
            or isinstance(expires, bool)
            or not math.isfinite(float(now))
            or not math.isfinite(float(issued))
            or not math.isfinite(float(expires))
            or float(now) < float(issued)
            or float(now) > float(expires)
        ):
            raise SeedHTTPError("seed_http_seed_time_invalid")
        return dict(statement)

    def identity(self, *, now: float) -> dict[str, Any]:
        envelope = self._request("GET", "/seed/identity")
        return self._verify_seed_envelope(
            envelope,
            expected_protocol=SEED_IDENTITY_PROTOCOL,
            now=now,
        )

    def join(
        self,
        *,
        invite_token: str,
        join_envelope: Mapping[str, Any],
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/seed/join",
            {
                "protocol": SEED_JOIN_HTTP_PROTOCOL,
                "invite_token": invite_token,
                "join_envelope": dict(join_envelope),
            },
        )
        return dict(response)

    def send_member_message(
        self,
        envelope: Mapping[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        message = envelope.get("message")
        if not isinstance(message, Mapping):
            raise SeedHTTPError("seed_http_member_envelope_invalid")
        expected_message_id = message.get("message_id")
        if not isinstance(expected_message_id, str) or not expected_message_id:
            raise SeedHTTPError("seed_http_member_envelope_invalid")
        response = self._request(
            "POST",
            "/seed/message",
            {
                "protocol": SEED_MEMBER_HTTP_PROTOCOL,
                "envelope": dict(envelope),
            },
        )
        receipt = self._verify_seed_envelope(
            response,
            expected_protocol=SEED_RECEIPT_PROTOCOL,
            now=now,
        )
        if receipt["accepted_message_id"] != expected_message_id:
            raise SeedHTTPError("seed_http_receipt_mismatch")
        return receipt
