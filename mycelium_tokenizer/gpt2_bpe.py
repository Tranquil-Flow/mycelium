"""Stdlib-only GPT-2 byte-level BPE tokenizer."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Sequence

_PATTERN = re.compile(
    r"'s|'t|'re|'ve|'m|'ll|'d| ?[A-Za-z]+| ?[0-9]+| ?[^\sA-Za-z0-9]+|\s+(?!\S)|\s+"
)


@lru_cache(maxsize=1)
def _byte_to_unicode() -> dict[int, str]:
    printable = (
        list(range(ord("!"), ord("~") + 1))
        + list(range(ord("¡"), ord("¬") + 1))
        + list(range(ord("®"), ord("ÿ") + 1))
    )
    mapped = list(printable)
    extra = 0
    for byte in range(256):
        if byte not in printable:
            printable.append(byte)
            mapped.append(256 + extra)
            extra += 1
    return dict(zip(printable, map(chr, mapped)))


def _pairs(word: tuple[str, ...]) -> set[tuple[str, str]]:
    return {(word[index], word[index + 1]) for index in range(len(word) - 1)}


class GPT2Tokenizer:
    """Reversible GPT-2 byte-level byte-pair encoder."""

    def __init__(
        self, encoder: dict[str, int], ranks: dict[tuple[str, str], int]
    ) -> None:
        self._encoder = encoder
        self._decoder = {index: token for token, index in encoder.items()}
        self._ranks = ranks
        self._byte_encoder = _byte_to_unicode()
        self._byte_decoder = {value: key for key, value in self._byte_encoder.items()}
        self._cache: dict[str, tuple[str, ...]] = {}

    @classmethod
    def from_files(cls, vocab_path: Path, merges_path: Path) -> GPT2Tokenizer:
        """Load GPT-2 vocabulary and merge ranks from local files."""

        encoder = json.loads(Path(vocab_path).read_text(encoding="utf-8"))
        lines = Path(merges_path).read_text(encoding="utf-8").split("\n")
        ranks: dict[tuple[str, str], int] = {}
        for rank, line in enumerate(
            line for line in lines[1:] if line and not line.startswith("#")
        ):
            first, _, second = line.partition(" ")
            ranks[(first, second)] = rank
        return cls(encoder, ranks)

    def _bpe(self, token: str) -> tuple[str, ...]:
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        word = tuple(token)
        while len(word) > 1:
            candidates = _pairs(word)
            best = min(
                candidates, key=lambda pair: self._ranks.get(pair, float("inf"))
            )
            if best not in self._ranks:
                break
            first, second = best
            merged: list[str] = []
            index = 0
            while index < len(word):
                if (
                    index < len(word) - 1
                    and word[index] == first
                    and word[index + 1] == second
                ):
                    merged.append(first + second)
                    index += 2
                else:
                    merged.append(word[index])
                    index += 1
            word = tuple(merged)
        self._cache[token] = word
        return word

    def encode(self, text: str) -> list[int]:
        """Encode text into GPT-2 token IDs."""

        token_ids: list[int] = []
        for chunk in _PATTERN.findall(text):
            mapped = "".join(
                self._byte_encoder[byte] for byte in chunk.encode("utf-8")
            )
            token_ids.extend(self._encoder[piece] for piece in self._bpe(mapped))
        return token_ids

    def decode(self, token_ids: Sequence[int]) -> str:
        """Decode GPT-2 token IDs into text."""

        text = "".join(self._decoder[index] for index in token_ids)
        return bytearray(self._byte_decoder[char] for char in text).decode(
            "utf-8", errors="replace"
        )
