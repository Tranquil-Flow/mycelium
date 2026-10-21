# SPDX-License-Identifier: AGPL-3.0-only
"""Grant-gated, read-only A7 evidence door mounted by the ordinary product.

The A7 physical gate measures a fenced KV-successor cutover from the product's
own evidence: the live entry node's signed ``a7_snapshot`` observation, the
route's request-scoped A4 incidents and the route's resource projection.  None
of that is reachable from outside the serve process, so this module mounts one
small read-only door beside the product origin, and only that:

* it exists only while the serve process holds the exact A7 fleet grant in its
  own environment, and every request must present the same grant again.  The
  grant is a consent marker, never a secret: the per-lifetime 256-bit token is
  the secret, it exists only in an owner-only file the door creates
  (``token_file``), it is compared in constant time, and neither the token nor
  its file content is ever logged, returned in a status or an error, or accepted
  from the query string;
* it listens on its own direct IPv4 loopback socket, refuses a non-loopback
  peer and any forwarding header, requires ``Host`` to be exactly
  ``127.0.0.1:<port>`` (DNS rebinding), refuses any browser context
  (``Origin``, ``Sec-Fetch-*``), rejects duplicated ``Host``, grant and token
  headers, and refuses to start behind a public origin or a trusted HTTPS proxy;
* it answers ``GET`` only, with two closed kinds: ``request`` (the entry's
  verified A7 request evidence plus this request's scoped incidents) and
  ``resources`` (per live node counters used for the terminal resource delta);
* it never sends a node anything but the read-only ``a7_snapshot`` command over
  the route's own signed command channel, never calls ``public_status`` while a
  request is being measured (``resources`` is the caller's explicit choice), and
  never mutates route, router or node state.

A refusal carries only a stable code.  The door is a measurement surface, not a
qualification: its status never claims an A7 result.
"""

from __future__ import annotations

import hmac
import json
import os
import platform
import re
import secrets
import stat
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

EXACT_FLEET_GRANT = "FLEET GRANTED TO A7"
GRANT_ENV = "MYCELIUM_A7_FLEET_GRANT"
GRANT_HEADER = "x-mycelium-a7-fleet-grant"
TOKEN_HEADER = "x-mycelium-a7-evidence-token"
CONTROL_PATH = "/a7/evidence"
STATUS_PROTOCOL = "mycelium.a7_evidence_control.v1"
EVIDENCE_PROTOCOL = "mycelium.a7_physical_evidence.v1"
KINDS = ("request", "resources")
_FORWARDING_HEADERS = frozenset({"forwarded", "via", "x-real-ip", "x-client-ip"})
_CODE = re.compile(r"[a-z][a-z0-9_:]{0,80}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}\Z")
_COMMAND_TIMEOUT_S = 3.0
_RETIRED_ROUTES_KEPT = 4
RETIRED_EXIT_SOURCE = "retired_route_session_exit_status"


class A7EvidenceControlRefused(ValueError):
    """The product refuses to mount the A7 evidence door."""


class A7EvidenceUnavailable(RuntimeError):
    """One evidence read could not be completed (stable code only)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def grant_from_environment() -> str | None:
    return os.environ.get(GRANT_ENV)


def exact_grant(value: Any) -> bool:
    return isinstance(value, str) and hmac.compare_digest(value.encode("utf-8"), EXACT_FLEET_GRANT.encode("utf-8"))


def _stable_code(error: BaseException) -> str | None:
    for candidate in (getattr(error, "code", None), str(error)):
        if isinstance(candidate, str) and _CODE.fullmatch(candidate):
            return candidate
    return None


def _require_posix_host() -> None:
    """Refuse on a host that cannot hold this door's secret safely.

    The token contract is a POSIX owner-only contract: ``os.getuid``, an
    ``O_EXCL | O_NOFOLLOW`` create and ``(st_dev, st_ino)`` identity.  On a host
    with no POSIX owner/mode (Windows) those checks do not exist, so the door
    would silently fall back to weaker semantics while claiming the same
    guarantees.  It refuses instead, with one stable code, until
    ``mycelium_host_platform`` grows owner-only create/read/unlink primitives
    that cover both hosts.  The door is therefore **POSIX-only today**.
    """

    if platform.system() == "Windows" or os.name != "posix":  # pragma: no cover - platform branch
        raise A7EvidenceControlRefused("a7_evidence_control_requires_posix_host")


def _owner_only_token_path(token_file: Any) -> Path:
    """The token path, accepted only under a private directory this owner owns.

    A world- or group-readable parent (or a parent owned by someone else) can
    never hold a secret, so the door refuses rather than writing into it.
    """

    _require_posix_host()
    path = Path(token_file)
    parent = path.parent
    try:
        parent_mode = parent.lstat()
    except OSError:
        raise A7EvidenceControlRefused("a7_evidence_control_token_dir_invalid") from None
    if (
        not stat.S_ISDIR(parent_mode.st_mode)
        or parent_mode.st_uid != os.getuid()
        or stat.S_IMODE(parent_mode.st_mode) & 0o077
    ):
        raise A7EvidenceControlRefused("a7_evidence_control_token_dir_invalid")
    return path


def _write_owner_only_token(path: Path) -> tuple[bytes, tuple[int, int]]:
    """Create this lifetime's token in an owner-only file, or refuse.

    ``O_EXCL`` and ``O_NOFOLLOW`` mean a stale file, a symlink or a directory
    left by an earlier life can never be silently overwritten or followed:
    such a file fails closed instead of handing our new token to whoever left
    it.  The returned identity is ``(st_dev, st_ino)`` of the file we created,
    taken with ``os.fstat`` on the *still-open* descriptor - never a fresh
    ``lstat`` of the path after the descriptor is closed, which can name a file
    that a concurrent actor swapped in between the write and the close.
    """

    token = secrets.token_hex(32).encode("ascii")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        raise A7EvidenceControlRefused("a7_evidence_control_token_file_invalid") from None
    identity: tuple[int, int] | None = None
    try:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1  # the handle owns it now
            handle.write(token + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        written = os.lstat(path)
        if (written.st_dev, written.st_ino) != identity or not stat.S_ISREG(written.st_mode):
            # The path no longer names the file we created; refuse rather than
            # report an identity we cannot later prove.
            raise OSError("token file replaced during creation")
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        # Clean up only the exact file we created: a replacement that appeared
        # under the path belongs to someone else and must survive.
        _safe_unlink(path, identity)
        raise A7EvidenceControlRefused("a7_evidence_control_token_file_invalid") from None
    return token, identity  # type: ignore[return-value]


def _safe_unlink(path: Path, identity: tuple[int, int] | None) -> bool:
    """Unlink ``path`` only while it is still the file ``identity`` names.

    ``None`` means "the file we just created", checked by non-symlink regular
    file.  A file that was replaced under us is left alone: it is not ours to
    delete, and the token inside it is not ours to have leaked.
    """

    try:
        metadata = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        return False
    if identity is not None and (metadata.st_dev, metadata.st_ino) != identity:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _unlink_own_token(path: Path, identity: tuple[int, int] | None) -> bool:
    return _safe_unlink(path, identity)


def _process_identity(observation: Any) -> dict[str, Any] | None:
    """The signing process of one verified observation (host and process id only)."""

    if not isinstance(observation, Mapping):
        return None
    host_id, process_id = observation.get("host_id"), observation.get("process_id")
    if not isinstance(host_id, str) or type(process_id) is not int:
        return None
    return {"host_id": host_id, "process_id": process_id}


def _detached(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


class RouteA7Evidence:
    """Read-only A7 evidence from one live route (and optionally its product router).

    ``route`` is the product's route owner or a concrete ``PhysicalLiveRoute``;
    the concrete route is resolved on every read so a cold restart is followed.
    """

    def __init__(
        self,
        route: Any,
        *,
        router: Any | None = None,
        command_timeout_s: float = _COMMAND_TIMEOUT_S,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._route = route
        self._router = router
        self._timeout = float(command_timeout_s)
        self._clock_ms = clock_ms or (lambda: int(time.monotonic() * 1_000))
        self._routes_lock = threading.Lock()
        self._last_route: Any = None
        self._retired_routes: list[Any] = []

    # -- route resolution --------------------------------------------------------------

    def _concrete(self) -> Any:
        current = self._route
        resolver = getattr(current, "_current_route_or_raise", None)
        if callable(resolver):
            try:
                current = resolver()
            except Exception as exc:
                raise A7EvidenceUnavailable("a7_evidence_route_unavailable") from exc
        for name in ("_sessions", "_peers", "_plan", "_send_before", "_verify_observation", "_command_id"):
            if not hasattr(current, name):
                raise A7EvidenceUnavailable("a7_evidence_route_unsupported")
        with self._routes_lock:
            if self._last_route is not None and self._last_route is not current:
                # A cold restart replaced the route: keep the retired route so
                # its entry process's exit status stays observable.
                self._retired_routes.append(self._last_route)
                del self._retired_routes[:-_RETIRED_ROUTES_KEPT]
            self._last_route = current
        return current

    def _retire_unresolvable_route(self) -> list[dict[str, Any]]:
        """Treat the last resolved route as retired when no route resolves now.

        The owner has no current route only after it retired the previous one
        (cold restart pending or failed), so the cached route's entry process
        is reported with its exit status although no replacement exists yet.
        """

        with self._routes_lock:
            if self._last_route is not None:
                self._retired_routes.append(self._last_route)
                del self._retired_routes[:-_RETIRED_ROUTES_KEPT]
                self._last_route = None
        return self._retired_entry_processes()

    def _retired_entry_processes(self) -> list[dict[str, Any]]:
        """Entry processes of routes this door saw replaced, with their exit status.

        A6 precedent (``mycelium_live/router_port.py``
        ``_dead_peer_cleanup_receipt``): process exit releases every resource
        that process held, so the identity-bound process, confirmed by its exit
        status, is its own cleanup proof.  A process without an integer exit
        status (still running or unknown) proves nothing and is reported so.
        """

        with self._routes_lock:
            retired = list(self._retired_routes)
        records: list[dict[str, Any]] = []
        for route in retired:
            plan = getattr(route, "_plan", None)
            node_id = plan.get("entry_node_id") if isinstance(plan, Mapping) else None
            identities = getattr(route, "_identities", None)
            identity = identities.get(node_id) if isinstance(identities, Mapping) and isinstance(node_id, str) else None
            process = _process_identity(identity)
            if process is None:
                continue
            session = getattr(route, "_sessions", {}).get(node_id)
            exit_status = getattr(session, "returncode", None)
            peers = getattr(route, "_peers", None)
            transport = getattr(peers.get(node_id), "process_transport", None) if isinstance(peers, Mapping) else None
            records.append(
                {
                    "node_id": node_id,
                    **process,
                    # The session's exit status is the local process's; for a
                    # remote (ssh) launch it is the transport client's, not the node's.
                    "process_transport": transport if isinstance(transport, str) and transport else None,
                    "exit_status": exit_status if type(exit_status) is int else None,
                    "observed_by": RETIRED_EXIT_SOURCE,
                }
            )
        return records

    @staticmethod
    def _alive(concrete: Any, node_id: str) -> bool:
        session = concrete._sessions.get(node_id)
        return session is not None and getattr(session, "returncode", None) is None

    def _node_a7(self, concrete: Any, node_id: str, request_id: str | None) -> dict[str, Any]:
        return self._node_a7_with_process(concrete, node_id, request_id)[0]

    def _node_a7_with_process(self, concrete: Any, node_id: str, request_id: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """The node's verified A7 details and the process identity that signed them."""

        payload = {} if request_id is None else {"request_id": request_id}
        try:
            response = concrete._send_before(
                node_id,
                command_id=concrete._command_id(node_id, "a7_snapshot"),
                command="a7_snapshot",
                payload=payload,
                deadline_monotonic_s=time.monotonic() + self._timeout,
            )
            observation = concrete._verify_observation(node_id, response, event="a7_snapshot")
        except Exception as exc:
            raise A7EvidenceUnavailable("a7_evidence_snapshot_failed") from exc
        details = observation.get("details") if isinstance(observation, Mapping) else None
        if not isinstance(details, Mapping):
            raise A7EvidenceUnavailable("a7_evidence_snapshot_invalid")
        return _detached(details), _process_identity(observation)

    # -- evidence kinds ----------------------------------------------------------------

    def request(self, request_id: str) -> dict[str, Any]:
        """The entry's verified A7 request evidence and this request's scoped incidents."""

        if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
            raise A7EvidenceUnavailable("a7_evidence_request_id_invalid")
        concrete = self._concrete()
        entry_node_id = concrete._plan.get("entry_node_id")
        if not isinstance(entry_node_id, str) or not self._alive(concrete, entry_node_id):
            raise A7EvidenceUnavailable("a7_evidence_entry_unavailable")
        details, entry_process = self._node_a7_with_process(concrete, entry_node_id, request_id)
        incidents_reader = getattr(concrete, "a4_scoped_runtime_incidents", None)
        incidents = (
            [
                {
                    key: item.get(key)
                    for key in ("request_id", "subject_id", "reason", "state", "observed_at_monotonic_ms")
                }
                for item in incidents_reader()
                if isinstance(item, Mapping) and item.get("request_id") == request_id
            ]
            if callable(incidents_reader)
            else []
        )
        return {
            "protocol": EVIDENCE_PROTOCOL,
            "kind": "request",
            "request_id": request_id,
            "entry_node_id": entry_node_id,
            "entry_process": entry_process,
            "observed_at_monotonic_ms": self._clock_ms(),
            "a7": details,
            "scoped_incidents": _detached(incidents),
        }

    def product(self) -> dict[str, Any] | None:
        """The A7 workspace evidence for the product projection; ``None`` when A7 is not staged.

        A7 counts as staged only when the route owner installed the peer-exit
        authority (from the nodes' signed configured attestations).  The entry
        answers with its own signed ``a7_snapshot`` product block; the route
        contributes its request-scoped incident counts.
        """

        current = self._route
        resolver = getattr(current, "_current_route_or_raise", None)
        if callable(resolver):
            try:
                current = resolver()
            except Exception:
                return None  # no live route: nothing is staged right now
        authority = getattr(current, "_peer_exit_successor_authority", None)
        entry_node_id = getattr(authority, "a7_entry_node_id", None)
        if authority is None or not isinstance(entry_node_id, str):
            return None
        concrete = self._concrete()
        if not self._alive(concrete, entry_node_id):
            raise A7EvidenceUnavailable("a7_evidence_entry_unavailable")
        details = self._node_a7(concrete, entry_node_id, None)
        node = details.get("product")
        if not isinstance(node, Mapping):
            raise A7EvidenceUnavailable("a7_evidence_product_missing")
        graph = getattr(concrete, "execution_graph", None)
        incidents_reader = getattr(concrete, "a4_scoped_runtime_incidents", None)
        incidents = [item for item in (incidents_reader() if callable(incidents_reader) else ()) if isinstance(item, Mapping)]
        now_ms = self._clock_ms()
        recent = [
            item
            for item in incidents
            if type(item.get("observed_at_monotonic_ms")) is int and now_ms - item["observed_at_monotonic_ms"] <= 3_600_000
        ]
        last_reason = None
        if recent:
            newest = max(recent, key=lambda item: item["observed_at_monotonic_ms"])
            reason = newest.get("reason")
            last_reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason) else None
        return {
            "protocol": "mycelium.a7_product_evidence.v1",
            "deployment_id": getattr(graph, "deployment_id", None),
            "deployment_epoch": getattr(graph, "deployment_epoch", None),
            "topology_version": getattr(graph, "topology_version", None),
            "node": _detached(node),
            "incidents": {
                "scoped_incident_count_1h": len(recent),
                "process_exited_incident_count": sum(1 for item in incidents if item.get("reason") == "node_process_exited"),
                "last_reason_code": last_reason,
            },
        }

    def resources(self) -> dict[str, Any]:
        """Per live node resource counters for the terminal resource delta.

        Exited nodes are listed, never measured: a process exit is its own
        cleanup proof (A6 precedent), and nothing is invented for it.
        """

        try:
            concrete = self._concrete()
        except A7EvidenceUnavailable as exc:
            retired = self._retire_unresolvable_route() if exc.code == "a7_evidence_route_unavailable" else []
            if not retired:
                raise
            # No route is open (a cold restart is pending or failed), but the
            # route this door last read was retired: its entry process exit is
            # the cleanup proof and is reported without inventing any live
            # counter for a route that does not exist.
            return {
                "protocol": EVIDENCE_PROTOCOL,
                "kind": "resources",
                "observed_at_monotonic_ms": self._clock_ms(),
                "entry_node_id": retired[-1].get("node_id"),
                "nodes": {},
                "exited_nodes": [],
                "retired_entry_processes": retired,
                "route_request_registrations": None,
                "gateway_reservations": None,
            }
        status = concrete.public_status() if callable(getattr(concrete, "public_status", None)) else None
        peers = {}
        if isinstance(status, Mapping):
            for item in status.get("peers", ()):
                if isinstance(item, Mapping) and isinstance(item.get("node_id"), str):
                    peers[item["node_id"]] = item
        nodes: dict[str, Any] = {}
        exited: list[str] = []
        for node_id in sorted(concrete._peers):
            if not self._alive(concrete, node_id):
                exited.append(node_id)
                continue
            peer = peers.get(node_id)
            if not isinstance(peer, Mapping):
                raise A7EvidenceUnavailable("a7_evidence_peer_status_missing")
            details, process = self._node_a7_with_process(concrete, node_id, None)
            agent = details.get("agent") if isinstance(details.get("agent"), Mapping) else {}
            nodes[node_id] = {
                "process": process,
                "a7_roles": list(details.get("roles", [])),
                "active_state_count": peer.get("active_kv_state_count"),
                "active_kv_bytes": peer.get("active_kv_bytes"),
                "shadow_kv_bytes": agent.get("shadow_kv_bytes"),
                "shadow_entry_count": agent.get("shadow_entry_count"),
                "shadow_checkpoint_count": agent.get("shadow_checkpoint_count"),
                "shadow_checkpoint_payload_bytes": agent.get("shadow_checkpoint_payload_bytes"),
                "entry_fence_record_count": details.get("entry_fence_record_count"),
                "standby_runtime": details.get("standby_runtime"),
            }
        with getattr(concrete, "_lock", threading.Lock()):
            registrations = len(getattr(concrete, "_active_route_requests", {}) or {})
        reservations = None
        coordinator = getattr(self._router, "_coordinator", None)
        if coordinator is not None:
            try:
                reservations = sum(int(item["reservation_count"]) for item in coordinator.status()["requests"])
            except Exception as exc:
                raise A7EvidenceUnavailable("a7_evidence_reservations_unavailable") from exc
        return {
            "protocol": EVIDENCE_PROTOCOL,
            "kind": "resources",
            "observed_at_monotonic_ms": self._clock_ms(),
            "entry_node_id": concrete._plan.get("entry_node_id"),
            "nodes": _detached(nodes),
            "exited_nodes": exited,
            "retired_entry_processes": self._retired_entry_processes(),
            "route_request_registrations": registrations,
            "gateway_reservations": reservations,
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
        self._reply(403, {"code": "a7_evidence_control_refused"})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        control = self.server.control
        # A duplicated Host/grant/token header is a smuggling attempt, never a
        # request; only a request that presents each exactly once is read.
        if any(len(self.headers.get_all(name) or []) != 1 for name in ("Host", GRANT_HEADER, TOKEN_HEADER)):
            self._refuse()
            return
        headers = {key.lower(): value for key, value in self.headers.items()}
        parsed = urllib.parse.urlsplit(self.path)
        if (
            self.client_address[0] != "127.0.0.1"
            or headers.get("host") != f"127.0.0.1:{control.port}"
            or parsed.path != CONTROL_PATH
            or "origin" in headers
            or any(key.startswith("sec-fetch-") for key in headers)
            or any(key in _FORWARDING_HEADERS or key.startswith("x-forwarded-") for key in headers)
            or not exact_grant(headers.get(GRANT_HEADER))
            or not control.token_matches(headers.get(TOKEN_HEADER))
        ):
            self._refuse()
            return
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
        kind = query.get("kind", [None])
        request_id = query.get("request_id", [None])
        if (
            len(kind) != 1
            or kind[0] not in KINDS
            or len(request_id) != 1
            or not set(query) <= {"kind", "request_id"}
            or (kind[0] == "request") != (request_id[0] is not None)
        ):
            self._reply(400, {"code": "a7_evidence_request_invalid"})
            return
        try:
            if kind[0] == "request":
                document = control.source.request(request_id[0])
            else:
                document = control.source.resources()
        except Exception as error:  # noqa: BLE001 - only a stable code leaves
            code = _stable_code(error)
            self._reply(409 if code else 500, {"code": code or "a7_evidence_failed"})
            return
        control.record_read(kind[0])
        self._reply(200, {"evidence": document})

    def _method_refused(self) -> None:
        self._refuse()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _method_refused  # noqa: N815


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    control: "A7EvidenceControl"


class A7EvidenceControl:
    """One product-owned evidence door lifetime on one direct loopback listener."""

    def __init__(
        self,
        *,
        grant: str | None,
        source: RouteA7Evidence,
        token_file: Path,
        product_port: int,
        port: int = 0,
        public_origin: str | None = None,
        trusted_https_proxy: bool = False,
    ) -> None:
        if not exact_grant(grant):
            raise A7EvidenceControlRefused("explicit_fleet_grant_required")
        if public_origin is not None or trusted_https_proxy is not False:
            raise A7EvidenceControlRefused("a7_evidence_control_requires_direct_loopback_product")
        if type(product_port) is not int or not 1 <= product_port <= 65_535:
            raise A7EvidenceControlRefused("a7_evidence_control_product_port_invalid")
        if type(port) is not int or (port != 0 and not 1_024 <= port <= 65_535):
            raise A7EvidenceControlRefused("a7_evidence_control_port_invalid")
        if port == product_port:
            raise A7EvidenceControlRefused("a7_evidence_control_port_is_product_origin")
        if not isinstance(source, RouteA7Evidence):
            raise A7EvidenceControlRefused("a7_evidence_control_source_required")
        self._token_path = _owner_only_token_path(token_file)
        self.source = source
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._lifetime_id: str | None = None
        self._token: bytes | None = None
        self._token_identity: tuple[int, int] | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._reads: dict[str, int] = {kind: 0 for kind in KINDS}

    @property
    def port(self) -> int:
        if self._server is None:
            raise A7EvidenceControlRefused("a7_evidence_control_not_started")
        return int(self._server.server_address[1])

    @property
    def token_file(self) -> Path:
        """Where this lifetime's token lives; the token itself never leaves here."""
        return self._token_path

    def start(self) -> None:
        if self._server is not None or self._closed:
            raise A7EvidenceControlRefused("a7_evidence_control_lifecycle_invalid")
        # Bind first: a bind failure must leave no token file behind, and a
        # token that outlives a door that never opened would be a live secret
        # with no listener and no known owner.
        server = _Server(("127.0.0.1", self._port), _Handler)
        server.control = self
        try:
            token, identity = _write_owner_only_token(self._token_path)
        except BaseException:
            server.server_close()
            raise
        self._token = token
        self._token_identity = identity
        self._lifetime_id = secrets.token_hex(32)
        self._server = server
        try:
            self._thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            self._thread.start()
        except BaseException:
            # The thread never ran: release the listener and the secret token
            # this lifetime created, and leave nothing half-started behind.
            self._server = None
            self._thread = None
            self._lifetime_id = None
            self._token = None
            self._token_identity = None
            try:
                server.server_close()
            finally:
                _unlink_own_token(self._token_path, identity)
            raise

    def close(self) -> None:
        if self._server is not None and not self._closed:
            self._server.shutdown()
            self._server.server_close()
            assert self._thread is not None
            self._thread.join(timeout=3)
        if self._token is not None:
            # Only unlink the exact file this lifetime created: a token file
            # that was replaced (or re-pointed by a symlink) belongs to someone
            # else now and must survive this close.
            _unlink_own_token(self._token_path, self._token_identity)
            self._token = None
            self._token_identity = None
        self._closed = True

    def token_matches(self, value: Any) -> bool:
        token = self._token
        return (
            token is not None
            and not self._closed
            and isinstance(value, str)
            and len(value) == len(token)
            and hmac.compare_digest(value.encode("utf-8", "replace"), token)
        )

    def record_read(self, kind: str) -> None:
        with self._lock:
            self._reads[kind] = self._reads.get(kind, 0) + 1

    def status(self) -> dict[str, Any]:
        with self._lock:
            reads = dict(self._reads)
        return {
            "protocol": STATUS_PROTOCOL,
            "mounted": self._server is not None and not self._closed,
            "path": CONTROL_PATH,
            "port": None if self._server is None else int(self._server.server_address[1]),
            "lifetime_id": self._lifetime_id,
            "token_file": str(self._token_path),
            "reads": reads,
            "claim_boundary": "read-only A7 measurement door; never a qualification or an A7 result",
        }


__all__ = (
    "A7EvidenceControl",
    "A7EvidenceControlRefused",
    "A7EvidenceUnavailable",
    "CONTROL_PATH",
    "EVIDENCE_PROTOCOL",
    "EXACT_FLEET_GRANT",
    "GRANT_HEADER",
    "RouteA7Evidence",
    "TOKEN_HEADER",
    "exact_grant",
    "grant_from_environment",
)
