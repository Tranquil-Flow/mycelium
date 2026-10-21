# SPDX-License-Identifier: AGPL-3.0-only
"""A6 grant-gated ASGI receiver for a direct local socket peer.

The ASGI server must supply the original, unmodified TCP peer in scope['client']
and its own loopback listening address in scope['server']. Never mount this on
an origin/proxy or behind peer-rewriting middleware. Construct it either in an
explicit test composition (``test_only=True``) or, for the ordinary product
serve process, with the exact fleet grant (``product_grant``) behind the
direct-socket listener in ``mycelium_a6_product_fault_control``. Either way it
only dispatches to injected FaultSeam instances. It does not start a listener,
select cases, or manufacture physical evidence.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from typing import Any, Callable, Mapping

from mycelium_a6_fault_control import (
    ControlledFaultAuthority, ControlledFaultMechanism, EXACT_FLEET_GRANT,
    EXECUTE_SOURCE_FAILURE_ACTION, FaultRejected, FaultSeam, INJECTION_POINTS,
)

_GRANT = b"x-mycelium-a6-fleet-grant"
_ALLOWED_HEADERS = frozenset({
    b"host", b"accept", b"content-type", b"content-length", b"connection",
    b"user-agent", _GRANT,
})
_MAX_BODY = 512
_LOGGER = logging.getLogger(__name__)
#: A FaultRejected code is a bounded identifier; anything else is not echoed.
_REFUSAL_CODE = re.compile(r"[a-z0-9_:.]{1,128}")


class _Refused(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__("controlled_fault_refused")


def _address(value: object) -> bool:
    return (
        isinstance(value, (tuple, list)) and len(value) == 2
        and value[0] in ("127.0.0.1", "::1")
        and isinstance(value[1], int) and not isinstance(value[1], bool)
        and 0 < value[1] <= 65535
    )


def _headers(scope: Mapping[str, Any]) -> tuple[bytes, int]:
    headers = scope.get("headers")
    if not isinstance(headers, (tuple, list)) or len(headers) > 32:
        raise _Refused(400)
    seen: dict[bytes, bytes] = {}
    for pair in headers:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise _Refused(400)
        key, value = pair
        if not isinstance(key, bytes) or not isinstance(value, bytes):
            raise _Refused(400)
        key = key.lower()
        # An allowlist also excludes Forwarded, XFF, Via, proxy and spoofed
        # client identity variants, even when the direct peer is loopback.
        if key not in _ALLOWED_HEADERS or key in seen or len(value) > 256:
            raise _Refused(403)
        seen[key] = value
    if seen.get(b"content-type") != b"application/json":
        raise _Refused(400)
    grant = seen.get(_GRANT)
    if grant is None or not hmac.compare_digest(grant, EXACT_FLEET_GRANT.encode("ascii")):
        raise _Refused(403)
    length = seen.get(b"content-length", b"")
    if not length or not length.isascii() or not length.isdigit():
        raise _Refused(400)
    size = int(length)
    if size > _MAX_BODY:
        raise _Refused(413)
    if size == 0:
        raise _Refused(400)
    return grant, size


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class A6LocalFaultReceiver:
    """Explicitly injected direct-peer ASGI control; product mount needs the grant.

    An ASGI scope is trustworthy only if a direct server provides its original
    socket peer; a proxy that rewrites scope['client'] invalidates this contract.
    """

    def __init__(self, *, seams: Mapping[str, FaultSeam], clock_ms: Callable[[], int],
                 path: str, test_only: bool = False, body_timeout_seconds: float = 2.0,
                 product_grant: str | None = None):
        # A product mount is admitted only by the exact fleet grant, compared in
        # constant time; anything else keeps the historical test-only refusal.
        granted = isinstance(product_grant, str) and hmac.compare_digest(
            product_grant.encode("utf-8"), EXACT_FLEET_GRANT.encode("ascii"))
        if test_only is not True and not granted:
            raise ValueError("test_only_composition_required")
        if not isinstance(path, str) or not path.startswith("/") or path == "/" or "?" in path:
            raise ValueError("exact_control_path_required")
        if not 0 < body_timeout_seconds <= 2:
            raise ValueError("bounded_timeout_required")
        self._path = path
        self._seams = dict(seams)
        self._clock_ms = clock_ms
        self._timeout = body_timeout_seconds
        self._executed: set[str] = set()
        self._execution_order: list[str] = []
        self._lock = asyncio.Lock()

    @property
    def executed_case_ids(self) -> tuple[str, ...]:
        """Receiver-local successful execution order; never physical authority."""
        return tuple(self._execution_order)

    async def __call__(self, scope, receive, send) -> None:
        status = 403
        try:
            if not isinstance(scope, Mapping) or scope.get("type") != "http":
                raise _Refused(403)
            # Both values must come from a direct trusted ASGI server. Neither
            # Host nor a caller-supplied forwarding header can substitute.
            if not _address(scope.get("client")) or not _address(scope.get("server")):
                raise _Refused(403)
            if (scope.get("method") != "POST" or scope.get("path") != self._path
                    or scope.get("raw_path") != self._path.encode("ascii")
                    or scope.get("query_string", b"") != b""):
                raise _Refused(403)
            grant, size = _headers(scope)
            body = bytearray()
            deadline = time.monotonic() + self._timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _Refused(400)
                try:
                    message = await asyncio.wait_for(receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise _Refused(400) from None
                if not isinstance(message, Mapping) or message.get("type") != "http.request":
                    raise _Refused(400)
                part = message.get("body", b"")
                more = message.get("more_body", False)
                if not isinstance(part, bytes) or not isinstance(more, bool):
                    raise _Refused(400)
                body.extend(part)
                if len(body) > size or len(body) > _MAX_BODY:
                    raise _Refused(413)
                if not more:
                    break
            if len(body) != size:
                raise _Refused(400)
            try:
                payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
            except (ValueError, UnicodeDecodeError):
                raise _Refused(400) from None
            if (not isinstance(payload, dict) or set(payload) != {"action", "case_id"}
                    or payload["action"] != EXECUTE_SOURCE_FAILURE_ACTION
                    or not isinstance(payload["case_id"], str)
                    or payload["case_id"] not in INJECTION_POINTS):
                raise _Refused(400)
            case_id = payload["case_id"]
            # The request's own grant and transport peer, never process env or
            # Host/JSON/XFF, are passed through the mechanism's authority.
            authority = ControlledFaultAuthority(grant.decode("ascii"), scope["client"][0])
            async with self._lock:
                if case_id in self._executed:
                    raise _Refused(409)
                # A seam may mutate before raising; consuming before invoking it
                # prevents a retry from applying the same partial fault twice.
                self._executed.add(case_id)
                mechanism = ControlledFaultMechanism(
                    seams=self._seams, authority=authority, clock_ms=self._clock_ms,
                )
                try:
                    mechanism.execute(EXECUTE_SOURCE_FAILURE_ACTION, case_id=case_id)
                except FaultRejected as rejected:
                    code = str(rejected)
                    # Operator diagnostics only: the bounded refusal code and
                    # case id; the HTTP response stays non-disclosing.
                    record = {"event": "a6_controlled_fault_refused", "case_id": case_id,
                              "code": code if _REFUSAL_CODE.fullmatch(code) else "unbounded"}
                    unmet = getattr(rejected, "unmet", None)
                    if isinstance(unmet, Mapping):
                        record["unmet"] = dict(unmet)
                    measured = getattr(rejected, "measured", None)
                    if isinstance(measured, Mapping) and measured:
                        record["measured"] = dict(measured)
                    _LOGGER.warning("%s", json.dumps(
                        record, sort_keys=True, separators=(",", ":"), default=str))
                    raise _Refused(403) from None
                self._execution_order.append(case_id)
            status = 200
        except _Refused as error:
            status = error.status
        except Exception:
            # Never expose an exception from a seam, case, grant or record.
            status = 500
        body_out = json.dumps({"code": "controlled_fault_executed" if status == 200
                               else "controlled_fault_refused"}).encode("ascii")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body_out)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body_out, "more_body": False})
