"""Minimal argv-only controller for the pre-existing Termux command bridge."""

from __future__ import annotations

import ipaddress
import json
import math
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

TERMUX_TOKEN_HEADER = "x-termux-bridge-token"
MAX_RESPONSE_BYTES = 1024 * 1024
_TAILSCALE_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")


class TermuxBridgeError(RuntimeError):
    """Sanitized bridge configuration, transport, or protocol failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        address = ipaddress.IPv4Address(parsed.hostname or "")
        port = parsed.port
    except (TypeError, ValueError, ipaddress.AddressValueError):
        raise TermuxBridgeError("termux_bridge_url_invalid") from None
    if (
        parsed.scheme != "http"
        or address not in _TAILSCALE_NETWORK
        or port != 9020
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise TermuxBridgeError("termux_bridge_url_invalid")
    return f"http://{address}:9020"


class TermuxBridgeClient:
    def __init__(self, base_url: str, *, token: str, timeout: float = 20.0) -> None:
        self.base_url = _url(base_url)
        if not isinstance(token, str) or len(token) < 32:
            raise TermuxBridgeError("termux_bridge_token_invalid")
        self.__token = token
        self._timeout = float(timeout)
        if not math.isfinite(self._timeout) or not 0.1 <= self._timeout <= 65.0:
            raise TermuxBridgeError("termux_bridge_timeout_invalid")
        self._open = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        ).open

    def __repr__(self) -> str:
        return (
            f"TermuxBridgeClient(base_url={self.base_url!r}, timeout={self._timeout!r})"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        authenticated: bool = False,
        expected_status: int = 200,
    ) -> dict[str, Any]:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            try:
                data = json.dumps(
                    dict(payload),
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            except (TypeError, ValueError, UnicodeError):
                raise TermuxBridgeError("termux_bridge_payload_invalid") from None
            headers["content-type"] = "application/json"
        if authenticated:
            headers[TERMUX_TOKEN_HEADER] = self.__token
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        response: Any = None
        try:
            response = self._open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            response = error
        except Exception:
            raise TermuxBridgeError("termux_bridge_transport_failed") from None
        try:
            raw_length = response.headers.get("content-length")
            try:
                length = int(raw_length) if raw_length is not None else -1
            except ValueError:
                raise TermuxBridgeError("termux_bridge_response_invalid") from None
            if not 0 <= length <= MAX_RESPONSE_BYTES:
                raise TermuxBridgeError("termux_bridge_response_invalid")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) != length:
                raise TermuxBridgeError("termux_bridge_response_invalid")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError):
                raise TermuxBridgeError("termux_bridge_response_invalid") from None
            if int(response.status) != expected_status or not isinstance(value, dict):
                raise TermuxBridgeError("termux_bridge_response_invalid")
            return value
        finally:
            try:
                response.close()
            except Exception:
                pass

    def health(self) -> dict[str, Any]:
        value = self._request("GET", "/health")
        if (
            value.get("status") != "ok"
            or value.get("allow_shell") is not False
            or value.get("claim") != "authenticated argv command bridge for Termux"
        ):
            raise TermuxBridgeError("termux_bridge_health_invalid")
        return value

    def unauthenticated_rejected(self) -> bool:
        value = self._request(
            "POST",
            "/run",
            payload={
                "argv": ["true"],
                "timeout_seconds": 1.0,
                "detach": False,
            },
            expected_status=401,
        )
        error = value.get("error")
        if isinstance(error, str):
            accepted = error in ("unauthorized", "invalid token")
        elif isinstance(error, dict):
            accepted = (
                error.get("type") == "unauthorized"
                and error.get("message") == "missing or invalid token"
                and set(error) == {"message", "type"}
            )
        else:
            accepted = False
        if not accepted:
            raise TermuxBridgeError("termux_bridge_unauthenticated_check_invalid")
        return True

    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_seconds: float = 10.0,
        detach: bool = False,
    ) -> dict[str, Any]:
        if (
            isinstance(argv, (str, bytes, bytearray))
            or not isinstance(argv, (list, tuple))
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
        ):
            raise TermuxBridgeError("termux_bridge_argv_invalid")
        payload: dict[str, Any] = {
            "argv": list(argv),
            "timeout_seconds": float(timeout_seconds),
            "detach": detach,
        }
        if cwd is not None:
            payload["cwd"] = cwd
        value = self._request("POST", "/run", payload=payload, authenticated=True)
        if value.get("shell") is not False:
            raise TermuxBridgeError("termux_bridge_shell_boundary_invalid")
        if detach:
            if value.get("detached") is not True or not isinstance(
                value.get("pid"), int
            ):
                raise TermuxBridgeError("termux_bridge_detach_invalid")
        else:
            if (
                isinstance(value.get("exit_code"), bool)
                or not isinstance(value.get("exit_code"), int)
                or not isinstance(value.get("stdout"), str)
                or not isinstance(value.get("stderr"), str)
            ):
                raise TermuxBridgeError("termux_bridge_run_invalid")
        return value
