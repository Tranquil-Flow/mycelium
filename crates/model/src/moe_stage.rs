// SPDX-License-Identifier: AGPL-3.0-only
//! Resident Qwen3.6-35B-A3B stage: an owned contiguous layer range, optional untied
//! embedding (entry) and norm + untied head (final), with one atomically committed position.
//! Backend-neutral numerics only: signed grants, session generations and transport belong
//! to the stage worker and L5. Source: qwen35_mlx.py / mlx_runtime.py `_execute_qwen35_stage`
//! @ f4e366e9 (embedding -> layers -> `hidden[:, -1:]` -> final norm -> lm_head).
use crate::{
    Weight,
    moe::MoeTensor,
    moe_decoder::{self, DecoderLayer, HIDDEN, LAYERS, MAX_ROWS},
};
use stage_contract::{Error, ErrorCode, Result};
use std::{collections::BTreeMap, ops::Range};

pub enum StageInput<'a, T> {
    /// `[1, n]` uint32 ids; the entry stage only.
    Tokens(&'a T),
    /// `[1, n, 2048]` float32 activations; every non-entry stage.
    Hidden(&'a T),
}
pub struct StageOutput<T> {
    /// All rows of the stage's last layer (what the next stage receives).
    pub hidden: T,
    /// Final stage only: logits of the LAST row (`[1, 1, vocab]`), as the oracle's
    /// `produce_logits` path.
    pub logits: Option<T>,
}
pub struct MoeStage<T> {
    range: Range<u32>,
    embedding: bool,
    head: bool,
    layers: Vec<DecoderLayer<T>>,
    weights: BTreeMap<String, Weight<T>>,
    position: i32,
    max_tokens: i32,
}
impl<T: MoeTensor> MoeStage<T> {
    /// Takes ownership of EXACTLY the assigned tensors: any missing, extra (unassigned
    /// layer, a tied head alias, the embedding on a non-entry stage) or misshapen tensor
    /// is refused.
    pub fn new(
        range: Range<u32>,
        embedding: bool,
        head: bool,
        max_tokens: u32,
        weights: BTreeMap<String, Weight<T>>,
    ) -> Result<Self> {
        Self::verify_inventory(range.clone(), embedding, head, &weights)?;
        let layers = range
            .clone()
            .map(|layer| DecoderLayer::new(layer, max_tokens))
            .collect::<Result<Vec<_>>>()?;
        let max_tokens = i32::try_from(max_tokens)
            .ok()
            .filter(|n| *n > 0)
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "moe-token-cap"))?;
        Ok(Self {
            range,
            embedding,
            head,
            layers,
            weights,
            position: 0,
            max_tokens,
        })
    }
    /// The exact-inventory rule of [`Self::new`], checkable without surrendering the tensors.
    pub fn verify_inventory(
        range: Range<u32>,
        embedding: bool,
        head: bool,
        weights: &BTreeMap<String, Weight<T>>,
    ) -> Result<()> {
        let wanted = moe_decoder::stage_weights(range, embedding, head)?;
        if wanted.len() != weights.len()
            || wanted
                .iter()
                .any(|(name, shape)| weights.get(name).is_none_or(|w| w.shape() != *shape))
        {
            return Err(Error::new(ErrorCode::MalformedTensor, "moe-stage-weights"));
        }
        Ok(())
    }
    pub fn layers(&self) -> Range<u32> {
        self.range.clone()
    }
    pub fn position(&self) -> i32 {
        self.position
    }
    pub fn weights(&self) -> &BTreeMap<String, Weight<T>> {
        &self.weights
    }
    pub fn layer_state(&self, layer: u32) -> Option<(&T, &T)> {
        let index = layer.checked_sub(self.range.start)? as usize;
        self.layers.get(index)?.state()
    }
    pub fn reset(&mut self) {
        self.layers.iter_mut().for_each(DecoderLayer::reset);
        self.position = 0;
    }
    /// All-or-nothing: no layer state or position changes unless every layer, the
    /// optional embedding and the optional head evaluate successfully.
    pub fn run(&mut self, input: StageInput<'_, T>, position: i32) -> Result<StageOutput<T>> {
        self.run_export(input, position, Ok)
    }
    /// Export/synchronize at the worker boundary BEFORE publishing any layer state.
    /// A failed export leaves every cursor and committed KV/recurrent pair untouched.
    pub fn run_export<R>(
        &mut self,
        input: StageInput<'_, T>,
        position: i32,
        finish: impl FnOnce(StageOutput<T>) -> Result<R>,
    ) -> Result<R> {
        if position != self.position {
            return Err(Error::new(ErrorCode::Position, "moe-stage-no-rewind"));
        }
        let mut x = match (input, self.embedding) {
            (StageInput::Tokens(ids), true) => moe_decoder::embed(ids, &self.weights)?,
            (StageInput::Hidden(x), false) => x.share()?,
            _ => {
                return Err(Error::new(
                    ErrorCode::MalformedTensor,
                    "moe-stage-input-kind",
                ));
            }
        };
        let shape = x.shape();
        if shape.len() != 3
            || shape[0] != 1
            || !(1..=MAX_ROWS).contains(&shape[1])
            || shape[2] != HIDDEN
        {
            return Err(Error::new(ErrorCode::MalformedTensor, "moe-stage-shape"));
        }
        let next = position
            .checked_add(shape[1])
            .filter(|n| *n <= self.max_tokens)
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "moe-token-cap"))?;
        let mut steps = Vec::with_capacity(self.layers.len());
        for layer in &self.layers {
            let step = layer.compute(&x, &self.weights, position, false)?;
            x = step.block.output.share()?;
            steps.push(step);
        }
        let logits = if self.head {
            let logits = moe_decoder::head(&x, &self.weights, false)?;
            logits.eval()?;
            Some(logits)
        } else {
            None
        };
        x.eval()?;
        let output = finish(StageOutput { hidden: x, logits })?;
        for (layer, step) in self.layers.iter_mut().zip(steps) {
            layer.commit(step);
        }
        self.position = next;
        debug_assert!(self.range.end <= LAYERS);
        Ok(output)
    }
}
