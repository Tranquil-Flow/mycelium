# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_router.openrouter_runtime — RuntimePort backed by OpenRouter.

Treats OpenRouter (https://openrouter.ai/api/v1/chat/completions) as a model
backend for the distributed layer. Implements the same RuntimePort protocol
as mlx_runtime and llama_runtime:

  * execute(item: HopWorkItem) -> RuntimeResult
  * execute_batch(batch: RuntimeBatch) -> tuple[RuntimeResult, ...]
  * cancel(path_id: str) -> None
  * decode_mode: str

The API key comes from OPENROUTER_API_KEY (or an explicit constructor arg).
It is NEVER printed: repr/str redact it, and failure messages never echo it.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping

from mycelium_router.contracts import HopWorkItem, RuntimeBatch, RuntimeResult

DEFAULT_BASE_URL = "https://openrouter.ai"
CHAT_COMPLETIONS_PATH = "/api/v1/chat/completions"
DEFAULT_TIMEOUT_S = 120.0


class OpenRouterError(RuntimeError):
    """An OpenRouter request failed at the HTTP layer or returned garbage."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def openrouter_request_from_hop(item: HopWorkItem, *, model: str,
                                max_tokens: int) -> dict[str, Any]:
    """Shape a HopWorkItem into an OpenRouter chat-completions body.

    The hop payload is the prompt text (str). OpenRouter's API is OpenAI-
    compatible, so the body is the standard messages/max_tokens shape.
    """
    if not isinstance(item.payload, str):
        raise OpenRouterError(f"openrouter_runtime: payload must be str, got "
                              f"{type(item.payload).__name__}")
    return {
        "model": model,
        "messages": [{"role": "user", "content": item.payload}],
        "max_tokens": int(max_tokens),
        "stream": False,
    }


def parse_openrouter_completion(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Parse an OpenRouter chat-completion response into a plain dict."""
    choices = doc.get("choices")
    if not isinstance(choices, list) or not choices:
        raise OpenRouterError(f"openrouter returned no choices: "
                              f"{str(doc)[:120]}")
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if not isinstance(content, str):
        raise OpenRouterError("openrouter message content is not a string")
    usage = doc.get("usage") or {}
    return {
        "text": content,
        "finish_reason": choices[0].get("finish_reason", "stop"),
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
    }


class OpenRouterRuntimePort:
    """RuntimePort over OpenRouter's public chat-completions API."""

    decode_mode = "openrouter_http"

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL,
                 api_key: str | None = None, model: str,
                 max_tokens: int = 256,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self._api_key = api_key if api_key is not None else os.environ.get(
            "OPENROUTER_API_KEY", "")
        if not self._api_key:
            raise OpenRouterError(
                "OPENROUTER_API_KEY is required (export it or pass api_key=)")

    def __repr__(self) -> str:
        return (f"OpenRouterRuntimePort(base_url={self.base_url!r}, "
                f"model={self.model!r}, api_key=<redacted>)")

    __str__ = __repr__

    def _post(self, body: Mapping[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Never echo the key; only the status and a bounded body prefix.
            snippet = exc.read()[:200].decode("utf-8", "replace")
            raise OpenRouterError(
                f"openrouter HTTP {exc.code}: {snippet}") from exc
        except urllib.error.URLError as exc:
            raise OpenRouterError(f"openrouter unreachable: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpenRouterError(
                f"openrouter returned invalid JSON: {raw[:200]}") from exc

    def execute(self, item: HopWorkItem) -> RuntimeResult:
        body = openrouter_request_from_hop(
            item, model=self.model, max_tokens=self.max_tokens)
        try:
            doc = self._post(body)
            parsed = parse_openrouter_completion(doc)
        except OpenRouterError as exc:
            return RuntimeResult(
                success=False, payload=None, token_id=None,
                failure_scope="openrouter", failure_reason=str(exc))
        return RuntimeResult(
            success=True, payload=parsed["text"], token_id=None,
            failure_scope="", failure_reason="")

    def execute_batch(self, batch: RuntimeBatch) -> tuple[RuntimeResult, ...]:
        return tuple(self.execute(item) for item in batch.items)

    def cancel(self, path_id: str) -> None:
        # Request-scoped synchronous HTTP; no in-flight cancel surface.
        return


__all__ = ["OpenRouterRuntimePort", "OpenRouterError",
           "openrouter_request_from_hop", "parse_openrouter_completion"]
