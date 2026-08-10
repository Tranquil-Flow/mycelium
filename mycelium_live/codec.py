"""Architecture-selected tokenizers bound to the gateway PromptCodec seam."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mycelium_tokenizer import GPT2Tokenizer


class GPT2PromptCodec:
    """Encode prompts and decode streamed tokens with the deployed tokenizer."""

    def __init__(self, tokenizer: GPT2Tokenizer) -> None:
        self._tokenizer = tokenizer

    @classmethod
    def from_deployment(cls, deployment_dir: Path) -> "GPT2PromptCodec":
        deployment_dir = Path(deployment_dir)
        return cls(
            GPT2Tokenizer.from_files(
                deployment_dir / "vocab.json", deployment_dir / "merges.txt"
            )
        )

    def encode(self, prompt: str) -> tuple[int, ...]:
        return tuple(self._tokenizer.encode(prompt))

    def decode_token(self, token_id: int) -> str:
        return self._tokenizer.decode([token_id])


class Qwen2PromptCodec:
    """Qwen2 byte-level tokenizer with the canonical instruct chat envelope."""

    _SYSTEM = (
        "You are a helpful and safe assistant. Follow the user's instructions, "
        "including exact output-format constraints; when a format is requested, "
        "return only that format without a preamble or commentary. "
        "Refuse requests that facilitate theft, credential abuse, violence, "
        "or other wrongdoing."
    )

    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer
        self._stop_token_ids = frozenset(
            token_id
            for token in ("<|im_end|>", "<|endoftext|>")
            if (token_id := tokenizer.token_to_id(token)) is not None
        )

    @classmethod
    def from_deployment(cls, deployment_dir: Path) -> "Qwen2PromptCodec":
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError("qwen2_tokenizer_backend_unavailable") from exc
        return cls(Tokenizer.from_file(str(Path(deployment_dir) / "tokenizer.json")))

    @property
    def stop_token_ids(self) -> frozenset[int]:
        return self._stop_token_ids

    def encode(self, prompt: str) -> tuple[int, ...]:
        chat = (
            f"<|im_start|>system\n{self._SYSTEM}<|im_end|>\n"
            f"<|im_start|>user\n{prompt}<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
        return tuple(
            self._tokenizer.encode(chat, add_special_tokens=False).ids
        )

    def policy_response(self, prompt: str) -> str | None:
        """Return the bounded gateway refusal for explicit credential theft."""

        folded = prompt.casefold()
        credential = any(
            marker in folded for marker in ("password", "credential", "login")
        )
        explicit_abuse = "steal" in folded or (
            "phishing" in folded
            and any(
                marker in folded
                for marker in ("write", "create", "compose", "design")
            )
        )
        if credential and explicit_abuse:
            return "I can't assist with credential theft."
        return None

    def decode_token(self, token_id: int) -> str:
        if token_id in self._stop_token_ids:
            return ""
        return self._tokenizer.decode([token_id], skip_special_tokens=False)


def prompt_codec_from_deployment(deployment_dir: Path) -> Any:
    """Select the tokenizer from the pinned deployment config."""

    deployment_dir = Path(deployment_dir)
    try:
        config = json.loads((deployment_dir / "config.json").read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("deployment_model_config_unavailable") from exc
    model_type = config.get("model_type")
    if model_type == "gpt2":
        return GPT2PromptCodec.from_deployment(deployment_dir)
    if model_type == "qwen2":
        return Qwen2PromptCodec.from_deployment(deployment_dir)
    raise RuntimeError(f"unsupported_prompt_codec:{model_type!r}")


__all__ = ["GPT2PromptCodec", "Qwen2PromptCodec", "prompt_codec_from_deployment"]
