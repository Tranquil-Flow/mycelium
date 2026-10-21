# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_openai_shim.anthropic — Anthropic Messages API translation layer.

Anthropic Messages API (https://docs.anthropic.com/claude/reference/messages_post):

  * POST /v1/messages  -> {type, role, content, model, stop_reason, usage, ...}
  * POST /v1/messages  (stream=true)  -> SSE event-stream of typed events:
        message_start, content_block_start, ping, content_block_delta,
        content_block_stop, message_delta, message_stop.

System prompt is a top-level field, not in messages. Content is a list of
typed blocks (text, image, tool_use, etc.). Model ids are bare names
(e.g. "claude-3-5-sonnet-20240620").
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Mapping, Sequence

from .translator import (
    TranslateError,
    default_detokenizer,
    to_mycelium_infer_request,
)

ANTHROPIC_EVENT_TYPES = (
    "message_start",
    "content_block_start",
    "ping",
    "content_block_delta",
    "content_block_stop",
    "message_delta",
    "message_stop",
)


def _msg_id(request_id: str) -> str:
    return f"msg_{request_id}"


def _now_ms() -> int:
    return int(time.time() * 1000)


# ----------------------------- Response translation -----------------------------

def to_anthropic_message_response(
    *,
    request_id: str,
    model: str,
    token_ids: Sequence[int],
    prompt_tokens: int,
    stop_reason: str = "end_turn",
    detokenizer: Any = None,
) -> dict[str, Any]:
    """Anthropic Messages non-streaming response."""
    detok = detokenizer or default_detokenizer()
    text = detok(token_ids)
    completion_tokens = len(token_ids)
    return {
        "id": _msg_id(request_id),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {"type": "text", "text": text},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(prompt_tokens),
            "output_tokens": completion_tokens,
        },
    }


def _make_event(*, type: str, message: dict[str, Any] | None = None,
                index: int | None = None, delta: dict[str, Any] | None = None,
                content_block: dict[str, Any] | None = None,
                usage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a typed SSE event per Anthropic's wire format."""
    evt: dict[str, Any] = {"type": type}
    if message is not None:
        evt["message"] = message
    if index is not None:
        evt["index"] = index
    if content_block is not None:
        evt["content_block"] = content_block
    if delta is not None:
        evt["delta"] = delta
    if usage is not None:
        evt["usage"] = usage
    return evt


def build_anthropic_stream_events(
    *,
    request_id: str,
    model: str,
    token_ids: Sequence[int],
    stop_reason: str = "end_turn",
    detokenizer: Any = None,
) -> list[dict[str, Any]]:
    """Build the full SSE event sequence for a Messages streaming response.

    Order: message_start, content_block_start, ping,
           content_block_delta (one per token), content_block_stop,
           message_delta (carrying stop_reason), message_stop.
    """
    detok = detokenizer or default_detokenizer()
    events: list[dict[str, Any]] = []

    # 1. message_start
    events.append(_make_event(
        type="message_start",
        message={
            "id": _msg_id(request_id),
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    ))
    # 2. content_block_start (text block)
    events.append(_make_event(
        type="content_block_start",
        index=0,
        content_block={"type": "text", "text": ""},
    ))
    # 3. ping (Anthropic sends a ping right after the first block)
    events.append({"type": "ping"})
    # 4. one content_block_delta per token
    for i, tid in enumerate(token_ids):
        events.append(_make_event(
            type="content_block_delta",
            index=0,
            delta={"type": "text_delta", "text": detok([tid])},
        ))
    # 5. content_block_stop
    events.append(_make_event(
        type="content_block_stop",
        index=0,
    ))
    # 6. message_delta (carries stop_reason + cumulative output tokens)
    events.append(_make_event(
        type="message_delta",
        delta={"stop_reason": stop_reason, "stop_sequence": None},
        usage={"output_tokens": len(token_ids)},
    ))
    # 7. message_stop
    events.append(_make_event(type="message_stop"))
    return events


# ----------------------------- Request translation -----------------------------

def from_anthropic_request(
    body: Mapping[str, Any], *,
    request_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Translate an Anthropic Messages request to a Mycelium-shaped infer request."""
    if not isinstance(body, Mapping):
        raise TranslateError("request body must be an object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise TranslateError("messages: required, non-empty list")

    system_text: str | None = None
    system = body.get("system")
    if isinstance(system, str) and system:
        system_text = system
    elif isinstance(system, list):
        # system can also be a list of content blocks
        system_text = "\n".join(
            block["text"] for block in system
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        ) or None
    prompt_parts: list[str] = []
    if system_text:
        prompt_parts.append(f"<|im_start|>system\n{system_text}<|im_end|>\n")
    for m in messages:
        if not isinstance(m, Mapping):
            raise TranslateError("messages: each entry must be an object")
        role = m.get("role")
        content = m.get("content")
        text = _extract_text(content)
        if not isinstance(text, str):
            raise TranslateError("messages: content must be a string or "
                                 f"a list with a single text block (got {type(content).__name__})")
        if role not in ("user", "assistant"):
            raise TranslateError(f"messages: unknown role {role!r}")
        prompt_parts.append(f"<|im_start|>{role}\n{text}<|im_end|>\n")
    prompt_parts.append("<|im_start|>assistant\n")
    prompt_text = "".join(prompt_parts)

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise TranslateError("model: required, non-empty string")

    max_tokens = body.get("max_tokens", 64)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise TranslateError("max_tokens: required, positive integer")

    # Anthropic uses bare model ids; pass through verbatim.
    return {
        "request_id": request_id,
        "model": model,
        "prompt_text": prompt_text,
        "token_ids": [],  # the caller tokenizes via the shared path
        "max_new_tokens": int(max_tokens),
        "stream": bool(body.get("stream", False)),
        "warnings": [],
    }


def _extract_text(content: Any) -> str:
    """Pull plain text out of Anthropic's content (str or list-of-blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                out.append(block["text"])
        return "".join(out)
    return ""  # type: ignore[return-value]


__all__ = [
    "ANTHROPIC_EVENT_TYPES",
    "to_anthropic_message_response",
    "build_anthropic_stream_events",
    "from_anthropic_request",
    "_make_event",
]
