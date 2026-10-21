#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Server-layer tests for mycelium_openai_shim.server.

These tests use a stub LiveRoute (not a real one) and bind it to the handler
subclass at construction time. They cover:

  * GET /healthz, /v1/models
  * POST /v1/chat/completions non-streaming
  * POST /v1/chat/completions streaming (SSE)
  * 400 on malformed body
  * 400 on translation error
  * 500 on LiveRoute.infer failure
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mycelium_openai_shim.translator import (  # noqa: E402
    default_tokenizer, default_detokenizer, TranslateError)
from mycelium_openai_shim.server import (  # noqa: E402
    OpenAIShimHandler, _make_handler, serve_in_thread,
    _ThreadingServer,
)


class StubRoute:
    """Minimal LiveRoute stand-in. Records the last infer call and emits
    a configurable token sequence."""

    def __init__(self, tokens: tuple[int, ...] = (100, 101, 102, 103, 104),
                 raise_after_emit: Exception | None = None):
        self.tokens = tokens
        self.raise_after_emit = raise_after_emit
        self.last_infer_kwargs: dict | None = None

    def infer(self, *, token_ids, max_new_tokens, request_id, sink, **_):
        self.last_infer_kwargs = {
            "token_ids": list(token_ids),
            "max_new_tokens": max_new_tokens,
            "request_id": request_id,
        }
        for i, t in enumerate(self.tokens):
            sink.emit(i, t)
            if self.raise_after_emit and i == 1:
                raise self.raise_after_emit


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def running_server(route, *, model_id="mycelium-default", port=None):
    port = port or _free_port()
    handler = _make_handler(route, default_tokenizer(),
                           default_detokenizer(), model_id)
    server = _ThreadingServer(("127.0.0.1", port), handler)
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


class HealthAndModelsTests(unittest.TestCase):

    def test_healthz(self):
        with running_server(StubRoute()) as port:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=10)
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read()), {"status": "ok"})

    def test_models_endpoint(self):
        with running_server(StubRoute(), model_id="mycelium-qwen-7b") as port:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=10)
            self.assertEqual(r.status, 200)
            doc = json.loads(r.read())
            self.assertEqual(doc["object"], "list")
            self.assertEqual(doc["data"][0]["id"], "mycelium-qwen-7b")

    def test_unknown_path_404(self):
        with running_server(StubRoute()) as port:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/nope", timeout=10)
                self.fail("expected 404")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404)


class NonStreamingChatTests(unittest.TestCase):

    def test_simple_chat_completion(self):
        route = StubRoute(tokens=(200, 201, 202))
        with running_server(route) as port:
            body = json.dumps({
                "model": "mycelium-qwen-7b",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 16,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=10)
            self.assertEqual(r.status, 200)
            doc = json.loads(r.read())
            self.assertEqual(doc["object"], "chat.completion")
            self.assertEqual(doc["choices"][0]["message"]["role"], "assistant")
            self.assertEqual(doc["choices"][0]["finish_reason"], "stop")
            self.assertEqual(doc["usage"]["completion_tokens"], 3)
            # StubRoute received the request with the tokenized prompt.
            assert route.last_infer_kwargs is not None
            self.assertEqual(route.last_infer_kwargs["max_new_tokens"], 16)
            self.assertGreater(len(route.last_infer_kwargs["token_ids"]), 0)

    def test_invalid_json_returns_400(self):
        with running_server(StubRoute()) as port:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=b"not json", method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("expected 400")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)
                self.assertEqual(json.loads(exc.read())["error"], "invalid_json")

    def test_missing_messages_returns_400(self):
        with running_server(StubRoute()) as port:
            body = json.dumps({"model": "x", "max_tokens": 1}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("expected 400")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)
                self.assertEqual(json.loads(exc.read())["error"], "invalid_request")

    def test_infer_failure_returns_500(self):
        route = StubRoute(raise_after_emit=RuntimeError("kaboom"))
        with running_server(route) as port:
            body = json.dumps({
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 4,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("expected 500")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 500)
                self.assertEqual(json.loads(exc.read())["error"], "infer_failed")


class StreamingChatTests(unittest.TestCase):

    def test_streaming_emits_sse_chunks(self):
        route = StubRoute(tokens=(300, 301, 302))
        with running_server(route) as port:
            body = json.dumps({
                "model": "mycelium-qwen-7b",
                "messages": [{"role": "user", "content": "stream please"}],
                "max_tokens": 10,
                "stream": True,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=10)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["Content-Type"], "text/event-stream")
            raw = r.read().decode("utf-8")
            # 3 token chunks + 1 final stop chunk + [DONE] sentinel
            chunks = [l for l in raw.split("\n\n") if l.startswith("data: ")]
            self.assertGreaterEqual(len(chunks), 4)
            # Last non-DONE chunk has finish_reason stop
            last_data = [c for c in chunks if c != "data: [DONE]"][-1]
            last_doc = json.loads(last_data[len("data: "):])
            self.assertEqual(last_doc["choices"][0]["finish_reason"], "stop")
            self.assertEqual(last_doc["object"], "chat.completion.chunk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
