# SPDX-License-Identifier: AGPL-3.0-only
import pytest
from test_codec_incremental import _qwen_tokenizer
from mycelium_live.codec import Qwen2PromptCodec


@pytest.mark.parametrize('content', ['é', '🌙', 'x'])
def test_added_tokens_match_full_native_decode_across_byte_boundary(content):
    tokenizer = _qwen_tokenizer()
    tokenizer.add_tokens([content])
    token_ids = [0, tokenizer.token_to_id(content), 1]
    decoder = Qwen2PromptCodec(tokenizer).new_decoder()
    streamed = ''.join(decoder.push(t) for t in token_ids) + decoder.finish()
    assert streamed == tokenizer.decode(token_ids, skip_special_tokens=False)
