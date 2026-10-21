# SPDX-License-Identifier: AGPL-3.0-only
"""Qwen3 deployments get the Qwen3 chat envelope with thinking disabled."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_live import codec as codec_module
from mycelium_live.codec import Qwen2PromptCodec, prompt_codec_from_deployment

QWEN3_SNAPSHOT = Path(
    "~/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B/snapshots/"
    "c1899de289a04d12100db370d81485cdf75e47ca"
).expanduser()


class _Tokenizer:
    def __init__(self) -> None:
        self.encoded = ""

    def token_to_id(self, token):
        return {"<|im_end|>": 151645, "<|endoftext|>": 151643}.get(token)

    def encode(self, value, *, add_special_tokens):
        assert add_special_tokens is False
        self.encoded = value
        return type("Encoded", (), {"ids": [1, 2]})()


def _deployment(tmp_path: Path, model_type: str) -> Path:
    (tmp_path / "config.json").write_text(
        json.dumps({"model_type": model_type}), encoding="utf-8"
    )
    return tmp_path


def test_qwen3_codec_closes_an_empty_think_block_after_the_assistant_cue() -> None:
    tokenizer = _Tokenizer()
    codec = codec_module.Qwen3PromptCodec(tokenizer)

    assert codec.encode("help me") == (1, 2)
    assert tokenizer.encoded.endswith(
        "<|im_start|>user\nhelp me<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )
    assert codec.stop_token_ids == frozenset({151643, 151645})


def test_qwen2_codec_envelope_is_unchanged() -> None:
    tokenizer = _Tokenizer()
    Qwen2PromptCodec(tokenizer).encode("help me")

    assert tokenizer.encoded.endswith(
        "<|im_start|>user\nhelp me<|im_end|>\n<|im_start|>assistant\n"
    )
    assert "<think>" not in tokenizer.encoded


def test_deployment_selects_codec_by_model_type(tmp_path, monkeypatch) -> None:
    seen: list[str] = []

    def loader(cls_name):
        def from_deployment(_root):
            seen.append(cls_name)
            return cls_name

        return from_deployment

    monkeypatch.setattr(
        codec_module.Qwen2PromptCodec, "from_deployment", loader("qwen2")
    )
    monkeypatch.setattr(
        codec_module.Qwen3PromptCodec, "from_deployment", loader("qwen3")
    )

    for name in ("a", "b"):
        (tmp_path / name).mkdir()

    assert prompt_codec_from_deployment(_deployment(tmp_path / "a", "qwen3")) == "qwen3"
    assert prompt_codec_from_deployment(_deployment(tmp_path / "b", "qwen2")) == "qwen2"
    assert seen == ["qwen3", "qwen2"]


def test_qwen3_codec_identity_is_distinct_from_qwen2() -> None:
    assert codec_module.Qwen3PromptCodec.TEMPLATE != codec_module.Qwen2PromptCodec.TEMPLATE
    assert codec_module.Qwen3PromptCodec.PROTOCOL != codec_module.Qwen2PromptCodec.PROTOCOL


@pytest.mark.skipif(
    not (QWEN3_SNAPSHOT / "tokenizer_config.json").is_file(),
    reason="pinned Qwen3-0.6B snapshot absent",
)
def test_qwen3_codec_matches_the_shipped_chat_template_with_thinking_off() -> None:
    jinja2 = pytest.importorskip("jinja2")
    template = json.loads(
        (QWEN3_SNAPSHOT / "tokenizer_config.json").read_text(encoding="utf-8")
    )["chat_template"]
    rendered = (
        jinja2.Environment()
        .from_string(template)
        .render(
            messages=[
                {"role": "system", "content": codec_module.Qwen3PromptCodec._SYSTEM},
                {"role": "user", "content": "Name three colours."},
            ],
            add_generation_prompt=True,
            enable_thinking=False,
        )
    )
    codec = codec_module.Qwen3PromptCodec.from_deployment(QWEN3_SNAPSHOT)
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(QWEN3_SNAPSHOT / "tokenizer.json"))

    assert codec.encode("Name three colours.") == tuple(
        tokenizer.encode(rendered, add_special_tokens=False).ids
    )
    assert codec.evidence_identity()["stop_token_ids"] == [151643, 151645]


def test_live_route_builder_selects_the_codec_from_the_manifest(monkeypatch) -> None:
    from scripts import build_qwen_live_route as builder
    from types import SimpleNamespace

    monkeypatch.setattr(
        builder, "Qwen2PromptCodec", SimpleNamespace(from_deployment=lambda root: "qwen2")
    )
    monkeypatch.setattr(
        builder, "Qwen3PromptCodec", SimpleNamespace(from_deployment=lambda root: "qwen3")
    )

    def manifest(architecture):
        return {"runtime_model": {"architecture": architecture}}

    assert builder._deployment_prompt_codec(manifest("qwen3"), Path(".")) == "qwen3"
    assert builder._deployment_prompt_codec(manifest("qwen2"), Path(".")) == "qwen2"
