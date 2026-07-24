# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded canonical-JSON HTTP transport for the seed coordinator."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import re
import socket
import threading
import time
from types import MappingProxyType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mycelium_invite import InviteError, verify_invite_bundle
from mycelium_membership import (
    HEARTBEAT_PROTOCOL,
    LEASE_RENEWAL_PROTOCOL,
    MembershipContractError,
    verify_membership_message,
)
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
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_REMOTE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SERVER_CLOSE_SECONDS = 5.0
JOIN_ROUTE_ERROR_STATUSES: Mapping[str, int] = MappingProxyType(
    {
        "invite_expired": HTTPStatus.BAD_REQUEST,
        "invite_malformed": HTTPStatus.BAD_REQUEST,
        "invite_protocol_invalid": HTTPStatus.BAD_REQUEST,
        "invite_replayed": HTTPStatus.CONFLICT,
        "invite_signature_invalid": HTTPStatus.UNAUTHORIZED,
        "join_request_protocol_required": HTTPStatus.BAD_REQUEST,
        "membership_endpoint_addr_invalid": HTTPStatus.BAD_REQUEST,
        "membership_endpoint_id_mismatch": HTTPStatus.BAD_REQUEST,
        "membership_envelope_invalid": HTTPStatus.BAD_REQUEST,
        "membership_field_unusable": HTTPStatus.BAD_REQUEST,
        "membership_fields_invalid": HTTPStatus.BAD_REQUEST,
        "membership_generation_invalid": HTTPStatus.BAD_REQUEST,
        "membership_identifier_invalid": HTTPStatus.BAD_REQUEST,
        "membership_join_generation_invalid": HTTPStatus.BAD_REQUEST,
        "membership_key_pin_invalid": HTTPStatus.UNAUTHORIZED,
        "membership_message_expired": HTTPStatus.BAD_REQUEST,
        "membership_message_from_future": HTTPStatus.BAD_REQUEST,
        "membership_message_invalid": HTTPStatus.BAD_REQUEST,
        "membership_peer_class_invalid": HTTPStatus.BAD_REQUEST,
        "membership_protocol_invalid": HTTPStatus.BAD_REQUEST,
        "membership_runtime_capability_invalid": HTTPStatus.BAD_REQUEST,
        "membership_runtime_capability_mismatch": HTTPStatus.BAD_REQUEST,
        "membership_signature_invalid": HTTPStatus.UNAUTHORIZED,
        "membership_text_invalid": HTTPStatus.BAD_REQUEST,
        "membership_time_invalid": HTTPStatus.BAD_REQUEST,
        "membership_ttl_invalid": HTTPStatus.BAD_REQUEST,
        "membership_verifier_invalid": HTTPStatus.BAD_REQUEST,
        "seed_join_key_invalid": HTTPStatus.BAD_REQUEST,
        "seed_join_mismatch": HTTPStatus.BAD_REQUEST,
        "seed_join_retry_mismatch": HTTPStatus.BAD_REQUEST,
        "seed_member_identity_reused": HTTPStatus.BAD_REQUEST,
        "seed_node_endpoint_conflict": HTTPStatus.BAD_REQUEST,
        "seed_node_key_conflict": HTTPStatus.CONFLICT,
    }
)


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _validate_bind_address(host: str, port: int) -> tuple[str, int]:
    if (
        not isinstance(host, str)
        or not host
        or host != host.strip()
        or any(character.isspace() for character in host)
        or any(character in host for character in "/\\@?#")
    ):
        raise ValueError("host is invalid")
    if host != "localhost":
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("host is invalid") from exc
    if not isinstance(port, int) or isinstance(port, bool) or port < 0 or port > 65535:
        raise ValueError("port is invalid")
    return host, port


def _validate_endpoint_url(
    value: str,
    *,
    expected_scheme: str | None = None,
    expected_host: str | None = None,
    expected_port: int | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
        or "\\" in value
    ):
        raise ValueError("URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or "%" in parsed.netloc
    ):
        raise ValueError("URL is invalid")
    host = parsed.hostname
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("URL is invalid") from exc
    try:
        ip_value = ipaddress.ip_address(host)
    except ValueError:
        labels = host.rstrip(".").split(".")
        if (
            host.endswith(".")
            or not labels
            or any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels)
        ):
            raise ValueError("URL is invalid")
        canonical_host = host.lower()
        bracketed_host = canonical_host
    else:
        canonical_host = ip_value.compressed
        bracketed_host = (
            f"[{canonical_host}]" if ip_value.version == 6 else canonical_host
        )
    effective_port = port
    if effective_port is None:
        effective_port = 80 if parsed.scheme == "http" else 443
    canonical_netloc = bracketed_host
    if port is not None:
        canonical_netloc = f"{canonical_netloc}:{port}"
    if parsed.netloc.lower() != canonical_netloc.lower():
        raise ValueError("URL is invalid")
    if expected_scheme is not None and parsed.scheme != expected_scheme:
        raise ValueError("URL is invalid")
    if expected_host is not None:
        try:
            expected_host = ipaddress.ip_address(expected_host).compressed
        except ValueError:
            expected_host = expected_host.lower()
        if canonical_host != expected_host:
            raise ValueError("URL is invalid")
    if expected_port is not None and effective_port != expected_port:
        raise ValueError("URL is invalid")
    canonical = f"{parsed.scheme}://{canonical_netloc}"
    if value != canonical:
        raise ValueError("URL is invalid")
    return canonical


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
    join_status = JOIN_ROUTE_ERROR_STATUSES.get(code)
    if join_status is not None:
        return join_status
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


def _authoritative_remote_error_code(raw: bytes) -> str | None:
    if not raw or len(raw) > MAX_HTTP_FRAME_BYTES:
        return None
    try:
        value = _canonical_json_loads(raw)
    except SeedHTTPError:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"protocol", "error"}
        or value.get("protocol") != SEED_HTTP_ERROR_PROTOCOL
    ):
        return None
    error = value.get("error")
    if not isinstance(error, Mapping) or set(error) != {"code"}:
        return None
    code = error.get("code")
    if not isinstance(code, str) or _REMOTE_ERROR_CODE_RE.fullmatch(code) is None:
        return None
    return code


def _has_exact_json_content_type(headers: Any) -> bool:
    try:
        values = headers.get_all("Content-Type")
    except (AttributeError, TypeError, ValueError):
        return False
    return values == ["application/json"]


class _SeedRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MyceliumSeed/1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(2.0)

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
        except Exception:
            self._fail(
                "seed_http_internal_error", status=HTTPStatus.INTERNAL_SERVER_ERROR
            )

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
                if expected_protocol == HEARTBEAT_PROTOCOL:
                    response = self.coordinator.lease_renewal(
                        node_id=message["sender_node_id"],
                        heartbeat_message_id=message["message_id"],
                    )
                else:
                    response = self.coordinator.receipt_envelope(message["message_id"])
                self._send(HTTPStatus.OK, response)
                return
            raise SeedHTTPError("seed_http_route_unknown", status=HTTPStatus.NOT_FOUND)
        except (
            SeedHTTPError,
            InviteError,
            MembershipContractError,
            SeedCoordinatorError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "seed_http_request_invalid")
            status = exc.status if isinstance(exc, SeedHTTPError) else None
            self._fail(code, status=status)
        except Exception:
            self._fail(
                "seed_http_internal_error", status=HTTPStatus.INTERNAL_SERVER_ERROR
            )


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
        host, requested_port = _validate_bind_address(host, port)
        if host in {"0.0.0.0", "::"} and advertised_url is None:
            raise ValueError("advertised_url is required for wildcard binds")
        if advertised_url is not None:
            _validate_endpoint_url(
                advertised_url,
                expected_scheme="http",
                expected_host=None if host in {"0.0.0.0", "::"} else host,
                expected_port=None if requested_port == 0 else requested_port,
            )
        server_class = (
            _IPv6ThreadingHTTPServer
            if host != "localhost" and ipaddress.ip_address(host).version == 6
            else ThreadingHTTPServer
        )
        self._server = server_class(
            (host, requested_port),
            _SeedRequestHandler,
        )
        self._server.daemon_threads = False
        self._server.block_on_close = True
        self._server.timeout = 0.05
        self._server.coordinator = coordinator  # type: ignore[attr-defined]
        self._server.handle_error = lambda *_args: None
        bound_host, bound_port = self._server.server_address[:2]
        published_host = {
            "0.0.0.0": "127.0.0.1",
            "::": "::1",
        }.get(str(bound_host), str(bound_host))
        formatted_host = (
            f"[{published_host}]" if ":" in published_host else published_host
        )
        self.base_url = f"http://{formatted_host}:{bound_port}"
        try:
            bound_url = (
                self.base_url
                if advertised_url is None
                else _validate_endpoint_url(
                    advertised_url,
                    expected_scheme="http",
                    expected_host=(
                        None if host in {"0.0.0.0", "::"} else str(bound_host)
                    ),
                    expected_port=(
                        None if requested_port == 0 else int(bound_port)
                    ),
                )
            )
            coordinator.bind_seed_url(bound_url)
        except Exception:
            self._server.server_close()
            raise
        self._thread: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._started = threading.Event()
        self._serve_failed = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        self._close_requested = False
        self._server_closed = False
        self._request_threads: tuple[Any, ...] = ()

    def _serve(self) -> None:
        self._started.set()
        try:
            while not self._stop.is_set():
                self._server.handle_request()
        except Exception:
            self._serve_failed.set()

    def start(self) -> "SeedHTTPServer":
        with self._state_lock:
            if self._closed or self._close_requested:
                raise RuntimeError("seed HTTP server is closed")
            if self._thread is not None:
                return self
            thread = threading.Thread(
                target=self._serve,
                name="mycelium-seed-http",
                daemon=False,
            )
            try:
                thread.start()
            except Exception:
                self._close_socket()
                raise
            self._thread = thread
        if (
            not self._started.wait(1.0)
            or self._serve_failed.is_set()
            or not thread.is_alive()
        ):
            self.close()
            raise RuntimeError("seed HTTP server failed to start")
        return self

    def _close_socket(self) -> None:
        if self._server_closed:
            return
        try:
            self._server.server_close()
        except Exception:
            raise RuntimeError("seed HTTP server cleanup failed") from None
        self._server_closed = True

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._close_requested = True
            thread = self._thread
            self._stop.set()
            deadline = time.monotonic() + _SERVER_CLOSE_SECONDS
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            try:
                observed_threads = tuple(getattr(self._server, "_threads", ()))
            except TypeError:
                observed_threads = ()
            known_threads = {
                id(request_thread): request_thread
                for request_thread in self._request_threads
            }
            known_threads.update(
                {
                    id(request_thread): request_thread
                    for request_thread in observed_threads
                }
            )
            request_threads = tuple(known_threads.values())
            self._request_threads = request_threads
            current = threading.current_thread()
            for request_thread in request_threads:
                if request_thread is current:
                    continue
                request_thread.join(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            self._server.block_on_close = False
            self._close_socket()
            if (
                thread is not None
                and thread.is_alive()
                or any(
                    request_thread.is_alive()
                    for request_thread in request_threads
                )
            ):
                raise RuntimeError("seed HTTP server cleanup failed")
            self._thread = None
            self._request_threads = ()
            self._closed = True

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
        try:
            canonical_seed_url = _validate_endpoint_url(seed_url)
        except ValueError as exc:
            raise ValueError("seed_url is invalid") from exc
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("timeout is invalid")
        if len(seed_key_records) != 1:
            raise ValueError("seed_key_records is invalid")
        record = dict(seed_key_records[0])
        if record.get("verification_key_digest") != seed_key_digest:
            raise ValueError("seed key pin mismatch")
        self.seed_url = canonical_seed_url
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
                response_headers = response.headers
        except HTTPError as exc:
            response_body = exc.read(MAX_HTTP_FRAME_BYTES + 1)
            code = (
                _authoritative_remote_error_code(response_body)
                if _has_exact_json_content_type(exc.headers)
                else None
            ) or "seed_http_remote_error"
            raise SeedHTTPError(code, status=exc.code) from exc
        except (OSError, URLError) as exc:
            raise SeedHTTPError("seed_http_unreachable") from exc
        if len(response_body) > MAX_HTTP_FRAME_BYTES:
            raise SeedHTTPError("seed_http_response_too_large", status=status)
        if not _has_exact_json_content_type(response_headers):
            raise SeedHTTPError("seed_http_remote_error", status=status)
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
        if (
            not isinstance(statement, Mapping)
            or not isinstance(signature, Mapping)
            or not isinstance(record, Mapping)
        ):
            raise SeedHTTPError("seed_http_seed_envelope_invalid")
        expected_fields = set(_BASE_STATEMENT_FIELDS)
        if expected_protocol == SEED_RECEIPT_PROTOCOL:
            expected_fields.add("accepted_message_id")
        if (
            set(statement) != expected_fields
            or statement.get("protocol") != expected_protocol
        ):
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
        if message.get("protocol") == HEARTBEAT_PROTOCOL:
            try:
                renewal = verify_membership_message(
                    response,
                    now=now,
                    expected_key_digest=self.seed_key_digest,
                    expected_protocol=LEASE_RENEWAL_PROTOCOL,
                    expected_swarm_id=self.swarm_id,
                    expected_sender_node_id=message.get("recipient_node_id"),
                    expected_recipient_node_id=message.get("sender_node_id"),
                )
            except MembershipContractError as exc:
                raise SeedHTTPError(exc.code) from exc
            if (
                renewal["heartbeat_message_id"] != expected_message_id
                or renewal["member_incarnation"] != message.get("incarnation")
                or renewal["membership_generation"] != message.get("generation")
            ):
                raise SeedHTTPError("seed_http_lease_renewal_mismatch")
            return dict(response)
        receipt = self._verify_seed_envelope(
            response,
            expected_protocol=SEED_RECEIPT_PROTOCOL,
            now=now,
        )
        if receipt["accepted_message_id"] != expected_message_id:
            raise SeedHTTPError("seed_http_receipt_mismatch")
        return receipt
