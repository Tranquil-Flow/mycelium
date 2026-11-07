// SPDX-License-Identifier: AGPL-3.0-only
//! The chat edge's prompt envelope.
//!
//! v0.1.4's chat page posts the user's plain prompt and the gateway codec wraps it in the Qwen2
//! system/user/assistant envelope before tokenising (`mycelium_live/codec.py:20-24,126-132,153-157
//! @ f4e366e9`, `Qwen2PromptCodec`). The native service takes a finished ChatML string (the
//! explicit raw-prompt gateway of `native.rs`), so the chat edge does the wrapping. No trimming
//! or escaping: `str.format` in v0.1.4 substitutes the prompt verbatim. Expected values are the
//! executed oracle output `tests/fixtures/qwen2-chat-template-oracle.json` (FX-L11-chat-template).

/// The question the qualification probes type into the chat (the user part of the recorded oracle
/// workload; equals the first case of the template fixture).
pub const ORACLE_USER_PROMPT: &str =
    "Explain how rain forms and why plants need it. Give a detailed answer.";

/// `Qwen2PromptCodec._SYSTEM`, byte for byte.
pub const QWEN2_SYSTEM: &str = "You are a helpful and safe assistant. Follow the user's instructions, \
including exact output-format constraints; when a format is requested, \
return only that format without a preamble or commentary. \
Refuse requests that facilitate theft, credential abuse, violence, \
or other wrongdoing.";

/// `Qwen2PromptCodec.TEMPLATE.format(system=_SYSTEM, prompt=user)`.
pub fn qwen2_chat_prompt(user: &str) -> String {
    let mut chat = String::with_capacity(QWEN2_SYSTEM.len() + user.len() + 96);
    chat.push_str("<|im_start|>system\n");
    chat.push_str(QWEN2_SYSTEM);
    chat.push_str("<|im_end|>\n<|im_start|>user\n");
    chat.push_str(user);
    chat.push_str("<|im_end|>\n<|im_start|>assistant\n");
    chat
}
