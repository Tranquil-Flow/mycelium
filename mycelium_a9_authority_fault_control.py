# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in A9 authority fault control: one direct-loopback, grant-gated publisher.

Publishes one of three A9 failures (stage withdrawal, codec withdrawal, a
registered-unknown runtime value) for a *current* member into the passive
authority overlay that the product evidence projection reads, so a two-session
browser withdrawal check can observe it live.  The acknowledgement means
published, not rendered: only the product's own projection, read by the
browsers afterwards, can establish withdrawal.  Mirrors the A11 draft-loss door.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mycelium_a9_authority_ledger import (
    EXACT_FLEET_GRANT, LEDGER, AuthorityLedger, LedgerRefused, exact_grant,
)

PATH = "/a9/authority-fault"
GRANT_HEADER = "x-mycelium-a9-fleet-grant"
TOKEN_HEADER = "x-mycelium-a9-authority-fault-token"
TOKEN_FILE = "a9-authority-fault.token"


class AuthorityFaultRefused(ValueError):
    pass


class RouteAuthorityFault:
    """Publish only for a node the route currently lists as a member."""

    def __init__(self, route: Any, ledger: AuthorityLedger = LEDGER) -> None:
        self.route = route
        self.ledger = ledger

    def publish(self, kind: str, node_id: str, value: str | None) -> dict[str, Any]:
        reader = getattr(self.route, "product_membership_records", None)
        if not callable(reader):
            raise AuthorityFaultRefused("a9_authority_fault_membership_unavailable")
        if node_id not in {r.get("node_id") for r in reader()}:
            raise AuthorityFaultRefused("a9_authority_fault_node_not_member")
        generation = self.ledger.publish(kind, node_id, grant=EXACT_FLEET_GRANT, value=value)
        return {"kind": kind, "node_id": node_id, "generation": generation}


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    def _reply(self, status: int, doc: dict[str, Any]) -> None:
        raw = json.dumps(doc, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802
        control = self.server.control
        headers = {key.lower(): value for key, value in self.headers.items()}
        if (self.client_address[0] != "127.0.0.1"
                or self.path != PATH or self.headers.get_all("host") != [f"127.0.0.1:{control.port}"]
                or len(self.headers.get_all(GRANT_HEADER) or []) != 1
                or len(self.headers.get_all(TOKEN_HEADER) or []) != 1
                or not exact_grant(headers.get(GRANT_HEADER))
                or not control.token_matches(headers.get(TOKEN_HEADER))
                or "origin" in headers or any(k.startswith("sec-fetch-") or k.startswith("x-forwarded-")
                                               or k in {"forwarded", "via", "x-real-ip", "x-client-ip"} for k in headers)):
            self._reply(403, {"code": "a9_authority_fault_control_refused"})
            return
        try:
            length = int(headers.get("content-length", ""))
            if not 1 <= length <= 1024 or headers.get("transfer-encoding"):
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if (not isinstance(payload, dict) or not {"kind", "node_id"} <= set(payload)
                    or set(payload) - {"kind", "node_id", "value"}):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"code": "invalid_a9_authority_fault_fields"})
            return
        try:
            result = control.source.publish(payload["kind"], payload["node_id"], payload.get("value"))
        except LedgerRefused:
            self._reply(400, {"code": "invalid_a9_authority_fault_fields"})
            return
        except AuthorityFaultRefused as exc:
            self._reply(409, {"code": str(exc)})
            return
        except Exception:
            self._reply(409, {"code": "a9_authority_fault_outcome_unknown"})
            return
        self._reply(200, {"published": result})

    def do_GET(self) -> None:  # noqa: N802
        self._reply(403, {"code": "a9_authority_fault_control_refused"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    control: "AuthorityFaultControl"


class AuthorityFaultControl:
    def __init__(self, *, grant: str | None, source: RouteAuthorityFault, token_file: Path,
                 product_port: int, port: int = 0, public_origin: str | None = None,
                 trusted_https_proxy: bool = False) -> None:
        if not exact_grant(grant):
            raise AuthorityFaultRefused("explicit_fleet_grant_required")
        if public_origin is not None or trusted_https_proxy:
            raise AuthorityFaultRefused("a9_authority_fault_requires_direct_loopback_product")
        if type(product_port) is not int or not 1 <= product_port <= 65535 or type(port) is not int or (port != 0 and not 1024 <= port <= 65535) or port == product_port:
            raise AuthorityFaultRefused("a9_authority_fault_port_invalid")
        self.source = source
        self._token_file = Path(token_file)
        parent = self._token_file.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise AuthorityFaultRefused("a9_authority_fault_token_dir_invalid")
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._token: str | None = None
        self._token_fd: int | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise AuthorityFaultRefused("a9_authority_fault_not_listening")
        return int(self._server.server_address[1])

    def token_matches(self, value: Any) -> bool:
        return isinstance(value, str) and self._token is not None and hmac.compare_digest(value, self._token)

    def start(self) -> None:
        if self._server is not None or self._token_fd is not None:
            raise AuthorityFaultRefused("a9_authority_fault_lifecycle_invalid")
        server = _Server(("127.0.0.1", self._port), _Handler)
        try:
            fd = os.open(self._token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self._token_fd = fd
            self._token = secrets.token_hex(32)
            os.write(fd, (self._token + "\n").encode("ascii"))
            server.control = self
            self._server = server
            self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            self._thread.start()
        except BaseException:
            server.server_close()
            self.close()
            raise

    def close(self) -> None:
        self._token = None
        if self._server is not None:
            if self._thread is not None and self._thread.is_alive():
                self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=3)
            self._server = None
        # Closing the door ends the fault: no published failure outlives the session.
        self.source.ledger.clear()
        if self._token_fd is not None:
            fd = self._token_fd
            self._token_fd = None
            try:
                original = os.fstat(fd)
                current = self._token_file.lstat()
                if stat.S_ISREG(current.st_mode) and (original.st_dev, original.st_ino) == (current.st_dev, current.st_ino):
                    self._token_file.unlink()
            except OSError:
                pass
            finally:
                os.close(fd)

    def status(self) -> dict[str, Any]:
        return {"mounted": self._server is not None, "port": self.port if self._server else None,
                "path": PATH, "qualification_claim": False, "physical_evidence": False}
