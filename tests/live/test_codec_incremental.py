# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path

import pytest
from tokenizers import Tokenizer, decoders, models

from mycelium_live.codec import GPT2PromptCodec, Qwen2PromptCodec
from mycelium_tokenizer.gpt2_bpe import GPT2Tokenizer, _byte_to_unicode


UNICODE_POLICY = "utf-8:incremental:errors=replace:flush=finish:v1"


def _mapped(raw: bytes) -> str:
    table = _byte_to_unicode()
    return "".join(table[value] for value in raw)


def _gpt2_tokenizer() -> GPT2Tokenizer:
    return GPT2Tokenizer(
        {
            _mapped(b"\xe2"): 10,
            _mapped(b"\x82\xac"): 11,
            _mapped("�".encode("utf-8")): 12,
            _mapped(b"\xff"): 13,
            _mapped(b"x"): 14,
        },
        {},
    )


def _qwen_tokenizer(*, marker: str = "x") -> Tokenizer:
    tokenizer = Tokenizer(
        models.BPE(
            vocab={
                _mapped(b"\xe2"): 0,
                _mapped(b"\x82\xac"): 1,
                _mapped("�".encode("utf-8")): 2,
                _mapped(marker.encode("utf-8")): 3,
            },
            merges=[],
        )
    )
    tokenizer.decoder = decoders.ByteLevel()
    tokenizer.add_special_tokens(["<|im_end|>", "<|marker|>"])
    return tokenizer


def test_gpt2_decoder_buffers_incomplete_utf8_and_preserves_real_replacement() -> None:
    tokenizer = _gpt2_tokenizer()
    decoder = GPT2PromptCodec(tokenizer).new_decoder()

    assert decoder.push(10) == ""
    assert decoder.push(11) == "€"
    assert decoder.push(12) == "�"
    assert decoder.finish() == ""


def test_incremental_result_matches_whole_gpt2_decode_for_invalid_bytes() -> None:
    tokenizer = _gpt2_tokenizer()
    token_ids = (10, 14, 13, 11)
    decoder = GPT2PromptCodec(tokenizer).new_decoder()

    streamed = "".join(decoder.push(token_id) for token_id in token_ids)
    streamed += decoder.finish()

    assert streamed == tokenizer.decode(token_ids)


def test_finish_flushes_incomplete_utf8_once_and_closes_decoder() -> None:
    decoder = GPT2PromptCodec(_gpt2_tokenizer()).new_decoder()

    assert decoder.push(10) == ""
    assert decoder.finish() == "�"
    assert decoder.finish() == ""
    with pytest.raises(RuntimeError, match="decoder_finished"):
        decoder.push(11)


@pytest.mark.parametrize("token_id", [-1, 9, 15, True, 1.5, "10"])
def test_gpt2_incremental_decoder_rejects_invalid_token_ids(token_id: object) -> None:
    decoder = GPT2PromptCodec(_gpt2_tokenizer()).new_decoder()

    with pytest.raises((TypeError, ValueError), match="invalid_token_id"):
        decoder.push(token_id)  # type: ignore[arg-type]


def test_decode_token_keeps_legacy_per_token_replacement_behavior() -> None:
    codec = GPT2PromptCodec(_gpt2_tokenizer())

    assert codec.decode_token(10) == "�"


def test_qwen_decoder_omits_only_stops_without_losing_pending_or_other_special_ids() -> None:
    tokenizer = _qwen_tokenizer()
    codec = Qwen2PromptCodec(tokenizer)
    decoder = codec.new_decoder()
    stop_id = tokenizer.token_to_id("<|im_end|>")
    marker_id = tokenizer.token_to_id("<|marker|>")
    assert stop_id is not None
    assert marker_id is not None

    assert decoder.push(0) == ""
    assert decoder.push(stop_id) == ""
    assert decoder.push(1) == "€"
    assert decoder.push(marker_id) == "<|marker|>"
    assert decoder.finish() == ""


def test_qwen_decoder_preserves_real_replacement_and_rejects_unknown_id() -> None:
    tokenizer = _qwen_tokenizer()
    decoder = Qwen2PromptCodec(tokenizer).new_decoder()

    assert decoder.push(2) == "�"
    with pytest.raises(ValueError, match="invalid_token_id"):
        decoder.push(tokenizer.get_vocab_size(with_added_tokens=True) + 10)


def test_qwen_non_byte_level_tokenizer_fails_closed_for_incremental_and_evidence() -> None:
    tokenizer = Tokenizer(models.WordLevel(vocab={"hello": 0}, unk_token="hello"))
    codec = Qwen2PromptCodec(tokenizer)

    with pytest.raises(RuntimeError, match="unsupported_qwen_byte_level_tokenizer"):
        codec.new_decoder()
    with pytest.raises(RuntimeError, match="unsupported_qwen_byte_level_tokenizer"):
        codec.evidence_identity()


@pytest.mark.parametrize(
    ("codec", "protocol", "stop_ids"),
    [
        (
            GPT2PromptCodec(_gpt2_tokenizer()),
            "mycelium.prompt_codec.gpt2_byte_bpe.incremental_utf8.v1",
            [],
        ),
        (
            Qwen2PromptCodec(_qwen_tokenizer()),
            "mycelium.prompt_codec.qwen_byte_bpe.instruct.incremental_utf8.v1",
            [4],
        ),
    ],
)
def test_evidence_identity_is_closed_sanitized_and_content_bound(
    codec: object, protocol: str, stop_ids: list[int]
) -> None:
    identity = codec.evidence_identity()  # type: ignore[attr-defined]

    assert identity == {
        "codec_protocol": protocol,
        "tokenizer_digest": identity["tokenizer_digest"],
        "template_digest": identity["template_digest"],
        "unicode_policy": UNICODE_POLICY,
        "stop_token_ids": stop_ids,
    }
    assert identity["tokenizer_digest"].startswith("sha256:")
    assert identity["template_digest"].startswith("sha256:")
    serialized = json.dumps(identity, sort_keys=True)
    assert str(Path.home()) not in serialized


def test_tokenizer_and_template_digests_follow_actual_loaded_contents() -> None:
    first = Qwen2PromptCodec(_qwen_tokenizer(marker="x"))
    second = Qwen2PromptCodec(_qwen_tokenizer(marker="y"))

    assert first.evidence_identity()["tokenizer_digest"] != second.evidence_identity()[
        "tokenizer_digest"
    ]

    class AlternateSystemCodec(Qwen2PromptCodec):
        _SYSTEM = "A different system envelope."

    alternate = AlternateSystemCodec(_qwen_tokenizer(marker="x"))
    assert first.evidence_identity()["template_digest"] != alternate.evidence_identity()[
        "template_digest"
    ]
