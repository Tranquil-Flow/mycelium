// SPDX-License-Identifier: AGPL-3.0-only
//! The chat prompt codec: the ONE place a user's chat text becomes the model's prompt (lane
//! CHATFIX, ledger row 112 a). Every chat entry reaches it: the route chat's
//! [`crate::chat_service::ChatService::send`] (the page's `local` client and the relay's
//! `via_relay` client alike) and the development shell's one-Mac native chat.
//!
//! Port source: `mycelium_live/codec.py @ f4e366e9`.
//! * `_QWEN_TEMPLATE` (codec.py:17-21): the ChatML system/user/assistant envelope;
//!   `_QWEN3_TEMPLATE` (codec.py:25-28) = that envelope + the closed empty think block
//!   `<think>\n\n</think>\n\n` (Qwen3's shipped template rendered with `enable_thinking=False`, so
//!   the model answers directly instead of emitting a reasoning trace);
//! * `Qwen2PromptCodec._SYSTEM` (codec.py:125-131), shared by `Qwen3PromptCodec` (a subclass);
//! * `encode` (codec.py:153-157): `TEMPLATE.format(system=_SYSTEM, prompt=prompt)`, verbatim (no
//!   trimming or escaping: `str.format` substitutes the VALUE, so braces in a prompt stay literal),
//!   then the checkpoint's tokenizer with `add_special_tokens=False` (here: the route's tokenizer
//!   helper, `node_service::tokenize`, which encodes exactly that way);
//! * the stop ids (codec.py:135-139): `<|im_end|>` and `<|endoftext|>` of the checkpoint's
//!   tokenizer, 151645 and 151643 for every registered model (executed, FX-CHATFIX-prompt-codec);
//! * the selection (`prompt_codec_from_deployment`, codec.py:270-287): by architecture, `qwen2` ->
//!   Qwen2, `qwen3` -> Qwen3, anything else `unsupported_prompt_codec`. v0.2 reads the architecture
//!   from the run model's contract (`model_catalog::Model::architecture`, v0.1.4's
//!   `ModelContract.architecture`), which equals each registered snapshot's `config.json`
//!   `model_type`.
//!
//! Departures (refused by name, never a raw prompt): v0.1.4's `GPT2PromptCodec` (`{prompt}`, no
//! envelope) and `Qwen35PromptCodec` (same text as Qwen3 but its own 248k-id tokenizer and stop
//! ids) have no registered v0.2 model and are not ported.
//!
//! Expected values: FX-CHATFIX-prompt-codec, v0.1.4's codecs EXECUTED on the pinned Qwen3-0.6B
//! snapshot (`tests/fixtures/prompt-codec-oracle.json`).
use crate::schema::PublicError;

/// The question the qualification probes type into the chat (the user part of the recorded oracle
/// workload; the first case of the codec fixtures).
pub const ORACLE_USER_PROMPT: &str =
    "Explain how rain forms and why plants need it. Give a detailed answer.";

/// `Qwen2PromptCodec._SYSTEM` (codec.py:125-131), byte for byte.
pub const QWEN_SYSTEM: &str = "You are a helpful and safe assistant. Follow the user's instructions, \
including exact output-format constraints; when a format is requested, \
return only that format without a preamble or commentary. \
Refuse requests that facilitate theft, credential abuse, violence, \
or other wrongdoing.";

/// `_QWEN3_TEMPLATE`'s tail after the assistant cue (codec.py:28).
pub const QWEN3_EMPTY_THINK: &str = "<think>\n\n</think>\n\n";

/// `Qwen2PromptCodec._stop_token_ids` on every registered checkpoint, sorted as
/// `evidence_identity` lists them: `<|endoftext|>` 151643, `<|im_end|>` 151645.
pub const QWEN_STOP_TOKEN_IDS: [u32; 2] = [151_643, 151_645];

/// The refusal for a run model with no ported codec (`unsupported_prompt_codec:<model_type>`,
/// codec.py:287, without the model type: public codes are bounded).
pub const UNSUPPORTED: &str = "unsupported_prompt_codec";

/// A prompt codec of v0.1.4 (`mycelium_live/codec.py`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PromptCodec {
    /// `Qwen2PromptCodec`: ChatML + system.
    Qwen2,
    /// `Qwen3PromptCodec`: ChatML + system + the closed empty think block.
    Qwen3,
}

/// A chat prompt in its model's envelope, ready for the tokenizer. Only [`PromptCodec::chat`]
/// makes one, so no code can hand a route gateway a user's raw text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChatPrompt(String);
impl ChatPrompt {
    pub fn as_str(&self) -> &str {
        &self.0
    }
    pub fn into_string(self) -> String {
        self.0
    }
}

fn unsupported() -> PublicError {
    PublicError::new(503, UNSUPPORTED)
}

impl PromptCodec {
    /// `prompt_codec_from_deployment` (codec.py:270-287) by architecture (`model_type`).
    pub fn for_architecture(architecture: &str) -> Result<Self, PublicError> {
        match architecture {
            "qwen2" => Ok(Self::Qwen2),
            "qwen3" => Ok(Self::Qwen3),
            _ => Err(unsupported()),
        }
    }
    /// The codec of a registered run model (`app.run_model_contract()`); any other model id is
    /// refused [`UNSUPPORTED`].
    pub fn for_model(model_id: &str) -> Result<Self, PublicError> {
        let model = crate::model_catalog::registered()
            .iter()
            .find(|m| m.model_id == model_id)
            .ok_or_else(unsupported)?;
        Self::for_architecture(model.architecture())
    }
    /// `PROTOCOL` (codec.py:16, 22-24).
    pub fn protocol(self) -> &'static str {
        match self {
            Self::Qwen2 => "mycelium.prompt_codec.qwen_byte_bpe.instruct.incremental_utf8.v1",
            Self::Qwen3 => {
                "mycelium.prompt_codec.qwen3_byte_bpe.chat_no_thinking.incremental_utf8.v1"
            }
        }
    }
    /// The generation's stop ids (`stop_token_ids`, codec.py:135-139, 149-151): the route ends a
    /// chat at the first of them and never shows it.
    pub fn stop_token_ids(self) -> &'static [u32] {
        &QWEN_STOP_TOKEN_IDS
    }
    /// `TEMPLATE.format(system=_SYSTEM, prompt=user)` (codec.py:154), verbatim.
    pub fn chat(self, user: &str) -> ChatPrompt {
        let mut chat = String::with_capacity(QWEN_SYSTEM.len() + user.len() + 128);
        chat.push_str("<|im_start|>system\n");
        chat.push_str(QWEN_SYSTEM);
        chat.push_str("<|im_end|>\n<|im_start|>user\n");
        chat.push_str(user);
        chat.push_str("<|im_end|>\n<|im_start|>assistant\n");
        if self == Self::Qwen3 {
            chat.push_str(QWEN3_EMPTY_THINK);
        }
        ChatPrompt(chat)
    }
}
