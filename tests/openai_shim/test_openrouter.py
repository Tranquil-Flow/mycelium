# SPDX-License-Identifier: AGPL-3.0-only
"""RED tests for OpenRouter compatibility.

Two directions:

  1. SERVE — Mycelium emits OpenRouter-shaped responses so any tool
     configured for OpenRouter can point at Mycelium's endpoint:
       * build_openrouter_models_response()
       * to_openrouter_chat_response() / to_openrouter_chat_chunk()
       * from_openrouter_request() accepts OpenRouter extras (provider,
         transforms, models, route) and ignores them safely.
  2. CONSUME — Mycelium routes inference through OpenRouter as a model
     backend (OpenRouterRuntimePort implementing RuntimePort), reading the
     API key from OPENROUTER_API_KEY and never printing it.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class ServeSideTests(unittest.TestCase):

    def test_build_openrouter_models_response_shape(self):
        from mycelium_openai_shim.openrouter import build_openrouter_models_response
        doc = build_openrouter_models_response(
            model_id="mycelium/qwen2.5-7b", name="Mycelium: Qwen2.5 7B",
            context_length=32768)
        self.assertEqual(doc["object"], "list")
        m = doc["data"][0]
        self.assertEqual(m["id"], "mycelium/qwen2.5-7b")
        self.assertEqual(m["name"], "Mycelium: Qwen2.5 7B")
        self.assertEqual(m["context_length"], 32768)
        # OpenRouter pricing block must exist (zero-priced for a self-hosted node).
        self.assertIn("pricing", m)
        for field in ("prompt", "completion", "request"):
            self.assertIn(field, m["pricing"])
        # OpenRouter includes a top_provider block.
        self.assertIn("top_provider", m)

    def test_to_openrouter_chat_response_shape(self):
        from mycelium_openai_shim.openrouter import to_openrouter_chat_response
        doc = to_openrouter_chat_response(
            request_id="req-123", model="mycelium/qwen2.5-7b",
            token_ids=(10, 11, 12), prompt_tokens=5, provider="Mycelium")
        # OpenRouter ids are 'gen-...' (not 'chatcmpl-...').
        self.assertTrue(doc["id"].startswith("gen-"))
        self.assertEqual(doc["provider"], "Mycelium")
        self.assertEqual(doc["model"], "mycelium/qwen2.5-7b")
        self.assertEqual(doc["object"], "chat.completion")
        self.assertEqual(doc["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(doc["choices"][0]["finish_reason"], "stop")
        self.assertEqual(doc["usage"]["prompt_tokens"], 5)
        self.assertEqual(doc["usage"]["completion_tokens"], 3)
        self.assertEqual(doc["usage"]["total_tokens"], 8)

    def test_to_openrouter_chat_chunk_shape(self):
        from mycelium_openai_shim.openrouter import to_openrouter_chat_chunk
        chunk = to_openrouter_chat_chunk(
            request_id="req-123", model="mycelium/qwen2.5-7b",
            token_id=10, token_index=0, finish_reason=None, provider="Mycelium")
        self.assertTrue(chunk["id"].startswith("gen-"))
        self.assertEqual(chunk["provider"], "Mycelium")
        self.assertEqual(chunk["object"], "chat.completion.chunk")
        self.assertIn("content", chunk["choices"][0]["delta"])
        self.assertIsNone(chunk["choices"][0]["finish_reason"])
        final = to_openrouter_chat_chunk(
            request_id="req-123", model="mycelium/qwen2.5-7b",
            token_id=0, token_index=5, finish_reason="stop", provider="Mycelium")
        self.assertEqual(final["choices"][0]["finish_reason"], "stop")

    def test_from_openrouter_request_accepts_openrouter_extras(self):
        from mycelium_openai_shim.openrouter import from_openrouter_request
        from mycelium_openai_shim.translator import TranslateError
        body = {
            "model": "mycelium/qwen2.5-7b",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            # OpenRouter-specific extras — must be accepted, not rejected.
            "provider": {"order": ["OpenAI", "Anthropic"], "allow_fallbacks": True},
            "transforms": ["middle-out"],
            "models": ["mycelium/qwen2.5-7b"],
            "route": "fallback",
        }
        req = from_openrouter_request(body, request_id="r1")
        self.assertEqual(req["model"], "mycelium/qwen2.5-7b")
        # The extras are surfaced as documented warnings, never as errors.
        self.assertTrue(any("provider" in w for w in req["warnings"]))

    def test_from_openrouter_request_rejects_bad_body(self):
        from mycelium_openai_shim.openrouter import from_openrouter_request
        from mycelium_openai_shim.translator import TranslateError
        with self.assertRaises(TranslateError):
            from_openrouter_request({"model": "x"}, request_id="r1")


class _FakeOpenRouterHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for https://openrouter.ai/api/v1/chat/completions."""
    seen: dict = {}

    def log_message(self, *args):  # noqa: D102
        return

    def do_POST(self):  # noqa: N802
        if self.path != "/api/v1/chat/completions":
            self._send(404, {})
            return
        auth = self.headers.get("Authorization", "")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        type(self).seen = {"auth": auth, "body": body}
        self._send(200, {
            "id": "gen-fake-1",
            "provider": "Mycelium",
            "model": body.get("model", "unknown"),
            "object": "chat.completion",
            "created": 1,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "bonjour"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        })

    def _send(self, status, doc):
        data = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ConsumeSideTests(unittest.TestCase):
    """OpenRouterRuntimePort implements RuntimePort against OpenRouter's API."""

    def test_execute_posts_to_openrouter_with_bearer_auth(self):
        port = _free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _FakeOpenRouterHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            from mycelium_router.openrouter_runtime import OpenRouterRuntimePort
            from mycelium_router.contracts import HopWorkItem
            port_obj = OpenRouterRuntimePort(
                base_url=f"http://127.0.0.1:{port}",
                api_key="test-key-123", model="mycelium/qwen2.5-7b")
            item = HopWorkItem(
                request_id="r1", path_id="p1", path_attempt=1, phase="DECODE",
                token_index=0, hop_index=0, placement_id="pl-1",
                qos_class="interactive", deficit_ratio=0.0, enqueued_at=0.0,
                idempotency_key="k1", payload="say hi", emits_token=True)
            result = port_obj.execute(item)
            self.assertTrue(result.success)
            self.assertEqual(result.payload, "bonjour")
            # The fake saw the Bearer header (key never printed, only sent).
            self.assertEqual(_FakeOpenRouterHandler.seen["auth"], "Bearer test-key-123")
            self.assertEqual(_FakeOpenRouterHandler.seen["body"]["messages"],
                             [{"role": "user", "content": "say hi"}])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_execute_returns_failed_result_on_unreachable(self):
        from mycelium_router.openrouter_runtime import OpenRouterRuntimePort
        from mycelium_router.contracts import HopWorkItem
        port_obj = OpenRouterRuntimePort(
            base_url="http://127.0.0.1:1", api_key="k", model="m")
        item = HopWorkItem(
            request_id="r1", path_id="p1", path_attempt=1, phase="DECODE",
            token_index=0, hop_index=0, placement_id="pl-1",
            qos_class="interactive", deficit_ratio=0.0, enqueued_at=0.0,
            idempotency_key="k1", payload="hi", emits_token=True)
        result = port_obj.execute(item)
        self.assertFalse(result.success)
        self.assertEqual(result.failure_scope, "openrouter")

    def test_api_key_never_appears_in_repr(self):
        from mycelium_router.openrouter_runtime import OpenRouterRuntimePort
        port_obj = OpenRouterRuntimePort(
            base_url="https://openrouter.ai", api_key="super-secret", model="m")
        self.assertNotIn("super-secret", repr(port_obj))
        self.assertNotIn("super-secret", str(port_obj))





class _StubRoute:
    def infer(self, *, token_ids, max_new_tokens, request_id, sink, **_):
        for i, tid in enumerate((300, 301, 302)):
            sink.emit(i, tid)


class OpenRouterServerTests(unittest.TestCase):
    """The shim serves OpenRouter-shaped responses at /api/v1/*."""

    def _server(self, api_token=""):
        from mycelium_openai_shim.server import _make_handler, _ThreadingServer
        from mycelium_openai_shim.translator import default_tokenizer, default_detokenizer
        handler = _make_handler(
            _StubRoute(), default_tokenizer(), default_detokenizer(),
            "mycelium/qwen2.5-7b", model_name="Mycelium: Qwen2.5 7B",
            context_length=32768, provider="Mycelium", api_token=api_token)
        port = _free_port()
        httpd = _ThreadingServer(("127.0.0.1", port), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, thread, port

    def test_openrouter_models_endpoint(self):
        httpd, thread, port = self._server()
        try:
            import urllib.request
            r = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/v1/models", timeout=10)
            doc = json.loads(r.read())
            self.assertEqual(doc["object"], "list")
            self.assertEqual(doc["data"][0]["id"], "mycelium/qwen2.5-7b")
            self.assertEqual(doc["data"][0]["pricing"]["prompt"], "0")
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_openrouter_chat_completion_shape(self):
        httpd, thread, port = self._server()
        try:
            import urllib.request
            body = json.dumps({
                "model": "mycelium/qwen2.5-7b",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8,
                "provider": {"order": ["Mycelium"]},
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/chat/completions",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=10)
            doc = json.loads(r.read())
            self.assertTrue(doc["id"].startswith("gen-"))
            self.assertEqual(doc["provider"], "Mycelium")
            self.assertEqual(doc["object"], "chat.completion")
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_bearer_auth_enforced_when_configured(self):
        httpd, thread, port = self._server(api_token="sekrit")
        try:
            import urllib.request, urllib.error
            body = json.dumps({
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8,
            }).encode()
            # Missing token -> 401
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/chat/completions",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("expected 401 without bearer")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 401)
                self.assertEqual(json.loads(exc.read())["error"], "invalid_api_key")
            # Correct token -> 200
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/chat/completions",
                data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer sekrit"})
            r = urllib.request.urlopen(req2, timeout=10)
            self.assertEqual(r.status, 200)
        finally:
            httpd.shutdown(); httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

