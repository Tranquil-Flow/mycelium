// SPDX-License-Identifier: AGPL-3.0-only
//! The chat edge's v0.1.4 rules, as pure functions the native chat path calls before it admits a
//! request (the Rust node takes a finished, validated document; v0.1.4's `ChatService.send` and the
//! gateway codec decide these first):
//!
//! * `ChatService.send` (`mycelium_a13_macos/chat.py:303-313 @ f4e366e9`): body shape, prompt
//!   required / 2000-character cap, `max_new_tokens` default 128 / integer / clamp 256 + `clamped`;
//! * `ChatService._rate` (`chat.py:285-298`): 4 accepted sends per client and 30 swarm-wide per 60 s;
//! * the state document's `limits` (`chat.py:501-503`);
//! * `Qwen2PromptCodec.policy_response` (`mycelium_live/codec.py:159-173`), which the gateway applies
//!   before any model work (`mycelium_request_gateway/backend.py:196-204`).
//!
//! Expected values: FX-L11-chat-edge, executed v0.1.4 output (`tests/chat_edge.rs`).
//! SE-L11-CHAT-LIMITS: the five limits are source constants at `chat.py:39-43` that the L1 table does
//! not carry yet (only `chat.py:44` is there); they live here, pinned to the oracle by test, until L1
//! registers them.
use api::schema::PublicError;
use serde_json::{Value, json};
use std::collections::{HashMap, VecDeque};

/// `chat.py:39`.
pub const MAX_PROMPT_CHARS: usize = 2000;
/// `chat.py:40`.
pub const MAX_NEW_TOKENS_CAP: u64 = 256;
/// `chat.py:41`.
pub const DEFAULT_NEW_TOKENS: u64 = 128;
/// `chat.py:42`.
pub const PER_CLIENT_PER_MINUTE: usize = 4;
/// `chat.py:43`.
pub const GLOBAL_PER_MINUTE: usize = 30;
/// The window of `_rate` (`chat.py:290`, `now - queue[0] >= 60`), in seconds.
const RATE_WINDOW_SECONDS: f64 = 60.0;
/// `_rate` prunes empty per-client buckets once more than this many clients are tracked (`chat.py:297`).
const MAX_TRACKED_CLIENTS: usize = 4096;
/// The gateway's fixed answer to explicit credential theft (`codec.py:172`).
pub const POLICY_REFUSAL: &str = "I can't assist with credential theft.";

/// What `ChatService.send` hands the backend once the body is accepted.
#[derive(Debug, PartialEq, Eq)]
pub struct Plan {
    pub prompt: String,
    pub max_new_tokens: u64,
    pub clamped: bool,
}

/// Python's `str.isspace` (what `str.strip()` removes): the Unicode White_Space characters plus
/// U+001C..U+001F, which have bidirectional class B/S.
pub fn is_py_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

/// `ChatService.send`'s validation, in its order. `body` is the parsed JSON document.
pub fn plan(body: &Value) -> Result<Plan, PublicError> {
    let invalid = |code: &'static str| PublicError::new(400, code);
    let Some(fields) = body.as_object() else {
        return Err(invalid("chat_request_invalid"));
    };
    if !fields
        .keys()
        .all(|k| k == "prompt" || k == "max_new_tokens")
        || !fields.contains_key("prompt")
    {
        return Err(invalid("chat_request_invalid"));
    }
    let prompt = match &fields["prompt"] {
        Value::String(s) if !s.chars().all(is_py_space) => s.clone(),
        _ => return Err(invalid("chat_prompt_required")),
    };
    if prompt.chars().count() > MAX_PROMPT_CHARS {
        return Err(PublicError::new(413, "chat_prompt_too_long"));
    }
    let wanted = match fields.get("max_new_tokens") {
        None => DEFAULT_NEW_TOKENS,
        Some(Value::Number(n)) => {
            positive_integer(&n.to_string()).ok_or(invalid("chat_max_new_tokens_invalid"))?
        }
        Some(_) => return Err(invalid("chat_max_new_tokens_invalid")),
    };
    let max_new_tokens = wanted.min(MAX_NEW_TOKENS_CAP);
    Ok(Plan {
        prompt,
        max_new_tokens,
        clamped: wanted != max_new_tokens,
    })
}

/// A JSON number literal that Python's `json` reads as an `int` (no fraction or exponent) and that
/// is at least 1; astronomically large integers saturate (they are clamped to the cap anyway).
fn positive_integer(literal: &str) -> Option<u64> {
    let digits = literal.strip_prefix('-').map_or(Some(literal), |_| None)?;
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    match digits.parse::<u64>() {
        Ok(0) => None,
        Ok(v) => Some(v),
        Err(_) => Some(u64::MAX),
    }
}

/// The `limits` member of the chat state document.
pub fn limits_json() -> Value {
    json!({
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "max_new_tokens": MAX_NEW_TOKENS_CAP,
        "per_client_per_minute": PER_CLIENT_PER_MINUTE,
        "swarm_per_minute": GLOBAL_PER_MINUTE,
    })
}

/// `ChatService._rate`: sliding 60 s windows per client and swarm-wide. `now` is seconds on any
/// monotonic scale; only accepted requests are recorded.
#[derive(Default)]
pub struct RateWindow {
    per_client: HashMap<String, VecDeque<f64>>,
    global: VecDeque<f64>,
}
impl RateWindow {
    pub fn admit(&mut self, client: &str, now: f64) -> Result<(), PublicError> {
        let bucket = self.per_client.entry(client.to_owned()).or_default();
        for queue in [&mut *bucket, &mut self.global] {
            while queue
                .front()
                .is_some_and(|t| now - t >= RATE_WINDOW_SECONDS)
            {
                queue.pop_front();
            }
        }
        if bucket.len() >= PER_CLIENT_PER_MINUTE {
            return Err(PublicError::new(429, "chat_rate_limited"));
        }
        if self.global.len() >= GLOBAL_PER_MINUTE {
            return Err(PublicError::new(429, "chat_swarm_rate_limited"));
        }
        bucket.push_back(now);
        self.global.push_back(now);
        if self.per_client.len() > MAX_TRACKED_CLIENTS {
            self.per_client.retain(|_, q| !q.is_empty());
        }
        Ok(())
    }
}

/// Characters other than A-Z whose full case fold contains an ASCII letter, and that fold
/// (`str.casefold`, executed: `tests/fixtures/chat-edge-casefold-oracle.json`). Every other
/// non-ASCII character folds to text without an ASCII letter, so it cannot take part in a match
/// against the ASCII markers below; it only has to stay a distinct, non-matching character.
const ASCII_FOLDS: [(char, &str); 19] = [
    ('\u{df}', "ss"),
    ('\u{130}', "i\u{307}"),
    ('\u{149}', "\u{2bc}n"),
    ('\u{17f}', "s"),
    ('\u{1f0}', "j\u{30c}"),
    ('\u{1e96}', "h\u{331}"),
    ('\u{1e97}', "t\u{308}"),
    ('\u{1e98}', "w\u{30a}"),
    ('\u{1e99}', "y\u{30a}"),
    ('\u{1e9a}', "a\u{2be}"),
    ('\u{1e9e}', "ss"),
    ('\u{212a}', "k"),
    ('\u{fb00}', "ff"),
    ('\u{fb01}', "fi"),
    ('\u{fb02}', "fl"),
    ('\u{fb03}', "ffi"),
    ('\u{fb04}', "ffl"),
    ('\u{fb05}', "st"),
    ('\u{fb06}', "st"),
];

/// `str.casefold` as far as the policy markers can observe it (see [`ASCII_FOLDS`]).
pub fn fold(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if c.is_ascii() {
            out.push(c.to_ascii_lowercase());
        } else if let Some((_, folded)) = ASCII_FOLDS.iter().find(|(from, _)| *from == c) {
            out.push_str(folded);
        } else {
            out.push(c);
        }
    }
    out
}

/// `Qwen2PromptCodec.policy_response`: the fixed refusal for explicit credential theft.
pub fn policy_response(prompt: &str) -> Option<&'static str> {
    let folded = fold(prompt);
    let has = |markers: &[&str]| markers.iter().any(|m| folded.contains(m));
    let credential = has(&["password", "credential", "login"]);
    let explicit_abuse = folded.contains("steal")
        || (folded.contains("phishing") && has(&["write", "create", "compose", "design"]));
    (credential && explicit_abuse).then_some(POLICY_REFUSAL)
}
