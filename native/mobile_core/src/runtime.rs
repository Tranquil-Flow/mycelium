// SPDX-License-Identifier: AGPL-3.0-only
//! Registered stage runtimes behind one trait, selected by the registry's
//! `runtime_backend`, never by device brand or model.
//!
//! `mobile-rust-reference` is a Rust port of the `pixel-stdlib` executor in
//! `mycelium_mobile/pixel_stage.py`: one exact assignment-derived GPT-2 decoder
//! block, f64 math in the same operation order, a digest-bound stage pack and a
//! bounded replay ledger. KV stays stage-local (here: none, complete-context
//! replay), so nothing but hidden states crosses a hop.

use std::collections::{HashMap, VecDeque};

use serde_json::{Map, Value};

use crate::canonical;

pub const REFERENCE_BACKEND: &str = "mobile-rust-reference";
pub const STAGE_PACK_PROTOCOL: &str = "mycelium.pixel_stage_pack.v1";
pub const MAX_SEQUENCE_LENGTH: usize = 256;
pub const MAX_HIDDEN_SIZE: usize = 4096;
pub const MAX_INNER_SIZE: usize = 16384;
pub const MAX_REPLAY_RESULTS: usize = 128;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct RuntimeError(pub String);

fn reject<T>(code: &str) -> Result<T, RuntimeError> {
    Err(RuntimeError(code.to_owned()))
}

pub type Rows = Vec<Vec<f64>>;

/// Router phase of one stage call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StagePhase {
    Prefill,
    RecoveryPrefill,
    Decode,
}

impl StagePhase {
    /// Router wire names. `PREFILL_CHUNK` is refused: chunked prefill needs
    /// stage-local KV continuity, which no mobile runtime keeps yet.
    pub fn parse(name: &str) -> Result<Self, RuntimeError> {
        match name {
            "PREFILL" => Ok(Self::Prefill),
            "RECOVERY_PREFILL" => Ok(Self::RecoveryPrefill),
            "DECODE" => Ok(Self::Decode),
            "PREFILL_CHUNK" => reject("prefill_chunk_requires_kv_continuity"),
            _ => reject("stage_call_phase_invalid"),
        }
    }
}

/// One execution request as the router describes it: identity, phase and
/// position, plus `sequence` row-major f32 hidden rows (the MYAC payload).
#[derive(Debug, Clone, Copy)]
pub struct StageCall<'a> {
    pub request_id: &'a str,
    pub path_id: &'a str,
    pub path_attempt: u64,
    pub phase: StagePhase,
    /// -1 for a fresh prefill, otherwise the token being decoded.
    pub token_index: i64,
    /// Absolute position of the first row (0 for complete-context replay prefill).
    pub position: u64,
    pub sequence: usize,
    pub hidden: &'a [f32],
}

impl<'a> StageCall<'a> {
    /// A plain prefill of `sequence` rows at position 0.
    pub fn prefill(request_id: &'a str, sequence: usize, hidden: &'a [f32]) -> Self {
        Self { request_id, path_id: "", path_attempt: 0, phase: StagePhase::Prefill, token_index: -1, position: 0, sequence, hidden }
    }

    /// The router's complete-context admission rules
    /// (`NumpyRuntimePort._execute_complete_context`).
    pub fn check_complete_context(&self) -> Result<(), RuntimeError> {
        if self.request_id.is_empty() || self.request_id.chars().count() > 512 {
            return reject("request_id_invalid");
        }
        match self.phase {
            StagePhase::Prefill if self.token_index != -1 => reject("prefill_sequence_mismatch"),
            StagePhase::RecoveryPrefill if self.token_index < 0 => reject("recovery_prefill_sequence_mismatch"),
            StagePhase::Prefill | StagePhase::RecoveryPrefill if self.position != 0 => reject("prefill_position_mismatch"),
            StagePhase::Decode if self.position == 0 => reject("decode_position_mismatch"),
            StagePhase::Decode if self.sequence as u64 != self.position + 1 => reject("decode_context_position_mismatch"),
            StagePhase::Decode if self.token_index < 0 => reject("decode_sequence_mismatch"),
            _ => Ok(()),
        }
    }
}

/// `sequence` x `hidden_size` row-major f32 output rows.
#[derive(Debug, Clone, PartialEq)]
pub struct StageOutput {
    pub sequence: usize,
    pub hidden_size: usize,
    pub values: Vec<f32>,
}

/// One loaded stage. Implementations own their KV (if any) locally.
pub trait StageRuntime: Send {
    fn backend(&self) -> &'static str;
    fn assignment_id(&self) -> &str;
    fn stage_id(&self) -> &str;
    fn hidden_size(&self) -> usize;
    /// No provenance or counters are invented for runtimes without telemetry.
    fn golden_output_digest(&self) -> Option<&str> { None }
    fn replay_result_count(&self) -> Option<usize> { None }
    fn applied_operation_count(&self) -> Option<u64> { None }
    fn cooperative_work_snapshot(&self) -> Option<Value> { None }
    fn execute(&mut self, call: &StageCall<'_>) -> Result<StageOutput, RuntimeError>;
    /// Runtime implementations must override this to interrupt inside kernels.
    /// The default only fences entry/return and advertises no cooperation.
    fn execute_cooperative(&mut self, call: &StageCall<'_>, check: &mut dyn FnMut(&'static str) -> Result<(), RuntimeError>) -> Result<StageOutput, RuntimeError> {
        check("stage_entry")?;
        let out = self.execute(call)?;
        check("stage_return")?;
        Ok(out)
    }
    /// Drop every replay result (drain, new membership generation); returns how many.
    fn release_all(&mut self) -> usize;
    /// Drop the replay results of one request path (its cancellation).
    fn release_path(&mut self, _request_id: &str, _path_id: &str) -> usize {
        0
    }
    /// Drop the replay results of every path of one request (a cancellation
    /// that names the request, not one path): a successor path holds residue
    /// too. Returns how many were dropped.
    fn release_request(&mut self, _request_id: &str) -> usize {
        0
    }
}

struct BlockTensors {
    ln1_w: Vec<f64>,
    ln1_b: Vec<f64>,
    attn_w: Rows,
    attn_b: Vec<f64>,
    proj_w: Rows,
    proj_b: Vec<f64>,
    ln2_w: Vec<f64>,
    ln2_b: Vec<f64>,
    fc_w: Rows,
    fc_b: Vec<f64>,
    fc_proj_w: Rows,
    fc_proj_b: Vec<f64>,
}

pub struct Gpt2BlockStage {
    assignment_id: String,
    stage_id: String,
    pack_digest: String,
    hidden_size: usize,
    n_head: usize,
    epsilon: f64,
    t: BlockTensors,
    replay: HashMap<String, (String, Rows)>,
    replay_order: VecDeque<String>,
    pub request_count: u64,
}

const PACK_FIELDS: [&str; 25] = [
    "protocol",
    "run_id",
    "deployment_id",
    "assignment_id",
    "stage_id",
    "model_id",
    "resolved_commit",
    "manifest_digest",
    "parent_assignment_digest",
    "parent_load_proof_digest",
    "component_roles",
    "derived_substage",
    "start_layer",
    "end_layer_exclusive",
    "n_head",
    "hidden_size",
    "epsilon",
    "activation_function",
    "scale_attn_weights",
    "scale_attn_by_inverse_layer_idx",
    "reorder_and_upcast_attn",
    "add_cross_attention",
    "tensors",
    "pack_digest",
    "route_ready",
];

fn ascii_digest(value: &Value) -> Result<String, RuntimeError> {
    let bytes = canonical::to_ascii_bytes(value).map_err(|_| RuntimeError("noncanonical_document".into()))?;
    Ok(canonical::sha256_prefixed(&bytes))
}

fn number(v: &Value, code: &str) -> Result<f64, RuntimeError> {
    if v.is_boolean() {
        return reject(code);
    }
    match v.as_f64() {
        Some(f) if f.is_finite() => Ok(f),
        _ => reject(code),
    }
}

fn positive_int(v: &Value, code: &str) -> Result<usize, RuntimeError> {
    match v.as_u64() {
        Some(n) if n > 0 && v.is_u64() => Ok(n as usize),
        _ => reject(code),
    }
}

fn vector(v: &Value, width: usize, code: &str) -> Result<Vec<f64>, RuntimeError> {
    let items = v.as_array().filter(|a| a.len() == width);
    items.map_or_else(|| reject(code), |a| a.iter().map(|x| number(x, code)).collect())
}

fn matrix(v: &Value, rows: usize, cols: usize, code: &str) -> Result<Rows, RuntimeError> {
    let items = v.as_array().filter(|a| a.len() == rows);
    items.map_or_else(|| reject(code), |a| a.iter().map(|r| vector(r, cols, code)).collect())
}

fn nonempty(v: &Value, code: &str) -> Result<String, RuntimeError> {
    match v.as_str() {
        Some(s) if !s.is_empty() && s.chars().count() <= 512 => Ok(s.to_owned()),
        _ => reject(code),
    }
}

impl Gpt2BlockStage {
    /// `PixelStage.from_document`.
    pub fn from_document(value: &Value) -> Result<Self, RuntimeError> {
        let map: &Map<String, Value> = match value.as_object() {
            Some(m) if m.len() == PACK_FIELDS.len() && PACK_FIELDS.iter().all(|f| m.contains_key(*f)) => m,
            _ => return reject("stage_pack_fields_invalid"),
        };
        if map["protocol"] != STAGE_PACK_PROTOCOL {
            return reject("stage_pack_protocol_invalid");
        }
        if map["route_ready"] != Value::Bool(false) {
            return reject("stage_pack_route_ready_invalid");
        }
        let mut unsigned = map.clone();
        unsigned.remove("pack_digest");
        let digest = ascii_digest(&Value::Object(unsigned))?;
        if map["pack_digest"] != digest.as_str() {
            return reject("stage_pack_digest_mismatch");
        }
        for f in [
            "run_id",
            "deployment_id",
            "assignment_id",
            "stage_id",
            "model_id",
            "resolved_commit",
            "manifest_digest",
            "parent_assignment_digest",
            "parent_load_proof_digest",
        ] {
            nonempty(&map[f], &format!("stage_pack_{f}_invalid"))?;
        }
        for f in ["manifest_digest", "parent_assignment_digest", "parent_load_proof_digest"] {
            let d = map[f].as_str().unwrap_or("");
            let ok = d.len() == 71 && d.starts_with("sha256:") && d[7..].bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
            if !ok {
                return reject(&format!("stage_pack_{f}_invalid"));
            }
        }
        if map["component_roles"] != serde_json::json!(["decoder"]) {
            return reject("stage_pack_component_roles_invalid");
        }
        if map["derived_substage"] != Value::Bool(true) {
            return reject("stage_pack_derived_substage_invalid");
        }
        let start = positive_int(&map["start_layer"], "stage_pack_range_invalid")?;
        let end = positive_int(&map["end_layer_exclusive"], "stage_pack_range_invalid")?;
        if end != start + 1 {
            return reject("stage_pack_range_invalid");
        }
        let hidden = positive_int(&map["hidden_size"], "stage_pack_hidden_size_invalid")?;
        if hidden > MAX_HIDDEN_SIZE {
            return reject("stage_pack_hidden_size_invalid");
        }
        let n_head = positive_int(&map["n_head"], "stage_pack_n_head_invalid")?;
        if hidden % n_head != 0 {
            return reject("stage_pack_n_head_invalid");
        }
        let epsilon = number(&map["epsilon"], "stage_pack_epsilon_invalid")?;
        if epsilon <= 0.0 {
            return reject("stage_pack_epsilon_invalid");
        }
        if map["activation_function"] != "gelu_new" {
            return reject("stage_pack_activation_function_unsupported");
        }
        let flags = [
            ("scale_attn_weights", true, "stage_pack_scale_attn_weights_unsupported"),
            ("scale_attn_by_inverse_layer_idx", false, "stage_pack_inverse_layer_scaling_unsupported"),
            ("reorder_and_upcast_attn", false, "stage_pack_reordered_attention_unsupported"),
            ("add_cross_attention", false, "stage_pack_cross_attention_unsupported"),
        ];
        for (f, expected, code) in flags {
            if map[f] != Value::Bool(expected) {
                return reject(code);
            }
        }
        let prefix = format!("transformer.h.{start}.");
        let suffixes = [
            "ln_1.weight",
            "ln_1.bias",
            "attn.c_attn.weight",
            "attn.c_attn.bias",
            "attn.c_proj.weight",
            "attn.c_proj.bias",
            "ln_2.weight",
            "ln_2.bias",
            "mlp.c_fc.weight",
            "mlp.c_fc.bias",
            "mlp.c_proj.weight",
            "mlp.c_proj.bias",
        ];
        let raw = match map["tensors"].as_object() {
            Some(t) if t.len() == suffixes.len() && suffixes.iter().all(|s| t.contains_key(&format!("{prefix}{s}"))) => t,
            _ => return reject("stage_pack_tensor_set_invalid"),
        };
        let get = |s: &str| &raw[&format!("{prefix}{s}")];
        let shape = "stage_pack_tensor_shape_invalid";
        let inner = get("mlp.c_fc.bias").as_array().map(Vec::len).unwrap_or(0);
        if inner == 0 || inner > MAX_INNER_SIZE {
            return reject(shape);
        }
        let t = BlockTensors {
            ln1_w: vector(get("ln_1.weight"), hidden, shape)?,
            ln1_b: vector(get("ln_1.bias"), hidden, shape)?,
            attn_w: matrix(get("attn.c_attn.weight"), hidden, 3 * hidden, shape)?,
            attn_b: vector(get("attn.c_attn.bias"), 3 * hidden, shape)?,
            proj_w: matrix(get("attn.c_proj.weight"), hidden, hidden, shape)?,
            proj_b: vector(get("attn.c_proj.bias"), hidden, shape)?,
            ln2_w: vector(get("ln_2.weight"), hidden, shape)?,
            ln2_b: vector(get("ln_2.bias"), hidden, shape)?,
            fc_w: matrix(get("mlp.c_fc.weight"), hidden, inner, shape)?,
            fc_b: vector(get("mlp.c_fc.bias"), inner, shape)?,
            fc_proj_w: matrix(get("mlp.c_proj.weight"), inner, hidden, shape)?,
            fc_proj_b: vector(get("mlp.c_proj.bias"), hidden, shape)?,
        };
        Ok(Self {
            assignment_id: map["assignment_id"].as_str().unwrap().to_owned(),
            stage_id: map["stage_id"].as_str().unwrap().to_owned(),
            pack_digest: digest,
            hidden_size: hidden,
            n_head,
            epsilon,
            t,
            replay: HashMap::new(),
            replay_order: VecDeque::new(),
            request_count: 0,
        })
    }

    pub fn pack_digest(&self) -> &str {
        &self.pack_digest
    }

    fn forward(&self, hidden: &[Vec<f64>]) -> Result<Rows, RuntimeError> {
        let t = &self.t;
        let h = self.hidden_size;
        let head = h / self.n_head;
        let normalized = layer_norm(hidden, &t.ln1_w, &t.ln1_b, self.epsilon);
        let qkv = linear(&normalized, &t.attn_w, &t.attn_b);
        let scale = (head as f64).powf(-0.5);
        let mut attended = Vec::with_capacity(qkv.len());
        for (ti, row) in qkv.iter().enumerate() {
            let query = &row[..h];
            let mut combined = Vec::with_capacity(h);
            for hd in 0..self.n_head {
                let s = hd * head;
                let scores: Vec<f64> = (0..=ti)
                    .map(|ki| {
                        let key = &qkv[ki][h..2 * h];
                        (0..head).fold(0.0, |acc, o| acc + query[s + o] * key[s + o]) * scale
                    })
                    .collect();
                let weights = softmax(&scores);
                for o in 0..head {
                    combined.push((0..=ti).fold(0.0, |acc, ki| acc + weights[ki] * qkv[ki][2 * h + s + o]));
                }
            }
            attended.push(combined);
        }
        let attention = linear(&attended, &t.proj_w, &t.proj_b);
        let after: Rows = hidden.iter().zip(&attention).map(|(a, b)| add(a, b)).collect();
        let normalized = layer_norm(&after, &t.ln2_w, &t.ln2_b, self.epsilon);
        let ff = linear(&normalized, &t.fc_w, &t.fc_b);
        let activated: Rows = ff.iter().map(|r| r.iter().map(|v| gelu_new(*v)).collect()).collect();
        let projected = linear(&activated, &t.fc_proj_w, &t.fc_proj_b);
        let out: Rows = after.iter().zip(&projected).map(|(a, b)| add(a, b)).collect();
        if out.iter().flatten().any(|v| !v.is_finite()) {
            return reject("stage_output_nonfinite");
        }
        Ok(out)
    }
}

impl Gpt2BlockStage {
    /// `PixelStage.execute`: replay-safe by request id and input fingerprint.
    pub fn execute(&mut self, request_id: &str, hidden: &[Vec<f64>]) -> Result<Rows, RuntimeError> {
        if request_id.is_empty() || request_id.chars().count() > 512 {
            return reject("request_id_invalid");
        }
        if hidden.is_empty() || hidden.len() > MAX_SEQUENCE_LENGTH {
            return reject("request_hidden_shape_invalid");
        }
        for row in hidden {
            if row.len() != self.hidden_size {
                return reject("request_hidden_shape_invalid");
            }
            if row.iter().any(|v| !v.is_finite()) {
                return reject("request_hidden_nonfinite");
            }
        }
        let fingerprint = ascii_digest(&serde_json::json!({
            "assignment_id": self.assignment_id,
            "stage_id": self.stage_id,
            "hidden": hidden,
        }))?;
        if let Some((fp, out)) = self.replay.get(request_id) {
            if *fp != fingerprint {
                return reject("request_replay_conflict");
            }
            return Ok(out.clone());
        }
        if self.replay.len() >= MAX_REPLAY_RESULTS {
            return reject("request_replay_ledger_full");
        }
        let out = self.forward(hidden)?;
        self.replay.insert(request_id.to_owned(), (fingerprint, out.clone()));
        self.replay_order.push_back(request_id.to_owned());
        self.request_count += 1;
        Ok(out)
    }
}

/// Adapter: the GPT-2 reference keeps its f64 math and request-id ledger; a
/// call's f32 rows widen exactly to f64 and the result narrows to f32, which
/// is what the MYAC hop carried before (`Activation::from_rows_f32`).
impl StageRuntime for Gpt2BlockStage {
    fn backend(&self) -> &'static str {
        REFERENCE_BACKEND
    }
    fn assignment_id(&self) -> &str {
        &self.assignment_id
    }
    fn stage_id(&self) -> &str {
        &self.stage_id
    }
    fn hidden_size(&self) -> usize {
        self.hidden_size
    }

    fn execute(&mut self, call: &StageCall<'_>) -> Result<StageOutput, RuntimeError> {
        let width = self.hidden_size;
        if call.sequence == 0 || call.sequence.checked_mul(width) != Some(call.hidden.len()) {
            return reject("request_hidden_shape_invalid");
        }
        let rows: Rows = call.hidden.chunks_exact(width).map(|r| r.iter().map(|v| f64::from(*v)).collect()).collect();
        let out = Gpt2BlockStage::execute(self, call.request_id, &rows)?;
        let values = out.iter().flatten().map(|v| *v as f32).collect();
        Ok(StageOutput { sequence: out.len(), hidden_size: width, values })
    }

    fn release_all(&mut self) -> usize {
        let n = self.replay.len();
        self.replay.clear();
        self.replay_order.clear();
        n
    }
}

fn add(a: &[f64], b: &[f64]) -> Vec<f64> {
    a.iter().zip(b).map(|(x, y)| x + y).collect()
}

fn linear(rows: &[Vec<f64>], weight: &[Vec<f64>], bias: &[f64]) -> Rows {
    rows.iter()
        .map(|row| (0..bias.len()).map(|c| (0..row.len()).fold(0.0, |acc, i| acc + row[i] * weight[i][c]) + bias[c]).collect())
        .collect()
}

fn layer_norm(rows: &[Vec<f64>], w: &[f64], b: &[f64], eps: f64) -> Rows {
    rows.iter()
        .map(|row| {
            let n = row.len() as f64;
            let mean = row.iter().fold(0.0, |a, v| a + v) / n;
            let var = row.iter().fold(0.0, |a, v| a + (v - mean).powi(2)) / n;
            let inv = 1.0 / (var + eps).sqrt();
            row.iter().enumerate().map(|(i, v)| (v - mean) * inv * w[i] + b[i]).collect()
        })
        .collect()
}

fn softmax(values: &[f64]) -> Vec<f64> {
    let max = values.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let exps: Vec<f64> = values.iter().map(|v| (v - max).exp()).collect();
    let denom = exps.iter().fold(0.0, |a, v| a + v);
    exps.iter().map(|v| v / denom).collect()
}

fn gelu_new(x: f64) -> f64 {
    0.5 * x * (1.0 + ((2.0 / std::f64::consts::PI).sqrt() * (x + 0.044715 * x.powf(3.0))).tanh())
}
