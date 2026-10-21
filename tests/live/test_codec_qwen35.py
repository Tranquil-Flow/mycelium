# SPDX-License-Identifier: AGPL-3.0-only
"""Qwen3.5 deployments get their own tokenizer and the thinking-off chat envelope."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_live import codec as codec_module
from mycelium_live.codec import prompt_codec_from_deployment

QWEN35_SNAPSHOT = Path(
    "~/.cache/huggingface/hub/models--Qwen--Qwen3.5-0.8B/snapshots/"
    "2fc06364715b967f1860aea9cf38778875588b17"
).expanduser()
FIXTURE = Path(__file__).parents[1] / "model" / "fixtures" / "qwen35_0_8b_mlx_lm_reference.json"

requires_snapshot = pytest.mark.skipif(
    not (QWEN35_SNAPSHOT / "tokenizer.json").exists(),
    reason="pinned Qwen/Qwen3.5-0.8B snapshot absent",
)


class _Tokenizer:
    def __init__(self) -> None:
        self.encoded = ""

    def token_to_id(self, token):
        return {"<|im_end|>": 248046, "<|endoftext|>": 248044}.get(token)

    def encode(self, value, *, add_special_tokens):
        assert add_special_tokens is False
        self.encoded = value
        return SimpleNamespace(ids=[1, 2])


def _render(template: str, messages: list[dict[str, str]]) -> str:
    jinja2 = pytest.importorskip("jinja2")

    def raise_exception(message: str) -> None:
        raise ValueError(message)

    environment = jinja2.Environment()
    environment.globals["raise_exception"] = raise_exception
    return environment.from_string(template).render(
        messages=messages, add_generation_prompt=True, enable_thinking=False
    )


def test_qwen35_codec_closes_an_empty_think_block() -> None:
    tokenizer = _Tokenizer()
    codec = codec_module.Qwen35PromptCodec(tokenizer)

    assert codec.encode("help me") == (1, 2)
    assert tokenizer.encoded.endswith(
        "<|im_start|>user\nhelp me<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )
    assert codec.stop_token_ids == frozenset({248044, 248046})


def test_qwen35_codec_identity_is_distinct() -> None:
    assert codec_module.Qwen35PromptCodec.PROTOCOL not in {
        codec_module.Qwen2PromptCodec.PROTOCOL,
        codec_module.Qwen3PromptCodec.PROTOCOL,
    }


def test_deployment_and_route_builder_select_the_qwen35_codec(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        codec_module.Qwen35PromptCodec, "from_deployment", lambda _root: "qwen3_5"
    )
    (tmp_path / "config.json").write_text(json.dumps({"model_type": "qwen3_5"}))
    assert prompt_codec_from_deployment(tmp_path) == "qwen3_5"

    from scripts import build_qwen_live_route as builder

    monkeypatch.setattr(
        builder, "Qwen35PromptCodec", SimpleNamespace(from_deployment=lambda root: "qwen3_5")
    )
    manifest = {"runtime_model": {"architecture": "qwen3_5"}}
    assert builder._deployment_prompt_codec(manifest, Path(".")) == "qwen3_5"


@requires_snapshot
def test_qwen35_codec_matches_the_shipped_template_with_thinking_off() -> None:
    template = (QWEN35_SNAPSHOT / "chat_template.jinja").read_text(encoding="utf-8")
    rendered = _render(
        template,
        [
            {"role": "system", "content": codec_module.Qwen35PromptCodec._SYSTEM},
            {"role": "user", "content": "Name three colours."},
        ],
    )
    codec = codec_module.Qwen35PromptCodec.from_deployment(QWEN35_SNAPSHOT)
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(QWEN35_SNAPSHOT / "tokenizer.json"))

    assert codec.encode("Name three colours.") == tuple(
        tokenizer.encode(rendered, add_special_tokens=False).ids
    )
    assert codec.evidence_identity()["stop_token_ids"] == [248044, 248046]


@requires_snapshot
def test_parity_fixture_prompt_ids_are_the_shipped_template_tokens() -> None:
    fixture = json.loads(FIXTURE.read_text())
    template = (QWEN35_SNAPSHOT / "chat_template.jinja").read_text(encoding="utf-8")
    rendered = _render(template, [{"role": "user", "content": fixture["prompt"]}])
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(QWEN35_SNAPSHOT / "tokenizer.json"))

    assert rendered == fixture["rendered_prompt"]
    assert tokenizer.encode(rendered, add_special_tokens=False).ids == fixture["prompt_ids"]
