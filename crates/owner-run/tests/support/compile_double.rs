// SPDX-License-Identifier: AGPL-3.0-only
//! The compile step's port, scripted from a recorded case (`inputs` block of FX-L7-chain-compile-r11 or of
//! the r12 run probe): the planner stays real, the builder thread is a poll countdown.
#![allow(dead_code)]
use owner_run::chain::PortError;
use owner_run::steps::compile::{
    BuildPoll, CompileContext, CompileInputs, CompilePort, LocalDevice,
};
use planner::chain_compile::{BuildOutput, BuildRequest, GrantCaps, ModelContract};
use serde_json::Value;

pub fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_owned())
        .collect()
}

#[derive(Clone)]
pub struct Port {
    pub inputs: CompileInputs,
    pub adapter: Value,
    pub slice_mode: bool,
    /// polls answered `Pending` before the builder is done
    pub delay: usize,
    pub polls: usize,
    pub retired: usize,
    pub requests: Vec<BuildRequest>,
}

impl CompilePort for Port {
    fn compile_context(&mut self) -> Result<CompileContext, PortError> {
        Ok(CompileContext {
            facts: self.inputs.facts.clone(),
            local: self.inputs.local.clone(),
            joined_reports: self.inputs.joined_reports.clone(),
            now: self.inputs.now,
        })
    }
    fn compile_inputs(&mut self) -> Result<CompileInputs, PortError> {
        Ok(self.inputs.clone())
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        self.retired += 1;
        Ok(())
    }
    fn start_build(&mut self, request: &BuildRequest) {
        self.requests.push(request.clone());
    }
    fn poll_build(&mut self) -> BuildPoll {
        let a = &self.adapter;
        if a["block_build"] == true {
            return BuildPoll::Pending;
        }
        self.polls += 1;
        if self.polls <= self.delay {
            return BuildPoll::Pending;
        }
        if a["build_raises"] == true {
            return BuildPoll::Done(Err(PortError::Failed {
                code: None,
                text: "builder failed".into(),
            }));
        }
        BuildPoll::Done(Ok(BuildOutput {
            operator_plan_digest: a["operator_plan_digest"].as_str().unwrap().into(),
            physical_host_ids: strings(&a["physical_host_ids"]),
            cache_hit: a["cache_hit"].as_bool().unwrap(),
            build_seconds: a["build_seconds"].as_f64().unwrap(),
            slice_plan_digest: self
                .slice_mode
                .then(|| a["slice_plan_digest"].as_str().unwrap().to_owned()),
            node_slice_plan_digests: self.slice_mode.then(|| {
                a["node_slice_plan_digests"]
                    .as_object()
                    .unwrap()
                    .iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
                    .collect()
            }),
        }))
    }
}

pub fn inputs_of(i: &Value) -> CompileInputs {
    let local = i["local_node"].as_str();
    let device = &i["local_device"];
    let grant = i["grant"].as_object().map(|g| GrantCaps {
        storage_bytes: g.get("storage_bytes").and_then(Value::as_u64),
        max_memory_bytes: g.get("max_memory_bytes").and_then(Value::as_u64),
        max_wall_seconds: g.get("max_wall_seconds").and_then(Value::as_u64),
    });
    let p = &i["prepared"];
    CompileInputs {
        facts: i["facts"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect(),
        observation_hosts: Some(strings(&i["observation_hosts"])),
        local: local.map(|n| LocalDevice {
            memory_bytes: device["memory_bytes"].as_u64().unwrap(),
            available_storage_bytes: device.get("available_storage_bytes").cloned(),
            generation: i["current_members"][n]["generation"].clone(),
            grant: grant.clone(),
            policy_storage_bytes: i["policy"]["storage_bytes"].as_u64(),
        }),
        joined_reports: i["joined_reports"].clone(),
        now: i["now"].as_f64().unwrap(),
        owner_ready: i["owner_ready"].as_bool().unwrap(),
        already_started: i["already_started"].as_bool().unwrap(),
        resource_denied: i["resource_denied"].as_bool().unwrap(),
        model: ModelContract {
            model_id: p["model_id"].as_str().unwrap().into(),
            resolved_commit: p["resolved_commit"].as_str().unwrap().into(),
            source_representation: p["source_representation"].as_str().unwrap().into(),
            runtime_dtype: p["runtime_dtype"].as_str().unwrap().into(),
            runtime_quantization: p["runtime_quantization"].as_str().unwrap().into(),
            // The oracle's `DEMO_CONTEXT_TOKENS` (model_resources.py:76), its fixtures' context.
            serving_context_tokens: 4096,
        },
        manifest_digest: p["manifest_digest"].as_str().unwrap().into(),
        manifest: p["manifest"].clone(),
        source_bytes: p["source_bytes"].as_u64(),
        context_members: i["context_members"].as_array().unwrap().clone(),
        current_members: i["current_members"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect(),
        keys: i["keys"].clone(),
        epoch: i["epoch"].as_u64().unwrap(),
    }
}
