#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""RED tests for mycelium_openai_shim.qwen_tokenizer — the real Qwen2 tokenizer.

Ground truth: the product's own deployed tokenizer (tokenizer.json +
tokenizer_config.json) from the frozen transfer bundle of the standing origin
run (w6-ethonline-20260912T090309Z). The shim must encode/decode EXACTLY as the
gateway does; the char-based stub is a test-only fallback, never a silent
substitute for the real tokenizer.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mycelium_openai_shim.qwen_tokenizer import (  # noqa: E402
    load_qwen_tokenizer,
    build_qwen_chat_prompt,
    QwenTokenizerUnavailable,
)

FROZEN_DEPLOYMENT = Path(
    os.path.expanduser("~/mycelium-physical-run/w6-ethonline-20260912T090309Z/"
    "native-preparation-01/transfer-bundle/deployment")
)


class QwenTokenizerTests(unittest.TestCase):

    def test_loads_real_tokenizer_from_frozen_bundle(self):
        tok = load_qwen_tokenizer(FROZEN_DEPLOYMENT)
        self.assertIsNotNone(tok)
        # Special tokens from the deployed tokenizer_config.
        eos_id = tok.token_to_id("<|im_end|>")
        self.assertIsNotNone(eos_id)
        pad_id = tok.token_to_id("<|endoftext|>")
        self.assertIsNotNone(pad_id)

    def test_roundtrip_preserves_text(self):
        tok = load_qwen_tokenizer(FROZEN_DEPLOYMENT)
        sample = "A distributed pipeline splits a transformer by layers."
        ids = tok.encode(sample)
        self.assertTrue(ids)
        self.assertIsInstance(ids, tuple)
        self.assertTrue(all(isinstance(i, int) and not isinstance(i, bool) for i in ids))
        decoded = tok.decode(ids)
        self.assertIn("distributed", decoded)
        self.assertIn("transformer", decoded)

    def test_chat_prompt_builder_uses_qwen_markers(self):
        prompt = build_qwen_chat_prompt(
            messages=[
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
                {"role": "user", "content": "Bye"},
            ],
            system="You are concise.",
        )
        self.assertIn("<|im_start|>system", prompt)
        self.assertIn("You are concise.", prompt)
        self.assertIn("<|im_start|>user", prompt)
        self.assertIn("Hi", prompt)
        self.assertIn("<|im_start|>assistant", prompt)
        self.assertIn("Hello!", prompt)
        self.assertIn("<|im_end|>", prompt)
        # Final assistant cue for continuation.
        self.assertTrue(prompt.rstrip().endswith("<|im_start|>assistant"))

    def test_prompt_encodes_without_error(self):
        tok = load_qwen_tokenizer(FROZEN_DEPLOYMENT)
        prompt = build_qwen_chat_prompt(
            messages=[{"role": "user", "content": "why does greedy decoding work?"}])
        ids = tok.encode(prompt)
        self.assertGreater(len(ids), 0)

    def test_missing_directory_raises_loud_error(self):
        with self.assertRaises(QwenTokenizerUnavailable):
            load_qwen_tokenizer(Path("/nonexistent/tokenizer-dir"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
