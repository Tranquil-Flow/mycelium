// SPDX-License-Identifier: AGPL-3.0-only
//! Qwen2 decoder-layer stage: a pure-Rust port of the NumPy serving block
//! `numpy_runtime._qwen2_block_with_kv` at position 0 with no past
//! (complete-context replay), on row-wise int8 weights.
//!
//! Numerics contract (plan R1):
//! - f32 activations and accumulators throughout; weights are
//!   [`Int8Rowwise`] produced on device from exact bf16 -> f32 source rows by
//!   the NumPy-identical quantizer; `linear = (x . row_i8 as f32) * scale (+ bias)`.
//! - No BLAS, no fast-math, no FMA contraction (Rust never fuses `a * b + c`),
//!   transcendental functions from the pure-Rust `libm` crate, one thread, and a
//!   fixed summation order: dot products use eight interleaved lane
//!   accumulators reduced as `((l0+l1)+(l2+l3))+((l4+l5)+(l6+l7))` plus a
//!   sequential tail; RMSNorm means and softmax denominators use NumPy's own
//!   pairwise summation. The same source therefore gives bit-identical output
//!   on the phone (aarch64) and on a host build.
//! - Against NumPy the result is close, not bit-identical: NumPy's matmuls run
//!   through BLAS (blocked, opaque order) and its `exp`/`sin`/`cos`/`power`
//!   differ from musl's by an ulp now and then. Elementwise steps (RMSNorm,
//!   RoPE products, SiLU, residuals, bias adds) use NumPy's exact operation
//!   order so they add no further drift.

use serde_json::Value;

use crate::bf16::{SourceDType, decode_into};
use crate::quant::Int8Rowwise;
use crate::runtime::{RuntimeError, StageCall, StageOutput, StageRuntime};

pub const QWEN2_BACKEND: &str = "mobile-rust-qwen2";
/// Source work bounds, NOT a wall-time cleanup guarantee. A dot is at most
/// MAX_QWEN2_INNER terms; copies/elementwise loops yield every WORK_CHUNK.
pub const WORK_CHUNK: usize = 8192;
type Check<'a> = dyn FnMut(&'static str) -> Result<(), RuntimeError> + 'a;

fn zeros(n: usize, check: &mut Check<'_>) -> Result<Vec<f32>, RuntimeError> {
    check("allocation")?;
    let mut v = Vec::with_capacity(n);
    while v.len() < n {
        check("zero_chunk")?;
        v.resize((v.len() + WORK_CHUNK).min(n), 0.0);
    }
    Ok(v)
}
fn copy_values(values: &[f32], check: &mut Check<'_>) -> Result<Vec<f32>, RuntimeError> {
    check("allocation")?;
    let mut out = Vec::with_capacity(values.len());
    for chunk in values.chunks(WORK_CHUNK) { check("copy_chunk")?; out.extend_from_slice(chunk); }
    Ok(out)
}
fn finite(values: &[f32], check: &mut Check<'_>) -> Result<bool, RuntimeError> {
    for chunk in values.chunks(WORK_CHUNK) {
        check("finite_chunk")?;
        if chunk.iter().any(|v| !v.is_finite()) { return Ok(false); }
    }
    Ok(true)
}

/// Complete-context replay bound (tokens per call).
pub const MAX_QWEN2_SEQUENCE: usize = 1024;
pub const MAX_QWEN2_HIDDEN: usize = 8192;
pub const MAX_QWEN2_INNER: usize = 32768;
pub const MAX_QWEN2_LAYERS_PER_STAGE: usize = 64;
/// Replay ledger bounds (M3a): entries and cached output bytes; the oldest
/// entry is evicted first. A retry of an evicted call recomputes the same
/// output (the stage is deterministic), so eviction only costs compute.
pub const MAX_QWEN2_REPLAY_RESULTS: usize = 64;
pub const MAX_QWEN2_REPLAY_BYTES: usize = 64 << 20;

/// The seven row-wise int8 matrices of one decoder layer.
pub const MATRIX_SUFFIXES: [&str; 7] = [
    "self_attn.q_proj.weight",
    "self_attn.k_proj.weight",
    "self_attn.v_proj.weight",
    "self_attn.o_proj.weight",
    "mlp.gate_proj.weight",
    "mlp.up_proj.weight",
    "mlp.down_proj.weight",
];
/// The five f32 vectors of one decoder layer.
pub const VECTOR_SUFFIXES: [&str; 5] = [
    "input_layernorm.weight",
    "self_attn.q_proj.bias",
    "self_attn.k_proj.bias",
    "self_attn.v_proj.bias",
    "post_attention_layernorm.weight",
];

fn reject<T>(code: &str) -> Result<T, RuntimeError> {
    Err(RuntimeError(code.to_owned()))
}

#[derive(Debug, Clone, PartialEq)]
pub struct Qwen2Config {
    pub hidden_size: usize,
    pub intermediate_size: usize,
    pub num_attention_heads: usize,
    pub num_key_value_heads: usize,
    pub head_dim: usize,
    pub rms_norm_eps: f64,
    pub rope_theta: f64,
}

impl Qwen2Config {
    /// From a Hugging Face `config.json` document (only the fields a decoder
    /// layer needs; `head_dim` = hidden / heads as in the serving runtimes).
    pub fn from_hf_config(v: &Value) -> Result<Self, RuntimeError> {
        let code = "qwen2_config_invalid";
        if let Some(t) = v.get("model_type")
            && t != "qwen2"
        {
            return reject("qwen2_config_model_type_unsupported");
        }
        if let Some(a) = v.get("hidden_act")
            && a != "silu"
        {
            return reject("qwen2_config_activation_unsupported");
        }
        let int =
            |k: &str| v.get(k).and_then(Value::as_u64).filter(|n| *n > 0).map(|n| n as usize).ok_or_else(|| RuntimeError(code.into()));
        let float =
            |k: &str| v.get(k).and_then(Value::as_f64).filter(|f| f.is_finite() && *f > 0.0).ok_or_else(|| RuntimeError(code.into()));
        let hidden = int("hidden_size")?;
        let heads = int("num_attention_heads")?;
        let config = Self {
            hidden_size: hidden,
            intermediate_size: int("intermediate_size")?,
            num_attention_heads: heads,
            num_key_value_heads: int("num_key_value_heads")?,
            head_dim: if hidden.is_multiple_of(heads) { hidden / heads } else { 0 },
            rms_norm_eps: float("rms_norm_eps")?,
            rope_theta: float("rope_theta")?,
        };
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Result<(), RuntimeError> {
        let ok = self.hidden_size > 0
            && self.hidden_size <= MAX_QWEN2_HIDDEN
            && self.intermediate_size > 0
            && self.intermediate_size <= MAX_QWEN2_INNER
            && self.num_attention_heads > 0
            && self.num_key_value_heads > 0
            && self.num_attention_heads.is_multiple_of(self.num_key_value_heads)
            && self.head_dim > 0
            && self.head_dim.is_multiple_of(2)
            && self.head_dim * self.num_attention_heads == self.hidden_size
            && self.rms_norm_eps.is_finite()
            && self.rms_norm_eps > 0.0
            && self.rope_theta.is_finite()
            && self.rope_theta > 1.0;
        if ok { Ok(()) } else { reject("qwen2_config_invalid") }
    }

    fn kv_width(&self) -> usize {
        self.num_key_value_heads * self.head_dim
    }
}

/// One upstream tensor as delivered: source dtype, shape and raw LE bytes.
#[derive(Debug, Clone)]
pub struct RawTensor {
    pub dtype: SourceDType,
    pub shape: Vec<usize>,
    pub bytes: Vec<u8>,
}

/// Supplies upstream tensors by their full name (`model.layers.N.<suffix>`).
pub trait TensorSource {
    fn tensor(&mut self, name: &str) -> Result<RawTensor, RuntimeError>;
}

/// A complete Safetensors file held in memory.
pub struct SafetensorsBytes<'a> {
    bytes: &'a [u8],
    header: crate::safetensors::Header,
}

impl<'a> SafetensorsBytes<'a> {
    pub fn new(bytes: &'a [u8]) -> Result<Self, RuntimeError> {
        let header = crate::safetensors::parse_file(bytes).map_err(|e| RuntimeError(e.0.to_owned()))?;
        Ok(Self { bytes, header })
    }
}

impl TensorSource for SafetensorsBytes<'_> {
    fn tensor(&mut self, name: &str) -> Result<RawTensor, RuntimeError> {
        let entry = self.header.tensors.get(name).ok_or_else(|| RuntimeError("qwen2_tensor_missing".into()))?;
        let dtype = SourceDType::parse(&entry.dtype).ok_or_else(|| RuntimeError("qwen2_tensor_dtype_unsupported".into()))?;
        let (s, e) = self.header.range(name).unwrap();
        Ok(RawTensor { dtype, shape: entry.shape.clone(), bytes: self.bytes[s as usize..e as usize].to_vec() })
    }
}

impl TensorSource for crate::safetensors::SafetensorsFile {
    fn tensor(&mut self, name: &str) -> Result<RawTensor, RuntimeError> {
        let entry = self.header.tensors.get(name).ok_or_else(|| RuntimeError("qwen2_tensor_missing".into()))?;
        let dtype = SourceDType::parse(&entry.dtype).ok_or_else(|| RuntimeError("qwen2_tensor_dtype_unsupported".into()))?;
        let shape = entry.shape.clone();
        let bytes = self.read(name).map_err(|e| RuntimeError(e.0.to_owned()))?;
        Ok(RawTensor { dtype, shape, bytes })
    }
}

#[derive(Debug, Clone)]
pub struct Qwen2Layer {
    pub index: usize,
    pub input_layernorm: Vec<f32>,
    pub q_proj: Int8Rowwise,
    pub q_bias: Vec<f32>,
    pub k_proj: Int8Rowwise,
    pub k_bias: Vec<f32>,
    pub v_proj: Int8Rowwise,
    pub v_bias: Vec<f32>,
    pub o_proj: Int8Rowwise,
    pub post_attention_layernorm: Vec<f32>,
    pub gate_proj: Int8Rowwise,
    pub up_proj: Int8Rowwise,
    pub down_proj: Int8Rowwise,
}

impl Qwen2Layer {
    /// The quantized matrix for one of [`MATRIX_SUFFIXES`].
    pub fn matrix(&self, suffix: &str) -> Option<&Int8Rowwise> {
        Some(match suffix {
            "self_attn.q_proj.weight" => &self.q_proj,
            "self_attn.k_proj.weight" => &self.k_proj,
            "self_attn.v_proj.weight" => &self.v_proj,
            "self_attn.o_proj.weight" => &self.o_proj,
            "mlp.gate_proj.weight" => &self.gate_proj,
            "mlp.up_proj.weight" => &self.up_proj,
            "mlp.down_proj.weight" => &self.down_proj,
            _ => return None,
        })
    }

    fn resident_bytes(&self) -> usize {
        let m = MATRIX_SUFFIXES.iter().map(|s| self.matrix(s).unwrap().bytes()).sum::<usize>();
        let v =
            self.input_layernorm.len() + self.q_bias.len() + self.k_bias.len() + self.v_bias.len() + self.post_attention_layernorm.len();
        m + 4 * v
    }
}

fn load_matrix(src: &mut dyn TensorSource, name: &str, rows: usize, cols: usize) -> Result<Int8Rowwise, RuntimeError> {
    let t = src.tensor(name)?;
    if t.shape != [rows, cols] {
        return reject("qwen2_tensor_shape_invalid");
    }
    Int8Rowwise::from_source(rows, cols, t.dtype, &t.bytes).ok_or_else(|| RuntimeError("qwen2_tensor_invalid".into()))
}

fn load_vector(src: &mut dyn TensorSource, name: &str, len: usize) -> Result<Vec<f32>, RuntimeError> {
    let t = src.tensor(name)?;
    if t.shape != [len] {
        return reject("qwen2_tensor_shape_invalid");
    }
    let mut out = vec![0.0f32; len];
    if !decode_into(t.dtype, &t.bytes, &mut out) || out.iter().any(|v| !v.is_finite()) {
        return reject("qwen2_tensor_invalid");
    }
    Ok(out)
}

impl Qwen2Layer {
    pub fn load(config: &Qwen2Config, index: usize, src: &mut dyn TensorSource) -> Result<Self, RuntimeError> {
        let (h, i, kv) = (config.hidden_size, config.intermediate_size, config.kv_width());
        let n = |s: &str| format!("model.layers.{index}.{s}");
        Ok(Self {
            index,
            input_layernorm: load_vector(src, &n("input_layernorm.weight"), h)?,
            q_proj: load_matrix(src, &n("self_attn.q_proj.weight"), h, h)?,
            q_bias: load_vector(src, &n("self_attn.q_proj.bias"), h)?,
            k_proj: load_matrix(src, &n("self_attn.k_proj.weight"), kv, h)?,
            k_bias: load_vector(src, &n("self_attn.k_proj.bias"), kv)?,
            v_proj: load_matrix(src, &n("self_attn.v_proj.weight"), kv, h)?,
            v_bias: load_vector(src, &n("self_attn.v_proj.bias"), kv)?,
            o_proj: load_matrix(src, &n("self_attn.o_proj.weight"), h, h)?,
            post_attention_layernorm: load_vector(src, &n("post_attention_layernorm.weight"), h)?,
            gate_proj: load_matrix(src, &n("mlp.gate_proj.weight"), i, h)?,
            up_proj: load_matrix(src, &n("mlp.up_proj.weight"), i, h)?,
            down_proj: load_matrix(src, &n("mlp.down_proj.weight"), h, i)?,
        })
    }
}

/// Decoder layers `[start_layer, end_layer_exclusive)` of one Qwen2 model.
#[derive(Debug, Clone)]
pub struct Qwen2Stage {
    config: Qwen2Config,
    start_layer: usize,
    layers: Vec<Qwen2Layer>,
}

impl Qwen2Stage {
    pub fn load(
        config: Qwen2Config,
        start_layer: usize,
        end_layer_exclusive: usize,
        src: &mut dyn TensorSource,
    ) -> Result<Self, RuntimeError> {
        config.validate()?;
        if end_layer_exclusive <= start_layer || end_layer_exclusive - start_layer > MAX_QWEN2_LAYERS_PER_STAGE {
            return reject("qwen2_layer_range_invalid");
        }
        let layers = (start_layer..end_layer_exclusive).map(|l| Qwen2Layer::load(&config, l, src)).collect::<Result<Vec<_>, _>>()?;
        Ok(Self { config, start_layer, layers })
    }

    pub fn config(&self) -> &Qwen2Config {
        &self.config
    }
    pub fn start_layer(&self) -> usize {
        self.start_layer
    }
    pub fn end_layer_exclusive(&self) -> usize {
        self.start_layer + self.layers.len()
    }
    pub fn layers(&self) -> &[Qwen2Layer] {
        &self.layers
    }
    /// Resident weight bytes (int8 values, f32 scales and vectors).
    pub fn resident_bytes(&self) -> usize {
        self.layers.iter().map(Qwen2Layer::resident_bytes).sum()
    }

    fn check_input(&self, seq: usize, hidden: &[f32], check: &mut Check<'_>) -> Result<(), RuntimeError> {
        if seq == 0 || seq > MAX_QWEN2_SEQUENCE || seq.checked_mul(self.config.hidden_size) != Some(hidden.len()) {
            return reject("request_hidden_shape_invalid");
        }
        if !finite(hidden, check)? {
            return reject("request_hidden_nonfinite");
        }
        Ok(())
    }

    /// Run every layer of the stage over `seq` row-major hidden rows.
    pub fn forward(&self, seq: usize, hidden: &[f32]) -> Result<Vec<f32>, RuntimeError> {
        self.forward_cooperative(seq, hidden, &mut |_| Ok(()))
    }

    fn forward_cooperative(&self, seq: usize, hidden: &[f32], check: &mut Check<'_>) -> Result<Vec<f32>, RuntimeError> {
        self.check_input(seq, hidden, check)?;
        let rope = Rope::new(&self.config, seq, check)?;
        let mut x = copy_values(hidden, check)?;
        for layer in &self.layers {
            check("layer")?;
            x = layer_forward(&self.config, &rope, layer, seq, &x, check)?;
        }
        if !finite(&x, check)? {
            return reject("stage_output_nonfinite");
        }
        Ok(x)
    }

    /// Run one layer (by its position inside the stage); used for per-layer
    /// parity and timing.
    pub fn forward_layer(&self, offset: usize, seq: usize, hidden: &[f32]) -> Result<Vec<f32>, RuntimeError> {
        let check = &mut |_| Ok(());
        self.check_input(seq, hidden, check)?;
        let layer = self.layers.get(offset).ok_or_else(|| RuntimeError("qwen2_layer_range_invalid".into()))?;
        layer_forward(&self.config, &Rope::new(&self.config, seq, check)?, layer, seq, hidden, check)
    }
}

/// A loaded Qwen2 stage behind the [`StageRuntime`] trait. Complete-context
/// replay only: every call carries the full context from position 0; no KV is
/// kept between calls (stage-local KV is M3b).
///
/// Replay ledger (M3a): a call is identified by
/// `request:path:attempt:phase:token` (the router idempotency key without the
/// hop index; one stage is one hop) and fingerprinted over its position,
/// sequence and exact f32 input bytes. A retry returns the cached output
/// without recomputing; the same identity with another input is refused
/// (`request_replay_conflict`). Bounded by count and bytes, oldest first;
/// released per path (cancellation) or entirely (drain, new generation).
pub struct Qwen2Runtime {
    assignment_id: String,
    stage_id: String,
    stage: Qwen2Stage,
    pub request_count: u64,
    golden_output_digest: Option<String>,
    ledger: std::collections::HashMap<CallIdentity, ([u8; 32], StageOutput)>,
    ledger_order: std::collections::VecDeque<CallIdentity>,
    ledger_bytes: usize,
    ledger_byte_cap: usize,
    work_unit_count: u64,
    maximum_observed_work_unit_ms: f64,
}

/// (request, path, attempt, phase, token): structured, so ids containing ':'
/// can never share a replay result.
type CallIdentity = (String, String, u64, &'static str, i64);

fn call_identity(call: &StageCall<'_>) -> CallIdentity {
    let phase = match call.phase {
        crate::runtime::StagePhase::Prefill => "PREFILL",
        crate::runtime::StagePhase::RecoveryPrefill => "RECOVERY_PREFILL",
        crate::runtime::StagePhase::Decode => "DECODE",
    };
    (call.request_id.to_owned(), call.path_id.to_owned(), call.path_attempt, phase, call.token_index)
}

fn call_fingerprint(call: &StageCall<'_>, check: &mut Check<'_>) -> Result<[u8; 32], RuntimeError> {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(b"mycelium.qwen2_stage_call.v1\0");
    h.update(call.position.to_be_bytes());
    h.update((call.sequence as u64).to_be_bytes());
    for chunk in call.hidden.chunks(WORK_CHUNK) {
        check("fingerprint_chunk")?;
        for v in chunk { h.update(v.to_le_bytes()); }
    }
    Ok(h.finalize().into())
}

impl Qwen2Runtime {
    pub fn new(assignment_id: &str, stage_id: &str, stage: Qwen2Stage) -> Result<Self, RuntimeError> {
        for (v, code) in [(assignment_id, "qwen2_assignment_id_invalid"), (stage_id, "qwen2_stage_id_invalid")] {
            if v.is_empty() || v.chars().count() > 512 {
                return reject(code);
            }
        }
        Ok(Self {
            assignment_id: assignment_id.to_owned(),
            stage_id: stage_id.to_owned(),
            stage,
            request_count: 0,
            golden_output_digest: None,
            ledger: std::collections::HashMap::new(),
            ledger_order: std::collections::VecDeque::new(),
            ledger_bytes: 0,
            ledger_byte_cap: MAX_QWEN2_REPLAY_BYTES,
            work_unit_count: 0,
            maximum_observed_work_unit_ms: 0.0,
        })
    }

    pub fn new_verified(assignment_id: &str, stage_id: &str, stage: Qwen2Stage,
        golden: &crate::stage_manifest::Golden) -> Result<Self, RuntimeError> {
        // Recompute from applied bytes. Never accept a controller's digest.
        let digest = crate::stage_manifest::golden_digest(&stage, golden.seed, golden.seq)
            .map_err(|e| RuntimeError(e.0))?;
        if digest != golden.output_digest { return reject("stage_manifest_golden_mismatch"); }
        let mut runtime = Self::new(assignment_id, stage_id, stage)?;
        runtime.golden_output_digest = Some(digest);
        Ok(runtime)
    }

    fn execute_checked(&mut self, call: &StageCall<'_>, check: &mut Check<'_>) -> Result<StageOutput, RuntimeError> {
        check("stage_entry")?;
        call.check_complete_context()?;
        self.stage.check_input(call.sequence, call.hidden, check)?;
        let identity = call_identity(call);
        let fingerprint = call_fingerprint(call, check)?;
        if let Some((fp, out)) = self.ledger.get(&identity) {
            if *fp != fingerprint {
                return reject("request_replay_conflict");
            }
            return Ok(StageOutput { sequence:out.sequence, hidden_size:out.hidden_size, values:copy_values(&out.values, check)? });
        }
        let values = self.stage.forward_cooperative(call.sequence, call.hidden, check)?;
        let out = StageOutput { sequence: call.sequence, hidden_size: self.stage.config.hidden_size, values };
        let size = out.values.len() * 4;
        if size <= self.ledger_byte_cap {
            let cached = StageOutput { sequence:out.sequence, hidden_size:out.hidden_size, values:copy_values(&out.values, check)? };
            check("replay_commit")?;
            self.evict_to(MAX_QWEN2_REPLAY_RESULTS - 1, self.ledger_byte_cap - size);
            self.ledger.insert(identity.clone(), (fingerprint, cached));
            self.ledger_order.push_back(identity);
            self.ledger_bytes += size;
        }
        self.request_count += 1;
        Ok(out)
    }

    pub fn stage(&self) -> &Qwen2Stage {
        &self.stage
    }

    pub fn ledger_len(&self) -> usize {
        self.ledger.len()
    }

    /// Cached output bytes (f32 values) held by the ledger.
    pub fn ledger_bytes(&self) -> usize {
        self.ledger_bytes
    }

    /// Lower the byte bound (memory pressure); evicts down to it at once.
    pub fn set_ledger_byte_cap(&mut self, cap: usize) {
        self.ledger_byte_cap = cap.min(MAX_QWEN2_REPLAY_BYTES);
        self.evict_to(MAX_QWEN2_REPLAY_RESULTS, self.ledger_byte_cap);
    }

    /// Forget every call of one request path (its cancellation or completion).
    pub fn release_path(&mut self, request_id: &str, path_id: &str) -> usize {
        let gone: Vec<CallIdentity> = self.ledger_order.iter().filter(|k| k.0 == request_id && k.1 == path_id).cloned().collect();
        for key in &gone {
            self.remove(key);
        }
        gone.len()
    }

    fn remove(&mut self, key: &CallIdentity) {
        if let Some((_, out)) = self.ledger.remove(key) {
            self.ledger_bytes -= out.values.len() * 4;
            self.ledger_order.retain(|k| k != key);
        }
    }

    fn evict_to(&mut self, count: usize, bytes: usize) {
        while self.ledger.len() > count || self.ledger_bytes > bytes {
            let Some(oldest) = self.ledger_order.pop_front() else {
                break;
            };
            if let Some((_, out)) = self.ledger.remove(&oldest) {
                self.ledger_bytes -= out.values.len() * 4;
            }
        }
    }
}

impl StageRuntime for Qwen2Runtime {
    fn golden_output_digest(&self) -> Option<&str> { self.golden_output_digest.as_deref() }
    fn replay_result_count(&self) -> Option<usize> { Some(self.ledger.len()) }
    fn applied_operation_count(&self) -> Option<u64> { Some(self.request_count) }
    fn backend(&self) -> &'static str {
        QWEN2_BACKEND
    }
    fn assignment_id(&self) -> &str {
        &self.assignment_id
    }
    fn stage_id(&self) -> &str {
        &self.stage_id
    }
    fn hidden_size(&self) -> usize {
        self.stage.config.hidden_size
    }

    fn execute(&mut self, call: &StageCall<'_>) -> Result<StageOutput, RuntimeError> {
        self.execute_cooperative(call, &mut |_| Ok(()))
    }

    fn cooperative_work_snapshot(&self) -> Option<Value> {
        Some(serde_json::json!({
            "work_unit":"bounded_qwen2_replay_chunk",
            "maximum_dot_terms":MAX_QWEN2_INNER,
            "maximum_vector_chunk_elements":WORK_CHUNK,
            "observed_work_unit_count":self.work_unit_count,
            "maximum_observed_work_unit_ms":if self.work_unit_count > 0 { Some(self.maximum_observed_work_unit_ms) } else { None },
            "cooperative_interruption":true,
            "backend_candidate":true,
            "cooperative_bound_candidate":false,
            "maximum_total_cleanup_ms":null,
            "physical_proof_required":true,
            "bound_kind":"source_work_not_wall_time",
            "reason":"cooperative_chunks_implemented_wall_time_cleanup_unqualified",
            "request_scoped_cleanup":true,
            "shared_process_termination_used":false
        }))
    }

    fn execute_cooperative(&mut self, call: &StageCall<'_>, check: &mut Check<'_>) -> Result<StageOutput, RuntimeError> {
        let mut count = 0u64;
        let mut maximum = 0.0f64;
        let mut start = std::time::Instant::now();
        let result = self.execute_checked(call, &mut |unit| {
            maximum = maximum.max(start.elapsed().as_secs_f64() * 1000.0);
            count = count.saturating_add(1);
            // Includes callback wait in the following interval. These are
            // actual observations, never a real-time scheduling guarantee.
            start = std::time::Instant::now();
            check(unit)
        });
        maximum = maximum.max(start.elapsed().as_secs_f64() * 1000.0);
        self.work_unit_count = self.work_unit_count.saturating_add(count);
        self.maximum_observed_work_unit_ms = self.maximum_observed_work_unit_ms.max(maximum);
        result
    }

    fn release_path(&mut self, request_id: &str, path_id: &str) -> usize {
        Qwen2Runtime::release_path(self, request_id, path_id)
    }

    fn release_request(&mut self, request_id: &str) -> usize {
        let gone: Vec<CallIdentity> = self.ledger_order.iter().filter(|k| k.0 == request_id).cloned().collect();
        for key in &gone {
            self.remove(key);
        }
        gone.len()
    }

    fn release_all(&mut self) -> usize {
        let n = self.ledger.len();
        self.ledger.clear();
        self.ledger_order.clear();
        self.ledger_bytes = 0;
        n
    }
}

/// RoPE tables for positions `0..seq`, computed with NumPy's f32 operation
/// order: `inv = 1 / theta ** (arange(0, d, 2) / d)`, `freq = pos * inv`.
struct Rope {
    half: usize,
    cos: Vec<f32>,
    sin: Vec<f32>,
}

impl Rope {
    fn new(config: &Qwen2Config, seq: usize, check: &mut Check<'_>) -> Result<Self, RuntimeError> {
        let d = config.head_dim;
        let half = d / 2;
        let theta = config.rope_theta as f32;
        let inv: Vec<f32> = (0..half).map(|i| 1.0f32 / libm::powf(theta, (2 * i) as f32 / d as f32)).collect();
        let mut cos = Vec::with_capacity(seq * half);
        let mut sin = Vec::with_capacity(seq * half);
        for p in 0..seq {
            check("rope_table")?;
            for f in &inv {
                let freq = p as f32 * f;
                cos.push(libm::cosf(freq));
                sin.push(libm::sinf(freq));
            }
        }
        Ok(Self { half, cos, sin })
    }

    /// `x * cos + rotate_half(x) * sin` for every head of one row at `pos`.
    fn apply(&self, row: &mut [f32], pos: usize, head_dim: usize, check: &mut Check<'_>) -> Result<(), RuntimeError> {
        let (c, s) = (&self.cos[pos * self.half..(pos + 1) * self.half], &self.sin[pos * self.half..(pos + 1) * self.half]);
        let mut rotated = vec![0.0f32; head_dim];
        for head in row.chunks_exact_mut(head_dim) {
            check("rope")?;
            let (first, second) = head.split_at(self.half);
            for j in 0..self.half {
                rotated[j] = first[j] * c[j] + -second[j] * s[j];
                rotated[self.half + j] = second[j] * c[j] + first[j] * s[j];
            }
            head.copy_from_slice(&rotated);
        }
        Ok(())
    }
}

/// `sum(a[i] * b[i])` with eight interleaved lane accumulators (a fixed
/// order that the compiler may vectorise without reassociating).
#[inline]
pub fn dot_f32(a: &[f32], b: &[f32]) -> f32 {
    let n = a.len().min(b.len());
    let body = n - n % 8;
    let mut l = [0.0f32; 8];
    for (ca, cb) in a[..body].chunks_exact(8).zip(b[..body].chunks_exact(8)) {
        for k in 0..8 {
            l[k] += ca[k] * cb[k];
        }
    }
    let mut sum = ((l[0] + l[1]) + (l[2] + l[3])) + ((l[4] + l[5]) + (l[6] + l[7]));
    for i in body..n {
        sum += a[i] * b[i];
    }
    sum
}

/// [`dot_f32`] against an int8 row widened to f32.
#[inline]
pub fn dot_i8(x: &[f32], w: &[i8]) -> f32 {
    let n = x.len().min(w.len());
    let body = n - n % 8;
    let mut l = [0.0f32; 8];
    for (cx, cw) in x[..body].chunks_exact(8).zip(w[..body].chunks_exact(8)) {
        for k in 0..8 {
            l[k] += cx[k] * f32::from(cw[k]);
        }
    }
    let mut sum = ((l[0] + l[1]) + (l[2] + l[3])) + ((l[4] + l[5]) + (l[6] + l[7]));
    for i in body..n {
        sum += x[i] * f32::from(w[i]);
    }
    sum
}

/// NumPy's float32 `add.reduce` over a contiguous axis (`pairwise_sum` in
/// `loops_utils.h.src`, block 128, unroll 8), bit for bit.
pub fn pairwise_sum(a: &[f32]) -> f32 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0f32;
        for v in a {
            res += v;
        }
        res
    } else if n <= 128 {
        let mut r = [0.0f32; 8];
        r.copy_from_slice(&a[..8]);
        let body = n - n % 8;
        let mut i = 8;
        while i < body {
            for k in 0..8 {
                r[k] += a[i + k];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        for v in &a[body..] {
            res += v;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
    }
}

/// `numpy_runtime._rms_norm` for one row, same operation order.
fn rms_norm(row: &[f32], weight: &[f32], eps: f32, out: &mut [f32], scratch: &mut Vec<f32>) {
    scratch.clear();
    scratch.extend(row.iter().map(|v| v * v));
    let mean = pairwise_sum(scratch) / row.len() as f32;
    let inv = 1.0f32 / (mean + eps).sqrt();
    for ((o, v), w) in out.iter_mut().zip(row).zip(weight) {
        *o = (v * inv) * w;
    }
}

/// `y = (x . w_row) * scale (+ bias)` for every row of `x` (`seq x in`).
fn linear(x: &[f32], seq: usize, w: &Int8Rowwise, bias: Option<&[f32]>, check: &mut Check<'_>) -> Result<Vec<f32>, RuntimeError> {
    let (inp, out) = (w.cols, w.rows);
    let mut y = zeros(seq * out, check)?;
    for o in 0..out {
        let row = w.row(o);
        let scale = w.scales[o];
        for t in 0..seq {
            check("linear_dot")?;
            let mut v = dot_i8(&x[t * inp..(t + 1) * inp], row) * scale;
            if let Some(b) = bias {
                v += b[o];
            }
            y[t * out + o] = v;
        }
    }
    Ok(y)
}

fn layer_forward(c: &Qwen2Config, rope: &Rope, l: &Qwen2Layer, seq: usize, x: &[f32], check: &mut Check<'_>) -> Result<Vec<f32>, RuntimeError> {
    let (h, d, kvw) = (c.hidden_size, c.head_dim, c.kv_width());
    let eps = c.rms_norm_eps as f32;
    let mut scratch = Vec::with_capacity(c.intermediate_size.max(h));

    let mut normed = zeros(seq * h, check)?;
    for t in 0..seq {
        check("rms_norm")?;
        rms_norm(&x[t * h..(t + 1) * h], &l.input_layernorm, eps, &mut normed[t * h..(t + 1) * h], &mut scratch);
    }
    let mut q = linear(&normed, seq, &l.q_proj, Some(&l.q_bias), check)?;
    let mut k = linear(&normed, seq, &l.k_proj, Some(&l.k_bias), check)?;
    let v = linear(&normed, seq, &l.v_proj, Some(&l.v_bias), check)?;
    for t in 0..seq {
        rope.apply(&mut q[t * h..(t + 1) * h], t, d, check)?;
        rope.apply(&mut k[t * kvw..(t + 1) * kvw], t, d, check)?;
    }

    // Causal grouped-query attention; query head `hd` reads KV head
    // `hd / (heads / kv_heads)` (NumPy's `np.repeat` along the head axis).
    let group = c.num_attention_heads / c.num_key_value_heads;
    let root = (d as f32).sqrt();
    let mut attended = zeros(seq * h, check)?;
    let mut scores = vec![0.0f32; seq];
    for hd in 0..c.num_attention_heads {
        let kvh = hd / group;
        for t in 0..seq {
            let qrow = &q[t * h + hd * d..t * h + (hd + 1) * d];
            let mut max = f32::NEG_INFINITY;
            for (j, s) in scores.iter_mut().enumerate().take(t + 1) {
                check("attention_score")?;
                *s = dot_f32(qrow, &k[j * kvw + kvh * d..j * kvw + (kvh + 1) * d]) / root;
                max = max.max(*s);
            }
            // Masked positions are exp(-inf) = 0 in NumPy; keep them in the
            // pairwise sum so the grouping matches its full-row reduction.
            check("softmax")?;
            for (j, s) in scores.iter_mut().enumerate() {
                *s = if j <= t { libm::expf(*s - max) } else { 0.0 };
            }
            let denom = pairwise_sum(&scores);
            let out = &mut attended[t * h + hd * d..t * h + (hd + 1) * d];
            for j in 0..=t {
                check("attention_value")?;
                let p = scores[j] / denom;
                let vrow = &v[j * kvw + kvh * d..j * kvw + (kvh + 1) * d];
                for (o, vv) in out.iter_mut().zip(vrow) {
                    *o += p * vv;
                }
            }
        }
    }
    let o = linear(&attended, seq, &l.o_proj, None, check)?;
    let mut after = Vec::with_capacity(x.len());
    for (a, b) in x.chunks(WORK_CHUNK).zip(o.chunks(WORK_CHUNK)) {
        check("residual")?;
        after.extend(a.iter().zip(b).map(|(a,b)| a+b));
    }

    for t in 0..seq {
        check("rms_norm")?;
        rms_norm(&after[t * h..(t + 1) * h], &l.post_attention_layernorm, eps, &mut normed[t * h..(t + 1) * h], &mut scratch);
    }
    let gate = linear(&normed, seq, &l.gate_proj, None, check)?;
    let up = linear(&normed, seq, &l.up_proj, None, check)?;
    let mut gated = Vec::with_capacity(gate.len());
    for (g, u) in gate.chunks(WORK_CHUNK).zip(up.chunks(WORK_CHUNK)) {
        check("silu")?;
        gated.extend(g.iter().zip(u).map(|(g,u)| (g / (1.0f32 + libm::expf(-g))) * u));
    }
    let down = linear(&gated, seq, &l.down_proj, None, check)?;
    let mut out = Vec::with_capacity(after.len());
    for (a,b) in after.chunks(WORK_CHUNK).zip(down.chunks(WORK_CHUNK)) {
        check("residual")?;
        out.extend(a.iter().zip(b).map(|(a,b)| a+b));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dot_tail_and_lanes_cover_every_element() {
        for n in [0usize, 1, 7, 8, 9, 15, 16, 17, 896] {
            let a: Vec<f32> = (0..n).map(|i| (i % 5) as f32 - 2.0).collect();
            let b: Vec<f32> = (0..n).map(|i| (i % 3) as f32).collect();
            let w: Vec<i8> = b.iter().map(|v| *v as i8).collect();
            let exact: f32 = a.iter().zip(&b).map(|(x, y)| x * y).sum();
            assert_eq!(dot_f32(&a, &b), exact, "n={n}");
            assert_eq!(dot_i8(&a, &w), exact, "n={n}");
        }
    }

    #[test]
    fn pairwise_sum_small_cases() {
        // Bit-exactness against NumPy is checked on fixture vectors in
        // tests/qwen2_parity.rs; here only the block structure.
        assert_eq!(pairwise_sum(&[]), 0.0);
        assert_eq!(pairwise_sum(&[1.0, 2.0, 3.0]), 6.0);
        let a: Vec<f32> = (0..300).map(|i| i as f32).collect();
        assert_eq!(pairwise_sum(&a), 44850.0);
    }

    #[test]
    fn config_validation_fails_closed() {
        let good = serde_json::json!({"model_type":"qwen2","hidden_act":"silu","hidden_size":64,"intermediate_size":128,
            "num_attention_heads":4,"num_key_value_heads":2,"rms_norm_eps":1e-6,"rope_theta":1e6});
        assert_eq!(Qwen2Config::from_hf_config(&good).unwrap().head_dim, 16);
        for (k, v) in [
            ("num_key_value_heads", serde_json::json!(3)),
            ("hidden_size", serde_json::json!(66)),
            ("hidden_act", serde_json::json!("gelu")),
            ("model_type", serde_json::json!("llama")),
            ("rms_norm_eps", serde_json::json!(0.0)),
            ("intermediate_size", serde_json::json!(null)),
        ] {
            let mut bad = good.clone();
            bad[k] = v;
            assert!(Qwen2Config::from_hf_config(&bad).is_err(), "{k}");
        }
    }
}
