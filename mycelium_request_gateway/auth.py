"""Constant-time bearer authentication isolated from Observatory credentials."""
from __future__ import annotations

import hmac
from typing import Any, Mapping, Protocol


class Authenticator(Protocol):
    def is_authorized(self, scope: Mapping[str, Any]) -> bool: ...


class StaticBearerAuthenticator:
    """One explicitly configured request-gateway credential; deny by default."""

    def __init__(self, token: str) -> None:
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > 4_096
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
        ):
            raise ValueError("invalid_request_gateway_bearer_token")
        self._token = token.encode("ascii")

    def is_authorized(self, scope: Mapping[str, Any]) -> bool:
        headers = scope.get("headers", [])
        if not isinstance(headers, (list, tuple)):
            return False
        values: list[bytes] = []
        for item in headers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return False
            name, value = item
            if not isinstance(name, bytes) or not isinstance(value, bytes):
                return False
            if name.lower() == b"authorization":
                values.append(value)
        if len(values) != 1 or len(values[0]) > 4_103:
            return False
        scheme, separator, candidate = values[0].partition(b" ")
        return (
            separator == b" "
            and scheme.lower() == b"bearer"
            and bool(candidate)
            and hmac.compare_digest(candidate, self._token)
        )
