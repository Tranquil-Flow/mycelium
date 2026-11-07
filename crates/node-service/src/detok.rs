// SPDX-License-Identifier: AGPL-3.0-only
//! Streaming detokenizer with v0.1.4's semantics.
//!
//! `mycelium_live/codec.py:44-67 @ f4e366e9` (`IncrementalTokenDecoder`): each streamed token
//! contributes its bytes to an incremental UTF-8 decoder (`errors="replace"`), stop ids are silent,
//! and the final flush turns an unfinished sequence into one U+FFFD. A token boundary inside a
//! character therefore emits nothing until the character is complete. The token's bytes come from
//! the byte-level vocabulary mapping (`Qwen2PromptCodec._token_bytes_reader`, codec.py:197-216):
//! each symbol of the token string is mapped back through GPT-2's `bytes_to_unicode`; a token
//! with any other symbol (added/special tokens) is spelled as its own UTF-8.
//!
//! The previous path decoded the whole token list each step and failed the request with
//! `nonappend_token_decode` when the text did not extend the earlier one, which is what an
//! emoji split across tokens does.

fn printable(byte: u32) -> bool {
    (33..=126).contains(&byte) || (161..=172).contains(&byte) || (174..=255).contains(&byte)
}

/// GPT-2 `bytes_to_unicode` inverse: the byte a vocabulary symbol stands for. Printable bytes map
/// to themselves; the other 68 bytes map to U+0100.. in byte order.
fn symbol_byte(symbol: char) -> Option<u8> {
    let code = u32::from(symbol);
    if printable(code) {
        return u8::try_from(code).ok();
    }
    (0u32..=255)
        .filter(|b| !printable(*b))
        .zip(256u32..)
        .find(|(_, mapped)| *mapped == code)
        .and_then(|(byte, _)| u8::try_from(byte).ok())
}

/// The bytes of a byte-level vocabulary token; the token's own UTF-8 when any symbol is not a
/// byte-level one (v0.1.4's `KeyError` fallback).
pub fn byte_level_bytes(token: &str) -> Vec<u8> {
    token
        .chars()
        .map(symbol_byte)
        .collect::<Option<Vec<u8>>>()
        .unwrap_or_else(|| token.as_bytes().to_vec())
}

pub struct Detokenizer {
    stop: Vec<u32>,
    pending: Vec<u8>,
}

impl Detokenizer {
    pub fn new(stop: &[u32]) -> Self {
        Self {
            stop: stop.to_vec(),
            pending: Vec::new(),
        }
    }
    /// New complete text for one streamed token whose vocabulary bytes are `bytes`.
    pub fn push(&mut self, id: u32, bytes: &[u8]) -> String {
        if self.stop.contains(&id) {
            return String::new();
        }
        self.pending.extend_from_slice(bytes);
        let mut out = String::new();
        loop {
            match std::str::from_utf8(&self.pending) {
                Ok(valid) => {
                    out.push_str(valid);
                    self.pending.clear();
                    return out;
                }
                Err(error) => {
                    let good = error.valid_up_to();
                    out.push_str(&String::from_utf8_lossy(&self.pending[..good]));
                    match error.error_len() {
                        // An invalid sequence: one replacement per maximal invalid prefix.
                        Some(len) => {
                            out.push('\u{fffd}');
                            self.pending.drain(..good + len);
                        }
                        // An unfinished sequence at the end: kept for the next token.
                        None => {
                            self.pending.drain(..good);
                            return out;
                        }
                    }
                }
            }
        }
    }
    /// Flush at the end of the stream: an unfinished sequence becomes one U+FFFD.
    pub fn finish(&mut self) -> String {
        if self.pending.is_empty() {
            String::new()
        } else {
            self.pending.clear();
            "\u{fffd}".to_owned()
        }
    }
}
