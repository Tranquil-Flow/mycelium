// SPDX-License-Identifier: AGPL-3.0-only
//! Pure bookkeeping used by the single MLX worker. No tensor runtime, artifact
//! loader, signed-authority issuer or transport is provided here.
use crate::{admission, moe_admission};
use model::{DenseConfig, moe_decoder};
use sha2::{Digest, Sha256};
use stage_contract::*;
use std::sync::atomic::{AtomicU64, Ordering};
static OWNER: AtomicU64 = AtomicU64::new(1);
#[derive(Debug, Clone, Copy)]
pub enum Config {
    Dense(DenseConfig),
    Moe,
}
impl Config {
    pub fn for_descriptor(d: &StageDescriptor) -> Result<Self> {
        if d.architecture == Architecture::Qwen35MoeHybrid {
            moe_admission::validate(d)?;
            Ok(Self::Moe)
        } else {
            Ok(Self::Dense(DenseConfig::for_descriptor(d)?))
        }
    }
    pub fn hidden(self) -> i32 {
        match self {
            Self::Dense(c) => c.hidden,
            Self::Moe => moe_decoder::HIDDEN,
        }
    }
    pub fn vocab(self) -> i32 {
        match self {
            Self::Dense(c) => c.vocab,
            Self::Moe => moe_decoder::VOCAB,
        }
    }
    pub fn hybrid(self) -> bool {
        match self {
            Self::Dense(c) => c.hybrid,
            Self::Moe => true,
        }
    }
    pub fn state_bytes(self, layers: std::ops::Range<u32>, tokens: u32) -> Result<u64> {
        match self {
            Self::Dense(c) => c.state_bytes_for(layers, tokens),
            Self::Moe => moe_admission::state_bytes(layers, tokens),
        }
    }
}
#[derive(Debug)]
pub struct Cursor {
    owner: u64,
    binding: Binding,
    limits: Limits,
    position: u32,
    state_generation: u64,
    cancelled: bool,
}
impl Cursor {
    pub fn position(&self) -> u32 {
        self.position
    }
    pub fn state_generation(&self) -> u64 {
        self.state_generation
    }
}
#[derive(Debug)]
pub struct CheckedStep {
    owner: u64,
    generation: u64,
    start: u32,
    end: u32,
    rows: u32,
    state_bytes: u64,
}
impl CheckedStep {
    pub fn rows(&self) -> u32 {
        self.rows
    }
    pub fn end(&self) -> u32 {
        self.end
    }
    pub fn state_bytes(&self) -> u64 {
        self.state_bytes
    }
}
pub struct Lifecycle {
    descriptor: StageDescriptor,
    config: Config,
    limits: Limits,
    generation: u64,
    owner: u64,
    next_generation: u64,
    active: bool,
    closed: bool,
}
pub fn payload_id(input: &Input) -> [u8; 32] {
    let mut hash = Sha256::new();
    match input {
        Input::Tokens(ids) => {
            for id in ids {
                hash.update(id.to_le_bytes());
            }
        }
        Input::Hidden(a) => hash.update(&a.bytes),
    }
    hash.finalize().into()
}
impl Lifecycle {
    pub fn new(descriptor: StageDescriptor, limits: Limits, generation: u64) -> Result<Self> {
        admission::admit(&descriptor, limits)?;
        let config = Config::for_descriptor(&descriptor)?;
        if generation == 0 {
            return Err(Error::new(ErrorCode::Binding, "load-generation"));
        }
        let owner = OWNER
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |id| id.checked_add(1))
            .map_err(|_| Error::new(ErrorCode::ResourceCap, "worker-id-overflow"))?;
        Ok(Self {
            descriptor,
            config,
            limits,
            generation,
            owner,
            next_generation: 0,
            active: false,
            closed: false,
        })
    }
    pub fn live(&self) -> Result<()> {
        if self.closed {
            Err(Error::new(ErrorCode::Closed, "worker"))
        } else {
            Ok(())
        }
    }
    pub fn session(&self, s: &Cursor) -> Result<()> {
        self.live()?;
        if !self.active || s.owner != self.owner {
            return Err(Error::new(ErrorCode::Binding, "session-owner"));
        }
        if s.cancelled {
            return Err(Error::new(ErrorCode::Cancelled, "session"));
        }
        Ok(())
    }
    pub fn new_session(&mut self, binding: Binding, limits: Limits) -> Result<Cursor> {
        self.live()?;
        if self.active {
            return Err(Error::new(ErrorCode::ResourceCap, "one-session-per-stage"));
        }
        if binding.descriptor != self.descriptor
            || binding.load_generation != self.generation
            || binding.request == [0; 32]
            || binding.assignment == [0; 32]
        {
            return Err(Error::new(ErrorCode::Binding, "session-binding"));
        }
        if limits.weight_bytes > self.limits.weight_bytes
            || limits.max_tokens == 0
            || limits.max_tokens > self.limits.max_tokens
            || limits.state_bytes > self.limits.state_bytes
            || limits.scratch_bytes > self.limits.scratch_bytes
        {
            return Err(Error::new(ErrorCode::ResourceCap, "session-limits"));
        }
        if self
            .config
            .state_bytes(self.descriptor.layers.clone(), limits.max_tokens)?
            > limits.state_bytes
        {
            return Err(Error::new(ErrorCode::ResourceCap, "session-kv"));
        }
        admission::admit(&self.descriptor, limits)?;
        let state_generation = self.next_generation;
        self.next_generation = state_generation
            .checked_add(1)
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "state-generation"))?;
        self.active = true;
        Ok(Cursor {
            owner: self.owner,
            binding,
            limits,
            position: 0,
            state_generation,
            cancelled: false,
        })
    }
    pub fn check_step(&self, s: &Cursor, step: &Step, prefill: bool) -> Result<CheckedStep> {
        self.session(s)?;
        if step.binding != s.binding
            || step.state_generation != s.state_generation
            || payload_id(&step.input) != step.payload
        {
            return Err(Error::new(ErrorCode::Binding, "step-binding"));
        }
        if step.position != s.position
            || (prefill && s.position != 0)
            || (!prefill && s.position == 0)
        {
            return Err(Error::new(ErrorCode::Position, "step-position"));
        }
        let rows = match &step.input {
            Input::Tokens(ids) => {
                if !self.descriptor.embedding
                    || ids.is_empty()
                    || ids.iter().any(|id| *id >= self.config.vocab() as u32)
                {
                    return Err(Error::new(ErrorCode::MalformedTensor, "token-input"));
                }
                u32::try_from(ids.len())
                    .map_err(|_| Error::new(ErrorCode::ResourceCap, "input-length"))?
            }
            Input::Hidden(a) => {
                if self.descriptor.embedding {
                    return Err(Error::new(ErrorCode::MalformedTensor, "hidden-input"));
                }
                a.validate(self.config.hidden() as u32, s.limits.scratch_bytes)?;
                a.shape[1]
            }
        };
        if (!prefill && rows != 1)
            || (matches!(self.config, Config::Moe) && rows > moe_decoder::MAX_ROWS as u32)
        {
            return Err(Error::new(ErrorCode::MalformedTensor, "decode-length"));
        }
        let end = s
            .position
            .checked_add(rows)
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "position-overflow"))?;
        let state_bytes = self
            .config
            .state_bytes(self.descriptor.layers.clone(), end)?;
        if end > s.limits.max_tokens || state_bytes > s.limits.state_bytes {
            return Err(Error::new(ErrorCode::ResourceCap, "session-state"));
        }
        Ok(CheckedStep {
            owner: self.owner,
            generation: s.state_generation,
            start: s.position,
            end,
            rows,
            state_bytes,
        })
    }
    /// Publication occurs only after the worker synchronizes and exports successfully.
    pub fn commit(&self, s: &mut Cursor, checked: CheckedStep) -> Result<()> {
        self.session(s)?;
        if checked.owner != self.owner
            || checked.generation != s.state_generation
            || checked.start != s.position
        {
            return Err(Error::new(ErrorCode::Binding, "commit-origin"));
        }
        s.position = checked.end;
        Ok(())
    }
    pub fn reset(&mut self, s: &mut Cursor) -> Result<()> {
        let next = self.reset_check(s)?;
        s.position = 0;
        s.state_generation = self.next_generation;
        self.next_generation = next;
        Ok(())
    }
    pub fn reset_check(&self, s: &Cursor) -> Result<u64> {
        self.session(s)?;
        self.next_generation
            .checked_add(1)
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "state-generation"))
    }
    pub fn cancel(&self, s: &mut Cursor) -> Result<()> {
        self.session(s)?;
        s.cancelled = true;
        Ok(())
    }
    pub fn release_check(&self, s: &Cursor) -> Result<()> {
        self.live()?;
        if !self.active || s.owner != self.owner {
            return Err(Error::new(ErrorCode::Binding, "release-owner"));
        }
        Ok(())
    }
    pub fn release(&mut self, s: Cursor) -> Result<()> {
        self.release_check(&s)?;
        self.active = false;
        Ok(())
    }
    pub fn close_check(&self) -> Result<()> {
        self.live()?;
        if self.active {
            Err(Error::new(ErrorCode::Cleanup, "active-session"))
        } else {
            Ok(())
        }
    }
    pub fn close(&mut self) -> Result<()> {
        self.close_check()?;
        self.closed = true;
        Ok(())
    }
}
