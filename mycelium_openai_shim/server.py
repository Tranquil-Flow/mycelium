# SPDX-License-Identifier: AGPL-3.0-or-later
"""mycelium_openai_shim.server — stdlib HTTP server in front of LiveRoute.

Endpoints:
  POST /v1/chat/completions   -> LiveRoute.infer (streaming + non-streaming)
  GET  /v1/models              -> static model list
  GET  /healthz                -> liveness

Zero non-stdlib deps. Wire it up like:

    from mycelium_openai_shim.server import serve
    serve(live_route=route, tokenizer=qwen_bpe, port=8791)
"""
from __future__ import annotations

import json
import threading
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


class OpenAIShimHandler(BaseHTTPRequestHandler):
    """One HTTP request → one LiveRoute.infer call (or one streamed call)."""

    # Subclass sets these.
    live_route: Any
    tokenizer: Any
    detokenizer: Any
    model_id: str
    model_name: str
    context_length: int
    provider: str
    api_token: str

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif path in ("/v1/models", "/api/v1/models"):
            self._send_json(200, build_openrouter_models_response(
                model_id=self.model_id, name=self.model_name,
                context_length=self.context_length, provider=self.provider))
        else:
            self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/api/v1/chat/completions"):
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
        try:
            translator = from_openrouter_request if path.startswith("/api/") else to_mycelium_infer_request
            req = translator(
                body, request_id=uuid.uuid4().hex,
                tokenizer=self.tokenizer, model_id=self.model_id)
        except TranslateError as exc:
            self._send_json(400, {"error": "invalid_request", "detail": str(exc)})
            return

        openrouter = path.startswith("/api/")
        if req["stream"]:
            self._stream(req, openrouter=openrouter)
        else:
            self._non_stream(req, openrouter=openrouter)

    def _non_stream(self, req: Mapping[str, Any], *, openrouter: bool = False) -> None:
        sink = SyncSink()
        try:
            self.live_route.infer(
                token_ids=req["token_ids"],
                max_new_tokens=req["max_new_tokens"],
                request_id=req["request_id"],
                sink=sink,
            )
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
                request_id=req["request_id"],
                model=req["model"],
                token_ids=sink.token_ids,
                prompt_tokens=len(req["token_ids"]),
                detokenizer=self.detokenizer,
            )
        self._send_json(200, response)

    def _stream(self, req: Mapping[str, Any], *, openrouter: bool = False) -> None:
        sink = StreamingSink(req["request_id"], req["model"], self.detokenizer,
                             chunk_builder=(to_openrouter_chat_chunk if openrouter else None))
        try:
            self.live_route.infer(
                token_ids=req["token_ids"],
                max_new_tokens=req["max_new_tokens"],
                request_id=req["request_id"],
                sink=sink,
            )
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
                  provider: str = "Mycelium", api_token: str = ""):
    """Bind config into a fresh handler subclass at call time."""
    class _Bound(OpenAIShimHandler):
        pass
    # Functions assigned as class attributes become bound methods when
    # accessed via an instance — use staticmethod to keep them plain callables.
    _Bound.live_route = live_route
    _Bound.tokenizer = staticmethod(tokenizer) if callable(tokenizer) else tokenizer
    _Bound.detokenizer = staticmethod(detokenizer) if callable(detokenizer) else detokenizer
    _Bound.model_id = model_id
    _Bound.model_name = model_name or f"Mycelium: {model_id}"
    _Bound.context_length = context_length
    _Bound.provider = provider
    _Bound.api_token = api_token
    return _Bound


def serve(*, live_route: Any, host: str = "127.0.0.1", port: int = 8791,
          tokenizer: Any = None, detokenizer: Any = None,
          model_id: str = "mycelium-default", model_name: str | None = None,
          context_length: int = 32768, provider: str = "Mycelium",
          api_token: str = ""):
    """Block until the server is interrupted."""
    handler = _make_handler(
        live_route, tokenizer or default_tokenizer(),
        detokenizer or default_detokenizer(), model_id,
        model_name=model_name, context_length=context_length,
        provider=provider, api_token=api_token)
    server = _ThreadingServer((host, port), handler)
    print(f"[openai-shim] listening on http://{host}:{port}/v1/chat/completions "
          f"(model={model_id})", flush=True)
    server.serve_forever()


def serve_in_thread(*, live_route: Any, host: str = "127.0.0.1", port: int = 8791,
                    tokenizer: Any = None, detokenizer: Any = None,
                    model_id: str = "mycelium-default",
                    model_name: str | None = None, context_length: int = 32768,
                    provider: str = "Mycelium", api_token: str = ""):
    """Start the shim in a daemon thread. Returns (server, thread)."""
    handler = _make_handler(
        live_route, tokenizer or default_tokenizer(),
        detokenizer or default_detokenizer(), model_id,
        model_name=model_name, context_length=context_length,
        provider=provider, api_token=api_token)
    server = _ThreadingServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


__all__ = ["OpenAIShimHandler", "serve", "serve_in_thread"]
