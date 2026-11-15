// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned model catalog and the consent numbers derived from it: model_resources.py
//! (shapes, whole-model member, ceilings, `consent_document`) and serving.py (the registered
//! contracts) @ f4e366e9, checked against the executed oracle
//! (`tests/fixtures/oracle_run_model.json`). All arithmetic is exact integer arithmetic; the
//! oracle's `Fraction` factors are numerator/denominator pairs rounded up (`math.ceil`) or
//! down exactly where the source does. Nothing here loads tensors, reads the network or
//! qualifies a host.
use crate::wire::Json;
use budgets::BudgetId;
use serde_json::Value;

/// Disk headroom over a member's computed need (7B: 10%).
const DISK_HEADROOM: (u128, u128) = (110, 100);
/// Headroom over a member's group load peak for the guard.
const GUARD_HEADROOM: (u128, u128) = (115, 100);
const LOAD_PEAK_FACTOR: u128 = 2;
const GROUP_FIXED_BYTES: u128 = 1 << 30;
/// int8-weight-only resident bytes per bf16 source byte (every registered contract).
const RESIDENT_FACTOR: (u128, u128) = (501, 1000);
/// float32 KV element size (every registered contract).
const KV_BYTES_PER_ELEMENT: u128 = 4;
/// The serving context of every registered model today, in tokens: one request's prompt plus its
/// new tokens. v0.1.4's `DEMO_CONTEXT_TOKENS` (model_resources.py:72-76 @ f4e366e9: a 2,000-character
/// prompt, a template under 100 tokens and 256 new tokens, rounded up to 4,096). Owner ruling 15 Nov:
/// the stage capacity is this per-model setting; raising it is a contract value plus the memory
/// consent that covers it, not a code change.
const SERVING_CONTEXT_TOKENS: u64 = 4096;
/// Largest guard limit a member may run under, as a share of its RAM.
const MEMBER_PEAK_MEMORY_FRACTION: (u128, u128) = (3, 4);
const MEMORY_CAP_CHOICES: [(u128, u128); 3] = [(1, 2), (5, 8), (3, 4)];
const MEMORY_CAP_GRANULARITY: u128 = 256 * 1024 * 1024;
/// The runner derives each member's worker limits as proposal / 2.
const RUNNER_LIMIT_DIVISOR: u128 = 2;
const SOURCE_BYTES_PER_PARAMETER: u128 = 2;

struct Shape {
    label: &'static str,
    num_layers: u128,
    hidden: u128,
    intermediate: u128,
    heads: u128,
    kv_heads: u128,
    head_dim: u128,
    vocab: u128,
    tie_word_embeddings: bool,
    attention_bias: bool,
    qk_norm: bool,
    snapshot_bytes: u128,
    stores_tied_lm_head: bool,
}
/// Measured numbers that override the model for one contract (7B only).
struct Anchor {
    group_peak_bytes: u128,
    member_disk_bytes: u128,
    owner_storage_bytes: u128,
}
pub struct Model {
    pub model_id: &'static str,
    pub resolved_commit: &'static str,
    shape: Shape,
    anchor: Option<Anchor>,
    /// Tokens one request may hold on every stage (prompt plus new tokens).
    serving_context_tokens: u64,
}

/// `serving.REGISTERED_MODELS` order: candidate, A9 route, Qwen3 0.6B / 8B / 14B.
static REGISTERED: [Model; 5] = [
    Model {
        model_id: "Qwen/Qwen2.5-7B-Instruct",
        resolved_commit: "a09a35458c702b33eeacc393d103063234e8bc28",
        shape: Shape {
            label: "Qwen2.5-7B",
            num_layers: 28,
            hidden: 3584,
            intermediate: 18944,
            heads: 28,
            kv_heads: 4,
            head_dim: 128,
            vocab: 152064,
            tie_word_embeddings: false,
            attention_bias: true,
            qk_norm: false,
            snapshot_bytes: 15_242_807_270,
            stores_tied_lm_head: false,
        },
        anchor: Some(Anchor {
            group_peak_bytes: 7_371_915_264,
            member_disk_bytes: 11_297_336_366,
            owner_storage_bytes: 23_052_975_062,
        }),
        serving_context_tokens: SERVING_CONTEXT_TOKENS,
    },
    Model {
        model_id: "Qwen/Qwen2.5-0.5B-Instruct",
        resolved_commit: "7ae557604adf67be50417f59c2c2f167def9a775",
        shape: Shape {
            label: "Qwen2.5-0.5B",
            num_layers: 24,
            hidden: 896,
            intermediate: 4864,
            heads: 14,
            kv_heads: 2,
            head_dim: 64,
            vocab: 151936,
            tie_word_embeddings: true,
            attention_bias: true,
            qk_norm: false,
            snapshot_bytes: 999_604_126,
            stores_tied_lm_head: false,
        },
        anchor: None,
        serving_context_tokens: SERVING_CONTEXT_TOKENS,
    },
    Model {
        model_id: "Qwen/Qwen3-0.6B",
        resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca",
        shape: Shape {
            label: "Qwen3-0.6B",
            num_layers: 28,
            hidden: 1024,
            intermediate: 3072,
            heads: 16,
            kv_heads: 8,
            head_dim: 128,
            vocab: 151936,
            tie_word_embeddings: true,
            attention_bias: false,
            qk_norm: true,
            snapshot_bytes: 1_519_209_243,
            stores_tied_lm_head: true,
        },
        anchor: None,
        serving_context_tokens: SERVING_CONTEXT_TOKENS,
    },
    Model {
        model_id: "Qwen/Qwen3-8B",
        resolved_commit: "b968826d9c46dd6066d109eabc6255188de91218",
        shape: Shape {
            label: "Qwen3-8B",
            num_layers: 36,
            hidden: 4096,
            intermediate: 12288,
            heads: 32,
            kv_heads: 8,
            head_dim: 128,
            vocab: 151936,
            tie_word_embeddings: false,
            attention_bias: false,
            qk_norm: true,
            snapshot_bytes: 16_397_461_266,
            stores_tied_lm_head: false,
        },
        anchor: None,
        serving_context_tokens: SERVING_CONTEXT_TOKENS,
    },
    Model {
        model_id: "Qwen/Qwen3-14B",
        resolved_commit: "40c069824f4251a91eefaf281ebe4c544efd3e18",
        shape: Shape {
            label: "Qwen3-14B",
            num_layers: 40,
            hidden: 5120,
            intermediate: 17408,
            heads: 40,
            kv_heads: 8,
            head_dim: 128,
            vocab: 151936,
            tie_word_embeddings: false,
            attention_bias: false,
            qk_norm: true,
            snapshot_bytes: 29_552_613_776,
            stores_tied_lm_head: false,
        },
        anchor: None,
        serving_context_tokens: SERVING_CONTEXT_TOKENS,
    },
];

impl Model {
    /// `model_shape(model).label`.
    pub fn label(&self) -> &'static str {
        self.shape.label
    }
    /// `ModelContract.architecture` (`_MODEL_ARCHITECTURES`, default `qwen2`).
    pub fn architecture(&self) -> &'static str {
        crate::serving_source::architecture(self)
    }
    /// The serving context of this model's contract, in tokens: the one source the compile's
    /// memory plan, every stage's prepare and the route's refusal read.
    pub fn serving_context_tokens(&self) -> u64 {
        self.serving_context_tokens
    }
}
/// `run_model.choice_for_model(model).name`: the picker choice that pins this model (the 7B candidate has none).
pub fn choice_for_model(model: &Model) -> Option<&'static str> {
    let index = REGISTERED
        .iter()
        .position(|m| m.model_id == model.model_id && m.resolved_commit == model.resolved_commit)?;
    crate::preferences::run_model::CHOICES
        .get(index.checked_sub(1)?)
        .copied()
}

pub fn registered() -> &'static [Model] {
    &REGISTERED
}
pub fn find(model_id: &str, resolved_commit: &str) -> Option<&'static Model> {
    REGISTERED
        .iter()
        .find(|m| m.model_id == model_id && m.resolved_commit == resolved_commit)
}
/// The serving context of the registered contract with this model id (ids are unique in the
/// catalog), for a reader that knows only the run's model id (the chat page's `limits`).
pub fn serving_context_for(model_id: &str) -> Option<u64> {
    let mut matching = REGISTERED.iter().filter(|m| m.model_id == model_id);
    let model = matching.next()?;
    matching
        .next()
        .is_none()
        .then_some(model.serving_context_tokens)
}
/// `serving._RUN_MODEL_NAMES`: the environment override accepts `candidate` as well.
pub fn by_run_model_name(name: &str) -> Option<&'static Model> {
    let index = match name {
        "candidate" => 0,
        "a9-route" => 1,
        "qwen3-0.6b" => 2,
        "qwen3-8b" => 3,
        "qwen3-14b" => 4,
        _ => return None,
    };
    REGISTERED.get(index)
}

fn ceil_ratio(value: u128, (num, den): (u128, u128)) -> u128 {
    (value * num).div_ceil(den)
}

impl Shape {
    fn layer_parameters(&self) -> u128 {
        let (h, q, kv) = (
            self.hidden,
            self.heads * self.head_dim,
            self.kv_heads * self.head_dim,
        );
        let mut params = h * q + 2 * h * kv + q * h + 3 * h * self.intermediate + 2 * h;
        if self.attention_bias {
            params += q + 2 * kv;
        }
        if self.qk_norm {
            params += 2 * self.head_dim;
        }
        params
    }
    fn layer_source_bytes(&self) -> u128 {
        self.layer_parameters() * SOURCE_BYTES_PER_PARAMETER
    }
    fn embedding_source_bytes(&self) -> u128 {
        self.vocab * self.hidden * SOURCE_BYTES_PER_PARAMETER
    }
    /// Final norm, plus lm_head when untied (or stored although tied).
    fn output_source_bytes(&self) -> u128 {
        let mut params = self.hidden;
        if !self.tie_word_embeddings || self.stores_tied_lm_head {
            params += self.vocab * self.hidden;
        }
        params * SOURCE_BYTES_PER_PARAMETER
    }
    fn weight_source_bytes(&self) -> u128 {
        self.num_layers * self.layer_source_bytes()
            + self.embedding_source_bytes()
            + self.output_source_bytes()
    }
    /// `_whole_model_split`: embedding + every layer + the tail (output, plus the tied copy).
    fn whole_stage_bytes(&self) -> u128 {
        let tail = self.output_source_bytes()
            + if self.tie_word_embeddings {
                self.embedding_source_bytes()
            } else {
                0
            };
        self.embedding_source_bytes() + self.num_layers * self.layer_source_bytes() + tail
    }
}

struct Member {
    stage_source_bytes: u128,
    resident_bytes: u128,
    kv_bytes: u128,
    load_peak_bytes: u128,
    max_rss_bytes: u128,
    disk_need_bytes: u128,
    max_disk_bytes: u128,
    disk_footprint_bytes: u128,
    basis: &'static str,
}
fn budget_bytes(id: BudgetId) -> u128 {
    // Registered, closed ids: the table always resolves them.
    id.bytes().map_or(0, |b| u128::from(b.get()))
}
fn budget_count(id: BudgetId) -> u128 {
    id.count().map_or(0, |c| u128::from(c.get()))
}
/// `_member_resource` for the whole-model split (slice mode, no owner, no memory report).
fn whole_member(model: &Model) -> Member {
    let shape = &model.shape;
    let stage = shape.whole_stage_bytes();
    let resident = ceil_ratio(stage, RESIDENT_FACTOR);
    let kv_per_layer = 2 * shape.kv_heads * shape.head_dim * KV_BYTES_PER_ELEMENT;
    let kv = shape.num_layers * kv_per_layer * u128::from(model.serving_context_tokens);
    let (peak, disk_need, basis) = match &model.anchor {
        Some(a) => (a.group_peak_bytes, a.member_disk_bytes, "measured"),
        None => (
            LOAD_PEAK_FACTOR * resident + GROUP_FIXED_BYTES + kv,
            stage
                + budget_bytes(
                    BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxInlineBytesPerNodeL64,
                )
                + budget_bytes(BudgetId::ControlArchiveAllowance),
            "modelled",
        ),
    };
    Member {
        stage_source_bytes: stage,
        resident_bytes: resident,
        kv_bytes: kv,
        load_peak_bytes: peak,
        max_rss_bytes: ceil_ratio(peak, GUARD_HEADROOM),
        disk_need_bytes: disk_need,
        max_disk_bytes: ceil_ratio(disk_need, DISK_HEADROOM),
        disk_footprint_bytes: budget_count(BudgetId::StagedCopyFactor) * disk_need,
        basis,
    }
}
fn u(value: u128) -> u64 {
    u64::try_from(value).unwrap_or(u64::MAX)
}

/// `member_resources(model, whole split)[0]` of the registered model, by id.
pub fn whole_model_member_json(model_id: &str) -> Option<Json> {
    let model = REGISTERED.iter().find(|m| m.model_id == model_id)?;
    let m = whole_member(model);
    Some(crate::jobj! {
        "node_id" => "whole-model", "start" => 0u64, "end" => u(model.shape.num_layers),
        "layers" => u(model.shape.num_layers), "stage_source_bytes" => u(m.stage_source_bytes),
        "resident_bytes" => u(m.resident_bytes), "kv_bytes" => u(m.kv_bytes),
        "load_peak_bytes" => u(m.load_peak_bytes), "max_rss_bytes" => u(m.max_rss_bytes),
        "disk_need_bytes" => u(m.disk_need_bytes), "max_disk_bytes" => u(m.max_disk_bytes),
        "disk_footprint_bytes" => u(m.disk_footprint_bytes), "basis" => m.basis,
        "memory_bytes" => Json::Null, "memory_ceiling_bytes" => Json::Null,
    })
}
pub fn whole_model_member(model_id: &str) -> Option<Value> {
    whole_model_member_json(model_id).map(|j| j.to_value())
}

/// Storage the owner must consent to for this model (snapshot + 10%; 7B: the measured figure).
pub fn owner_storage_requirement(model: &Model) -> u64 {
    u(match &model.anchor {
        Some(a) => a.owner_storage_bytes,
        None => ceil_ratio(model.shape.snapshot_bytes, DISK_HEADROOM),
    })
}
/// Pairing quota a model can give one member: the whole model (anchored: its proposal, i.e.
/// `RUNNER_LIMIT_DIVISOR` x the member disk limit).
fn quota_of(model: &Model) -> u128 {
    let disk = whole_member(model).max_disk_bytes;
    if model.anchor.is_some() {
        RUNNER_LIMIT_DIVISOR * disk
    } else {
        disk
    }
}
fn model_storage_ceiling(model: &Model) -> u128 {
    u128::from(owner_storage_requirement(model)).max(quota_of(model))
}
/// `app.consent_valid`'s bound: the largest per-model storage ceiling.
pub fn storage_consent_ceiling() -> u64 {
    u(REGISTERED
        .iter()
        .map(model_storage_ceiling)
        .max()
        .unwrap_or(0))
}
pub fn pairing_quota_ceiling() -> u64 {
    u(REGISTERED.iter().map(quota_of).max().unwrap_or(0))
}
pub fn member_memory_ceiling(memory_bytes: u64) -> u64 {
    u(u128::from(memory_bytes) * MEMBER_PEAK_MEMORY_FRACTION.0 / MEMBER_PEAK_MEMORY_FRACTION.1)
}
/// Realistic per-run memory limits a Mac with this RAM may consent to.
pub fn memory_cap_options(memory_bytes: u64) -> Vec<u64> {
    let ceiling = u128::from(member_memory_ceiling(memory_bytes));
    let mut values = std::collections::BTreeSet::new();
    for (num, den) in MEMORY_CAP_CHOICES {
        let value =
            u128::from(memory_bytes) * num / den / MEMORY_CAP_GRANULARITY * MEMORY_CAP_GRANULARITY;
        if value > 0 && value <= ceiling {
            values.insert(u(value));
        }
    }
    values.into_iter().collect()
}

fn model_row(model: &Model) -> Json {
    let whole = whole_member(model);
    crate::jobj! {
        "model_id" => model.model_id, "resolved_commit" => model.resolved_commit,
        "label" => model.shape.label, "snapshot_bytes" => u(model.shape.snapshot_bytes),
        "weight_source_bytes" => u(model.shape.weight_source_bytes()),
        "owner_storage_bytes" => owner_storage_requirement(model),
        "whole_model_member_disk_bytes" => u(whole.max_disk_bytes),
        "whole_model_member_rss_bytes" => u(whole.max_rss_bytes),
    }
}
/// `consent_document(model, device_memory_bytes=...)`: the numbers the consent UI shows, in the
/// source's dict order.
pub fn consent_document_json(
    selected: Option<(&str, &str)>,
    device_memory_bytes: Option<u64>,
) -> Json {
    let selected_row = selected
        .and_then(|(id, commit)| find(id, commit))
        .map_or(Json::Null, model_row);
    let mut document = crate::jobj! {
        "storage_consent_ceiling_bytes" => storage_consent_ceiling(),
        "pairing_quota_ceiling_bytes" => pairing_quota_ceiling(),
        "selected" => selected_row,
        "models" => REGISTERED.iter().map(model_row).collect::<Vec<_>>(),
        "device_memory_bytes" => device_memory_bytes,
        "memory_cap_ceiling_bytes" => Json::Null,
        "memory_cap_options" => Json::Arr(Vec::new()),
    };
    if let Some(memory) = device_memory_bytes.filter(|m| *m > 0) {
        document.set(
            "memory_cap_ceiling_bytes",
            member_memory_ceiling(memory).into(),
        );
        document.set("memory_cap_options", memory_cap_options(memory).into());
    }
    document
}
pub fn consent_document(selected: Option<(&str, &str)>, device_memory_bytes: Option<u64>) -> Value {
    consent_document_json(selected, device_memory_bytes).to_value()
}
