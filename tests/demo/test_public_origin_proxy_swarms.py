# SPDX-License-Identifier: AGPL-3.0-only
"""The swarm directory mounted as a path route (/swarms) on the apex public-origin proxy.

FAKES (labelled): both upstreams are recording stubs.  Everything else is the shipped proxy.
"""
from __future__ import annotations

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any, Iterator

import pytest

from mycelium_demo import public_origin_proxy as proxy
from mycelium_swarm_directory import hosted_defaults as defaults

CAPABILITY = "a" * 64


class _Recording(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, label: str) -> None:
        self.label = label
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"method": self.command, "path": self.path, "body": body,
                                       "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "raw": self.headers.get_all("Cf-Connecting-Ip") or []})
                payload = json.dumps({"from": outer.label}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = do_HEAD = _answer

        super().__init__(("127.0.0.1", 0), Handler)


@contextmanager
def _serve(server: ThreadingHTTPServer) -> Iterator[Any]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _world(*, mount: bool = True, directory_port: int | None = None,
           require_ip: bool | None = None, redirect_root: bool = True,
           product_routes: bool = False) -> Iterator[tuple[Any, _Recording, _Recording]]:
    product, directory = _Recording("product"), _Recording("directory")
    with _serve(product), _serve(directory):
        extra: dict[str, Any] = {}
        if mount:
            extra = {"directory_host": "127.0.0.1", "directory_port": directory_port or directory.server_port}
            if require_ip is not None:
                extra["directory_require_client_ip"] = require_ip
            extra["redirect_root_to_swarms"] = redirect_root
            extra["enable_product_routes"] = product_routes
        config = proxy.ProxyConfig(
            listen_host="127.0.0.1", listen_port=4350, upstream_host="127.0.0.1",
            upstream_port=product.server_port, public_host="mycelium.now", capability=CAPABILITY,
            upstream_timeout_seconds=3, **extra)
        with _serve(proxy.PublicOriginProxyServer(("127.0.0.1", 0), config)) as public:
            yield public, product, directory


def _call(port: int, method: str, path: str, *, body: bytes = b"", headers: dict[str, str] | None = None,
          chunks: list[bytes] | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        if chunks is not None:
            connection.putrequest(method, path)
            for name, value in (headers or {}).items():
                connection.putheader(name, value)
            connection.putheader("Transfer-Encoding", "chunked")
            connection.endheaders()
            for chunk in chunks:
                connection.send(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            connection.send(b"0\r\n\r\n")
        else:
            connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _raw(port: int, head: bytes) -> tuple[int, dict[str, Any]]:
    """Send only a request head (an over-cap body is refused before it is read: no racy upload)."""
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(head)
        reader = connection.makefile("rb")
        status = int(reader.readline().split()[1])
        length = 0
        while (line := reader.readline().strip()):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        return status, json.loads(reader.read(length))


def _slow_upload(port: int, path: str, headers: dict[str, str], pieces: list[bytes]) -> tuple[int, dict[str, Any]]:
    """Deterministic race: the client uploads slower than the proxy refuses (a real client behind a slow link).

    Each piece is sent after a pause, so the proxy has refused and closed before the later pieces leave.  A proxy
    that closes with its peer still sending makes the kernel answer with a reset: the client's next send raises
    (BrokenPipe/ConnectionReset) and the refusal is never read.  A lingering close keeps reading and discarding,
    so every send succeeds and the client reads the refusal.
    """
    import time

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("POST", path)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        for piece in pieces:
            time.sleep(0.15)
            connection.send(piece)  # must not raise: the proxy lingers rather than resetting
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


IP = {"Cf-Connecting-Ip": "203.0.113.7"}
POST = {"Content-Type": "application/json", "X-Mycelium-Swarms": "1", **IP}


def test_defaults_are_named_in_one_module() -> None:
    assert defaults.DIRECTORY_MOUNT_PREFIX == "/swarms"
    assert defaults.DIRECTORY_PORT == 4360
    assert defaults.DIRECTORY_PUBLIC_ORIGIN == "https://mycelium.now"
    assert defaults.PROXY_PORT == 4350
    assert defaults.SWARM_DAILY_REQUESTS == 200
    assert defaults.SWARM_DAILY_TOKENS == 40_000


def test_swarms_page_and_phonebook_reach_the_directory_not_the_product_gateway() -> None:
    with _world() as (public, product, directory):
        for path in ("/swarms", "/swarms/", "/swarms/swarms.js", "/swarms/swarms.css",
                     "/swarms/api/v1/config", "/swarms/api/v1/swarms",
                     "/swarms/api/v1/swarms/sw-aaaaaaaaaaaaaaaaaaaa",
                     "/swarms/api/v1/swarms/sw-aaaaaaaaaaaaaaaaaaaa/link.svg"):
            status, headers, payload = _call(public.server_port, "GET", path, headers=IP)
            assert (status, json.loads(payload)) == (200, {"from": "directory"}), path
        assert headers.get("Content-Security-Policy") == "default-src 'none'"  # directory CSP passes through
        assert product.requests == []
        assert [r["path"] for r in directory.requests][:2] == ["/swarms", "/swarms/"]


def test_directory_hop_gets_no_owner_authority_or_credentials() -> None:
    with _world() as (public, product, directory):
        body = b'{"protocol":"x"}'
        status, _h, payload = _call(public.server_port, "POST", "/swarms/api/v1/records", body=body, headers={
            **POST, "Authorization": "Bearer stolen", "Cookie": "s=1", "Proxy-Authorization": "x",
            "X-Mycelium-Authenticated-User": "mallory", "X-Mycelium-Proxy-Capability": "b" * 64,
            "X-Forwarded-For": "198.51.100.9", "X-Real-Ip": "198.51.100.9", "True-Client-Ip": "198.51.100.9",
            "Forwarded": "for=198.51.100.9"})
        assert status == 200 and json.loads(payload) == {"from": "directory"}
        (seen,) = directory.requests
        headers = seen["headers"]
        assert seen["body"] == body and seen["method"] == "POST"
        assert headers["content-type"] == "application/json" and headers["x-mycelium-swarms"] == "1"
        assert headers["host"] == "mycelium.now" and headers["x-forwarded-proto"] == "https"
        for name in ("authorization", "cookie", "proxy-authorization", "x-mycelium-authenticated-user",
                     "x-mycelium-proxy-capability", "x-forwarded-for", "x-real-ip", "true-client-ip", "forwarded"):
            assert name not in headers, name
        assert CAPABILITY not in json.dumps(headers) and "b" * 64 not in json.dumps(headers)
        assert seen["raw"] == ["203.0.113.7"]
        assert product.requests == []


def test_client_ip_header_is_validated_normalised_and_required() -> None:
    with _world() as (public, _product, directory):
        port = public.server_port
        assert _call(port, "GET", "/swarms/api/v1/config", headers={"Cf-Connecting-Ip": "2001:DB8::0001"})[0] == 200
        assert directory.requests[-1]["raw"] == ["2001:db8::1"]
        before = len(directory.requests)
        for bad in ("not-an-ip", "1.2.3.4, 5.6.7.8", "1.2.3.4:80", "999.1.1.1", "", "0x7f.1"):
            status, _h, payload = _call(port, "GET", "/swarms/api/v1/config", headers={"Cf-Connecting-Ip": bad})
            assert (status, json.loads(payload)["error"]["code"]) == (400, "client_ip_invalid"), bad
        status, _h, payload = _call(port, "GET", "/swarms/api/v1/config")
        assert (status, json.loads(payload)["error"]["code"]) == (400, "client_ip_missing")
        assert len(directory.requests) == before


def test_duplicate_client_ip_headers_are_refused() -> None:
    with _world() as (public, _product, directory):
        connection = http.client.HTTPConnection("127.0.0.1", public.server_port, timeout=5)
        try:
            connection.putrequest("GET", "/swarms/api/v1/config")
            connection.putheader("Cf-Connecting-Ip", "203.0.113.7")
            connection.putheader("Cf-Connecting-Ip", "203.0.113.8")
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 400
            assert json.loads(response.read())["error"]["code"] == "client_ip_invalid"
        finally:
            connection.close()
        assert directory.requests == []


def test_missing_client_ip_can_be_allowed_by_configuration_and_is_then_not_invented() -> None:
    with _world(require_ip=False) as (public, _product, directory):
        assert _call(public.server_port, "GET", "/swarms/api/v1/config")[0] == 200
        assert directory.requests[-1]["raw"] == []
        # a present-but-invalid value is still refused
        assert _call(public.server_port, "GET", "/swarms/api/v1/config", headers={"Cf-Connecting-Ip": "x"})[0] == 400


def test_traversal_and_unknown_swarms_paths_never_reach_either_upstream() -> None:
    with _world() as (public, product, directory):
        for path in ("/swarms/../api/v1/inference", "/swarms/%2e%2e/api/v1/bootstrap", "/swarms/api/v1/../../x",
                     "/swarms/unknown", "/swarms/api/v2/swarms", "/swarms/api/v1", "/swarms/api/v1/swarms?x=1",
                     "/swarms/api/v1/swarms#f", "/swarms/\\x", "/swarms/index.html", "/swarms//api/v1/config"):
            status, _h, payload = _call(public.server_port, "GET", path, headers=IP)
            assert status in {403, 404}, (path, status)
            assert "error" in json.loads(payload), path
        assert directory.requests == [] and product.requests == []


def test_swarms_methods_are_limited_to_get_head_post() -> None:
    with _world() as (public, _product, directory):
        port = public.server_port
        for method in ("PUT", "PATCH", "OPTIONS"):
            assert _call(port, method, "/swarms/api/v1/records", headers={**POST})[0] == 403, method
        assert _call(port, "DELETE", "/swarms/api/v1/records", headers=IP)[0] in {403, 404}
        assert directory.requests == []
        assert _call(port, "HEAD", "/swarms/api/v1/config", headers=IP)[0] == 200
        # POST only on the API, never on the static page
        assert _call(port, "POST", "/swarms", body=b"{}", headers=POST)[0] == 404
        assert _call(port, "POST", "/swarms/swarms.js", body=b"{}", headers=POST)[0] == 404
        assert len(directory.requests) == 1


def test_existing_allowlist_and_blocklist_are_unchanged_with_the_mount_on_and_product_routes_enabled() -> None:
    with _world(redirect_root=False, product_routes=True) as (public, product, directory):
        port = public.server_port
        assert _call(port, "GET", "/")[0] == 200
        assert _call(port, "GET", "/assets/app.js")[0] == 200
        assert _call(port, "GET", "/swarmsfoo")[0] == 200  # not the mount: still the product bundle
        assert _call(port, "GET", "/api/v1/bootstrap")[0] == 200
        assert _call(port, "POST", "/api/v1/inference", body=b"{}", headers={"Content-Type": "application/json"})[0] == 200
        assert [r["path"] for r in product.requests] == ["/", "/assets/app.js", "/swarmsfoo", "/api/v1/bootstrap",
                                                          "/api/v1/inference"]
        assert directory.requests == []
        for method, path, want in (("GET", "/__mycelium/live-status", 403), ("GET", "/api/v1/swarm/invites", 403),
                                   ("POST", "/api/v1/swarm/join", 403), ("POST", "/api/v1/swarm/leave", 403),
                                   ("POST", "/v1/inference", 404), ("GET", "/api/v1/swarms", 404),
                                   ("GET", "/api/v1/secret", 404), ("PUT", "/", 403)):
            assert _call(port, method, path, headers=IP)[0] == want, (method, path)
        # the product hop still carries the owner capability; the directory hop never did
        assert product.requests[0]["headers"]["x-mycelium-proxy-capability"] == CAPABILITY
        assert directory.requests == []


def test_mount_is_off_unless_configured_and_then_swarms_behaves_as_before() -> None:
    with _world(mount=False) as (public, product, directory):
        assert _call(public.server_port, "GET", "/swarms")[0] == 200  # product bundle path, as today
        assert [r["path"] for r in product.requests] == ["/swarms"]
        assert _call(public.server_port, "POST", "/swarms/api/v1/records", body=b"{}", headers=POST)[0] == 404
        assert directory.requests == []


def test_directory_down_is_a_502_not_a_product_fallback() -> None:
    with _world(directory_port=1) as (public, product, _directory):
        status, _h, payload = _call(public.server_port, "GET", "/swarms/api/v1/config", headers=IP)
        assert (status, json.loads(payload)["error"]["code"]) == (502, "upstream_unavailable")
        assert product.requests == []


def test_directory_body_cap_and_chunked_reframing() -> None:
    with _world(product_routes=True) as (public, product, directory):
        port = public.server_port
        over = defaults.DIRECTORY_MAX_BODY_BYTES + 1
        head = (b"POST /swarms/api/v1/records HTTP/1.1\r\nHost: x\r\nCf-Connecting-Ip: 203.0.113.7\r\n"
                b"Content-Type: application/json\r\n")
        status, document = _raw(port, head + b"Content-Length: %d\r\n\r\n" % over)
        assert (status, document["error"]["code"]) == (413, "request_body_too_large")
        assert _call(port, "POST", "/swarms/api/v1/records", headers=POST, chunks=[b"x" * 10, b"y" * 10])[0] == 200
        assert directory.requests[-1]["body"] == b"x" * 10 + b"y" * 10
        assert directory.requests[-1]["headers"]["content-length"] == "20"
        assert "transfer-encoding" not in directory.requests[-1]["headers"]
        status, document = _raw(port, head + b"Transfer-Encoding: chunked\r\n\r\n%x\r\n" % over)
        assert (status, document["error"]["code"]) == (413, "request_body_too_large")
        # chunked stays refused on the product route
        status, _h, payload = _call(port, "POST", "/api/v1/inference", headers={"Content-Type": "application/json"},
                                    chunks=[b"{}"])
        assert (status, json.loads(payload)["error"]["code"]) == (400, "unsupported_transfer_encoding")
        assert product.requests == []


def test_refusal_survives_a_body_uploaded_after_the_response() -> None:
    """Lingering close: an early refusal must reach a client that is still sending its (unread) body."""
    with _world(product_routes=True) as (public, product, directory):
        port = public.server_port
        over = defaults.DIRECTORY_MAX_BODY_BYTES + 1
        status, document = _slow_upload(
            port, "/swarms/api/v1/records",
            {**POST, "Content-Length": str(over)}, [b"x" * 16384, b"x" * 16384, b"x" * 16384])
        assert (status, document["error"]["code"]) == (413, "request_body_too_large")
        status, document = _slow_upload(
            port, "/api/v1/inference",
            {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            [b"2\r\n{}\r\n", b"2\r\n{}\r\n", b"2\r\n{}\r\n", b"0\r\n\r\n"])
        assert (status, document["error"]["code"]) == (400, "unsupported_transfer_encoding")
        assert product.requests == [] and directory.requests == []


def test_config_rejects_non_loopback_directory_upstream_and_cli_wires_it(tmp_path) -> None:
    base = dict(listen_host="127.0.0.1", listen_port=4350, upstream_host="127.0.0.1", upstream_port=4351,
                public_host="mycelium.now", capability=CAPABILITY)
    with pytest.raises(proxy.PublicOriginProxyError, match="non_loopback_directory_host"):
        proxy.ProxyConfig(**base, directory_host="10.0.0.5", directory_port=4360)
    with pytest.raises(proxy.PublicOriginProxyError, match="invalid_port"):
        proxy.ProxyConfig(**base, directory_host="127.0.0.1", directory_port=0)
    with pytest.raises(proxy.PublicOriginProxyError, match="directory_incomplete"):
        proxy.ProxyConfig(**base, directory_host="127.0.0.1")
    cap = tmp_path / "cap"
    cap.write_text(CAPABILITY)
    cap.chmod(0o600)
    argv = ["--upstream", "http://127.0.0.1:4351", "--trusted-proxy-capability-file", str(cap)]
    assert proxy.build_config_from_args(argv).directory_host is None  # off by default
    config = proxy.build_config_from_args([*argv, "--directory-upstream", "http://127.0.0.1:4360"])
    assert (config.directory_host, config.directory_port, config.directory_require_client_ip) == \
        ("127.0.0.1", 4360, True)
    relaxed = proxy.build_config_from_args([*argv, "--directory-upstream", "http://127.0.0.1:4360",
                                            "--directory-allow-missing-client-ip"])
    assert relaxed.directory_require_client_ip is False


def test_real_directory_behind_the_proxy_page_config_and_rate_key() -> None:
    """The shipped DirectoryServer (trusted-proxy mode) behind the shipped proxy; no fakes on the directory side."""
    from mycelium_swarm_directory.crypto import verify
    from mycelium_swarm_directory.directory import Directory
    from mycelium_swarm_directory.server import DirectoryServer
    from mycelium_swarm_directory.store import MemoryStore

    directory = Directory(MemoryStore(), verify)
    server = DirectoryServer(directory, port=0, public_origin=defaults.DIRECTORY_PUBLIC_ORIGIN, trusted_proxy=True)
    with _serve(server), _world(directory_port=server.server_port) as (public, product, _fake):
        status, headers, page = _call(public.server_port, "GET", "/swarms", headers=IP)
        assert status == 200 and b"<html" in page.lower() and "default-src 'none'" in headers["Content-Security-Policy"]
        status, _h, payload = _call(public.server_port, "GET", "/swarms/api/v1/config", headers=IP)
        assert status == 200 and json.loads(payload)["directory"] == "https://mycelium.now"
        status, _h, payload = _call(public.server_port, "POST", "/swarms/api/v1/records", body=b"{}", headers=POST)
        assert status in {400, 403, 422} and "error" in json.loads(payload)  # reached the directory, refused there
        assert product.requests == []
