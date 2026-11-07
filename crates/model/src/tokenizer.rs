// SPDX-License-Identifier: AGPL-3.0-only
//! Qwen3.6 tokenizer: the same `tokenizers` crate the product already uses (node-service) over the
//! snapshot's `tokenizer.json`, which v0.1.4 loads with the Python `tokenizers` 0.22.2
//! (`mycelium_openai_shim/qwen_tokenizer.py:48-60 @ f4e366e9`). Ids are checked against the recorded
//! identity fixture; no BPE or pre-tokenizer logic is hand-written.
use stage_contract::{Error, ErrorCode, Result};
use std::path::Path;
use tokenizers::Tokenizer;

pub struct Qwen36Tokenizer(Tokenizer);
impl Qwen36Tokenizer {
    pub fn from_file(path: &Path) -> Result<Self> {
        Tokenizer::from_file(path)
            .map(Self)
            .map_err(|_| Error::new(ErrorCode::Artifact, "tokenizer-load"))
    }
    /// `encode(text).ids` with the tokenizer's own (empty) post-processing, as the oracle does.
    pub fn encode(&self, text: &str) -> Result<Vec<u32>> {
        self.0
            .encode(text, false)
            .map(|e| e.get_ids().to_vec())
            .map_err(|_| Error::new(ErrorCode::Artifact, "tokenizer-encode"))
    }
    pub fn token_to_id(&self, token: &str) -> Option<u32> {
        self.0.token_to_id(token)
    }
    pub fn vocab_size_with_added(&self) -> usize {
        self.0.get_vocab_size(true)
    }
}
