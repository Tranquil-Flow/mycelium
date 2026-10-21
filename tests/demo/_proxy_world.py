# SPDX-License-Identifier: AGPL-3.0-only
"""Shared stubs for the /swarms proxy tests. FAKES (labelled): the product and directory upstreams are
recording stubs; everything else is the shipped proxy."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any, Iterator

from mycelium_demo import public_origin_proxy as proxy

CAPABILITY = "a" * 64
IP = {"Cf-Connecting-Ip": "203.0.113.7"}


class Recording(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, label: str, reply: dict[str, Any] | None = None) -> None:
        self.label = label
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"method": self.command, "path": self.path, "body": body})
                payload = json.dumps(reply if reply is not None else {"from": outer.label}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = do_HEAD = _answer

        super().__init__(("127.0.0.1", 0), Handler)


@contextmanager
def serve(server: ThreadingHTTPServer) -> Iterator[Any]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def world(*, mount: bool = True, directory_reply: dict[str, Any] | None = None,
          **config_extra: Any) -> Iterator[tuple[Any, Recording, Recording]]:
    product, directory = Recording("product"), Recording("directory", directory_reply)
    with serve(product), serve(directory):
        extra: dict[str, Any] = dict(config_extra)
        if mount:
            extra.update(directory_host="127.0.0.1", directory_port=directory.server_port)
        config = proxy.ProxyConfig(
            listen_host="127.0.0.1", listen_port=4350, upstream_host="127.0.0.1",
            upstream_port=product.server_port, public_host="mycelium.now", capability=CAPABILITY,
            upstream_timeout_seconds=3, **extra)
        with serve(proxy.PublicOriginProxyServer(("127.0.0.1", 0), config)) as public:
            yield public, product, directory


def call(port: int, method: str, path: str, *, body: bytes = b"",
         headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()
