// SPDX-License-Identifier: AGPL-3.0-only
//! Greedy decode driver for a stage chain: prefill the prompt, then feed each SELECTED token back
//! (no teacher forcing), recording per step the selected token, position, row count and the SHA-256
//! of the f32 little-endian logits payload (and of the final hidden state when the chain provides it).
//! Backend-neutral; the numerics live in `moe_stage`. Source semantics: the oracle's
//! `_execute_qwen35_stage` loop @ f4e366e9 (one prefill, then one single-token step per selection;
//! `new_tokens` selections = 1 prefill + `new_tokens - 1` decodes).
use crate::{
    moe::MoeTensor,
    moe_decoder::{MAX_ROWS, VOCAB, select_greedy},
    moe_stage::{MoeStage, StageInput},
};
use sha2::{Digest, Sha256};
use stage_contract::{Error, ErrorCode, Result};

pub type Hash = [u8; 32];
pub fn sha256_f32(values: &[f32]) -> Hash {
    let mut hash = Sha256::new();
    for v in values {
        hash.update(v.to_le_bytes());
    }
    hash.finalize().into()
}
pub fn hex(hash: &Hash) -> String {
    hash.iter().map(|b| format!("{b:02x}")).collect()
}

pub struct ChainOutput {
    /// Last-row logits, exactly `VOCAB` finite floats.
    pub logits: Vec<f32>,
    /// SHA-256 of the f32 payload of the last stage's output hidden state, when available.
    pub hidden_sha256: Option<Hash>,
}
/// All-or-nothing: a failed step must leave the chain's committed state untouched.
pub trait Chain {
    fn step(&mut self, ids: &[u32], position: i32) -> Result<ChainOutput>;
    fn reset(&mut self);
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StepRecord {
    pub index: usize,
    pub rows: usize,
    pub position: i32,
    pub token: u32,
    pub logits_sha256: Hash,
    pub hidden_sha256: Option<Hash>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Generation {
    pub prompt: Vec<u32>,
    pub steps: Vec<StepRecord>,
    /// The stop id that ended generation early (it is the last recorded token), if any.
    pub stopped_on: Option<u32>,
}
impl Generation {
    pub fn tokens(&self) -> Vec<u32> {
        self.steps.iter().map(|s| s.token).collect()
    }
}
fn bad(phase: &'static str) -> Error {
    Error::new(ErrorCode::MalformedTensor, phase)
}
/// `cap` is the chain's token capacity (prompt + decodes - 1 must fit).
pub fn generate<C: Chain>(
    chain: &mut C,
    prompt: &[u32],
    new_tokens: usize,
    stop: &[u32],
    cap: usize,
) -> Result<Generation> {
    let rows = i32::try_from(prompt.len()).map_err(|_| bad("greedy-prompt"))?;
    if new_tokens == 0 || !(1..=MAX_ROWS).contains(&rows) {
        return Err(bad("greedy-prompt"));
    }
    if prompt.len().saturating_add(new_tokens - 1) > cap {
        return Err(Error::new(ErrorCode::ResourceCap, "greedy-token-cap"));
    }
    chain.reset();
    let mut steps: Vec<StepRecord> = Vec::with_capacity(new_tokens);
    let mut position = 0_i32;
    let mut feed = prompt.to_vec();
    let mut stopped_on = None;
    for index in 0..new_tokens {
        let out = chain.step(&feed, position)?;
        let token = select_greedy(&out.logits)?;
        steps.push(StepRecord {
            index,
            rows: feed.len(),
            position,
            token,
            logits_sha256: sha256_f32(&out.logits),
            hidden_sha256: out.hidden_sha256,
        });
        position = position
            .checked_add(i32::try_from(feed.len()).map_err(|_| bad("greedy-rows"))?)
            .ok_or_else(|| bad("greedy-position"))?;
        if stop.contains(&token) {
            stopped_on = Some(token);
            break;
        }
        feed = vec![token];
    }
    Ok(Generation {
        prompt: prompt.to_vec(),
        steps,
        stopped_on,
    })
}

/// First index at which two runs differ (token, logits hash or, when both have it, hidden hash),
/// or `None` when equal in length and content.
pub fn first_divergence(a: &[StepRecord], b: &[StepRecord]) -> Option<usize> {
    let common = a.len().min(b.len());
    for i in 0..common {
        let (x, y) = (&a[i], &b[i]);
        let hidden_differs = match (x.hidden_sha256, y.hidden_sha256) {
            (Some(p), Some(q)) => p != q,
            _ => false,
        };
        if x.token != y.token
            || x.logits_sha256 != y.logits_sha256
            || x.position != y.position
            || x.rows != y.rows
            || hidden_differs
        {
            return Some(i);
        }
    }
    (a.len() != b.len()).then_some(common)
}

/// Host-side tensor construction/readback and the stage-to-stage hop (for a real run: the L5
/// `Activation` byte round trip).
pub trait StageIo<T> {
    fn ids(&self, ids: &[u32]) -> Result<T>;
    fn floats(&self, tensor: &T) -> Result<Vec<f32>>;
    fn hop(&self, hidden: &T) -> Result<T>;
}
/// Entry stage -> ... -> final stage, resident for the whole run.
pub struct StageChain<T, I> {
    stages: Vec<MoeStage<T>>,
    io: I,
}
impl<T: MoeTensor, I: StageIo<T>> StageChain<T, I> {
    /// Refuses anything but contiguous stages covering layers `0..LAYERS` with the embedding on the
    /// first and the head on the last stage only.
    pub fn new(stages: Vec<MoeStage<T>>, io: I) -> Result<Self> {
        let mut next = 0;
        for stage in &stages {
            let range = stage.layers();
            if range.start != next || range.end <= range.start {
                return Err(Error::new(ErrorCode::Binding, "greedy-stage-order"));
            }
            next = range.end;
        }
        if stages.is_empty() || next != crate::moe_decoder::LAYERS {
            return Err(Error::new(ErrorCode::Binding, "greedy-stage-cover"));
        }
        Ok(Self { stages, io })
    }
}
impl<T: MoeTensor, I: StageIo<T>> Chain for StageChain<T, I> {
    fn step(&mut self, ids: &[u32], position: i32) -> Result<ChainOutput> {
        let ids = self.io.ids(ids)?;
        let mut carried: Option<T> = None;
        let mut logits = None;
        let last = self.stages.len() - 1;
        for (n, stage) in self.stages.iter_mut().enumerate() {
            let out = match carried.as_ref() {
                None => stage.run(StageInput::Tokens(&ids), position)?,
                Some(h) => stage.run(StageInput::Hidden(h), position)?,
            };
            if n == last {
                let l = out.logits.ok_or_else(|| bad("greedy-no-logits"))?;
                let hidden = self.io.floats(&out.hidden)?;
                let shape = l.shape();
                if shape.iter().product::<i32>() != VOCAB {
                    return Err(bad("greedy-logits-shape"));
                }
                logits = Some((self.io.floats(&l)?, sha256_f32(&hidden)));
            } else {
                carried = Some(self.io.hop(&out.hidden)?);
            }
        }
        let (logits, hidden) = logits.ok_or_else(|| bad("greedy-no-logits"))?;
        Ok(ChainOutput {
            logits,
            hidden_sha256: Some(hidden),
        })
    }
    fn reset(&mut self) {
        self.stages.iter_mut().for_each(MoeStage::reset);
    }
}
