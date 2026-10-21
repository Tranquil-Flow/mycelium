# SPDX-License-Identifier: AGPL-3.0-only
"""Offline HTTP/SSE fixture integration for the reviewed A10 ProductClient.

The real ProductClient and ProductSession use urllib sockets. The only listener is
an owned ThreadingHTTPServer bound to 127.0.0.1:0; this is not a product server,
physical route, model inference, or qualification evidence.
"""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import unittest
from contextlib import contextmanager
from dataclasses import asdict, FrozenInstanceError, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from unittest.mock import patch
from scripts import run_a5_benchmark_gate as a5

from scripts import run_a10_physical_gate as a10
from scripts.run_a5_benchmark_gate import ProductSession

CANARY = "fixture-secret-prompt-cookie-csrf-https://private.invalid/path"
COOKIE = "fixture-session=" + CANARY
SHAPE = a10.RequestShape(7, "prompt_short", "output_short", "interactive", 0.0)
REF = "fixture-request-7"
EVENT_PATH = "/api/v1/inference/fixture-request-7/events?private=" + CANARY
EXPECTED_REF = "sha256:" + hashlib.sha256(REF.encode("ascii")).hexdigest()
ALLOWED_FIELDS = {"shape_index", "request_ref", "stage", "outcome", "status", "token_events", "terminal_kind", "cancel_outcome", "gateway_code"}


class FixtureHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    def __init__(self, mode: str):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.mode = mode
        self.calls: list[tuple[str, str]] = []
        self.errors: list[str] = []
        self.lock = threading.Lock()
        self.abrupt_sent = threading.Event()

    def record(self, method: str, path: str) -> None:
        with self.lock:
            self.calls.append((method, path))

    def handle_error(self, request, client_address):
        # Handler errors are test failures, not silently discarded stderr logs.
        with self.lock:
            self.errors.append("handler_exception")


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        return  # no cookie, prompt, origin or query in server logs

    def json_response(self, status: int, document: dict) -> None:
        data = json.dumps(document).encode("utf-8")
        self.send_response(status, "fixture")  # private body must never leak to diagnostics
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if self.path == "/api/v1/bootstrap":
            self.send_header("Set-Cookie", COOKIE + "; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.record("GET", path)
        if path == "/api/v1/bootstrap":
            self.json_response(200, {"session": {"csrf_header": "X-Fixture-CSRF", "csrf_token": CANARY}})
        elif path == "/api/v1/qualification/current":
            assert self.headers.get("Cookie") == COOKIE
            self.json_response(200, {"binding": {"fixture": "offline-only"}})
        elif path == urlsplit(EVENT_PATH).path:
            assert self.headers.get("Cookie") == COOKIE
            assert self.path == EVENT_PATH
            if self.server.mode == "get404":
                self.json_response(404, {"private": CANARY})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: token\ndata: " + CANARY.encode() + b"\n\n")
            self.wfile.flush()
            if self.server.mode == "invalid_utf8":
                self.wfile.write(b"\xff\n")
                self.wfile.flush()
                return
            if self.server.mode == "abrupt":
                self.server.abrupt_sent.set()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.server.mode != "eof":
                kind = self.server.mode
                assert kind in {"completed", "cancelled", "failed"}
                self.wfile.write(f"event: {kind}\ndata: {{}}\n\n".encode())
                self.wfile.flush()
        else:
            self.json_response(500, {"unexpected_path": CANARY})

    def do_POST(self):
        path = urlsplit(self.path).path
        self.server.record("POST", path)
        if path == "/__mycelium/replica-qualification/install":
            assert self.headers.get("Authorization") == "Bearer fixture-only-" + "x" * 32
            assert self.headers.get("Origin") == f"http://127.0.0.1:{self.server.server_port}"
            assert json.loads(self.rfile.read(int(self.headers["Content-Length"]))) == {"documents": []}
            self.json_response(503, {"private": CANARY})
            return
        assert path == "/api/v1/inference"
        assert self.headers.get("Cookie") == COOKIE
        assert self.headers.get("X-Fixture-CSRF") == CANARY
        assert self.headers.get("Origin") == f"http://127.0.0.1:{self.server.server_port}"
        assert self.headers.get("Content-Type") == "application/json"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body == {
            "protocol": "mycelium.request_gateway.v2",
            "prompt": a10.prompt_text(getattr(self.server, "shape", SHAPE)),
            "max_new_tokens": a10.OUTPUT_BUCKETS[SHAPE.output_bucket],
            "qualification": {"fixture": "offline-only"},
            "workload_profile_id": "interactive_chat_v1",
            "qos_class": "interactive",
        }
        if self.server.mode in {"post429", "post503"}:
            self.json_response(int(self.server.mode[4:]), {"private": CANARY})
        else:
            self.json_response(200, {"request_id": REF, "event_path": EVENT_PATH,
                                     "cancel_path": "/api/v1/inference/fixture-request-7/cancel"})

    def do_DELETE(self):
        path = urlsplit(self.path).path
        self.server.record("DELETE", path)
        assert path == "/api/v1/inference/fixture-request-7/cancel"
        assert self.headers.get("Cookie") == COOKIE
        self.json_response(202, {"cancelled": True})


@contextmanager
def loopback(mode: str):
    server = FixtureHTTPServer(mode)
    thread = threading.Thread(target=server.serve_forever, name="fixture-loopback", daemon=False)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()  # block_on_close joins all non-daemon request workers
        thread.join(timeout=3)
        if thread.is_alive() or any(worker.is_alive() for worker in server._threads):
            raise AssertionError("fixture server or request thread leaked")
        if server.errors:
            raise AssertionError(f"fixture handler failures: {server.errors}")


class ProductClientLoopbackTests(unittest.TestCase):
    def test_real_http_sse_and_default_scoring_parity(self):
        cases = {
            "completed": ("completed", "terminal", None, "completed", 1),
            "cancelled": ("cancelled", "terminal", None, "cancelled", 1),
            "failed": ("failed", "terminal", None, "failed", 1),
            "eof": ("failed", "stream_eof", None, None, 1),
            "invalid_utf8": ("failed", "stream_exception", None, None, 1),
            "post429": ("overflowed", "submit_error", 429, None, 0),
            "post503": ("overflowed", "submit_error", 503, None, 0),
            "get404": ("failed", "stream_exception", 404, None, 0),
            "abrupt": ("failed", "stream_eof", None, None, 1),
        }
        for mode, (outcome, stage, status, terminal, tokens) in cases.items():
            with self.subTest(mode=mode), loopback(mode) as (server, origin):
                default = a10.ProductClient(origin)
                instrumented = a10.ProductClient(origin, diagnostic_limit=2)
                self.assertIs(type(default._session), ProductSession)
                self.assertIs(type(instrumented._session), ProductSession)
                a = default.run(SHAPE)
                b = instrumented.run(SHAPE)
                self.assertEqual(default.request_diagnostics(), ())
                self.assertEqual((a.outcome, a.tokens, a.output_digest),
                                 (b.outcome, b.tokens, b.output_digest))
                self.assertEqual((b.outcome, b.tokens), (outcome, tokens))
                self.assertEqual(b.output_digest,
                                 "sha256:" + hashlib.sha256(CANARY.encode()).hexdigest()
                                 if outcome == "completed" else None)
                (record,) = instrumented.request_diagnostics()
                self.assertEqual(set(asdict(record)), ALLOWED_FIELDS)
                self.assertEqual((record.shape_index, record.request_ref, record.stage,
                                  record.outcome, record.status, record.token_events,
                                  record.terminal_kind),
                                 (SHAPE.index, None if stage == "submit_error" else EXPECTED_REF,
                                  stage, outcome, status, tokens, terminal))
                self.assertEqual(record.cancel_outcome,
                                 "sent" if mode in {"eof", "invalid_utf8", "get404", "abrupt"} else None)
                self.assertIsNone(record.gateway_code)
                self.assertNotIn(CANARY, repr(record))
                self.assertNotIn(CANARY, json.dumps(asdict(record)))
                self.assertNotIn(origin, repr(record))
                with self.assertRaises(FrozenInstanceError):
                    record.stage = "tamper"
                if mode == "abrupt":
                    self.assertTrue(server.abrupt_sent.wait(timeout=2))
                expected_bootstrap = [
                    ("GET", "/api/v1/bootstrap"),
                    ("GET", "/api/v1/qualification/current"),
                ]
                expected_run = [("POST", "/api/v1/inference")]
                if not mode.startswith("post"):
                    expected_run.append(("GET", urlsplit(EVENT_PATH).path))
                    if mode in {"eof", "invalid_utf8", "get404", "abrupt"}:
                        expected_run.append(("DELETE", "/api/v1/inference/fixture-request-7/cancel"))
                self.assertEqual(server.calls, expected_bootstrap * 2 + expected_run * 2)

    def test_real_public_and_operator_error_responses_are_closed(self):
        with loopback("completed") as (server, origin):
            with self.assertRaises(a5.GateError) as public:
                a5.public_json(origin, "/fixture-refusal")
            self.assertEqual(str(public.exception), "public_status_unavailable")
            self.assertTrue(public.exception.__cause__.fp.closed)
            with patch.dict(a5.os.environ, {"MYCELIUM_A5_OPERATOR_TOKEN": "fixture-only-" + "x" * 32}):
                with self.assertRaises(a5.GateError) as operator:
                    a5.install_replica_qualification(origin, [])
            self.assertEqual(str(operator.exception), "qualification_install_http_503")
            self.assertTrue(operator.exception.__cause__.fp.closed)
            self.assertEqual(server.calls, [("GET", "/fixture-refusal"), ("POST", "/__mycelium/replica-qualification/install")])

    def test_bounded_retention_over_real_transport(self):
        with loopback("completed") as (server, origin):
            client = a10.ProductClient(origin, diagnostic_limit=2)
            snapshots = []
            for index in range(3):
                server.shape = replace(SHAPE, index=index)
                self.assertEqual(client.run(server.shape).outcome, "completed")
                snapshots.append(client.request_diagnostics())
            self.assertEqual(list(map(len, snapshots)), [1, 2, 2])
            self.assertEqual(len(snapshots[0]), 1)  # previous snapshot unaffected
            self.assertEqual(len(client.request_diagnostics()), 2)
            self.assertEqual([row.shape_index for row in snapshots[0]], [0])
            self.assertEqual([row.shape_index for row in client.request_diagnostics()], [1, 2])
            self.assertEqual(len(server.calls), 2 + 3 * 2)


if __name__ == "__main__":
    unittest.main()
