// SPDX-License-Identifier: AGPL-3.0-only
//! `ctx.state`: the chain-owned private state one run's steps leave for the steps after them
//! (`demo_chain.py` `ctx.state[...]`; never shown in status, unlike `ctx.result`).
use crate::machine::Reason;
use crate::steps::model::{ModelStepOutput, PreparedSource};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Debug)]
pub struct RunState {
    pub run_id: String,
    pub deployment_id: String,
    pub reason: Reason,
    /// `ctx.state['execution_ceiling']`.
    pub execution_ceiling: Option<Value>,
    /// `ctx.state['rollover_binding']`.
    pub rollover_binding: Option<Value>,
    /// `ctx.state['local_node']` (contributor step).
    pub local_node: Option<String>,
    /// Model step: `prepared`, `slice_mode`, `manifest_digest`, `model`.
    pub model: Option<ModelStepOutput>,
    /// `ctx.state['participants']`.
    pub participants: Vec<String>,
    /// `ctx.state['descriptors']`: signed cards, sorted by `target_node_id`.
    pub descriptors: Vec<Value>,
    /// Compile step: `proposal`, `worker_wall_limits`, `wall_cap_fallback_nodes`, `compile_state`, `layers`.
    pub proposal: Option<Value>,
    pub worker_wall_limits: BTreeMap<String, u64>,
    pub wall_cap_fallback_nodes: Vec<String>,
    pub compile_state: Option<Value>,
    /// `app._member_capability_reports`: the records the compile was planned from.
    pub member_records: Option<Value>,
    /// `app._chain_compile_state` once compiled (proposal, layer split, slice plan digests, ...).
    pub chain_compile_state: Option<Value>,
    pub layers: BTreeMap<String, [usize; 2]>,
    /// Slices: run-wide limits and source-bound per-node work announcement.
    pub work_limits: Option<Value>,
    pub work_nodes: Option<Value>,
    /// Earliest publish/targets clock; setdefault preserves the first step's value.
    pub execution_clock_started_at: Option<f64>,
    /// None means absent, Some(Null) means start returned no registry origin.
    pub registry_origin: Option<Value>,
}

impl RunState {
    pub fn new(run_id: &str, deployment_id: &str, reason: Reason) -> Self {
        Self {
            run_id: run_id.to_owned(),
            deployment_id: deployment_id.to_owned(),
            reason,
            execution_ceiling: None,
            rollover_binding: None,
            local_node: None,
            model: None,
            participants: Vec::new(),
            descriptors: Vec::new(),
            proposal: None,
            worker_wall_limits: BTreeMap::new(),
            wall_cap_fallback_nodes: Vec::new(),
            compile_state: None,
            member_records: None,
            chain_compile_state: None,
            layers: BTreeMap::new(),
            work_limits: None,
            work_nodes: None,
            execution_clock_started_at: None,
            registry_origin: None,
        }
    }
    pub fn prepared(&self) -> Option<&PreparedSource> {
        self.model.as_ref().map(|m| &m.prepared)
    }
}
