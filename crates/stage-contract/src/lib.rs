// SPDX-License-Identifier: AGPL-3.0-only
//! Backend-neutral, in-process stage seam. Not a new wire protocol.
pub mod joined_native;
pub mod native_proof;
pub mod native_session;
pub mod native_shard;
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorCode {
    InvalidDescriptor,
    MalformedTensor,
    Unsupported,
    ResourceCap,
    Binding,
    Position,
    Cancelled,
    Closed,
    Native,
    Cleanup,
    Artifact,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub code: ErrorCode,
    pub phase: &'static str,
    pub native_status: Option<i32>,
    pub native_callback: bool,
}
impl Error {
    pub const fn new(code: ErrorCode, phase: &'static str) -> Self {
        Self {
            code,
            phase,
            native_status: None,
            native_callback: false,
        }
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{:?}: {} (native_status={:?}, callback={})",
            self.code, self.phase, self.native_status, self.native_callback
        )
    }
}
impl std::error::Error for Error {}
pub type Result<T> = std::result::Result<T, Error>;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Architecture {
    Qwen2,
    Qwen3,
    Qwen35DenseHybrid,
    Qwen35MoeHybrid,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NumericsProfile {
    MlxExplicitF32,
    MlxPackedInt8,
    CpuInt8F32,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StateKind {
    AttentionKv,
    ReplayOnly,
    Hybrid,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StageDescriptor {
    pub checkpoint: [u8; 32],
    pub revision: String,
    pub tensor_manifest: [u8; 32],
    pub semantics: String,
    pub architecture: Architecture,
    pub layers: std::ops::Range<u32>,
    pub embedding: bool,
    pub head: bool,
    pub representation: String,
    pub quantizer: String,
    pub profile: NumericsProfile,
    pub state_schema: String,
}
impl StageDescriptor {
    pub fn validate(&self, total_layers: u32) -> Result<()> {
        if self.layers.start >= self.layers.end
            || self.layers.end > total_layers
            || (self.embedding && self.layers.start != 0)
            || (self.head && self.layers.end != total_layers)
            || self.checkpoint == [0; 32]
            || self.tensor_manifest == [0; 32]
            || [
                &self.revision,
                &self.semantics,
                &self.representation,
                &self.quantizer,
                &self.state_schema,
            ]
            .iter()
            .any(|s| s.is_empty())
        {
            return Err(Error::new(ErrorCode::InvalidDescriptor, "descriptor"));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub request: [u8; 32],
    pub assignment: [u8; 32],
    pub load_generation: u64,
    pub descriptor: StageDescriptor,
}
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub weight_bytes: u64,
    pub state_bytes: u64,
    pub scratch_bytes: u64,
    pub max_tokens: u32,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DType {
    F32,
    Bf16,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ByteOrder {
    Little,
    Big,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Layout {
    BatchSequenceHidden,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Activation {
    pub shape: [u32; 3],
    pub dtype: DType,
    pub order: ByteOrder,
    pub layout: Layout,
    pub bytes: Vec<u8>,
}
impl Activation {
    pub fn validate(&self, hidden: u32, cap: u64) -> Result<()> {
        let bad = || Error::new(ErrorCode::MalformedTensor, "activation");
        if self.dtype != DType::F32
            || self.order != ByteOrder::Little
            || self.shape[0] != 1
            || self.shape[1] == 0
            || self.shape[2] != hidden
            || hidden == 0
        {
            return Err(bad());
        }
        let bytes = self
            .shape
            .iter()
            .try_fold(4u64, |n, d| n.checked_mul(u64::from(*d)))
            .ok_or_else(bad)?;
        if bytes > cap {
            return Err(Error::new(ErrorCode::ResourceCap, "activation"));
        }
        if bytes != self.bytes.len() as u64 {
            return Err(bad());
        }
        for word in self.bytes.chunks_exact(4) {
            if !f32::from_le_bytes([word[0], word[1], word[2], word[3]]).is_finite() {
                return Err(bad());
            }
        }
        Ok(())
    }
}
#[derive(Debug, Clone)]
pub enum Input {
    Tokens(Vec<u32>),
    Hidden(Activation),
}
#[derive(Debug, Clone)]
pub struct Step {
    pub binding: Binding,
    pub state_generation: u64,
    pub position: u32,
    pub payload: [u8; 32],
    pub input: Input,
}
#[derive(Debug, Clone)]
pub struct Output {
    pub hidden: Activation,
    pub logits: Option<Activation>,
    pub state_generation: u64,
    pub committed_position: u32,
    pub state_bytes: u64,
}
#[derive(Debug, Clone)]
pub struct Capabilities {
    pub architectures: Vec<Architecture>,
    pub profiles: Vec<NumericsProfile>,
    pub state: StateKind,
    pub rewind: bool,
    pub wire_export: bool,
    pub device: String,
    pub build: String,
}
/// Native handles never enter this interface. Wire export stays refused until the
/// L5 existing versioned codec/conversion policy is integrated.
/// Admitted immutable local artifact; not an activation wire payload. L7 owns
/// staging/immutability, while the backend verifies the descriptor digests.
pub struct Artifacts {
    pub checkpoint: std::path::PathBuf,
}
/// Backend-neutral preparation surface. The factory owns its local runtime;
/// no native context or pointer crosses this contract.
pub trait BackendFactory {
    type Stage: StageBackend;
    fn capabilities(&self) -> Capabilities;
    fn prepare(
        &mut self,
        descriptor: StageDescriptor,
        artifacts: &Artifacts,
        limits: Limits,
        load_generation: u64,
    ) -> Result<Self::Stage>;
}
pub trait StageBackend {
    type Session;
    fn capabilities(&self) -> Capabilities;
    fn new_session(&mut self, binding: Binding, limits: Limits) -> Result<Self::Session>;
    fn prefill(&mut self, session: &mut Self::Session, step: Step) -> Result<Output>;
    fn decode(&mut self, session: &mut Self::Session, step: Step) -> Result<Output>;
    fn synchronize(&mut self) -> Result<()>;
    fn export_activation(&self, activation: &Activation) -> Result<Vec<u8>>;
    fn reset(&mut self, session: &mut Self::Session) -> Result<()>;
    fn cancel(&mut self, session: &mut Self::Session) -> Result<()>;
    fn release_session(&mut self, session: Self::Session) -> Result<()>;
    fn close(&mut self) -> Result<()>;
}

#[cfg(test)]
mod tests {
    use super::*;
    fn descriptor() -> StageDescriptor {
        StageDescriptor {
            checkpoint: [1; 32],
            revision: "pinned".into(),
            tensor_manifest: [2; 32],
            semantics: "qwen2-v1".into(),
            architecture: Architecture::Qwen2,
            layers: 0..8,
            embedding: true,
            head: false,
            representation: "bf16-expanded-f32".into(),
            quantizer: "none".into(),
            profile: NumericsProfile::MlxExplicitF32,
            state_schema: "attention-kv-f32-v1".into(),
        }
    }
    // L8 / F14: admission must happen before native allocation.
    #[test]
    fn malformed_activation_refuses() {
        let mut a = Activation {
            shape: [1, 2, 3],
            dtype: DType::F32,
            order: ByteOrder::Little,
            layout: Layout::BatchSequenceHidden,
            bytes: vec![0; 24],
        };
        assert!(a.validate(3, 24).is_ok());
        a.bytes.pop();
        assert_eq!(
            a.validate(3, 24).unwrap_err().code,
            ErrorCode::MalformedTensor
        );
    }
    #[test]
    fn cap_and_overflow_refuse() {
        let a = Activation {
            shape: [1, 2, 3],
            dtype: DType::F32,
            order: ByteOrder::Little,
            layout: Layout::BatchSequenceHidden,
            bytes: vec![0; 24],
        };
        assert_eq!(a.validate(3, 23).unwrap_err().code, ErrorCode::ResourceCap);
        let mut huge = a.clone();
        huge.shape = [u32::MAX; 3];
        assert!(huge.validate(u32::MAX, u64::MAX).is_err());
    }
    #[test]
    fn nonfinite_wrong_layout_dtype_and_batch_refuse() {
        let a = Activation {
            shape: [1, 1, 1],
            dtype: DType::F32,
            order: ByteOrder::Little,
            layout: Layout::BatchSequenceHidden,
            bytes: f32::NAN.to_le_bytes().to_vec(),
        };
        assert!(a.validate(1, 4).is_err());
        let mut b = a;
        b.bytes = 0f32.to_le_bytes().to_vec();
        b.order = ByteOrder::Big;
        assert!(b.validate(1, 4).is_err());
        b.order = ByteOrder::Little;
        b.dtype = DType::Bf16;
        assert!(b.validate(1, 4).is_err());
        b.dtype = DType::F32;
        b.shape[0] = 0;
        assert!(b.validate(1, 4).is_err());
    }
    #[test]
    fn descriptor_refuses_wrong_range_and_roles() {
        let mut d = descriptor();
        assert!(d.validate(24).is_ok());
        d.layers = 8..8;
        assert!(d.validate(24).is_err());
        d.layers = 8..25;
        assert!(d.validate(24).is_err());
        d.layers = 8..24;
        assert!(d.validate(24).is_err());
        d.embedding = false;
        d.head = true;
        assert!(d.validate(24).is_ok());
        d.checkpoint = [0; 32];
        assert!(d.validate(24).is_err());
    }
}
