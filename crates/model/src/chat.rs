// SPDX-License-Identifier: AGPL-3.0-only
//! Qwen3.6-35B-A3B chat template for the text-only, tool-free subset.
//!
//! Port source: the snapshot's own `chat_template.jinja` (sha256 in the recorded identity fixture
//! `tests/fixtures/qwen36-tokenizer-identity.json`, rendered by transformers 4.57.6 / jinja2 3.1.6).
//! Everything the template would do beyond this subset (tools, tool roles/responses, images/videos,
//! structured content, assistant reasoning, tool calls) is REFUSED, never approximated.
use stage_contract::{Error, ErrorCode, Result};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    System,
    User,
    Assistant,
}
#[derive(Debug, Clone, Copy)]
pub struct Message<'a> {
    pub role: Role,
    pub content: &'a str,
}
const START: &str = "<|im_start|>";
const END: &str = "<|im_end|>\n";

/// Jinja's `|trim` is Python `str.strip()`: Rust's `trim` misses U+001C..U+001F.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}
fn refuse(phase: &'static str) -> Error {
    Error::new(ErrorCode::Unsupported, phase)
}
/// Render with `add_generation_prompt=true`; `thinking=false` is `enable_thinking=false`
/// (the template's default is thinking on).
pub fn render(messages: &[Message<'_>], thinking: bool) -> Result<String> {
    if messages.is_empty() {
        return Err(refuse("chat-no-messages"));
    }
    let mut last_query = None;
    for (index, message) in messages.iter().enumerate() {
        let content = py_strip(message.content);
        if content.contains("</think>") || content.contains("<tool_response>") {
            return Err(refuse("chat-unsupported-content"));
        }
        match message.role {
            Role::System if index != 0 => return Err(refuse("chat-system-not-first")),
            Role::User => last_query = Some(index),
            _ => {}
        }
    }
    let last_query = last_query.ok_or_else(|| refuse("chat-no-user-query"))?;
    let mut out = String::new();
    for (index, message) in messages.iter().enumerate() {
        let content = py_strip(message.content);
        match message.role {
            Role::System => {
                out.push_str(START);
                out.push_str("system\n");
                out.push_str(content);
                out.push_str(END);
            }
            Role::User => {
                out.push_str(START);
                out.push_str("user\n");
                out.push_str(content);
                out.push_str(END);
            }
            Role::Assistant => {
                out.push_str(START);
                out.push_str("assistant\n");
                if index > last_query {
                    // reasoning_content is '' for plain content: '<think>\n' + '' + '\n</think>\n\n'
                    out.push_str("<think>\n\n</think>\n\n");
                }
                out.push_str(content);
                out.push_str(END);
            }
        }
    }
    out.push_str(START);
    out.push_str("assistant\n");
    out.push_str(if thinking {
        "<think>\n"
    } else {
        "<think>\n\n</think>\n\n"
    });
    Ok(out)
}
