# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_openai_shim.qwen_tokenizer — the product's real Qwen2 tokenizer.

Ground truth: tokenizer.json + tokenizer_config.json from the product's own
deployed transfer bundle. The shim must produce token ids EXACTLY as the
gateway does; the char-based stub in translator.py is a test-only default and
is NEVER silently substituted when a real tokenizer path is configured.

Requires the `tokenizers` library (present in both the ladder and packager
venvs). If it is missing, load_qwen_tokenizer raises a loud, explicit error —
no silent fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class QwenTokenizerUnavailable(RuntimeError):
    """The real tokenizer could not be loaded (missing lib or missing files)."""


@dataclass(frozen=True)
class QwenTokenizer:
    """A loaded Qwen2 fast tokenizer with the product's encode/decode surface."""

    _tokenizer: Any
    eos_token: str = "<|im_end|>"
    pad_token: str = "<|endoftext|>"

    def encode(self, text: str) -> tuple[int, ...]:
        return tuple(self._tokenizer.encode(text).ids)

    def decode(self, ids: Sequence[int]) -> str:
        return self._tokenizer.decode(list(ids))

    def token_to_id(self, token: str) -> int | None:
        value = self._tokenizer.token_to_id(token)
        return None if value is None else int(value)


def load_qwen_tokenizer(deployment_dir: Path | str) -> QwenTokenizer:
    """Load tokenizer.json from a deployment directory (the frozen bundle layout).

    Raises QwenTokenizerUnavailable with an actionable message when the
    tokenizers library or the files are missing — the caller must decide
    whether to fall back; this module never does it silently.
    """
    try:
        from tokenizers import Tokenizer  # type: ignore
    except ImportError as exc:
        raise QwenTokenizerUnavailable(
            "the 'tokenizers' library is required to load the real Qwen "
            "tokenizer; install it (pip install tokenizers) or serve with the "
            "explicitly-labelled test-only char tokenizer") from exc
    path = Path(deployment_dir) / "tokenizer.json"
    if not path.is_file():
        raise QwenTokenizerUnavailable(f"tokenizer.json not found at {path}")
    try:
        tok = Tokenizer.from_file(str(path))
    except Exception as exc:
        raise QwenTokenizerUnavailable(
            f"failed to load {path}: {exc}") from exc
    return QwenTokenizer(_tokenizer=tok)


def build_qwen_chat_prompt(
    *,
    messages: Sequence[Mapping[str, str]],
    system: str | None = None,
) -> str:
    """Build a Qwen2 chat prompt with the product's role markers.

    Mirrors the deployed tokenizer_config chat template: each turn is framed
    as '<|im_start|>role\\n<content><|im_end|>\\n', with an optional system
    turn first and a trailing assistant cue so the model continues.
    """
    parts: list[str] = []
    if system:
        parts.append(f"<|im_start|>system\n{system}<|im_end|>\n")
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"unknown chat role: {role!r}")
        parts.append(f"<|im_start|>{role}\n{content}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n")
    return "".join(parts)


__all__ = [
    "QwenTokenizer",
    "QwenTokenizerUnavailable",
    "load_qwen_tokenizer",
    "build_qwen_chat_prompt",
]
