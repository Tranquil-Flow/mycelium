# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_router.llama_runtime — llama.cpp-backed RuntimePort.

Wraps the llama-server OpenAI-compatible HTTP endpoint (from the llama.cpp
distribution; binary lives at /opt/homebrew/bin/llama-server). Implements the
same RuntimePort protocol as mycelium_router.mlx_runtime:

  * decode_mode: str
  * execute(item: HopWorkItem) -> RuntimeResult
  * execute_batch(batch: RuntimeBatch) -> tuple[RuntimeResult, ...]
  * cancel(path_id: str) -> None

The adapter is a THIN HTTP client — no model loading here. llama-server does
the actual compute. This means:

  * Any GGUF model llama.cpp can load, this adapter can serve.
  * The adapter lives anywhere the router lives; llama-server runs on the
    same host (or any reachable host, since it's plain HTTP).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from mycelium_router.contracts import HopWorkItem, RuntimeBatch, RuntimeResult

# The llama-server /completion endpoint. We use /completion (not /v1/chat/
# completions) because it returns raw token ids, which Mycelium's wire format
# needs.
COMPLETION_PATH = "/completion"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 32


class LlamaServerError(RuntimeError):
    """A llama-server request failed at the HTTP layer or returned garbage."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class _CompletionRequest:
    """The body we send to llama-server."""
    prompt: str
    n_predict: int
    temperature: float = 0.0
    seed: int = 0
    stream: bool = False


def llama_request_from_hop(item: HopWorkItem, *, max_tokens: int) -> dict[str, Any]:
    """Shape a HopWorkItem into a llama-server /completion request body.

    The hop payload is the prompt text (str). We set n_predict to the caller's
    max_tokens and pin greedy decoding (temperature=0) for reproducibility.
    """
    if not isinstance(item.payload, str):
        raise LlamaServerError(f"llama_runtime: payload must be str, got "
                               f"{type(item.payload).__name__}")
    return {
        "prompt": item.payload,
        "n_predict": int(max_tokens),
        "temperature": 0.0,
        "seed": 0,
        "stream": False,
    }


def parse_llama_completion(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Parse a llama-server /completion response into a plain dict."""
    if "content" not in doc or "tokens" not in doc:
        raise LlamaServerError(f"llama-server returned unexpected shape: "
                               f"{list(doc)[:5]}")
    tokens = doc.get("tokens", [])
    token_ids = [t.get("id") for t in tokens if isinstance(t, Mapping)]
    token_ids = [t for t in token_ids if isinstance(t, int)]
    if not token_ids:
        raise LlamaServerError("llama-server returned no token ids")
    return {
        "text": doc.get("content", ""),
        "token_ids": token_ids,
        "stopped_eos": bool(doc.get("stopped_eos", False)),
        "predicted_ms": float(doc.get("timings", {}).get("predicted_ms", 0.0)),
    }


class LlamaRuntimePort:
    """A RuntimePort backed by llama-server over HTTP."""

    decode_mode = "llama_http"

    def __init__(self, *, server_url: str, model_id: str,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.server_url = server_url.rstrip("/")
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    def _post_completion(self, body: Mapping[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.server_url}{COMPLETION_PATH}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.URLError as exc:
            raise LlamaServerError(f"llama-server unreachable: {exc}") from exc
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LlamaServerError(f"llama-server returned invalid JSON: {raw[:200]}") from exc
        return doc

    def execute(self, item: HopWorkItem) -> RuntimeResult:
        body = llama_request_from_hop(item, max_tokens=self.max_tokens)
        try:
            doc = self._post_completion(body)
            parsed = parse_llama_completion(doc)
        except LlamaServerError as exc:
            return RuntimeResult(
                success=False, payload=None, token_id=None,
                failure_scope="llama_server", failure_reason=str(exc))
        # The last token is the one this hop contributes. If the caller set
        # emits_token on the item, this is the streamed token.
        token_id = parsed["token_ids"][-1] if parsed["token_ids"] else None
        return RuntimeResult(
            success=True, payload=parsed["text"], token_id=token_id,
            failure_scope="", failure_reason="")

    def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]:
        # llama-server has a batch endpoint, but the simple/robust path is
        # one /completion per item. Batch-aware scheduling is A10's job.
        return tuple(self.execute(item) for item in batch.items)

    def cancel(self, path_id: str) -> None:
        # llama-server /completion is request-scoped and synchronous; there is
        # no in-flight cancel surface. A no-op here is honest: the next hop
        # simply won't be issued. Documented.
        return


__all__ = ["LlamaRuntimePort", "LlamaServerError",
           "llama_request_from_hop", "parse_llama_completion"]
