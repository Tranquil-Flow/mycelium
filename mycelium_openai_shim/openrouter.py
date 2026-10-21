# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_openai_shim.openrouter — OpenRouter-specific translation layer.

OpenRouter's API is a strict superset of OpenAI Chat Completions:

  * requests may carry `provider`, `transforms`, `models`, `route` extras —
    accepted, surfaced as warnings, never errors;
  * responses use `gen-...` ids, a `provider` field, and a `usage` block;
  * GET /api/v1/models returns a richer model list (name, context_length,
    pricing, top_provider).

This module reuses the core translator (`to_mycelium_infer_request`) so the
two surfaces share one translation path.
"""
from __future__ import annotations

import time
from typing import Any, Mapping, Sequence

from .translator import (
    TranslateError,
    default_detokenizer,
    to_mycelium_infer_request,
)

# OpenRouter request extras we accept-and-ignore (documented as warnings).
_OPENROUTER_EXTRAS = frozenset({"provider", "transforms", "models", "route"})


def _gen_id(request_id: str) -> str:
    return f"gen-{request_id}"


def _created_unix() -> int:
    return int(time.time())


def from_openrouter_request(openrouter_body: Mapping[str, Any], *,
                            request_id: str, **kwargs: Any) -> dict[str, Any]:
    """Translate an OpenRouter request to a Mycelium-shaped infer request.

    Identical to to_mycelium_infer_request, but additionally:
      * accepts OpenRouter extras (provider/transforms/models/route);
      * appends those extras to the warnings list.
    """
    if not isinstance(openrouter_body, Mapping):
        raise TranslateError("request body must be an object")
    req = to_mycelium_infer_request(
        openrouter_body, request_id=request_id, **kwargs)
    warnings: list[str] = list(req.get("warnings", []))
    for extra in _OPENROUTER_EXTRAS:
        if extra in openrouter_body:
            warnings.append(extra)
    req["warnings"] = warnings
    return req


def to_openrouter_chat_response(
    *,
    request_id: str,
    model: str,
    token_ids: Sequence[int],
    prompt_tokens: int,
    provider: str = "Mycelium",
    detokenizer: Any = None,
) -> dict[str, Any]:
    """OpenRouter-shaped non-streaming chat completion response."""
    detok = detokenizer or default_detokenizer()
    text = detok(token_ids)
    completion_tokens = len(token_ids)
    return {
        "id": _gen_id(request_id),
        "provider": provider,
        "model": model,
        "object": "chat.completion",
        "created": _created_unix(),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def to_openrouter_chat_chunk(
    *,
    request_id: str,
    model: str,
    token_id: int,
    token_index: int,
    finish_reason: str | None,
    provider: str = "Mycelium",
    detokenizer: Any = None,
) -> dict[str, Any]:
    """OpenRouter-shaped streaming chunk."""
    detok = detokenizer or default_detokenizer()
    delta_content: dict[str, Any] = {}
    if finish_reason is None:
        delta_content = {"content": detok([token_id])}
    return {
        "id": _gen_id(request_id),
        "provider": provider,
        "model": model,
        "object": "chat.completion.chunk",
        "created": _created_unix(),
        "choices": [
            {
                "index": 0,
                "delta": delta_content,
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
        "usage": None,
    }


def build_openrouter_models_response(
    *,
    model_id: str,
    name: str,
    context_length: int = 32768,
    description: str = "Mycelium self-hosted distributed inference node.",
    provider: str = "Mycelium",
) -> dict[str, Any]:
    """OpenRouter-shaped GET /api/v1/models response.

    A self-hosted node is zero-priced (the owner already owns the hardware),
    so pricing carries explicit zeroes rather than pretending a market rate.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "name": name,
                "created": _created_unix(),
                "description": description,
                "context_length": int(context_length),
                "architecture": {
                    "modality": "text->text",
                    "tokenizer": "Qwen",
                    "instruct_type": None,
                },
                "pricing": {
                    "prompt": "0",
                    "completion": "0",
                    "image": "0",
                    "request": "0",
                },
                "top_provider": {
                    "context_length": int(context_length),
                    "max_completion_tokens": 4096,
                    "is_moderated": False,
                    "provider_name": provider,
                },
            }
        ],
    }


__all__ = [
    "from_openrouter_request",
    "to_openrouter_chat_response",
    "to_openrouter_chat_chunk",
    "build_openrouter_models_response",
]
