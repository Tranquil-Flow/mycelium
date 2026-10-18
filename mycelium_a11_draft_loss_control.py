# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in A11 draft-loss control: one direct-loopback, grant-gated node command.

The acknowledgement means armed, not fired. Only the signed A11 node snapshot
and the actual gateway stream can establish fallback and parity afterwards.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mycelium_a11_evidence_control import EXACT_FLEET_GRANT, exact_grant

PATH = "/a11/draft-loss"
GRANT_HEADER = "x-mycelium-a11-fleet-grant"
TOKEN_HEADER = "x-mycelium-a11-draft-loss-token"


class DraftLossRefused(ValueError):
    pass


class RouteDraftLoss:
    """Send only to the actual entry node, and verify its signed acknowledgement."""

    def __init__(self, route: Any) -> None:
        self.route = route

    def arm(self, request_id: str, after_accepted: int) -> dict[str, Any]:
        route = self.route
        resolver = getattr(route, "_current_route_or_raise", None)
        if callable(resolver):
            route = resolver()
        if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
                or type(after_accepted) is not int or not 1 <= after_accepted <= 512):
            raise DraftLossRefused("invalid_a11_draft_loss_fields")
        node_id = route._plan["entry_node_id"]
        session = route._sessions.get(node_id)
        if session is None or getattr(session, "returncode", None) is not None:
            raise DraftLossRefused("a11_draft_loss_entry_unavailable")
        response = route._send_before(
            node_id, command_id=route._command_id(node_id, "a11_draft_loss"),
            command="a11_draft_loss", payload={
                "request_id": request_id, "after_accepted": after_accepted,
                "grant": EXACT_FLEET_GRANT,
            }, deadline_monotonic_s=time.monotonic() + 5.0,
        )
        signed = route._verify_observation(node_id, response, event="a11_draft_loss")
        details = signed.get("details", {})
        if (details.get("request_id") != request_id
                or details.get("after_accepted") != after_accepted
                or details.get("outcome") != "armed"):
            raise DraftLossRefused("a11_draft_loss_ack_invalid")
        return {"request_id": request_id, "after_accepted": after_accepted}


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
            self._reply(403, {"code": "a11_draft_loss_control_refused"})
            return
        try:
            length = int(headers.get("content-length", ""))
            if not 1 <= length <= 1024 or headers.get("transfer-encoding"):
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {"request_id", "after_accepted"}:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"code": "invalid_a11_draft_loss_fields"})
            return
        try:
            result = control.source.arm(payload["request_id"], payload["after_accepted"])
        except Exception:
            # A lost acknowledgement may follow a successfully armed node;
            # never assert that the command had no effect without a receipt.
            self._reply(409, {"code": "a11_draft_loss_outcome_unknown"})
            return
        self._reply(200, {"armed": result})

    def do_GET(self) -> None:  # noqa: N802
        self._reply(403, {"code": "a11_draft_loss_control_refused"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    control: "DraftLossControl"


class DraftLossControl:
    def __init__(self, *, grant: str | None, source: RouteDraftLoss, token_file: Path,
                 product_port: int, port: int = 0, public_origin: str | None = None,
                 trusted_https_proxy: bool = False) -> None:
        if not exact_grant(grant):
            raise DraftLossRefused("explicit_fleet_grant_required")
        if public_origin is not None or trusted_https_proxy:
            raise DraftLossRefused("a11_draft_loss_requires_direct_loopback_product")
        if type(product_port) is not int or not 1 <= product_port <= 65535 or type(port) is not int or (port != 0 and not 1024 <= port <= 65535) or port == product_port:
            raise DraftLossRefused("a11_draft_loss_port_invalid")
        self.source = source
        self._token_file = Path(token_file)
        parent = self._token_file.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise DraftLossRefused("a11_draft_loss_token_dir_invalid")
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._token: str | None = None
        self._token_fd: int | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise DraftLossRefused("a11_draft_loss_not_listening")
        return int(self._server.server_address[1])

    def token_matches(self, value: Any) -> bool:
        return isinstance(value, str) and self._token is not None and hmac.compare_digest(value, self._token)

    def start(self) -> None:
        if self._server is not None or self._token_fd is not None:
            raise DraftLossRefused("a11_draft_loss_lifecycle_invalid")
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
