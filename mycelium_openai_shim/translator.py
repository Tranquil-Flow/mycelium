# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_openai_shim.translator — translate OpenAI Chat Completions requests
to Mycelium LiveRoute.infer calls and back.

Pure data-in / data-out translation. No HTTP, no LiveRoute reference.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence


# ----------------------------- Errors -----------------------------

class TranslateError(ValueError):
    """An OpenAI request cannot be mapped to a Mycelium infer call."""


# ----------------------------- Tokenizer -----------------------------

Tokenizer = Callable[[str], tuple[int, ...]]
Detokenizer = Callable[[Sequence[int]], str]


def default_tokenizer() -> Tokenizer:
    """A trivial tokenizer that returns one integer per character."""
    def _tok(text: str) -> tuple[int, ...]:
        return tuple(ord(c) for c in text)
    return _tok


def default_detokenizer() -> Detokenizer:
    def _detok(ids: Sequence[int]) -> str:
        return "".join(chr(i) if 0 <= i < 0x110000 else "\ufffd" for i in ids)
    return _detok


# ----------------------------- Request translation -----------------------------

_IGNORED_OPENAI_FIELDS = frozenset({
    "temperature", "top_p", "n", "logprobs", "presence_penalty",
    "frequency_penalty", "user", "response_format", "seed", "stop",
    "tools", "tool_choice", "logit_bias",
})

DEFAULT_MODEL_ID = "mycelium-default"


def to_mycelium_infer_request(
    openai_body: Mapping[str, Any],
    *,
    request_id: str,
    tokenizer: Tokenizer | None = None,
    model_id: str = DEFAULT_MODEL_ID,
) -> dict[str, Any]:
    messages = openai_body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise TranslateError("messages: required, non-empty list")
    if any(not isinstance(m, Mapping) or "role" not in m or "content" not in m
           for m in messages):
        raise TranslateError("messages: each entry must have role and content")

    prompt_parts: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, str):
            raise TranslateError("messages: content must be a string")
        if role not in ("system", "user", "assistant"):
            raise TranslateError(f"messages: unknown role {role!r}")
        prompt_parts.append(f"<|im_start|>{role}\n{content}<|im_end|>\n")
    prompt_parts.append("<|im_start|>assistant\n")
    prompt_text = "".join(prompt_parts)

    model = openai_body.get("model", model_id)
    if not isinstance(model, str) or not model:
        model = model_id

    max_tokens = openai_body.get("max_tokens", 64)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise TranslateError("max_tokens: required, positive integer")

    stream = bool(openai_body.get("stream", False))

    tok = tokenizer or default_tokenizer()
    token_ids = list(tok(prompt_text))

    warnings: list[str] = []
    for ignored in _IGNORED_OPENAI_FIELDS:
        if ignored in openai_body:
            warnings.append(ignored)

    return {
        "request_id": request_id,
        "model": model,
        "prompt_text": prompt_text,
        "token_ids": token_ids,
        "max_new_tokens": max_tokens,
        "stream": stream,
        "warnings": warnings,
    }


# ----------------------------- Sinks -----------------------------

@dataclass
class SyncSink:
    token_ids: list[int] = field(default_factory=list)

    def emit(self, token_index: int, token_id: int) -> None:
        self.token_ids.append(int(token_id))


@dataclass
class StreamingSink:
    request_id: str
    model: str
    detokenizer: Detokenizer | None = None
    chunk_builder: Any = None
    _finished: bool = False
    _chunks: list[dict] = field(default_factory=list)

    def _build(self, token_id: int, token_index: int, finish_reason: str | None) -> dict:
        builder = self.chunk_builder or to_openai_chat_chunk
        return builder(
            request_id=self.request_id, model=self.model,
            token_id=int(token_id), token_index=int(token_index),
            finish_reason=finish_reason, detokenizer=self.detokenizer)

    def emit(self, token_index: int, token_id: int) -> None:
        if self._finished:
            return
        self._chunks.append(self._build(token_id, token_index, None))

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._chunks.append(self._build(0, len(self._chunks), "stop"))

    def drain(self) -> Iterator[dict]:
        yield from self._chunks


# ----------------------------- Response translation -----------------------------

def _chatcmpl_id(request_id: str) -> str:
    return f"chatcmpl-{request_id}"


def _created_unix() -> int:
    return int(time.time())


def to_openai_chat_response(
    *,
    request_id: str,
    model: str,
    token_ids: Sequence[int],
    prompt_tokens: int,
    detokenizer: Detokenizer | None = None,
) -> dict[str, Any]:
    detok = detokenizer or default_detokenizer()
    text = detok(token_ids)
    completion_tokens = len(token_ids)
    return {
        "id": _chatcmpl_id(request_id),
        "object": "chat.completion",
        "created": _created_unix(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def to_openai_chat_chunk(
    *,
    request_id: str,
    model: str,
    token_id: int,
    token_index: int,
    finish_reason: str | None,
    detokenizer: Detokenizer | None = None,
) -> dict[str, Any]:
    detok = detokenizer or default_detokenizer()
    delta_content: dict[str, Any] = {}
    if finish_reason is None:
        delta_content = {"content": detok([token_id])}
    return {
        "id": _chatcmpl_id(request_id),
        "object": "chat.completion.chunk",
        "created": _created_unix(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta_content,
                "finish_reason": finish_reason,
            }
        ],
    }


def build_openai_models_response(current_model: str = "mycelium-default") -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": current_model,
                "object": "model",
                "created": _created_unix(),
                "owned_by": "mycelium",
            }
        ],
    }


__all__ = [
    "TranslateError",
    "Tokenizer", "Detokenizer",
    "default_tokenizer", "default_detokenizer",
    "to_mycelium_infer_request",
    "to_openai_chat_response",
    "to_openai_chat_chunk",
    "build_openai_models_response",
    "SyncSink", "StreamingSink",
]
