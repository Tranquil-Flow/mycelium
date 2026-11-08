// SPDX-License-Identifier: AGPL-3.0-only
pub use crate::lifecycle::payload_id;
use crate::{
    lifecycle::{Config, Cursor, Lifecycle},
    moe_admission,
    native::{Array, Context},
    source_image::verify_artifact,
};
use model::{
    Kv, Qwen2, Tensor, Weight, final_norm, forward, linear,
    moe_stage::{MoeStage, StageInput},
};
use sha2::{Digest, Sha256};
use stage_contract::*;
use std::{collections::BTreeMap, path::Path, rc::Rc};

pub struct Worker {
    context: Rc<Context>,
    descriptor: StageDescriptor,
    config: Config,
    weights: BTreeMap<String, Weight<Array>>,
    moe: Option<MoeStage<Array>>,
    control: Lifecycle,
    loaded_bytes: u64,
    reservation: crate::resources::HeldReservation,
}
pub type KvSnapshot = (u32, Vec<i32>, Vec<u8>, Vec<u8>);
pub struct StateSnapshot {
    pub layer: u32,
    pub kind: &'static str,
    pub shape: Vec<i32>,
    pub bytes: Vec<u8>,
}
pub struct Session {
    cursor: Cursor,
    kv: Kv<Array>,
}
impl Session {
    pub fn position(&self) -> u32 {
        self.cursor.position()
    }
    pub fn state_generation(&self) -> u64 {
        self.cursor.state_generation()
    }
}

fn export(x: &Array, shape: [u32; 3]) -> Result<Activation> {
    let values = x.floats()?;
    let bytes = values.iter().flat_map(|v| v.to_le_bytes()).collect();
    let a = Activation {
        shape,
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes,
    };
    a.validate(shape[2], u64::MAX)?;
    Ok(a)
}
impl Worker {
    /// Artifacts must be immutable, identity-verified L7 inputs. The lane verifies
    /// dense file/header hashes, or MoE assigned payload/header hashes, before
    /// and after loading. MoE uses a shard directory; this does not own staging.
    pub fn prepare(
        context: Rc<Context>,
        descriptor: StageDescriptor,
        path: &Path,
        limits: Limits,
        generation: u64,
    ) -> Result<Self> {
        Self::prepare_image(context, descriptor, path, limits, generation, None)
    }
    /// Stage-only dense load. Verification is repeated before and after native IO.
    pub fn prepare_shard(
        context: Rc<Context>,
        descriptor: StageDescriptor,
        path: &Path,
        limits: Limits,
        generation: u64,
        shard: &serde_json::Value,
    ) -> Result<Self> {
        crate::stage_shard::verify(path, &descriptor, shard)?;
        Self::prepare_image(context, descriptor, path, limits, generation, Some(shard))
    }
    fn prepare_image(
        context: Rc<Context>,
        descriptor: StageDescriptor,
        path: &Path,
        limits: Limits,
        generation: u64,
        shard: Option<&serde_json::Value>,
    ) -> Result<Self> {
        context.healthy()?;
        let verify = || match shard {
            Some(shard) => crate::stage_shard::verify(path, &descriptor, shard),
            None => verify_artifact(path, &descriptor),
        };
        let amount = crate::admission::requirements(&descriptor, limits)?;
        let loaded_bytes = amount.resident.get();
        let config = Config::for_descriptor(&descriptor)?;
        let control = Lifecycle::new(descriptor.clone(), limits, generation)?;
        let mut reservation = context.resources.reserve(amount)?;
        let loaded = (|| {
            if matches!(config, Config::Moe) {
                let mut weights = BTreeMap::new();
                for (shard, wanted) in moe_admission::shard_plan(&descriptor)? {
                    moe_admission::verify_shard(path, &shard, &wanted)?;
                    let shard_path = path.join(&shard);
                    let name = shard_path
                        .to_str()
                        .ok_or_else(|| Error::new(ErrorCode::Artifact, "path-utf8"))?;
                    if descriptor.profile == NumericsProfile::MlxPackedInt8 {
                        weights.extend(context.load_packed(name, &wanted)?);
                    } else {
                        weights.extend(
                            context
                                .load(name, &wanted)?
                                .into_iter()
                                .map(|(k, v)| (k, Weight::Float(v))),
                        );
                    }
                    moe_admission::verify_shard(path, &shard, &wanted)?;
                }
                let moe = MoeStage::new(
                    descriptor.layers.clone(),
                    descriptor.embedding,
                    descriptor.head,
                    limits.max_tokens,
                    weights,
                )?;
                return Ok((BTreeMap::new(), Some(moe)));
            }
            verify()?;
            let wanted = Qwen2::weights(&descriptor)?;
            let path_string = path
                .to_str()
                .ok_or_else(|| Error::new(ErrorCode::Artifact, "path-utf8"))?;
            let weights = if descriptor.profile == NumericsProfile::MlxPackedInt8 {
                context.load_packed(path_string, &wanted)?
            } else {
                context
                    .load(path_string, &wanted)?
                    .into_iter()
                    .map(|(k, v)| (k, Weight::Float(v)))
                    .collect()
            };
            verify()?;
            Ok((weights, None))
        })();
        let (weights, moe) = match loaded {
            Ok(loaded) => loaded,
            Err(error) => {
                // Temporary arrays have dropped before this branch. Failure
                // keeps the reservation and fences subsequent context reuse.
                context.synchronize()?;
                context.clear_cache()?;
                context.healthy()?;
                reservation.release_verified()?;
                return Err(error);
            }
        };
        Ok(Self {
            context,
            descriptor,
            config,
            weights,
            moe,
            control,
            loaded_bytes,
            reservation,
        })
    }
    /// Diagnostic identity of the resident image, not a checkpoint/rewind API.
    pub fn weight_digests(&self) -> Result<serde_json::Value> {
        self.live()?;
        let mut out = serde_json::Map::new();
        for (name, weight) in self.resident_weights() {
            let mut parts = serde_json::Map::new();
            let items: Vec<(&str, &Array)> = match weight {
                Weight::Float(a) => vec![("float", a)],
                Weight::Packed {
                    codes,
                    scales,
                    group_scales,
                    group_biases,
                } => vec![
                    ("packed", codes),
                    ("scales", scales),
                    ("group_scales", group_scales),
                    ("group_biases", group_biases),
                ],
            };
            for (kind, a) in items {
                let (bytes, dtype): (Vec<u8>, _) = if kind == "packed" {
                    (
                        a.words()?.iter().flat_map(|v| v.to_le_bytes()).collect(),
                        "uint32",
                    )
                } else {
                    (
                        a.floats()?.iter().flat_map(|v| v.to_le_bytes()).collect(),
                        "float32",
                    )
                };
                parts.insert(kind.into(),serde_json::json!({"shape":a.shape(),"payload_bytes":bytes.len(),"payload_sha256":format!("{:x}",Sha256::digest(&bytes)),"dtype":dtype}));
            }
            out.insert(name.clone(), serde_json::Value::Object(parts));
        }
        Ok(serde_json::Value::Object(out))
    }
    pub fn loaded_bytes(&self) -> u64 {
        self.loaded_bytes
    }
    pub fn loaded_names(&self) -> Vec<&str> {
        self.resident_weights().keys().map(String::as_str).collect()
    }
    fn resident_weights(&self) -> &BTreeMap<String, Weight<Array>> {
        self.moe.as_ref().map_or(&self.weights, MoeStage::weights)
    }
    fn live(&self) -> Result<()> {
        self.control.live()?;
        self.context.healthy()
    }
    fn session(&self, s: &Session) -> Result<()> {
        self.live()?;
        self.control.session(&s.cursor)
    }
    fn execute(&mut self, s: &mut Session, step: Step, prefill: bool) -> Result<Output> {
        self.session(s)?;
        let checked = self.control.check_step(&s.cursor, &step, prefill)?;
        let seq = checked.rows();
        let end = checked.end();
        let state_bytes = checked.state_bytes();
        let position = s.position();
        let hidden_size = self.config.hidden();
        let vocab = self.config.vocab();
        let (hidden, logits) = if let Some(moe) = &mut self.moe {
            let input = match &step.input {
                Input::Tokens(ids) => self.context.ids(ids)?,
                Input::Hidden(a) => {
                    let data: Vec<f32> = a
                        .bytes
                        .chunks_exact(4)
                        .map(|v| f32::from_le_bytes([v[0], v[1], v[2], v[3]]))
                        .collect();
                    self.context.floats(&data, &[1, seq as i32, hidden_size])?
                }
            };
            let input = if self.descriptor.embedding {
                StageInput::Tokens(&input)
            } else {
                StageInput::Hidden(&input)
            };
            moe.run_export(input, position as i32, |output| {
                let hidden = export(&output.hidden, [1, seq, hidden_size as u32])?;
                let logits = output
                    .logits
                    .as_ref()
                    .map(|a| export(a, [1, 1, vocab as u32]))
                    .transpose()?;
                self.context.synchronize()?;
                self.context.healthy()?;
                Ok((hidden, logits))
            })?
        } else {
            let Config::Dense(config) = self.config else {
                return Err(Error::new(ErrorCode::Closed, "moe-stage-missing"));
            };
            let x = match step.input {
                Input::Tokens(ids) => self
                    .weight(config.embedding_key())?
                    .embedding(&self.context.ids(&ids)?)?,
                Input::Hidden(a) => {
                    let data: Vec<f32> = a
                        .bytes
                        .chunks_exact(4)
                        .map(|v| f32::from_le_bytes([v[0], v[1], v[2], v[3]]))
                        .collect();
                    self.context
                        .floats(&data, &[1, seq as i32, config.hidden])?
                }
            };
            let (hidden, next) =
                forward(&self.descriptor, x, &self.weights, &s.kv, position as i32)?;
            let logits = if self.descriptor.head {
                let last =
                    hidden.slice(&[0, seq as i32 - 1, 0], &[1, seq as i32, config.hidden])?;
                let logits = linear(
                    &final_norm(config, &last, self.weight(config.norm_key())?.float()?)?,
                    self.weight(config.embedding_key())?,
                )?;
                Some(export(&logits, [1, 1, config.vocab as u32])?)
            } else {
                None
            };
            let hidden = export(&hidden, [1, seq, config.hidden as u32])?;
            self.context.synchronize()?;
            self.context.healthy()?;
            s.kv = next;
            self.context.healthy()?;
            (hidden, logits)
        };
        self.context.healthy()?;
        self.control.commit(&mut s.cursor, checked)?;
        Ok(Output {
            hidden,
            logits,
            state_generation: s.state_generation(),
            committed_position: end,
            state_bytes,
        })
    }
    fn weight(&self, name: &str) -> Result<&Weight<Array>> {
        self.weights
            .get(name)
            .ok_or_else(|| Error::new(ErrorCode::Artifact, "weight"))
    }
    /// Shape/kind-tagged diagnostics only. No restore, slice or rewind interface.
    pub fn state_snapshot(&self, s: &Session) -> Result<Vec<StateSnapshot>> {
        self.session(s)?;
        let mut out = Vec::new();
        for (index, layer) in self.descriptor.layers.clone().enumerate() {
            let pair = if let Some(moe) = &self.moe {
                moe.layer_state(layer)
            } else {
                s.kv[index].as_ref().map(|(a, b)| (a, b))
            };
            if let Some((a, b)) = pair {
                let kinds = if self.config.hybrid() && !model::hybrid::full_attention(layer) {
                    ["conv", "recurrent"]
                } else {
                    ["k", "v"]
                };
                for (kind, a) in kinds.into_iter().zip([a, b]) {
                    out.push(StateSnapshot {
                        layer,
                        kind,
                        shape: a.shape(),
                        bytes: a.floats()?.iter().flat_map(|v| v.to_le_bytes()).collect(),
                    });
                }
            }
        }
        Ok(out)
    }
    /// Diagnostic snapshot, not a portable rewind/checkpoint interface.
    pub fn kv_snapshot(&self, s: &Session) -> Result<Vec<KvSnapshot>> {
        self.session(s)?;
        if self.config.hybrid() {
            return Err(Error::new(
                ErrorCode::Unsupported,
                "hybrid-not-sliceable-kv",
            ));
        }
        let mut out = Vec::new();
        for (index, pair) in s.kv.iter().enumerate() {
            if let Some((k, v)) = pair {
                let bytes = |a: &Array| -> Result<Vec<u8>> {
                    Ok(a.floats()?.iter().flat_map(|v| v.to_le_bytes()).collect())
                };
                out.push((
                    self.descriptor.layers.start + index as u32,
                    k.shape(),
                    bytes(k)?,
                    bytes(v)?,
                ));
            }
        }
        Ok(out)
    }
}
pub struct MlxBackend {
    context: Rc<Context>,
}
impl MlxBackend {
    pub fn new(context: Rc<Context>) -> Self {
        Self { context }
    }
}
fn capabilities() -> Capabilities {
    Capabilities {
        architectures: vec![
            Architecture::Qwen2,
            Architecture::Qwen3,
            Architecture::Qwen35DenseHybrid,
            Architecture::Qwen35MoeHybrid,
        ],
        profiles: vec![
            NumericsProfile::MlxExplicitF32,
            NumericsProfile::MlxPackedInt8,
        ],
        state: StateKind::Hybrid,
        rewind: false,
        wire_export: false,
        device: "Apple Metal; build-host qualification only".into(),
        build: "mlx-c 57731b00 / MLX 36969c09 / C API 0.7.0".into(),
    }
}
impl BackendFactory for MlxBackend {
    type Stage = Worker;
    fn capabilities(&self) -> Capabilities {
        capabilities()
    }
    fn prepare(
        &mut self,
        descriptor: StageDescriptor,
        artifacts: &Artifacts,
        limits: Limits,
        load_generation: u64,
    ) -> Result<Worker> {
        Worker::prepare(
            Rc::clone(&self.context),
            descriptor,
            &artifacts.checkpoint,
            limits,
            load_generation,
        )
    }
}
impl StageBackend for Worker {
    type Session = Session;
    fn capabilities(&self) -> Capabilities {
        let mut c = capabilities();
        c.architectures = vec![self.descriptor.architecture];
        c.profiles = vec![self.descriptor.profile];
        c.state = if self.config.hybrid() {
            StateKind::Hybrid
        } else {
            StateKind::AttentionKv
        };
        c
    }
    fn new_session(&mut self, binding: Binding, limits: Limits) -> Result<Session> {
        self.live()?;
        let cursor = self.control.new_session(binding, limits)?;
        Ok(Session {
            cursor,
            kv: self.descriptor.layers.clone().map(|_| None).collect(),
        })
    }
    fn prefill(&mut self, s: &mut Session, step: Step) -> Result<Output> {
        self.execute(s, step, true)
    }
    fn decode(&mut self, s: &mut Session, step: Step) -> Result<Output> {
        self.execute(s, step, false)
    }
    fn synchronize(&mut self) -> Result<()> {
        self.live()?;
        self.context.synchronize()
    }
    fn export_activation(&self, _activation: &Activation) -> Result<Vec<u8>> {
        Err(Error::new(
            ErrorCode::Unsupported,
            "L5-versioned-wire-codec-required",
        ))
    }
    fn reset(&mut self, s: &mut Session) -> Result<()> {
        self.session(s)?;
        self.control.reset_check(&s.cursor)?;
        self.context.synchronize()?;
        if let Some(moe) = &mut self.moe {
            moe.reset();
        }
        s.kv = self.descriptor.layers.clone().map(|_| None).collect();
        self.context.healthy()?;
        self.control.reset(&mut s.cursor)
    }
    fn cancel(&mut self, s: &mut Session) -> Result<()> {
        self.session(s)?;
        self.control.cancel(&mut s.cursor)?;
        self.context.synchronize()
    }
    fn release_session(&mut self, s: Session) -> Result<()> {
        self.live()?;
        self.control.release_check(&s.cursor)?;
        self.context.synchronize()?;
        let Session { cursor, kv } = s;
        drop(kv);
        if let Some(moe) = &mut self.moe {
            moe.reset();
        }
        self.context.healthy()?;
        self.control.release(cursor)
    }
    fn close(&mut self) -> Result<()> {
        self.live()?;
        self.control.close_check()?;
        self.context.synchronize()?;
        self.moe = None;
        self.weights.clear();
        self.context.healthy()?;
        self.context.clear_cache()?;
        self.reservation.release_verified()?;
        self.control.close()
    }
}
