# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grant-gated, read-only A11 evidence door mounted by the ordinary product.

Prereg R9 §6 bars 5 and 6 (zero KV residue after each window; observed proposal,
acceptance, correction and fallback coverage) live on the route's nodes, not in
the product gateway.  This door reads them without touching the route:

* every live route node answers the read-only ``a11_snapshot`` command over the
  route's own authenticated command channel with a **signed** node observation
  (its runtime's ordered KV release log; the entry also its speculation request
  history);
* the door verifies each observation against the node key the route bound at
  open (``_verify_observation``), then returns the complete signed envelopes, so
  the record stays verifiable offline against the bound plan's endpoint ids
  (``mycelium_a11_node_evidence.verify_node_evidence``) and cannot be forged by
  the serve, the door or the collector;
* it exists only while the serve holds the exact A11 fleet grant, every request
  must present it again, it listens on its own direct IPv4 loopback socket,
  refuses forwarding headers, answers ``GET`` only and never mutates anything.

A refusal carries only a stable code.  The door is a measurement surface, never
a qualification or an A11 result.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

EXACT_FLEET_GRANT = "FLEET GRANTED TO A11"
GRANT_ENV = "MYCELIUM_A11_FLEET_GRANT"
GRANT_HEADER = "x-mycelium-a11-fleet-grant"
CONTROL_PATH = "/a11/evidence"
STATUS_PROTOCOL = "mycelium.a11_evidence_control.v1"
EVIDENCE_PROTOCOL = "mycelium.a11_route_node_evidence.v1"
SNAPSHOT_COMMAND = "a11_snapshot"
_FORWARDING_HEADERS = frozenset({"forwarded", "via", "x-real-ip", "x-client-ip"})
_CODE = re.compile(r"[a-z][a-z0-9_:]{0,80}\Z")
_COMMAND_TIMEOUT_S = 5.0


class A11EvidenceControlRefused(ValueError):
    """The product refuses to mount the A11 evidence door."""


class A11EvidenceUnavailable(RuntimeError):
    """One evidence read could not be completed (stable code only)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def grant_from_environment() -> str | None:
    return os.environ.get(GRANT_ENV)


def exact_grant(value: Any) -> bool:
    return isinstance(value, str) and secrets.compare_digest(value, EXACT_FLEET_GRANT)


def _stable_code(error: BaseException) -> str | None:
    for candidate in (getattr(error, "code", None), str(error)):
        if isinstance(candidate, str) and _CODE.fullmatch(candidate):
            return candidate
    return None


def _detached(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


class RouteA11Evidence:
    """Signed A11 node evidence from every live node of one route.

    ``route`` is the product's route owner or a concrete ``PhysicalLiveRoute``;
    the concrete route is resolved on every read so a cold restart is followed.
    """

    def __init__(
        self,
        route: Any,
        *,
        command_timeout_s: float = _COMMAND_TIMEOUT_S,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._route = route
        self._timeout = float(command_timeout_s)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))

    def _concrete(self) -> Any:
        current = self._route
        resolver = getattr(current, "_current_route_or_raise", None)
        if callable(resolver):
            try:
                current = resolver()
            except Exception as exc:
                raise A11EvidenceUnavailable("a11_evidence_route_unavailable") from exc
        for name in ("_sessions", "_peers", "_plan", "_send_before", "_verify_observation", "_command_id"):
            if not hasattr(current, name):
                raise A11EvidenceUnavailable("a11_evidence_route_unsupported")
        return current

    def collect(self) -> dict[str, Any]:
        concrete = self._concrete()
        plan = concrete._plan
        nodes: dict[str, Any] = {}
        for node_id in sorted(concrete._peers):
            session = concrete._sessions.get(node_id)
            if session is None or getattr(session, "returncode", None) is not None:
                # An exited node cannot sign; the verifier treats a missing node
                # as missing evidence, never as zero residue.
                raise A11EvidenceUnavailable("a11_evidence_node_not_live")
            try:
                response = concrete._send_before(
                    node_id,
                    command_id=concrete._command_id(node_id, SNAPSHOT_COMMAND),
                    command=SNAPSHOT_COMMAND,
                    payload={},
                    deadline_monotonic_s=time.monotonic() + self._timeout,
                )
                concrete._verify_observation(node_id, response, event=SNAPSHOT_COMMAND)
            except Exception as exc:
                raise A11EvidenceUnavailable("a11_evidence_snapshot_failed") from exc
            nodes[node_id] = _detached(response["result"])
        return {
            "protocol": EVIDENCE_PROTOCOL,
            "collected_at_unix_ms": int(self._clock_ms()),
            "run_id": plan.get("run_id"),
            "deployment_id": plan.get("deployment_id"),
            "entry_node_id": plan.get("entry_node_id"),
            "nodes": nodes,
        }


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # never echo request material
        return

    def _reply(self, status: int, document: Mapping[str, Any]) -> None:
        body = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _refuse(self) -> None:
        self._reply(403, {"code": "a11_evidence_control_refused"})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        control = self.server.control
        headers = {key.lower(): value for key, value in self.headers.items()}
        parsed = urllib.parse.urlsplit(self.path)
        if (
            self.client_address[0] != "127.0.0.1"
            or parsed.path != CONTROL_PATH
            or parsed.query
            or any(key in _FORWARDING_HEADERS or key.startswith("x-forwarded-") for key in headers)
            or not exact_grant(headers.get(GRANT_HEADER))
        ):
            self._refuse()
            return
        try:
            document = control.source.collect()
        except Exception as error:  # noqa: BLE001 - only a stable code leaves
            code = _stable_code(error)
            self._reply(409 if code else 500, {"code": code or "a11_evidence_failed"})
            return
        control.record_read()
        self._reply(200, {"evidence": document})

    def _method_refused(self) -> None:
        self._refuse()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _method_refused  # noqa: N815


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    control: "A11EvidenceControl"


class A11EvidenceControl:
    """One product-owned evidence door lifetime on one direct loopback listener."""

    def __init__(
        self,
        *,
        grant: str | None,
        source: RouteA11Evidence,
        product_port: int,
        port: int = 0,
        public_origin: str | None = None,
        trusted_https_proxy: bool = False,
    ) -> None:
        if not exact_grant(grant):
            raise A11EvidenceControlRefused("explicit_fleet_grant_required")
        if public_origin is not None or trusted_https_proxy is not False:
            raise A11EvidenceControlRefused("a11_evidence_control_requires_direct_loopback_product")
        if type(product_port) is not int or not 1 <= product_port <= 65_535:
            raise A11EvidenceControlRefused("a11_evidence_control_product_port_invalid")
        if type(port) is not int or (port != 0 and not 1_024 <= port <= 65_535):
            raise A11EvidenceControlRefused("a11_evidence_control_port_invalid")
        if port == product_port:
            raise A11EvidenceControlRefused("a11_evidence_control_port_is_product_origin")
        if not isinstance(source, RouteA11Evidence):
            raise A11EvidenceControlRefused("a11_evidence_control_source_required")
        self.source = source
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._lifetime_id: str | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._reads = 0

    @property
    def port(self) -> int:
        if self._server is None:
            raise A11EvidenceControlRefused("a11_evidence_control_not_started")
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._server is not None or self._closed:
            raise A11EvidenceControlRefused("a11_evidence_control_lifecycle_invalid")
        server = _Server(("127.0.0.1", self._port), _Handler)
        server.control = self
        self._lifetime_id = secrets.token_hex(32)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is not None and not self._closed:
            self._server.shutdown()
            self._server.server_close()
            assert self._thread is not None
            self._thread.join(timeout=3)
        self._closed = True

    def record_read(self) -> None:
        with self._lock:
            self._reads += 1

    def status(self) -> dict[str, Any]:
        with self._lock:
            reads = self._reads
        return {
            "protocol": STATUS_PROTOCOL,
            "mounted": self._server is not None and not self._closed,
            "path": CONTROL_PATH,
            "port": None if self._server is None else int(self._server.server_address[1]),
            "lifetime_id": self._lifetime_id,
            "reads": reads,
            "claim_boundary": "read-only A11 measurement door; never a qualification or an A11 result",
        }


__all__ = (
    "A11EvidenceControl",
    "A11EvidenceControlRefused",
    "A11EvidenceUnavailable",
    "CONTROL_PATH",
    "EVIDENCE_PROTOCOL",
    "EXACT_FLEET_GRANT",
    "GRANT_HEADER",
    "RouteA11Evidence",
    "exact_grant",
    "grant_from_environment",
)
