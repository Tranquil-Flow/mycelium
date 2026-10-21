#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""RED tests for mycelium_openai_shim — translates OpenAI Chat Completions
requests to LiveRoute.infer calls and emits OpenAI-format responses.

The shim is a pure adapter: it lives in front of `LiveRoute` (the Mycelium
inference API). Zero product change — no shipping code is modified, only
new code is added.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mycelium_openai_shim.translator import (  # noqa: E402
    TranslateError,
    to_openai_chat_response,
    to_openai_chat_chunk,
    to_mycelium_infer_request,
    build_openai_models_response,
)


class RequestTranslationTests(unittest.TestCase):

    def test_simple_user_message_to_infer_request(self):
        body = {
            "model": "mycelium-qwen-7b",
            "messages": [
                {"role": "user", "content": "Hello, world."},
            ],
            "max_tokens": 32,
            "stream": False,
        }
        req = to_mycelium_infer_request(body, request_id="req-abc")
        # Token IDs are integers (we use a stub tokenizer for tests).
        self.assertIsInstance(req["token_ids"], list)
        self.assertEqual(req["max_new_tokens"], 32)
        self.assertEqual(req["request_id"], "req-abc")
        self.assertFalse(req["stream"])

    def test_multi_message_concatenates_user_and_assistant(self):
        body = {
            "model": "mycelium-qwen-7b",
            "messages": [
                {"role": "system", "content": "You are concise."},
                {"role": "user", "content": "Hi."},
                {"role": "assistant", "content": "Hello!"},
                {"role": "user", "content": "Bye."},
            ],
            "max_tokens": 10,
        }
        req = to_mycelium_infer_request(body, request_id="req-1")
        # prompt_text is what we'll tokenize; just verify it has the right structure.
        self.assertIn("prompt_text", req)
        # User and final user are present; assistant echo is present.
        self.assertIn("Hi.", req["prompt_text"])
        self.assertIn("Bye.", req["prompt_text"])
        self.assertIn("Hello!", req["prompt_text"])

    def test_missing_messages_raises_translate_error(self):
        with self.assertRaises(TranslateError):
            to_mycelium_infer_request({"model": "x", "max_tokens": 1}, request_id="r")

    def test_missing_model_defaults_to_mycelium_default(self):
        body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
        req = to_mycelium_infer_request(body, request_id="r")
        self.assertEqual(req["model"], "mycelium-default")

    def test_temperature_is_ignored_with_documented_warning(self):
        # Mycelium uses greedy decoding today; temperature is documented as
        # accepted-but-ignored so OpenAI clients don't error.
        body = {"messages": [{"role": "user", "content": "x"}], "temperature": 0.7}
        req = to_mycelium_infer_request(body, request_id="r")
        self.assertIn("temperature", req["warnings"])

    def test_stream_true_sets_flag(self):
        body = {"messages": [{"role": "user", "content": "x"}], "stream": True}
        req = to_mycelium_infer_request(body, request_id="r")
        self.assertTrue(req["stream"])


class ResponseTranslationTests(unittest.TestCase):

    def test_non_streaming_response_shape(self):
        result = to_openai_chat_response(
            request_id="req-abc",
            model="mycelium-qwen-7b",
            token_ids=(10, 11, 12, 13, 14),
            prompt_tokens=5,
        )
        self.assertEqual(result["id"], "chatcmpl-req-abc")
        self.assertEqual(result["object"], "chat.completion")
        self.assertEqual(result["model"], "mycelium-qwen-7b")
        self.assertEqual(result["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(result["usage"]["prompt_tokens"], 5)
        self.assertEqual(result["usage"]["completion_tokens"], 5)
        self.assertEqual(result["usage"]["total_tokens"], 10)

    def test_streaming_chunk_shape(self):
        chunk = to_openai_chat_chunk(
            request_id="req-abc", model="mycelium-qwen-7b",
            token_id=10, token_index=0, finish_reason=None)
        self.assertEqual(chunk["object"], "chat.completion.chunk")
        delta = chunk["choices"][0]["delta"]
        self.assertIn("content", delta)
        self.assertIsInstance(delta["content"], str)
        self.assertIsNone(chunk["choices"][0]["finish_reason"])

    def test_streaming_final_chunk_has_finish_reason(self):
        chunk = to_openai_chat_chunk(
            request_id="req-abc", model="mycelium-qwen-7b",
            token_id=99, token_index=5, finish_reason="stop")
        self.assertEqual(chunk["choices"][0]["finish_reason"], "stop")

    def test_models_response_lists_the_current_model(self):
        resp = build_openai_models_response(current_model="mycelium-qwen-7b")
        self.assertEqual(resp["object"], "list")
        self.assertEqual(resp["data"][0]["id"], "mycelium-qwen-7b")
        self.assertEqual(resp["data"][0]["object"], "model")


class ShimContractTests(unittest.TestCase):

    def test_shim_uses_synchronous_route_when_stream_false(self):
        # The shim collects all emitted token_ids from a TokenSink and builds
        # a single non-streaming response. The actual route call is exercised
        # in live tests (out of scope for unit tests).
        from mycelium_openai_shim.translator import SyncSink
        sink = SyncSink()
        for i in range(5):
            sink.emit(i, 100 + i)
        self.assertEqual(tuple(sink.token_ids), (100, 101, 102, 103, 104))

    def test_shim_streaming_collects_sse_events(self):
        from mycelium_openai_shim.translator import StreamingSink
        sink = StreamingSink(request_id="r", model="m")
        for i in range(3):
            sink.emit(i, 50 + i)
        sink.finish()
        chunks = list(sink.drain())
        # 1 chunk per token + 1 final stop chunk
        self.assertEqual(len(chunks), 4)
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
