# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterator

import pytest

from mycelium_demo import public_origin_proxy as proxy


CAPABILITY = "a" * 64


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        _ = (format, args)
        return


class _RecordingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.requests: list[dict[str, Any]] = []
        super().__init__(("127.0.0.1", 0), handler)


@contextmanager
def _running_server(server: ThreadingHTTPServer) -> Iterator[ThreadingHTTPServer]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _running_proxy(upstream_port: int) -> Iterator[proxy.PublicOriginProxyServer]:
    config = proxy.ProxyConfig(
        listen_host="127.0.0.1",
        listen_port=4350,
        upstream_host="127.0.0.1",
        upstream_port=upstream_port,
        public_host="mycelium.now",
        capability=CAPABILITY,
        authenticated_user="owner",
        upstream_timeout_seconds=3,
    )
    with _running_server(
        proxy.PublicOriginProxyServer(("127.0.0.1", 0), config)
    ) as server:
        yield server  # type: ignore[misc]


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


def _write_capability(path: Path, value: str = CAPABILITY, *, mode: int = 0o600) -> Path:
    path.write_text(value, encoding="ascii")
    os.chmod(path, mode)
    return path


def test_capability_loader_rejects_unsafe_files_and_accepts_owner_private(
    tmp_path: Path,
) -> None:
    with pytest.raises(proxy.PublicOriginProxyError, match="relative_capability_path"):
        proxy.load_proxy_capability(Path("relative-capability"))

    accepted = _write_capability(tmp_path / "capability")
    assert proxy.load_proxy_capability(accepted) == CAPABILITY

    world_readable = _write_capability(tmp_path / "world-readable", mode=0o644)
    with pytest.raises(
        proxy.PublicOriginProxyError, match="capability_file_not_owner_private"
    ):
        proxy.load_proxy_capability(world_readable)

    too_short = _write_capability(tmp_path / "too-short", "b" * 63)
    with pytest.raises(proxy.PublicOriginProxyError, match="invalid_proxy_capability"):
        proxy.load_proxy_capability(too_short)

    non_hex = _write_capability(tmp_path / "non-hex", "A" + "c" * 63)
    with pytest.raises(proxy.PublicOriginProxyError, match="invalid_proxy_capability"):
        proxy.load_proxy_capability(non_hex)

    target = _write_capability(tmp_path / "symlink-target")
    symlink = tmp_path / "capability-symlink"
    symlink.symlink_to(target)
    with pytest.raises(proxy.PublicOriginProxyError, match="capability_file_symlink"):
        proxy.load_proxy_capability(symlink)


def test_proxy_overwrites_trusted_headers_and_strips_client_authority() -> None:
    class Handler(_QuietHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.requests.append(  # type: ignore[attr-defined]
                {
                    "path": self.path,
                    "body": body,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                }
            )
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with _running_server(_RecordingServer(Handler)) as upstream:
        with _running_proxy(upstream.server_port) as public:
            status, _headers, payload = _request(
                public.server_port,
                "POST",
                "/api/v1/inference",
                body=b'{"prompt":"hi"}',
                headers={
                    "Host": "attacker.example",
                    "Authorization": "Bearer internet-client",
                    "Proxy-Authorization": "Basic bad",
                    "X-Forwarded-Proto": "http",
                    "X-Mycelium-Authenticated-User": "attacker",
                    "X-Mycelium-Proxy-Capability": "bad",
                    "Content-Type": "application/json",
                },
            )

    assert status == 200
    assert json.loads(payload) == {"ok": True}
    assert len(upstream.requests) == 1  # type: ignore[attr-defined]
    forwarded = upstream.requests[0]  # type: ignore[attr-defined]
    headers = forwarded["headers"]
    assert forwarded["path"] == "/api/v1/inference"
    assert forwarded["body"] == b'{"prompt":"hi"}'
    assert headers["host"] == "mycelium.now"
    assert headers["x-forwarded-proto"] == "https"
    assert headers["x-mycelium-authenticated-user"] == "owner"
    assert headers["x-mycelium-proxy-capability"] == CAPABILITY
    assert "authorization" not in headers
    assert "proxy-authorization" not in headers


def test_proxy_rejects_private_and_swarm_mutation_paths_without_upstream() -> None:
    class Handler(_QuietHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.server.requests.append({"path": self.path})  # type: ignore[attr-defined]
            self.send_response(204)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            self.server.requests.append({"path": self.path})  # type: ignore[attr-defined]
            self.send_response(204)
            self.end_headers()

    with _running_server(_RecordingServer(Handler)) as upstream:
        with _running_proxy(upstream.server_port) as public:
            private_status, _headers, private_body = _request(
                public.server_port, "GET", "/__mycelium/health"
            )
            invite_status, _headers, invite_body = _request(
                public.server_port, "POST", "/api/v1/swarm/invites", body=b"{}"
            )

    assert private_status == 403
    assert json.loads(private_body)["error"]["code"] == "public_route_forbidden"
    assert invite_status == 403
    assert json.loads(invite_body)["error"]["code"] == "public_route_forbidden"
    assert upstream.requests == []  # type: ignore[attr-defined]


def test_proxy_route_policy_matches_browser_gateway_contract() -> None:
    class Handler(_QuietHandler):
        def _record(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self.server.requests.append(  # type: ignore[attr-defined]
                {"method": self.command, "path": self.path}
            )
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._record()

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            self._record()

        def do_DELETE(self) -> None:  # noqa: N802 - http.server API
            self._record()

    allowed = [
        ("POST", "/api/v1/inference", b'{"prompt":"hello"}'),
        ("GET", "/api/v1/inference/request-1/events?cursor=7", b""),
        ("DELETE", "/api/v1/inference/request-1/cancel", b""),
    ]
    rejected = [
        ("DELETE", "/api/v1/inference/request-1"),
        ("GET", "/api/v1/inference/events/request-1"),
        ("DELETE", "/api/v1/inference/cancel/request-1"),
        ("GET", "/api/v1/inference/request-1/nested/events"),
        ("DELETE", "/api/v1/inference/request-1/nested/cancel"),
        ("GET", "/api/v1/inference//events"),
        ("DELETE", "/api/v1/inference//cancel"),
        ("POST", "/v1/inference"),
        ("GET", "/v1/inference/request-1/events"),
        ("DELETE", "/v1/inference/request-1/cancel"),
    ]

    with _running_server(_RecordingServer(Handler)) as upstream:
        with _running_proxy(upstream.server_port) as public:
            for method, path, body in allowed:
                status, _headers, payload = _request(
                    public.server_port, method, path, body=body
                )
                assert status == 204, (method, path, payload)

            for method, path in rejected:
                before = list(upstream.requests)  # type: ignore[attr-defined]
                status, _headers, payload = _request(public.server_port, method, path)
                assert status == 404, (method, path, payload)
                assert json.loads(payload)["error"]["code"] == "public_route_not_found"
                assert upstream.requests == before  # type: ignore[attr-defined]

    assert upstream.requests == [  # type: ignore[attr-defined]
        {"method": method, "path": path} for method, path, _body in allowed
    ]


def test_proxy_allows_bootstrap_inference_events_and_streams_chunked_sse() -> None:
    class Handler(_QuietHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.server.requests.append(  # type: ignore[attr-defined]
                {
                    "method": "GET",
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                }
            )
            if self.path == "/api/v1/bootstrap":
                payload = b'{"protocol":"bootstrap"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path.startswith("/api/v1/inference/request-1/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in (b"data: one\n\n", b"data: two\n\n"):
                    self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                    self.wfile.write(chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            self.send_response(500)
            self.end_headers()

        def do_DELETE(self) -> None:  # noqa: N802 - http.server API
            self.server.requests.append(  # type: ignore[attr-defined]
                {
                    "method": "DELETE",
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                }
            )
            payload = b'{"protocol":"mycelium.request_gateway.v1","request_id":"request-1","cancelled":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with _running_server(_RecordingServer(Handler)) as upstream:
        with _running_proxy(upstream.server_port) as public:
            bootstrap_status, _headers, bootstrap_body = _request(
                public.server_port, "GET", "/api/v1/bootstrap"
            )
            events_status, events_headers, events_body = _request(
                public.server_port,
                "GET",
                "/api/v1/inference/request-1/events?cursor=7",
            )
            cancel_status, _headers, cancel_body = _request(
                public.server_port,
                "DELETE",
                "/api/v1/inference/request-1/cancel",
            )

    assert bootstrap_status == 200
    assert json.loads(bootstrap_body) == {"protocol": "bootstrap"}
    assert events_status == 200
    assert events_headers["Content-Type"] == "text/event-stream"
    assert events_body == b"data: one\n\ndata: two\n\n"
    assert cancel_status == 200
    assert json.loads(cancel_body)["cancelled"] is True
    assert [(record["method"], record["path"]) for record in upstream.requests] == [  # type: ignore[attr-defined]
        ("GET", "/api/v1/bootstrap"),
        ("GET", "/api/v1/inference/request-1/events?cursor=7"),
        ("DELETE", "/api/v1/inference/request-1/cancel"),
    ]
    for record in upstream.requests:  # type: ignore[attr-defined]
        assert record["headers"]["host"] == "mycelium.now"
        assert record["headers"]["x-forwarded-proto"] == "https"


def test_cli_refuses_non_loopback_listen_and_upstream_by_default(tmp_path: Path) -> None:
    capability_file = _write_capability(tmp_path / "capability")
    base_args = [
        "--listen-port",
        "4350",
        "--public-host",
        "mycelium.now",
        "--trusted-proxy-capability-file",
        str(capability_file),
    ]

    with pytest.raises(SystemExit) as listen_exit:
        proxy.main(
            [
                *base_args,
                "--listen-host",
                "0.0.0.0",
                "--upstream",
                "http://127.0.0.1:4351",
            ]
        )
    assert listen_exit.value.code == 2

    with pytest.raises(SystemExit) as upstream_exit:
        proxy.main(
            [
                *base_args,
                "--listen-host",
                "127.0.0.1",
                "--upstream",
                "http://192.0.2.10:4351",
            ]
        )
    assert upstream_exit.value.code == 2
