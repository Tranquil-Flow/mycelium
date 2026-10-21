# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_openai_shim.server — stdlib HTTP server in front of LiveRoute.

Serves four API surfaces from one process (no non-stdlib deps):

  OpenAI        POST /v1/chat/completions      GET /v1/models
  OpenRouter    POST /api/v1/chat/completions  GET /api/v1/models
  Anthropic     POST /v1/messages
  Ollama        POST /api/chat  POST /api/generate  GET /api/tags
  liveness      GET /healthz

All POST surfaces drive the SAME LiveRoute.infer call. Streaming responses are
built after inference completes (buffered-then-emit): per-token flush is a later
refinement and does not change the wire format.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from .translator import (
    SyncSink,
    StreamingSink,
    TranslateError,
    build_openai_models_response,
    default_detokenizer,
    default_tokenizer,
    to_mycelium_infer_request,
    to_openai_chat_chunk,
    to_openai_chat_response,
)
from .openrouter import (
    build_openrouter_models_response,
    from_openrouter_request,
    to_openrouter_chat_chunk,
    to_openrouter_chat_response,
)
from .ollama import (
    build_ollama_tags_response,
    from_ollama_chat_request,
    from_ollama_generate_request,
    to_ollama_chat_chunk,
    to_ollama_chat_response,
    to_ollama_generate_response,
)
from .anthropic import (
    build_anthropic_stream_events,
    from_anthropic_request,
    to_anthropic_message_response,
)


class OpenAIShimHandler(BaseHTTPRequestHandler):
    """One HTTP request -> one LiveRoute.infer call (or one streamed call)."""

    # Subclass/instance config slots (bound by _make_handler).
    live_route: Any
    tokenizer: Any
    detokenizer: Any
    model_id: str
    model_name: str
    context_length: int
    provider: str
    api_token: str
    ollama_model_id: str
    ollama_model_name: str
    ollama_family: str
    ollama_parameter_size: str
    ollama_quantization_level: str

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # ---------------- GET ----------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif path in ("/v1/models", "/api/v1/models"):
            self._send_json(200, build_openrouter_models_response(
                model_id=self.model_id, name=self.model_name,
                context_length=self.context_length, provider=self.provider))
        elif path == "/api/tags":
            self._send_json(200, build_ollama_tags_response(
                model_id=self.ollama_model_id, name=self.ollama_model_name,
                family=self.ollama_family,
                parameter_size=self.ollama_parameter_size,
                quantization_level=self.ollama_quantization_level))
        else:
            self._send_json(404, {"error": "not_found", "path": path})

    # ---------------- POST routing ----------------

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/api/v1/chat/completions",
                        "/v1/messages", "/api/chat", "/api/generate"):
            self._send_json(404, {"error": "not_found", "path": path})
            return
        if self.api_token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {self.api_token}":
                self._send_json(401, {"error": "invalid_api_key"})
                return
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": "invalid_json", "detail": str(exc)})
            return

        if path == "/v1/messages":
            self._anthropic_messages(body)
            return
        if path == "/api/generate":
            self._ollama_generate(body)
            return
        if path == "/api/chat":
            self._ollama_chat(body, stream=bool(body.get("stream", False)))
            return

        # OpenAI / OpenRouter chat completions.
        try:
            translator = (from_openrouter_request
                          if path.startswith("/api/v1/chat")
                          else to_mycelium_infer_request)
            req = translator(
                body, request_id=uuid.uuid4().hex,
                tokenizer=self.tokenizer, model_id=self.model_id)
        except TranslateError as exc:
            self._send_json(400, {"error": "invalid_request", "detail": str(exc)})
            return
        openrouter = path.startswith("/api/v1/chat")
        if req["stream"]:
            self._stream(req, openrouter=openrouter)
        else:
            self._non_stream(req, openrouter=openrouter)

    # ---------------- OpenAI / OpenRouter ----------------

    def _non_stream(self, req: Mapping[str, Any], *, openrouter: bool = False) -> None:
        sink = SyncSink()
        try:
            self.live_route.infer(
                token_ids=req["token_ids"],
                max_new_tokens=req["max_new_tokens"],
                request_id=req["request_id"], sink=sink)
        except Exception as exc:
            self._send_json(500, {"error": "infer_failed", "detail": str(exc)})
            return
        if openrouter:
            response = to_openrouter_chat_response(
                request_id=req["request_id"], model=req["model"],
                token_ids=sink.token_ids, prompt_tokens=len(req["token_ids"]),
                provider=self.provider, detokenizer=self.detokenizer)
        else:
            response = to_openai_chat_response(
                request_id=req["request_id"], model=req["model"],
                token_ids=sink.token_ids, prompt_tokens=len(req["token_ids"]),
                detokenizer=self.detokenizer)
        self._send_json(200, response)

    def _stream(self, req: Mapping[str, Any], *, openrouter: bool = False) -> None:
        chunk_builder = (to_openrouter_chat_chunk if openrouter
                         else to_openai_chat_chunk)
        sink = StreamingSink(req["request_id"], req["model"], self.detokenizer,
                             chunk_builder=chunk_builder)
        try:
            self.live_route.infer(
                token_ids=req["token_ids"],
                max_new_tokens=req["max_new_tokens"],
                request_id=req["request_id"], sink=sink)
            sink.finish()
        except Exception as exc:
            self._send_sse_error(exc)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for chunk in sink.drain():
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except BrokenPipeError:
            return

    # ---------------- Anthropic Messages ----------------

    def _anthropic_messages(self, body: Mapping[str, Any]) -> None:
        try:
            req = from_anthropic_request(body, request_id=uuid.uuid4().hex)
        except TranslateError as exc:
            self._send_json(400, {"type": "error", "error": {
                "type": "invalid_request_error", "message": str(exc)}})
            return
        token_ids = list(self.tokenizer(req["prompt_text"]))
        if req["stream"]:
            sink = SyncSink()
            try:
                self.live_route.infer(
                    token_ids=token_ids,
                    max_new_tokens=req["max_new_tokens"],
                    request_id=req["request_id"], sink=sink)
            except Exception as exc:
                self._send_json(500, {"type": "error", "error": {
                    "type": "infer_failed", "message": str(exc)}})
                return
            events = build_anthropic_stream_events(
                request_id=req["request_id"], model=req["model"],
                token_ids=sink.token_ids, detokenizer=self.detokenizer)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for evt in events:
                    evt_type = evt.get("type", "message")
                    self.wfile.write(
                        f"event: {evt_type}\ndata: {json.dumps(evt)}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except BrokenPipeError:
                return
        else:
            sink = SyncSink()
            try:
                self.live_route.infer(
                    token_ids=token_ids,
                    max_new_tokens=req["max_new_tokens"],
                    request_id=req["request_id"], sink=sink)
            except Exception as exc:
                self._send_json(500, {"type": "error", "error": {
                    "type": "infer_failed", "message": str(exc)}})
                return
            response = to_anthropic_message_response(
                request_id=req["request_id"], model=req["model"],
                token_ids=sink.token_ids, prompt_tokens=len(token_ids),
                detokenizer=self.detokenizer)
            self._send_json(200, response)

    # ---------------- Ollama /api/chat ----------------

    def _ollama_chat(self, body: Mapping[str, Any], *, stream: bool) -> None:
        try:
            req = from_ollama_chat_request(body, request_id=uuid.uuid4().hex)
        except TranslateError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        token_ids = list(self.tokenizer(req["prompt_text"]))
        sink = SyncSink()
        try:
            self.live_route.infer(
                token_ids=token_ids,
                max_new_tokens=req["max_new_tokens"],
                request_id=req["request_id"], sink=sink)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return
        created = int(time.time())
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for i, tid in enumerate(sink.token_ids):
                    chunk = to_ollama_chat_chunk(
                        request_id=req["request_id"], model=req["model"],
                        token_id=tid, token_index=i, done=False,
                        detokenizer=self.detokenizer, created_at_unix=created)
                    self.wfile.write((json.dumps(chunk) + "\n").encode("utf-8"))
                    self.wfile.flush()
                final = to_ollama_chat_chunk(
                    request_id=req["request_id"], model=req["model"],
                    token_id=0, token_index=len(sink.token_ids), done=True,
                    detokenizer=self.detokenizer, created_at_unix=created)
                self.wfile.write((json.dumps(final) + "\n").encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                return
        else:
            response = to_ollama_chat_response(
                request_id=req["request_id"], model=req["model"],
                token_ids=sink.token_ids, prompt_tokens=len(token_ids),
                created_at_unix=created, detokenizer=self.detokenizer)
            self._send_json(200, response)

    # ---------------- Ollama /api/generate ----------------

    def _ollama_generate(self, body: Mapping[str, Any]) -> None:
        try:
            req = from_ollama_generate_request(body, request_id=uuid.uuid4().hex)
        except TranslateError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        token_ids = list(self.tokenizer(req["prompt_text"]))
        sink = SyncSink()
        try:
            self.live_route.infer(
                token_ids=token_ids,
                max_new_tokens=req["max_new_tokens"],
                request_id=req["request_id"], sink=sink)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return
        response = to_ollama_generate_response(
            model=req["model"], token_ids=sink.token_ids,
            prompt_tokens=len(token_ids), created_at_unix=int(time.time()),
            detokenizer=self.detokenizer)
        self._send_json(200, response)

    # ---------------- plumbing ----------------

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty body")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not valid JSON: {exc}") from exc

    def _send_json(self, status: int, body: Mapping[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            return

    def _send_sse_error(self, exc: Exception) -> None:
        payload = json.dumps({"error": "infer_failed", "detail": str(exc)})
        try:
            self.wfile.write(f"event: error\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
        except BrokenPipeError:
            return


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(live_route: Any, tokenizer: Any, detokenizer: Any, model_id: str,
                  model_name: str | None = None, context_length: int = 32768,
                  provider: str = "Mycelium", api_token: str = "",
                  ollama_model_id: str | None = None,
                  ollama_model_name: str | None = None,
                  ollama_family: str = "mycelium",
                  ollama_parameter_size: str = "7B",
                  ollama_quantization_level: str = "int8"):
    """Bind config into a fresh handler subclass at call time.

    Functions assigned as class attributes become bound methods when accessed
    via an instance — use staticmethod to keep them plain callables.
    """
    class _Bound(OpenAIShimHandler):
        pass
    _Bound.live_route = live_route
    _Bound.tokenizer = staticmethod(tokenizer) if callable(tokenizer) else tokenizer
    _Bound.detokenizer = staticmethod(detokenizer) if callable(detokenizer) else detokenizer
    _Bound.model_id = model_id
    _Bound.model_name = model_name or f"Mycelium: {model_id}"
    _Bound.context_length = context_length
    _Bound.provider = provider
    _Bound.api_token = api_token
    _Bound.ollama_model_id = ollama_model_id or model_id
    _Bound.ollama_model_name = ollama_model_name or f"Mycelium: {model_id}"
    _Bound.ollama_family = ollama_family
    _Bound.ollama_parameter_size = ollama_parameter_size
    _Bound.ollama_quantization_level = ollama_quantization_level
    return _Bound


def serve(*, live_route: Any, host: str = "127.0.0.1", port: int = 8791,
          tokenizer: Any = None, detokenizer: Any = None,
          tokenizer_path: str | None = None,
          model_id: str = "mycelium-default", model_name: str | None = None,
          context_length: int = 32768, provider: str = "Mycelium",
          api_token: str = "", ollama_model_id: str | None = None,
          ollama_model_name: str | None = None, ollama_family: str = "mycelium",
          ollama_parameter_size: str = "7B",
          ollama_quantization_level: str = "int8"):
    """Block until the server is interrupted.

    tokenizer_path, when given, loads the product's REAL Qwen2 tokenizer
    (tokenizer.json in that deployment dir) and uses it for every surface;
    without it the explicitly-labelled test-only char tokenizer is used.
    """
    if tokenizer_path is not None:
        from .qwen_tokenizer import load_qwen_tokenizer
        qwen = load_qwen_tokenizer(tokenizer_path)
        tokenizer, detokenizer = qwen.encode, qwen.decode
    handler = _make_handler(
        live_route, tokenizer or default_tokenizer(),
        detokenizer or default_detokenizer(), model_id,
        model_name=model_name, context_length=context_length,
        provider=provider, api_token=api_token,
        ollama_model_id=ollama_model_id, ollama_model_name=ollama_model_name,
        ollama_family=ollama_family, ollama_parameter_size=ollama_parameter_size,
        ollama_quantization_level=ollama_quantization_level)
    server = _ThreadingServer((host, port), handler)
    print(f"[openai-shim] listening on http://{host}:{port}/v1/chat/completions "
          f"(model={model_id})", flush=True)
    server.serve_forever()


def serve_in_thread(*, live_route: Any, host: str = "127.0.0.1", port: int = 8791,
                    tokenizer: Any = None, detokenizer: Any = None,
                    tokenizer_path: str | None = None,
                    model_id: str = "mycelium-default",
                    model_name: str | None = None, context_length: int = 32768,
                    provider: str = "Mycelium", api_token: str = "",
                    ollama_model_id: str | None = None,
                    ollama_model_name: str | None = None,
                    ollama_family: str = "mycelium",
                    ollama_parameter_size: str = "7B",
                    ollama_quantization_level: str = "int8"):
    """Start the shim in a daemon thread. Returns (server, thread).

    tokenizer_path, when given, loads the product's REAL Qwen2 tokenizer;
    without it the explicitly-labelled test-only char tokenizer is used.
    """
    if tokenizer_path is not None:
        from .qwen_tokenizer import load_qwen_tokenizer
        qwen = load_qwen_tokenizer(tokenizer_path)
        tokenizer, detokenizer = qwen.encode, qwen.decode
    handler = _make_handler(
        live_route, tokenizer or default_tokenizer(),
        detokenizer or default_detokenizer(), model_id,
        model_name=model_name, context_length=context_length,
        provider=provider, api_token=api_token,
        ollama_model_id=ollama_model_id, ollama_model_name=ollama_model_name,
        ollama_family=ollama_family, ollama_parameter_size=ollama_parameter_size,
        ollama_quantization_level=ollama_quantization_level)
    server = _ThreadingServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


__all__ = ["OpenAIShimHandler", "serve", "serve_in_thread"]
