// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-private replay checkpoint from `mycelium_a6_recovery.py:406-560` at f4e366e9.
//!
//! Private record only: the privacy-reduced public projection is validated by
//! [`crate::recovery`] and is not touched here. A valid record is not owner
//! authority, observed recovery, or permission to replay.
use crate::{ContractError, canonical};
use serde_json::{Number, Value, json};
use std::cmp::Ordering;

/// `CHECKPOINT_PROTOCOL` (`mycelium_a6_recovery.py:42`).
pub const CHECKPOINT_PROTOCOL: &str = "mycelium.private_request_replay_checkpoint.v1";

/// The `ReplayCheckpoint` dataclass keyword arguments (`:417-445`), as decoded JSON values.
///
/// Python checks each value's type at run time (`_text`, `_sha`, `_require_integer`), so the
/// arguments stay dynamic; [`ReplayCheckpoint::new`] is `__post_init__`. The dataclass defaults
/// of `encoded_prompt` and `committed_prefix_tokens` are `()`, i.e. an empty JSON array here.
#[derive(Clone, Debug, PartialEq)]
pub struct ReplayCheckpointArgs {
    pub protocol: Value,
    pub store_identity: Value,
    pub request_id: Value,
    pub session_id: Value,
    pub source_attempt: Value,
    pub path_id: Value,
    pub path_digest: Value,
    pub path_attempt: Value,
    pub prompt_digest: Value,
    pub generation_settings_digest: Value,
    pub committed_token_count: Value,
    pub committed_prefix_digest: Value,
    pub delivery_cursor: Value,
    pub watermark: Value,
    pub publisher_generation: Value,
    pub deployment_id: Value,
    pub deployment_epoch: Value,
    pub topology_generation: Value,
    pub source_qualification_digest: Value,
    pub model_id: Value,
    pub representation_digest: Value,
    pub tokenizer_digest: Value,
    pub recovery_attempt: Value,
    pub terminal_authority: Value,
    pub created_at_unix_ms: Value,
    pub expiry_unix_ms: Value,
    pub reconciliation_state: Value,
    pub encoded_prompt: Value,
    pub committed_prefix_tokens: Value,
}

/// A checkpoint that passed `__post_init__` (`:447-510`). Integers are Python ints
/// (unbounded), kept as exact JSON numbers.
#[derive(Clone, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub struct ReplayCheckpoint {
    pub protocol: String,
    pub store_identity: String,
    pub request_id: String,
    pub session_id: String,
    pub source_attempt: Number,
    pub path_id: String,
    pub path_digest: String,
    pub path_attempt: Number,
    pub prompt_digest: String,
    pub generation_settings_digest: String,
    pub committed_token_count: Number,
    pub committed_prefix_digest: Option<String>,
    pub delivery_cursor: Number,
    pub watermark: Number,
    pub publisher_generation: Number,
    pub deployment_id: String,
    pub deployment_epoch: Number,
    pub topology_generation: Number,
    pub source_qualification_digest: String,
    pub model_id: String,
    pub representation_digest: String,
    pub tokenizer_digest: String,
    pub recovery_attempt: Number,
    pub terminal_authority: Option<String>,
    pub created_at_unix_ms: Number,
    pub expiry_unix_ms: Number,
    pub reconciliation_state: String,
    pub encoded_prompt: Vec<Number>,
    pub committed_prefix_tokens: Vec<Number>,
}

impl ReplayCheckpoint {
    /// `ReplayCheckpoint(...)` with `__post_init__` (`:447-483`, `_verify_bindings` `:485-510`).
    /// Checks run in source order; the first failure is the oracle's exact `ValueError` text.
    pub fn new(a: &ReplayCheckpointArgs) -> Result<Self, ContractError> {
        let protocol = text(
            &a.protocol,
            96,
            "protocol must be a bounded non-empty string",
        )?;
        require(
            protocol == CHECKPOINT_PROTOCOL,
            "checkpoint_protocol_mismatch",
        )?;
        let store_identity = text(
            &a.store_identity,
            128,
            "store_identity must be a bounded non-empty string",
        )?;
        let request_id = text(
            &a.request_id,
            128,
            "request_id must be a bounded non-empty string",
        )?;
        let session_id = text(
            &a.session_id,
            128,
            "session_id must be a bounded non-empty string",
        )?;
        let path_id = text(
            &a.path_id,
            128,
            "path_id must be a bounded non-empty string",
        )?;
        let deployment_id = text(
            &a.deployment_id,
            128,
            "deployment_id must be a bounded non-empty string",
        )?;
        let model_id = text(
            &a.model_id,
            128,
            "model_id must be a bounded non-empty string",
        )?;
        let path_digest = sha(
            &a.path_digest,
            "path_digest must be a bounded non-empty string",
            "path_digest must be a lowercase SHA-256 reference",
        )?;
        let prompt_digest = sha(
            &a.prompt_digest,
            "prompt_digest must be a bounded non-empty string",
            "prompt_digest must be a lowercase SHA-256 reference",
        )?;
        let generation_settings_digest = sha(
            &a.generation_settings_digest,
            "generation_settings_digest must be a bounded non-empty string",
            "generation_settings_digest must be a lowercase SHA-256 reference",
        )?;
        let representation_digest = sha(
            &a.representation_digest,
            "representation_digest must be a bounded non-empty string",
            "representation_digest must be a lowercase SHA-256 reference",
        )?;
        let tokenizer_digest = sha(
            &a.tokenizer_digest,
            "tokenizer_digest must be a bounded non-empty string",
            "tokenizer_digest must be a lowercase SHA-256 reference",
        )?;
        let source_qualification_digest = sha(
            &a.source_qualification_digest,
            "source_qualification_digest must be a bounded non-empty string",
            "source_qualification_digest must be a lowercase SHA-256 reference",
        )?;
        // :463-468. Floors are the oracle's literal `minimum=` operands: no BudgetId is
        // registered for them and protocol takes no budgets dependency.
        let source_attempt = integer(
            &a.source_attempt,
            1,
            "source_attempt must be an integer >= 1",
        )?;
        let path_attempt = integer(&a.path_attempt, 0, "path_attempt must be an integer >= 0")?;
        let recovery_attempt = integer(
            &a.recovery_attempt,
            0,
            "recovery_attempt must be an integer >= 0",
        )?;
        let deployment_epoch = integer(
            &a.deployment_epoch,
            1,
            "deployment_epoch must be an integer >= 1",
        )?;
        let topology_generation = integer(
            &a.topology_generation,
            1,
            "topology_generation must be an integer >= 1",
        )?;
        // :469-470
        let committed_token_count = integer(
            &a.committed_token_count,
            0,
            "committed_token_count must be an integer >= 0",
        )?;
        let watermark = integer(&a.watermark, 0, "watermark must be an integer >= 0")?;
        // :471-472: -1 means "nothing committed yet" (gateway acknowledged_through).
        let delivery_cursor = integer(
            &a.delivery_cursor,
            -1,
            "delivery_cursor must be an integer >= -1",
        )?;
        // :473-475
        let publisher_generation = integer(
            &a.publisher_generation,
            0,
            "publisher_generation must be an integer >= 0",
        )?;
        let created_at_unix_ms = integer(
            &a.created_at_unix_ms,
            1,
            "created_at_unix_ms must be an integer >= 1",
        )?;
        let expiry_unix_ms = integer(
            &a.expiry_unix_ms,
            1,
            "expiry_unix_ms must be an integer >= 1",
        )?;
        // :476-477
        require(
            expiry_unix_ms > created_at_unix_ms,
            "checkpoint_already_expired",
        )?;
        // :478-480
        let reconciliation_state = text(
            &a.reconciliation_state,
            32,
            "reconciliation_state must be a bounded non-empty string",
        )?;
        require(
            ["active", "frozen", "reconciled"].contains(&reconciliation_state.as_str()),
            "checkpoint_reconciliation_state_invalid",
        )?;
        // :481-482
        let terminal_authority = match &a.terminal_authority {
            Value::Null => None,
            v => Some(text(
                v,
                64,
                "terminal_authority must be a bounded non-empty string",
            )?),
        };
        // _verify_bindings (:485-510). JSON arrays stand for the oracle's tuples.
        let encoded_prompt = token_ids(&a.encoded_prompt)
            .filter(|t| !t.is_empty())
            .ok_or(ContractError("checkpoint_encoded_prompt_invalid"))?;
        let committed_prefix_tokens = token_ids(&a.committed_prefix_tokens)
            .ok_or(ContractError("checkpoint_prefix_tokens_invalid"))?;
        require(
            prompt_digest == token_digest(&encoded_prompt),
            "checkpoint_prompt_digest_mismatch",
        )?;
        let committed_prefix_digest = match &a.committed_prefix_digest {
            Value::Null => {
                require(
                    committed_prefix_tokens.is_empty() && committed_token_count.is_zero(),
                    "checkpoint_prefix_digest_missing",
                )?;
                None
            }
            v => {
                let expected = token_digest(&committed_prefix_tokens);
                let digest = v
                    .as_str()
                    .filter(|d| *d == expected)
                    .ok_or(ContractError("checkpoint_prefix_digest_mismatch"))?;
                require(
                    Int::of_len(committed_prefix_tokens.len()) == committed_token_count,
                    "checkpoint_prefix_count_mismatch",
                )?;
                Some(digest.to_owned())
            }
        };
        require(
            watermark == committed_token_count,
            "checkpoint_watermark_mismatch",
        )?;
        Ok(Self {
            protocol,
            store_identity,
            request_id,
            session_id,
            source_attempt: source_attempt.number(),
            path_id,
            path_digest,
            path_attempt: path_attempt.number(),
            prompt_digest,
            generation_settings_digest,
            committed_token_count: committed_token_count.number(),
            committed_prefix_digest,
            delivery_cursor: delivery_cursor.number(),
            watermark: watermark.number(),
            publisher_generation: publisher_generation.number(),
            deployment_id,
            deployment_epoch: deployment_epoch.number(),
            topology_generation: topology_generation.number(),
            source_qualification_digest,
            model_id,
            representation_digest,
            tokenizer_digest,
            recovery_attempt: recovery_attempt.number(),
            terminal_authority,
            created_at_unix_ms: created_at_unix_ms.number(),
            expiry_unix_ms: expiry_unix_ms.number(),
            reconciliation_state,
            encoded_prompt,
            committed_prefix_tokens,
        })
    }

    /// `_canonical_document` (`:512-543`): every field, the private token sequences included.
    pub fn canonical_document(&self) -> Value {
        json!({
            "protocol": self.protocol,
            "store_identity": self.store_identity,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "source_attempt": self.source_attempt,
            "path_id": self.path_id,
            "path_digest": self.path_digest,
            "path_attempt": self.path_attempt,
            "prompt_digest": self.prompt_digest,
            "generation_settings_digest": self.generation_settings_digest,
            "committed_token_count": self.committed_token_count,
            "committed_prefix_digest": self.committed_prefix_digest,
            "delivery_cursor": self.delivery_cursor,
            "watermark": self.watermark,
            "publisher_generation": self.publisher_generation,
            "deployment_id": self.deployment_id,
            "deployment_epoch": self.deployment_epoch,
            "topology_generation": self.topology_generation,
            "source_qualification_digest": self.source_qualification_digest,
            "model_id": self.model_id,
            "representation_digest": self.representation_digest,
            "tokenizer_digest": self.tokenizer_digest,
            "recovery_attempt": self.recovery_attempt,
            "terminal_authority": self.terminal_authority,
            "created_at_unix_ms": self.created_at_unix_ms,
            "expiry_unix_ms": self.expiry_unix_ms,
            "reconciliation_state": self.reconciliation_state,
            "encoded_prompt": self.encoded_prompt,
            "committed_prefix_tokens": self.committed_prefix_tokens,
        })
    }

    /// `checkpoint_digest` (`:545-546`): `_digest` (`:123-125`) keeps `ensure_ascii=True`.
    pub fn checkpoint_digest(&self) -> String {
        let bytes = canonical::to_ascii_bytes(&self.canonical_document())
            .expect("checkpoint document holds no floats");
        canonical::sha256_prefixed(&bytes)
    }
}

/// A Python `int` decoded from JSON: exact and unbounded, `-0` folded to `0`.
#[derive(Clone, Debug)]
struct Int {
    negative: bool,
    /// Decimal digits; JSON allows no leading zeros.
    magnitude: String,
    literal: Number,
}

impl Int {
    /// `type(value) is int` (`:144`): bool is not a JSON number, and a float literal keeps
    /// its `.`, `e` or `E`.
    fn parse(value: &Value) -> Option<Self> {
        let literal = value.as_number()?;
        let text = literal.to_string();
        let (negative, digits) = match text.strip_prefix('-') {
            Some(digits) => (true, digits),
            None => (false, text.as_str()),
        };
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        Some(Self {
            negative: negative && digits != "0",
            magnitude: digits.to_owned(),
            literal: literal.clone(),
        })
    }

    fn of(n: i64) -> Self {
        Self {
            negative: n < 0,
            magnitude: n.unsigned_abs().to_string(),
            literal: Number::from(n),
        }
    }

    fn of_len(n: usize) -> Self {
        Self {
            negative: false,
            magnitude: n.to_string(),
            literal: Number::from(n),
        }
    }

    fn is_zero(&self) -> bool {
        self.magnitude == "0"
    }

    /// The exact JSON number Python would emit for this int.
    fn number(&self) -> Number {
        if self.is_zero() {
            Number::from(0_u8)
        } else {
            self.literal.clone()
        }
    }
}

impl Ord for Int {
    fn cmp(&self, other: &Self) -> Ordering {
        let magnitude = self
            .magnitude
            .len()
            .cmp(&other.magnitude.len())
            .then_with(|| self.magnitude.cmp(&other.magnitude));
        match (self.negative, other.negative) {
            (false, false) => magnitude,
            (true, true) => magnitude.reverse(),
            (false, true) => Ordering::Greater,
            (true, false) => Ordering::Less,
        }
    }
}

impl PartialOrd for Int {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl PartialEq for Int {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}

impl Eq for Int {}

fn require(ok: bool, code: &'static str) -> Result<(), ContractError> {
    if ok { Ok(()) } else { Err(ContractError(code)) }
}

/// `_text` (`:128-131`): a non-empty string of at most `maximum` code points.
fn text(value: &Value, maximum: usize, code: &'static str) -> Result<String, ContractError> {
    value
        .as_str()
        .filter(|s| !s.is_empty() && s.chars().count() <= maximum)
        .map(str::to_owned)
        .ok_or(ContractError(code))
}

/// `_sha` (`:134-140`): `_text(maximum=71)` first, then `sha256:` and 64 lowercase hex.
fn sha(
    value: &Value,
    text_code: &'static str,
    sha_code: &'static str,
) -> Result<String, ContractError> {
    let value = text(value, 71, text_code)?;
    require(
        value.chars().count() == 71
            && value.starts_with("sha256:")
            && value[7..]
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        sha_code,
    )?;
    Ok(value)
}

/// `_require_integer` (`:157-158`, via `_integer` `:143-146`).
fn integer(value: &Value, minimum: i64, code: &'static str) -> Result<Int, ContractError> {
    Int::parse(value)
        .filter(|n| *n >= Int::of(minimum))
        .ok_or(ContractError(code))
}

/// A JSON array of `type(token_id) is int and token_id >= 0` (`:487-497`).
fn token_ids(value: &Value) -> Option<Vec<Number>> {
    value
        .as_array()?
        .iter()
        .map(|t| Int::parse(t).filter(|n| !n.negative).map(|n| n.number()))
        .collect()
}

/// `_digest(tokens)` / `_prefix_digest(tokens)` (`:123-125`, `:399-400`): sha256 of the
/// compact JSON integer array.
fn token_digest(tokens: &[Number]) -> String {
    let items: Vec<String> = tokens.iter().map(Number::to_string).collect();
    canonical::sha256_prefixed(format!("[{}]", items.join(",")).as_bytes())
}
