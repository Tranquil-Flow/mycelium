"""Strict host client for the isolated Pixel exact-stage worker."""

from __future__ import annotations

import ipaddress
import math
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from .pixel_stage import (
    MAX_BODY_BYTES,
    MAX_HIDDEN_SIZE,
    MAX_SEQUENCE_LENGTH,
    STAGE_REQUEST_PROTOCOL,
    STAGE_RESPONSE_PROTOCOL,
    TOKEN_HEADER,
    _canonical,
    _digest,
    _strict_json_bytes,
)

_TAILSCALE_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")
_IDENTITY_FIELDS = frozenset(
    {
        "run_id",
        "deployment_id",
        "assignment_id",
        "stage_id",
        "pack_digest",
        "parent_assignment_digest",
        "parent_load_proof_digest",
        "worker_source_digest",
        "boot_id",
    }
)


class PixelStageClientError(RuntimeError):
    """Sanitized transport or protocol error from the physical Pixel worker."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _base_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) <= 0x20 for c in value):
        raise PixelStageClientError("pixel_url_invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        address = ipaddress.IPv4Address(parsed.hostname or "")
        port = parsed.port
    except (ValueError, ipaddress.AddressValueError):
        raise PixelStageClientError("pixel_url_invalid") from None
    if (
        parsed.scheme != "http"
        or address not in _TAILSCALE_NETWORK
        or port != 9018
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PixelStageClientError("pixel_url_invalid")
    return f"http://{address}:9018"


def _identity(value: Mapping[str, Any]) -> dict[str, str]:
    if set(value) != set(_IDENTITY_FIELDS):
        raise PixelStageClientError("pixel_identity_invalid")
    result: dict[str, str] = {}
    for field in _IDENTITY_FIELDS:
        item = value.get(field)
        if not isinstance(item, str) or not item:
            raise PixelStageClientError("pixel_identity_invalid")
        if field.endswith("digest") and (
            not item.startswith("sha256:")
            or len(item) != 71
            or any(character not in "0123456789abcdef" for character in item[7:])
        ):
            raise PixelStageClientError("pixel_identity_invalid")
        result[field] = item
    return result


class PixelStageClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        expected_identity: Mapping[str, Any],
        hidden_size: int,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = _base_url(base_url)
        if (
            not isinstance(token, str)
            or len(token) < 32
            or "\n" in token
            or "\r" in token
        ):
            raise PixelStageClientError("pixel_token_invalid")
        if isinstance(hidden_size, bool) or not isinstance(hidden_size, int):
            raise PixelStageClientError("pixel_hidden_size_invalid")
        if not 1 <= hidden_size <= MAX_HIDDEN_SIZE:
            raise PixelStageClientError("pixel_hidden_size_invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise PixelStageClientError("pixel_timeout_invalid")
        self._timeout = float(timeout)
        if not math.isfinite(self._timeout) or not 0.1 <= self._timeout <= 60.0:
            raise PixelStageClientError("pixel_timeout_invalid")
        self._identity = _identity(expected_identity)
        self._hidden_size = hidden_size
        self._runtime_instance_id: str | None = None
        self._request_count: int | None = None
        self.__token = token
        self._open = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        ).open

    def __repr__(self) -> str:
        return (
            f"PixelStageClient(base_url={self.base_url!r}, timeout={self._timeout!r})"
        )

    def _verify_identity(self, value: Mapping[str, Any]) -> bool:
        return all(
            value.get(field) == expected for field, expected in self._identity.items()
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        authenticated: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        raw = None if payload is None else _canonical(dict(payload))
        headers: dict[str, str] = {}
        if raw is not None:
            headers["content-type"] = "application/json"
        if authenticated:
            headers[TOKEN_HEADER] = self.__token
        request = urllib.request.Request(
            self.base_url + path, data=raw, headers=headers, method=method
        )
        response: Any = None
        try:
            response = self._open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            response = error
        except Exception:
            raise PixelStageClientError("pixel_transport_failed") from None
        try:
            length_values = response.headers.get_all("content-length", [])
            content_type_values = response.headers.get_all("content-type", [])
            if len(length_values) != 1 or len(content_type_values) != 1:
                raise PixelStageClientError("pixel_response_headers_invalid")
            if (
                content_type_values[0].split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise PixelStageClientError("pixel_response_headers_invalid")
            try:
                length = int(length_values[0])
            except ValueError:
                raise PixelStageClientError("pixel_response_length_invalid") from None
            if not 0 <= length <= MAX_BODY_BYTES:
                raise PixelStageClientError("pixel_response_length_invalid")
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) != length:
                raise PixelStageClientError("pixel_response_length_invalid")
            try:
                value = _strict_json_bytes(body)
            except Exception:
                raise PixelStageClientError("pixel_response_json_invalid") from None
            if not isinstance(value, dict):
                raise PixelStageClientError("pixel_response_json_invalid")
            return int(response.status), value
        finally:
            try:
                response.close()
            except Exception:
                pass

    def health(self) -> dict[str, Any]:
        status, value = self._request("GET", "/health")
        required = {
            "status",
            "route_ready",
            "protocol",
            *_IDENTITY_FIELDS,
            "runtime_instance_id",
            "request_count",
        }
        runtime_instance_id = value.get("runtime_instance_id")
        request_count = value.get("request_count")
        if (
            status != 200
            or set(value) != required
            or value.get("status") != "ok"
            or value.get("route_ready") is not False
            or value.get("protocol") != STAGE_RESPONSE_PROTOCOL
            or not self._verify_identity(value)
            or not isinstance(runtime_instance_id, str)
            or not runtime_instance_id
            or isinstance(request_count, bool)
            or not isinstance(request_count, int)
            or request_count < 0
        ):
            raise PixelStageClientError("pixel_health_invalid")
        if self._runtime_instance_id not in (None, runtime_instance_id):
            raise PixelStageClientError("pixel_runtime_identity_changed")
        self._runtime_instance_id = runtime_instance_id
        self._request_count = request_count
        return value

    def execute(
        self,
        *,
        request_id: str,
        assignment_id: str,
        stage_id: str,
        hidden: Sequence[Sequence[float]],
    ) -> dict[str, Any]:
        if self._runtime_instance_id is None or self._request_count is None:
            raise PixelStageClientError("pixel_health_required")
        if (
            assignment_id != self._identity["assignment_id"]
            or stage_id != self._identity["stage_id"]
        ):
            raise PixelStageClientError("pixel_request_identity_invalid")
        normalized = [[float(item) for item in row] for row in hidden]
        if (
            not 1 <= len(normalized) <= MAX_SEQUENCE_LENGTH
            or any(len(row) != self._hidden_size for row in normalized)
            or any(not math.isfinite(value) for row in normalized for value in row)
        ):
            raise PixelStageClientError("pixel_input_shape_invalid")
        payload = {
            "protocol": STAGE_REQUEST_PROTOCOL,
            "request_id": request_id,
            "assignment_id": assignment_id,
            "stage_id": stage_id,
            "hidden": normalized,
            "input_digest": _digest(normalized),
        }
        status, value = self._request("POST", "/execute", payload=payload)
        if status != 200:
            code = value.get("error")
            if not isinstance(code, str) or not code:
                code = "pixel_execute_rejected"
            raise PixelStageClientError(code)
        required = {
            "protocol",
            "route_ready",
            *_IDENTITY_FIELDS,
            "request_id",
            "runtime_instance_id",
            "request_count",
            "output",
            "output_digest",
            "evidence_digest",
            "duration_ms",
        }
        output = value.get("output")
        request_count = value.get("request_count")
        if (
            set(value) != required
            or value.get("protocol") != STAGE_RESPONSE_PROTOCOL
            or value.get("route_ready") is not False
            or not self._verify_identity(value)
            or value.get("request_id") != request_id
            or value.get("runtime_instance_id") != self._runtime_instance_id
            or isinstance(request_count, bool)
            or request_count != self._request_count + 1
            or not isinstance(output, list)
            or len(output) != len(normalized)
            or any(
                not isinstance(row, list) or len(row) != self._hidden_size
                for row in output
            )
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for row in output
                for item in row
            )
            or value.get("output_digest") != _digest(output)
        ):
            raise PixelStageClientError("pixel_execute_response_invalid")
        self._request_count = request_count
        return value

    def shutdown(self) -> dict[str, Any]:
        status, value = self._request("POST", "/shutdown", payload={})
        if status != 200 or value != {"route_ready": False, "status": "stopping"}:
            raise PixelStageClientError("pixel_shutdown_invalid")
        return value
