from pathlib import Path

import pytest

from mycelium_tokenizer.gpt2_bpe import GPT2Tokenizer

SNAPSHOT = Path(
    "~/.cache/huggingface/hub/models--microsoft--DialoGPT-small/snapshots/"
    "49c537161a457d5256512f9d2d38a87d81ae0f0e"
).expanduser()

pytestmark = pytest.mark.skipif(
    not SNAPSHOT.is_dir(), reason="DialoGPT snapshot not present"
)


@pytest.fixture(scope="module")
def tokenizer() -> GPT2Tokenizer:
    return GPT2Tokenizer.from_files(
        SNAPSHOT / "vocab.json", SNAPSHOT / "merges.txt"
    )


def test_roundtrip_preserves_text(tokenizer: GPT2Tokenizer) -> None:
    text = "Hello, how are you today?"
    assert tokenizer.decode(tokenizer.encode(text)) == text


def test_known_gpt2_encoding(tokenizer: GPT2Tokenizer) -> None:
    assert tokenizer.encode("Hello world") == [15496, 995]


def test_roundtrip_preserves_unicode(tokenizer: GPT2Tokenizer) -> None:
    text = "café — naïve 日本語"
    assert tokenizer.decode(tokenizer.encode(text)) == text


def test_leading_space_is_significant(tokenizer: GPT2Tokenizer) -> None:
    assert tokenizer.encode(" world") != tokenizer.encode("world")
