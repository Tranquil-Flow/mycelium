# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_openai_shim.ollama — Ollama-compatible translation layer.

Ollama's API is JSON over HTTP with newline-delimited JSON (NDJSON) for
streaming. Exposes:

  * GET  /api/tags
  * POST /api/chat            (stream=true|false)
  * POST /api/generate        (stream=true|false)

Model names are bare (no namespace prefix); the caller may pass any string.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .translator import (
    TranslateError,
    default_detokenizer,
    to_mycelium_infer_request,
)


# ----------------------------- Format helpers -----------------------------

def _iso8601_z(unix_seconds: int) -> str:
    return datetime.fromtimestamp(int(unix_seconds), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ----------------------------- Response translation -----------------------------

def to_ollama_chat_response(
    *,
    request_id: str,
    model: str,
    token_ids: Sequence[int],
    prompt_tokens: int,
    created_at_unix: int,
    detokenizer: Any = None,
) -> dict[str, Any]:
    """Ollama /api/chat (non-streaming) response."""
    detok = detokenizer or default_detokenizer()
    text = detok(token_ids)
    completion_tokens = len(token_ids)
    return {
        "model": model,
        "created_at": _iso8601_z(created_at_unix),
        "message": {"role": "assistant", "content": text},
        "done": True,
        "done_reason": "stop",
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_count": int(prompt_tokens),
        "prompt_eval_duration": 0,
        "eval_count": completion_tokens,
        "eval_duration": 0,
    }


def to_ollama_chat_chunk(
    *,
    request_id: str,
    model: str,
    token_id: int,
    token_index: int,
    done: bool = False,
    detokenizer: Any = None,
    created_at_unix: int | None = None,
) -> dict[str, Any]:
    """Ollama /api/chat (streaming) chunk — one JSON object per line."""
    detok = detokenizer or default_detokenizer()
    out: dict[str, Any] = {
        "model": model,
        "created_at": _iso8601_z(created_at_unix or 0),
        "message": {"role": "assistant", "content": ""},
        "done": bool(done),
    }
    if not done:
        out["message"]["content"] = detok([token_id])
    else:
        # Final chunk carries the totals.
        out["done_reason"] = "stop"
        out["total_duration"] = 0
        out["load_duration"] = 0
        out["prompt_eval_count"] = 0
        out["prompt_eval_duration"] = 0
        out["eval_count"] = int(token_index)
        out["eval_duration"] = 0
    return out


def to_ollama_generate_response(
    *,
    model: str,
    token_ids: Sequence[int],
    prompt_tokens: int,
    created_at_unix: int,
    detokenizer: Any = None,
) -> dict[str, Any]:
    """Ollama /api/generate (non-streaming) response."""
    detok = detokenizer or default_detokenizer()
    text = detok(token_ids)
    completion_tokens = len(token_ids)
    return {
        "model": model,
        "created_at": _iso8601_z(created_at_unix),
        "response": text,
        "done": True,
        "done_reason": "stop",
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_count": int(prompt_tokens),
        "prompt_eval_duration": 0,
        "eval_count": completion_tokens,
        "eval_duration": 0,
    }


def build_ollama_tags_response(
    *,
    model_id: str,
    name: str,
    family: str = "mycelium",
    parameter_size: str = "7B",
    quantization_level: str = "int8",
) -> dict[str, Any]:
    """Ollama GET /api/tags response."""
    return {
        "models": [
            {
                "name": model_id,
                "model": model_id,
                "modified_at": _iso8601_z(0),
                "size": 0,
                "digest": "mycelium-local",
                "family": family,
                "parameter_size": parameter_size,
                "quantization_level": quantization_level,
                "details": {
                    "format": "mycelium-local",
                    "family": family,
                    "families": [family],
                    "parameter_size": parameter_size,
                    "quantization_level": quantization_level,
                },
            }
        ]
    }


# ----------------------------- Request translation -----------------------------

def from_ollama_chat_request(
    ollama_body: Mapping[str, Any], *,
    request_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Translate an Ollama /api/chat request to a Mycelium-shaped infer request.

    We pass through the core translator; Ollama's shape (messages list, role,
    content) maps cleanly onto the OpenAI shape we already support.
    """
    if not isinstance(ollama_body, Mapping):
        raise TranslateError("request body must be an object")
    return to_mycelium_infer_request(
        ollama_body, request_id=request_id, **kwargs)


def from_ollama_generate_request(
    ollama_body: Mapping[str, Any], *,
    request_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Translate an Ollama /api/generate request (prompt string + options)."""
    if not isinstance(ollama_body, Mapping):
        raise TranslateError("request body must be an object")
    prompt = ollama_body.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise TranslateError("prompt: required, non-empty string")
    # Wrap as a single-user message so the core translator is reusable.
    messages = [{"role": "user", "content": prompt}]
    if isinstance(ollama_body.get("system"), str) and ollama_body["system"]:
        messages.insert(0, {"role": "system", "content": ollama_body["system"]})
    body = dict(ollama_body)
    body.pop("prompt", None)
    body.pop("system", None)
    body["messages"] = messages
    body.setdefault("max_tokens", 64)
    return to_mycelium_infer_request(body, request_id=request_id, **kwargs)


__all__ = [
    "to_ollama_chat_response", "to_ollama_chat_chunk",
    "to_ollama_generate_response", "build_ollama_tags_response",
    "from_ollama_chat_request", "from_ollama_generate_request",
]
