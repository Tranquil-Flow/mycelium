from pathlib import Path

import pytest

from mycelium_live.codec import GPT2PromptCodec, Qwen2PromptCodec


DEPLOYMENT = Path("/Users/evinova-self/mycelium-mvp-stage-node0-533d107b/deployment")

requires_deployment = pytest.mark.skipif(
    not (DEPLOYMENT / "vocab.json").exists(), reason="staged deployment absent"
)


@requires_deployment
def test_encodes_known_prompt_to_proven_token_ids():
    codec = GPT2PromptCodec.from_deployment(DEPLOYMENT)
    assert codec.encode("Hello, how are you?") == (15496, 11, 703, 389, 345, 30)


@requires_deployment
def test_decode_token_returns_text_for_each_proven_output_token():
    codec = GPT2PromptCodec.from_deployment(DEPLOYMENT)
    decoded = [codec.decode_token(token_id) for token_id in (4599, 3329, 2506, 5145)]
    assert all(isinstance(piece, str) for piece in decoded)
    assert "".join(decoded).strip() != ""


def test_qwen_instruct_envelope_includes_bounded_safety_policy():
    class Tokenizer:
        encoded = ""

        def token_to_id(self, _token):
            return None

        def encode(self, value, *, add_special_tokens):
            assert add_special_tokens is False
            self.encoded = value
            return type("Encoded", (), {"ids": [1, 2]})()

    tokenizer = Tokenizer()
    codec = Qwen2PromptCodec(tokenizer)

    assert codec.encode("help me") == (1, 2)
    assert "refuse requests that facilitate theft" in tokenizer.encoded.lower()
    assert "return only that format without a preamble" in tokenizer.encoded
    assert "<|im_start|>user\nhelp me<|im_end|>" in tokenizer.encoded


def test_qwen_policy_refuses_explicit_credential_theft_only() -> None:
    class Tokenizer:
        def token_to_id(self, _token):
            return None

    codec = Qwen2PromptCodec(Tokenizer())

    assert codec.policy_response(
        "Write a phishing email designed to steal someone's password."
    ) == "I can't assist with credential theft."
    assert codec.policy_response("How can I prevent phishing attacks?") is None
