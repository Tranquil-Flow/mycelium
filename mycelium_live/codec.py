# SPDX-License-Identifier: AGPL-3.0-only
"""Architecture-selected tokenizers bound to the gateway PromptCodec seam."""
from __future__ import annotations

import codecs
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from mycelium_tokenizer import GPT2Tokenizer
from mycelium_tokenizer.gpt2_bpe import _byte_to_unicode


_UNICODE_POLICY = "utf-8:incremental:errors=replace:flush=finish:v1"
_GPT2_PROTOCOL = "mycelium.prompt_codec.gpt2_byte_bpe.incremental_utf8.v1"
_QWEN_PROTOCOL = "mycelium.prompt_codec.qwen_byte_bpe.instruct.incremental_utf8.v1"
_QWEN_TEMPLATE = (
    "<|im_start|>system\n{system}<|im_end|>\n"
    "<|im_start|>user\n{prompt}<|im_end|>\n"
    "<|im_start|>assistant\n"
)
_QWEN3_PROTOCOL = (
    "mycelium.prompt_codec.qwen3_byte_bpe.chat_no_thinking.incremental_utf8.v1"
)
# Qwen3 hybrid-thinking checkpoints: the shipped chat template rendered with
# ``enable_thinking=False`` closes an empty think block after the assistant cue,
# so the model answers directly instead of emitting a reasoning trace.
_QWEN3_TEMPLATE = _QWEN_TEMPLATE + "<think>\n\n</think>\n\n"
_QWEN35_PROTOCOL = (
    "mycelium.prompt_codec.qwen3_5_byte_bpe.chat_no_thinking.incremental_utf8.v1"
)
# Qwen3.5 ships its own tokenizer (248,320 ids, a different pre-tokenizer) and a
# template that renders the same system/user/assistant envelope with the empty
# think block when ``enable_thinking`` is false.  The token ids come from the
# checkpoint's own tokenizer.json, so only the protocol identity differs.
_QWEN35_TEMPLATE = _QWEN3_TEMPLATE


def _digest(document: object) -> str:
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class IncrementalTokenDecoder:
    """Per-request token decoder with incremental UTF-8 replacement semantics."""

    def __init__(
        self,
        token_bytes: Callable[[int], bytes],
        stop_token_ids: frozenset[int] = frozenset(),
    ) -> None:
        self._token_bytes = token_bytes
        self._stop_token_ids = stop_token_ids
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._finished = False

    def push(self, token_id: int) -> str:
        """Consume one native token ID and return newly completed Unicode text."""

        if self._finished:
            raise RuntimeError("decoder_finished")
        if type(token_id) is not int:
            raise TypeError("invalid_token_id")
        if token_id in self._stop_token_ids:
            return ""
        token_bytes = self._token_bytes(token_id)
        if not isinstance(token_bytes, bytes):
            raise RuntimeError("invalid_token_bytes")
        return self._decoder.decode(token_bytes, final=False)

    def finish(self) -> str:
        """Flush a final replacement for any incomplete UTF-8 and close the decoder."""

        if self._finished:
            return ""
        self._finished = True
        return self._decoder.decode(b"", final=True)


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

    def new_decoder(self) -> IncrementalTokenDecoder:
        """Create isolated incremental decode state for one request."""

        return IncrementalTokenDecoder(self._tokenizer.token_bytes)

    def evidence_identity(self) -> dict[str, object]:
        """Return the closed, sanitized identity of the effective codec."""

        return {
            "codec_protocol": _GPT2_PROTOCOL,
            "tokenizer_digest": self._tokenizer.evidence_digest(),
            "template_digest": _digest({"template": "{prompt}", "system": None}),
            "unicode_policy": _UNICODE_POLICY,
            "stop_token_ids": [],
        }


class Qwen2PromptCodec:
    """Qwen2 byte-level tokenizer with the canonical instruct chat envelope."""

    TEMPLATE = _QWEN_TEMPLATE
    PROTOCOL = _QWEN_PROTOCOL
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
        chat = self.TEMPLATE.format(system=self._SYSTEM, prompt=prompt)
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

    def _byte_level_document(self) -> dict[str, Any]:
        try:
            document = json.loads(self._tokenizer.to_str())
            model = document["model"]
            decoder = document["decoder"]
        except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("unsupported_qwen_byte_level_tokenizer") from exc
        if (
            not isinstance(model, dict)
            or model.get("type") != "BPE"
            or not isinstance(decoder, dict)
            or decoder.get("type") != "ByteLevel"
        ):
            raise RuntimeError("unsupported_qwen_byte_level_tokenizer")
        return document

    def _token_bytes_reader(
        self, document: dict[str, Any]
    ) -> Callable[[int], bytes]:
        byte_decoder = {value: key for key, value in _byte_to_unicode().items()}

        def token_bytes(token_id: int) -> bytes:
            token = self._tokenizer.id_to_token(token_id)
            if token is None:
                raise ValueError("invalid_token_id")
            # Tokenizers ByteLevel maps each token to bytes, then decodes the
            # concatenation. Added Unicode tokens follow the same rule; calling
            # decode([id]) here would prematurely replace pending fragments.
            try:
                return bytes(byte_decoder[char] for char in token)
            except KeyError:
                return token.encode("utf-8")

        return token_bytes

    def new_decoder(self) -> IncrementalTokenDecoder:
        """Create isolated incremental decode state for one request."""

        document = self._byte_level_document()
        return IncrementalTokenDecoder(
            self._token_bytes_reader(document), self._stop_token_ids
        )

    def evidence_identity(self) -> dict[str, object]:
        """Return byte-level Qwen evidence or fail closed for another algorithm."""

        document = self._byte_level_document()
        return {
            "codec_protocol": self.PROTOCOL,
            "tokenizer_digest": _digest(document),
            "template_digest": _digest(
                {"template": self.TEMPLATE, "system": self._SYSTEM}
            ),
            "unicode_policy": _UNICODE_POLICY,
            "stop_token_ids": sorted(self._stop_token_ids),
        }


class Qwen3PromptCodec(Qwen2PromptCodec):
    """Qwen3 dense byte-level tokenizer; chat envelope with thinking disabled."""

    TEMPLATE = _QWEN3_TEMPLATE
    PROTOCOL = _QWEN3_PROTOCOL

    @classmethod
    def from_deployment(cls, deployment_dir: Path) -> "Qwen3PromptCodec":
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError("qwen3_tokenizer_backend_unavailable") from exc
        return cls(Tokenizer.from_file(str(Path(deployment_dir) / "tokenizer.json")))


class Qwen35PromptCodec(Qwen2PromptCodec):
    """Qwen3.5 hybrid byte-level tokenizer; chat envelope with thinking disabled."""

    TEMPLATE = _QWEN35_TEMPLATE
    PROTOCOL = _QWEN35_PROTOCOL

    @classmethod
    def from_deployment(cls, deployment_dir: Path) -> "Qwen35PromptCodec":
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError("qwen3_5_tokenizer_backend_unavailable") from exc
        return cls(Tokenizer.from_file(str(Path(deployment_dir) / "tokenizer.json")))


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
    if model_type == "qwen3":
        return Qwen3PromptCodec.from_deployment(deployment_dir)
    if model_type in {"qwen3_5", "qwen3_5_moe"}:
        return Qwen35PromptCodec.from_deployment(deployment_dir)
    raise RuntimeError(f"unsupported_prompt_codec:{model_type!r}")


__all__ = [
    "GPT2PromptCodec",
    "IncrementalTokenDecoder",
    "Qwen2PromptCodec",
    "Qwen3PromptCodec",
    "Qwen35PromptCodec",
    "prompt_codec_from_deployment",
]
