#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""RED tests for Ollama + Anthropic API compatibility.

Ollama API:
  * GET /api/tags                     -> list of local models
  * POST /api/chat                    -> non-streamed chat
  * POST /api/chat (stream=true)      -> newline-delimited JSON chat chunks
  * POST /api/generate                -> non-streamed text completion
  * POST /api/generate (stream=true)  -> newline-delimited JSON

Anthropic Messages API:
  * POST /v1/messages                 -> {type, role, content, model, ...}
  * POST /v1/messages (stream=true)    -> event-stream of content_block_delta etc.
  * shape: system is a top-level field, not in messages;
           content is a list of typed blocks;
           model id is bare (e.g. "claude-3-5-sonnet").
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mycelium_openai_shim.translator import (  # noqa: E402
    default_tokenizer, default_detokenizer, TranslateError)
from mycelium_openai_shim.server import OpenAIShimHandler  # noqa: E402


# ----------------------------- Ollama -----------------------------

class OllamaTranslatorTests(unittest.TestCase):

    def test_ollama_chat_response_shape(self):
        from mycelium_openai_shim.ollama import to_ollama_chat_response
        doc = to_ollama_chat_response(
            request_id="req-1", model="mycelium-qwen-7b",
            token_ids=(10, 11, 12), prompt_tokens=5,
            created_at_unix=1700000000)
        self.assertEqual(doc["model"], "mycelium-qwen-7b")
        self.assertEqual(doc["created_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(doc["message"]["role"], "assistant")
        self.assertIsInstance(doc["message"]["content"], str)
        self.assertGreater(len(doc["message"]["content"]), 0)
        self.assertTrue(doc["done"])
        self.assertIn("total_duration", doc)
        self.assertIn("eval_count", doc)

    def test_ollama_chat_chunk_shape(self):
        from mycelium_openai_shim.ollama import to_ollama_chat_chunk
        chunk = to_ollama_chat_chunk(
            request_id="req-1", model="mycelium-qwen-7b",
            token_id=10, token_index=0)
        self.assertEqual(chunk["model"], "mycelium-qwen-7b")
        self.assertEqual(chunk["message"]["role"], "assistant")
        self.assertIsInstance(chunk["message"]["content"], str)
        self.assertFalse(chunk["done"])
        final = to_ollama_chat_chunk(
            request_id="req-1", model="mycelium-qwen-7b",
            token_id=0, token_index=5, done=True)
        self.assertTrue(final["done"])

    def test_ollama_generate_response_shape(self):
        from mycelium_openai_shim.ollama import to_ollama_generate_response
        doc = to_ollama_generate_response(
            model="mycelium-qwen-7b", token_ids=(1, 2, 3),
            prompt_tokens=4, created_at_unix=1700000000)
        self.assertEqual(doc["model"], "mycelium-qwen-7b")
        self.assertIsInstance(doc["response"], str)
        self.assertTrue(doc["done"])
        self.assertEqual(doc["eval_count"], 3)

    def test_ollama_tags_response_shape(self):
        from mycelium_openai_shim.ollama import build_ollama_tags_response
        doc = build_ollama_tags_response(
            model_id="mycelium-qwen-7b", name="Mycelium: Qwen2.5 7B",
            family="qwen2", parameter_size="7B", quantization_level="int8")
        self.assertEqual(doc["models"][0]["name"], "mycelium-qwen-7b")
        self.assertEqual(doc["models"][0]["family"], "qwen2")
        self.assertEqual(doc["models"][0]["parameter_size"], "7B")
        self.assertEqual(doc["models"][0]["quantization_level"], "int8")

    def test_ollama_request_translation(self):
        from mycelium_openai_shim.ollama import from_ollama_chat_request
        body = {
            "model": "mycelium-qwen-7b",
            "messages": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
                {"role": "user", "content": "Bye"},
            ],
            "stream": True,
        }
        req = from_ollama_chat_request(body, request_id="r1")
        self.assertIn("Hi", req["prompt_text"])
        self.assertIn("Hello!", req["prompt_text"])
        self.assertIn("Bye", req["prompt_text"])
        self.assertTrue(req["stream"])

    def test_ollama_request_translation_rejects_missing_messages(self):
        from mycelium_openai_shim.ollama import from_ollama_chat_request
        with self.assertRaises(TranslateError):
            from_ollama_chat_request({"model": "x"}, request_id="r1")


# ----------------------------- Anthropic -----------------------------

class AnthropicTranslatorTests(unittest.TestCase):

    def test_anthropic_response_shape(self):
        from mycelium_openai_shim.anthropic import to_anthropic_message_response
        doc = to_anthropic_message_response(
            request_id="req-abc", model="claude-3-5-sonnet-mock",
            token_ids=(10, 11, 12), prompt_tokens=5,
            stop_reason="end_turn")
        self.assertEqual(doc["type"], "message")
        self.assertEqual(doc["role"], "assistant")
        self.assertEqual(doc["model"], "claude-3-5-sonnet-mock")
        self.assertEqual(doc["stop_reason"], "end_turn")
        self.assertEqual(doc["stop_sequence"], None)
        # Content is a list of typed blocks (text block here).
        self.assertEqual(doc["content"][0]["type"], "text")
        self.assertIsInstance(doc["content"][0]["text"], str)
        # Usage.
        self.assertEqual(doc["usage"]["input_tokens"], 5)
        self.assertEqual(doc["usage"]["output_tokens"], 3)

    def test_anthropic_chunk_event_sequence(self):
        from mycelium_openai_shim.anthropic import (
            build_anthropic_stream_events, ANTHROPIC_EVENT_TYPES)
        events = build_anthropic_stream_events(
            request_id="req-abc", model="claude-3-5-sonnet-mock",
            token_ids=(10, 11, 12), stop_reason="end_turn")
        types = [e["type"] for e in events]
        # Must open with message_start, then content_block_start,
        # then 3 content_block_delta, then content_block_stop,
        # then message_delta (with stop_reason), then message_stop.
        self.assertEqual(types[0], "message_start")
        self.assertEqual(types[1], "content_block_start")
        self.assertEqual(types[2], "ping")  # optional but expected
        deltas = [t for t in types if t == "content_block_delta"]
        self.assertEqual(len(deltas), 3)
        self.assertIn("message_delta", types)
        self.assertEqual(types[-1], "message_stop")
        # Every event carries the required keys for its type.
        self.assertEqual(events[0]["message"]["id"], "msg_req-abc")

    def test_anthropic_request_translation(self):
        from mycelium_openai_shim.anthropic import from_anthropic_request
        body = {
            "model": "claude-3-5-sonnet-mock",
            "system": "You are concise.",
            "messages": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
                {"role": "user", "content": "Bye"},
            ],
            "max_tokens": 64,
        }
        req = from_anthropic_request(body, request_id="r1")
        self.assertIn("You are concise.", req["prompt_text"])
        self.assertIn("Hi", req["prompt_text"])
        self.assertIn("Hello!", req["prompt_text"])
        self.assertIn("Bye", req["prompt_text"])
        self.assertEqual(req["max_new_tokens"], 64)
        # The model id passes through verbatim (Anthropic uses bare names).
        self.assertEqual(req["model"], "claude-3-5-sonnet-mock")

    def test_anthropic_content_blocks_as_string(self):
        from mycelium_openai_shim.anthropic import from_anthropic_request
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "plain"}],
            "max_tokens": 8,
        }
        req = from_anthropic_request(body, request_id="r1")
        self.assertIn("plain", req["prompt_text"])

    def test_anthropic_request_translation_rejects_missing_messages(self):
        from mycelium_openai_shim.anthropic import from_anthropic_request
        with self.assertRaises(TranslateError):
            from_anthropic_request({"model": "x", "max_tokens": 8}, request_id="r1")





class _StubRoute:
    def infer(self, *, token_ids, max_new_tokens, request_id, sink, **_):
        for i, tid in enumerate((400, 401, 402)):
            sink.emit(i, tid)


class _ServerThread(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.httpd.daemon_threads = True

    def run(self):
        self.httpd.serve_forever()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _start_server():
    from mycelium_openai_shim.server import _make_handler, _ThreadingServer
    handler = _make_handler(
        _StubRoute(), default_tokenizer(), default_detokenizer(),
        model_id="mycelium-qwen-7b", model_name="Mycelium: Qwen2.5 7B",
        context_length=32768, provider="Mycelium", api_token="",
        # Ollama-config attrs via class-attr mutation (not in _make_handler yet).
    )
    # Inject the Ollama config slots.
    handler.ollama_model_id = "mycelium-qwen-7b"
    handler.ollama_model_name = "Mycelium: Qwen2.5 7B"
    handler.ollama_family = "qwen2"
    handler.ollama_parameter_size = "7B"
    handler.ollama_quantization_level = "int8"
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    httpd = _ThreadingServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, t, port


class OllamaServerTests(unittest.TestCase):

    def test_tags_endpoint(self):
        httpd, t, port = _start_server()
        try:
            r = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/tags", timeout=5)
            doc = json.loads(r.read())
            self.assertEqual(doc["models"][0]["name"], "mycelium-qwen-7b")
            self.assertEqual(doc["models"][0]["family"], "qwen2")
            self.assertEqual(doc["models"][0]["parameter_size"], "7B")
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_chat_endpoint(self):
        httpd, t, port = _start_server()
        try:
            body = json.dumps({
                "model": "mycelium-qwen-7b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/chat", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=5)
            doc = json.loads(r.read())
            self.assertEqual(doc["model"], "mycelium-qwen-7b")
            self.assertEqual(doc["message"]["role"], "assistant")
            self.assertTrue(doc["done"])
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_generate_endpoint(self):
        httpd, t, port = _start_server()
        try:
            body = json.dumps({
                "model": "mycelium-qwen-7b",
                "prompt": "say hi",
                "stream": False,
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/generate", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=5)
            doc = json.loads(r.read())
            self.assertTrue(doc["done"])
            self.assertIsInstance(doc["response"], str)
        finally:
            httpd.shutdown(); httpd.server_close()


class AnthropicServerTests(unittest.TestCase):

    def test_messages_endpoint(self):
        httpd, t, port = _start_server()
        try:
            body = json.dumps({
                "model": "claude-3-5-sonnet-mock",
                "system": "You are concise.",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/messages", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=5)
            doc = json.loads(r.read())
            self.assertEqual(doc["type"], "message")
            self.assertEqual(doc["role"], "assistant")
            self.assertEqual(doc["stop_reason"], "end_turn")
            self.assertEqual(doc["content"][0]["type"], "text")
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_messages_streaming_event_sequence(self):
        httpd, t, port = _start_server()
        try:
            body = json.dumps({
                "model": "claude-3-5-sonnet-mock",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
                "stream": True,
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/messages", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=5)
            self.assertEqual(r.headers["Content-Type"], "text/event-stream")
            raw = r.read().decode()
            # Each event is `event: TYPE\ndata: JSON\n\n`.
            events = []
            for chunk in raw.split("\n\n"):
                if chunk.startswith("event: "):
                    ev_line = chunk.split("\n", 1)[0]
                    data_line = chunk.split("data: ", 1)[1] if "\ndata: " in chunk else ""
                    events.append((ev_line[len("event: "):], data_line))
            types = [t for t, _ in events]
            self.assertEqual(types[0], "message_start")
            self.assertIn("content_block_start", types)
            self.assertIn("ping", types)
            deltas = [t for t in types if t == "content_block_delta"]
            self.assertEqual(len(deltas), 3)
            self.assertIn("message_delta", types)
            self.assertEqual(types[-1], "message_stop")
        finally:
            httpd.shutdown(); httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

