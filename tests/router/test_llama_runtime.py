#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""RED tests for mycelium_router.llama_runtime — a llama.cpp-backed RuntimePort.

The adapter wraps the llama-server OpenAI-compatible HTTP server (from the
llama.cpp distribution already installed at /opt/homebrew/bin/llama-server).
It implements the same RuntimePort protocol as mlx_runtime:

  * execute(item: HopWorkItem) -> RuntimeResult
  * execute_batch(batch: RuntimeBatch) -> tuple[RuntimeResult, ...]
  * cancel(path_id: str) -> None
  * decode_mode: str

The tests here use a FAKE llama-server subprocess (a tiny stub) so they run
without a real model or GPU. They verify the adapter's HTTP client + request
shaping + response parsing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mycelium_router.llama_runtime import (  # noqa: E402
    LlamaRuntimePort,
    LlamaServerError,
    llama_request_from_hop,
    parse_llama_completion,
)


# ----------------------------- Fake llama-server -----------------------------

class _FakeLlamaHandler(BaseHTTPRequestHandler):
    """A tiny stub that speaks enough of the llama-server /completion API."""
    last_body: dict | None = None
    server_logs: list[str] = []

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_POST(self):  # noqa: N802
        if self.path != "/completion":
            self._send(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        type(self).last_body = body
        # Echo back a canned completion.
        self._send(200, {
            "content": "the answer",
            "generation_settings": {"n_predict": 10},
            "tokens": [{"id": 100}, {"id": 101}, {"id": 102}],
            "stopped_eos": True,
            "timings": {"predicted_ms": 123.4},
        })

    def _send(self, status, doc):
        data = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ServerThread(threading.Thread):
    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _FakeLlamaHandler)
        self.httpd.daemon_threads = True

    def run(self):
        self.httpd.serve_forever()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ----------------------------- Request shaping -----------------------------

class RequestShapingTests(unittest.TestCase):

    def test_llama_request_from_hop_shapes_prompt(self):
        from mycelium_router.contracts import HopWorkItem
        item = HopWorkItem(
            request_id="req-1", path_id="p1", path_attempt=1, phase="DECODE",
            token_index=3, hop_index=0, placement_id="pl-1", qos_class="interactive",
            deficit_ratio=0.0, enqueued_at=0.0, idempotency_key="k1",
            payload="what is the capital of France?", emits_token=True)
        body = llama_request_from_hop(item, max_tokens=32)
        self.assertEqual(body["prompt"], "what is the capital of France?")
        self.assertEqual(body["n_predict"], 32)
        self.assertEqual(body["temperature"], 0.0)
        self.assertIn("seed", body)

    def test_parse_llama_completion_returns_token_ids_and_text(self):
        doc = {
            "content": "the answer",
            "generation_settings": {"n_predict": 10},
            "tokens": [{"id": 100}, {"id": 101}, {"id": 102}],
            "stopped_eos": True,
            "timings": {"predicted_ms": 123.4},
        }
        parsed = parse_llama_completion(doc)
        self.assertEqual(parsed["text"], "the answer")
        self.assertEqual(parsed["token_ids"], [100, 101, 102])
        self.assertEqual(parsed["stopped_eos"], True)
        self.assertEqual(parsed["predicted_ms"], 123.4)

    def test_parse_llama_completion_rejects_garbage(self):
        with self.assertRaises(LlamaServerError):
            parse_llama_completion({"not": "a completion"})


# ----------------------------- Adapter behavior -----------------------------

class LlamaRuntimePortTests(unittest.TestCase):

    def test_execute_calls_llama_server_and_returns_result(self):
        port = _free_port()
        server = _ServerThread(port)
        server.start()
        try:
            runtime = LlamaRuntimePort(server_url=f"http://127.0.0.1:{port}",
                                       model_id="test-model")
            from mycelium_router.contracts import HopWorkItem, RuntimeResult
            item = HopWorkItem(
                request_id="req-1", path_id="p1", path_attempt=1, phase="DECODE",
                token_index=0, hop_index=0, placement_id="pl-1", qos_class="interactive",
                deficit_ratio=0.0, enqueued_at=0.0, idempotency_key="k1",
                payload="hello", emits_token=True)
            result = runtime.execute(item)
            self.assertIsInstance(result, RuntimeResult)
            self.assertTrue(result.success)
            self.assertEqual(result.token_id, 102)  # last token id
            # The fake server saw the right prompt.
            self.assertEqual(_FakeLlamaHandler.last_body["prompt"], "hello")
            self.assertEqual(_FakeLlamaHandler.last_body["n_predict"], 32)
        finally:
            server.stop()

    def test_execute_batch_calls_server_per_item(self):
        port = _free_port()
        server = _ServerThread(port)
        server.start()
        try:
            runtime = LlamaRuntimePort(server_url=f"http://127.0.0.1:{port}",
                                       model_id="test-model")
            from mycelium_router.contracts import (
                HopWorkItem, RuntimeBatch, RuntimeBatchKey)
            key = RuntimeBatchKey(
                deployment_id="dep", deployment_epoch=1, model_commit="abc",
                manifest_digest="def", placement_id="pl", assignment_id="asg",
                stage_signature="sig", load_proof_digest="prf",
                runtime_backend="llama_http", phase="DECODE", hidden_size=1,
                activation_bytes=1, token_span=1)
            items = tuple(
                HopWorkItem(
                    request_id=f"req-{i}", path_id=f"p{i}", path_attempt=1,
                    phase="DECODE", token_index=0, hop_index=0,
                    placement_id=f"pl-{i}", qos_class="interactive",
                    deficit_ratio=0.0, enqueued_at=0.0, idempotency_key=f"k{i}",
                    payload=f"prompt {i}", emits_token=True, batch_key=key)
                for i in range(3))
            results = runtime.execute_batch(RuntimeBatch(
                compatibility_key=key, items=items))
            self.assertEqual(len(results), 3)
            for r in results:
                self.assertTrue(r.success)
        finally:
            server.stop()

    def test_server_unreachable_raises_llama_server_error(self):
        runtime = LlamaRuntimePort(server_url="http://127.0.0.1:1",
                                   model_id="test-model")
        from mycelium_router.contracts import HopWorkItem
        item = HopWorkItem(
            request_id="req-1", path_id="p1", path_attempt=1, phase="DECODE",
            token_index=0, hop_index=0, placement_id="pl-1", qos_class="interactive",
            deficit_ratio=0.0, enqueued_at=0.0, idempotency_key="k1",
            payload="hello", emits_token=True)
        result = runtime.execute(item)
        self.assertFalse(result.success)
        self.assertEqual(result.failure_scope, "llama_server")
        self.assertTrue(result.failure_reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
