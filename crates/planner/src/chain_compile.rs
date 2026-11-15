// SPDX-License-Identifier: AGPL-3.0-only
//! The owner run's `compile` step (SE-L7-CHAIN-COMPILE): v0.1.4 `demo_chain.AppDemoChain._step_compile`
//! (`demo_chain.py:792-850`) over `chain_compile.start_compile` (`chain_compile.py:136-300`), as planner API
//! that owner-run's compile step calls through an adapter. Sans-IO: the heavy route builder, the workflow, the
//! files under `chain-runs/<run>` and the clock are the host's.
//!
//! Flow (the host performs the `host:` lines, the planner the rest):
//! 1. `member_capability_records` (`demo_chain.py:169-211`): verified reports + the owner's own observation.
//! 2. `prepare` = `start_compile` up to the thread start: consent, host count, member/model binding, capability
//!    staleness, the memory-proportional split with EVERY member's standing storage/memory caps and free disk
//!    (`resource_stage_check`: a Mac whose consented storage is below its share is refused BY NAME), per-member
//!    resource plan, the run's proposal, the artifact reuse key and the builder request.
//!    host: `retire_bootstrap()` after an `Ok`, write the run files, run the builder (a thread).
//! 3. `PreparedCompile::finish` = the thread's tail: the `_chain_compile_state` the chain polls
//!    (or `failed_state()` when the builder raised).
//! 4. `admit` = the rest of `_step_compile`: execution-ceiling admission, per-worker wall limits, the
//!    fields written into the run context.
use crate::{
    Error, messages,
    placement::{
        self, ModelSpec, ResourcePolicy, SplitPolicy, members_from_document, registered_model,
    },
    py,
};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

/// `model_resources.MAX_WALL_SECONDS` (model_resources.py:95): the proposal's wall.
const PROPOSED_WALL_SECONDS: u64 = 1_800;
/// `model_resources.RUNNER_LIMIT_DIVISOR` (model_resources.py:44): the runner halves the proposal.
const RUNNER_LIMIT_DIVISOR: u64 = 2;
/// `demo_chain.py:213-220` / `worker.py:117`: no worker runs longer than this.
const WORKER_WALL_MAX_SECONDS: u64 = 3_600;
/// `demo_chain.py:834`: the standing minimum a Mac with unknown time gets, and the lowest consentable time.
const WALL_FALLBACK_SECONDS: u64 = 60;
/// `demo_chain.py:187,196`: a freshly observed owner record is valid this long.
const LOCAL_RECORD_TTL_SECONDS: f64 = 900.0;
/// `serving.py:56-57`: slice fetch applies to sources above/at these byte counts.
const SLICE_FETCH_MIN_SOURCE_BYTES: u64 = 4_000_000_000;
const SLICE_FETCH_MAX_SOURCE_BYTES: u64 = 32_000_000_000;
/// `layer_split.py:44-45` (`MIN_HOSTS`, `MAX_HOSTS`).
pub const MIN_HOSTS: usize = 2;
pub const MAX_HOSTS: usize = 8;
const UNREGISTERED: &str =
    "This model has no registered resource derivation, so the app cannot bound it.";

/// A refusal the owner run shows: closed code plus the optional human message (`OwnerRunError`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub code: String,
    pub message: Option<String>,
}
impl Refusal {
    fn code(code: &str) -> Self {
        Self {
            code: code.into(),
            message: None,
        }
    }
}

/// `prepared.model` (the pinned model contract the owner selected).
#[derive(Debug, Clone)]
pub struct ModelContract {
    pub model_id: String,
    pub resolved_commit: String,
    pub source_representation: String,
    pub runtime_dtype: String,
    pub runtime_quantization: String,
    /// The model contract's serving context in tokens (`api::model_catalog::Model::
    /// serving_context_tokens`): what the memory plan charges KV for and what every stage is
    /// prepared for (carried to the stages by [`BuildRequest::serving_context_tokens`]).
    pub serving_context_tokens: u64,
}

/// Split and resource policies (`layer_split.policy_for_runtime`, `model_resources` constants), pinned data
/// `data/chain-policy.json`. TODO(L1): replace by budget-registry declarations.
#[derive(Debug, Clone)]
pub struct ChainPolicy {
    split: BTreeMap<String, SplitPolicy>,
    resources: ResourcePolicy,
}
impl ChainPolicy {
    pub fn pinned() -> Self {
        let doc: Value = serde_json::from_str(include_str!("../data/chain-policy.json"))
            .expect("chain-policy.json is valid");
        Self {
            split: serde_json::from_value(doc["split"].clone()).expect("pinned split policies"),
            resources: serde_json::from_value(doc["resources"].clone())
                .expect("pinned resource policy"),
        }
    }
}

/// The owner's own `standing_consent` grant caps (already validated by the consent module).
#[derive(Debug, Clone, Default)]
pub struct GrantCaps {
    pub storage_bytes: Option<u64>,
    pub max_memory_bytes: Option<u64>,
    pub max_wall_seconds: Option<u64>,
}
/// The owner's own Mac (`local_contributor`): a fresh observation, never a signed report.
pub struct LocalRecord<'a> {
    pub node_id: &'a str,
    pub memory_bytes: u64,
    /// `local_device['available_storage_bytes']`; kept only when it is a JSON integer (`type(x) is int`).
    pub available_storage_bytes: Option<&'a Value>,
    /// `coordinator.member(node)['generation']`.
    pub generation: &'a Value,
    /// `standing_consent.load_standing_consent(root)` caps, if a grant exists.
    pub grant: Option<GrantCaps>,
    /// `read_resource_policy(root)['storage_bytes']` when it is an integer.
    pub policy_storage_bytes: Option<u64>,
}

fn is_int(v: &Value) -> bool {
    v.is_i64() || v.is_u64()
}

/// `demo_chain.member_capability_records`: owner-verified capability input for the split. `joined` is
/// `app.member_capability_reports()` (node -> signed-report content). Nodes with no usable report are absent
/// (the compile then refuses `member_capability_report_required`). `now` is wall-clock seconds.
pub fn member_capability_records(
    nodes: &[String],
    local: Option<&LocalRecord<'_>>,
    joined: &Value,
    now: f64,
) -> Value {
    let mut out = Map::new();
    for node in nodes {
        if let Some(l) = local.filter(|l| l.node_id == node) {
            let mut r = Map::new();
            r.insert("memory_bytes".into(), json!(l.memory_bytes));
            r.insert("generation".into(), l.generation.clone());
            r.insert("expires_at".into(), json!(now + LOCAL_RECORD_TTL_SECONDS));
            // standing_consent.planning_caps: grant caps intersected with the app storage policy
            let mut caps = Map::new();
            if let Some(g) = &l.grant {
                for (k, v) in [
                    ("storage_bytes", g.storage_bytes),
                    ("max_memory_bytes", g.max_memory_bytes),
                    ("max_wall_seconds", g.max_wall_seconds),
                ] {
                    if let Some(v) = v {
                        caps.insert(k.into(), json!(v));
                    }
                }
            }
            if let Some(p) = l.policy_storage_bytes {
                let held = caps
                    .get("storage_bytes")
                    .and_then(Value::as_u64)
                    .unwrap_or(p);
                caps.insert("storage_bytes".into(), json!(held.min(p)));
            }
            if !caps.is_empty() {
                r.insert("caps".into(), Value::Object(caps));
            }
            if let Some(a) = l.available_storage_bytes.filter(|a| is_int(a)) {
                r.insert("available_storage_bytes".into(), a.clone());
            }
            out.insert(node.clone(), Value::Object(r));
            continue;
        }
        let report = &joined[node.as_str()];
        let Some(device) = report.get("device").filter(|d| d.is_object()) else {
            continue;
        };
        if !report.is_object() {
            continue;
        }
        let mut r = Map::new();
        r.insert("memory_bytes".into(), device["memory_bytes"].clone());
        r.insert("generation".into(), report["generation"].clone());
        r.insert("expires_at".into(), report["expires_at"].clone());
        if let Some(c) = report.get("caps").filter(|c| c.is_object()) {
            r.insert("caps".into(), c.clone());
        }
        if let Some(a) = device.get("available_storage_bytes").filter(|a| is_int(a)) {
            r.insert("available_storage_bytes".into(), a.clone());
        }
        out.insert(node.clone(), Value::Object(r));
    }
    Value::Object(out)
}

/// `serving.slice_fetch_permitted(source_bytes, architecture=…, model=…)` (serving.py:61-78).
pub fn slice_fetch_permitted(
    source_bytes: Option<u64>,
    architecture: &str,
    registered: bool,
) -> bool {
    registered
        && architecture == "qwen3"
        && source_bytes
            .is_some_and(|b| b > SLICE_FETCH_MIN_SOURCE_BYTES && b <= SLICE_FETCH_MAX_SOURCE_BYTES)
}

/// Everything `start_compile` reads before it starts the compile thread.
pub struct PrepareInput<'a> {
    /// `installation_role == 'owner' and running` (the chain always passes `acknowledged=True`).
    pub owner_ready: bool,
    /// `[o['host_id'] for o in context.observations['observations']]`; `None` without an observation.
    pub observation_hosts: Option<&'a [String]>,
    /// `context.facts` (node id -> native hello facts); its keys are the fleet.
    pub facts: &'a BTreeMap<String, Value>,
    /// `app._chain_compile_state is not None`.
    pub already_started: bool,
    /// `app._resource_check() is not None`.
    pub resource_denied: bool,
    pub model: &'a ModelContract,
    /// `prepared.manifest_digest` / `prepared.manifest` / `prepared.source_bytes`.
    pub manifest_digest: &'a str,
    pub manifest: &'a Value,
    pub source_bytes: Option<u64>,
    /// `context.members` and the coordinator's current record per member id (`lease_expires_at` included).
    pub context_members: &'a [Value],
    pub current_members: &'a BTreeMap<String, Value>,
    /// `app._member_capability_reports` (from `member_capability_records`); `None` when the attribute is unset.
    pub records: Option<&'a Value>,
    /// `local_contributor.member.node_id`.
    pub owner_node_id: Option<&'a str>,
    /// `app._chain_slice_fetch`.
    pub slice_mode: bool,
    /// The host stages by slice whatever the source size (a host with only the slice stage
    /// flavour). Only this skips the ported 4 GB..32 GB window below; the window itself is
    /// unchanged for every other run.
    pub slice_staging: bool,
    /// `{'gossip': [owner signer record], 'load_proof': target keys}`.
    pub keys: &'a Value,
    pub deployment_id: &'a str,
    /// `app._chain_epoch`.
    pub epoch: u64,
    /// `time.time()` (wall clock, seconds).
    pub now: f64,
    pub policy: &'a ChainPolicy,
}

/// What the route builder (`scripts/build_qwen_live_route.build`) is called with, in the planner's terms.
#[derive(Debug, Clone)]
pub struct BuildRequest {
    pub model_id: String,
    pub resolved_commit: String,
    pub deployment_id: String,
    /// `route_label='a13'`.
    pub route_label: &'static str,
    /// `stage_sharded` = `emit_stage_shards` set = slice mode.
    pub stage_sharded: bool,
    /// `compile_cache.cache_key(...)`, also the state's `artifact_reuse_key`.
    pub compilation_cache_key: String,
    /// Topology order: the fleet's node ids, sorted.
    pub node_ids: Vec<String>,
    /// `managed_layer_ranges`: `[start, end)` per stage in node order.
    pub managed_layer_ranges: Vec<[usize; 2]>,
    /// The compiled split document (`layer-split.json`).
    pub layer_split: Value,
    /// The serving context this compile planned every stage's memory for, in tokens: the
    /// capacity each stage is prepared for, so a stage holds exactly what the compile charged.
    pub serving_context_tokens: u64,
}

/// What the host's builder and workflow returned (the thread body after `build`).
#[derive(Debug, Clone)]
pub struct BuildOutput {
    /// `workflow.launch.operator_plan_digest` / `.physical_host_ids`.
    pub operator_plan_digest: String,
    pub physical_host_ids: Vec<String>,
    /// `report['compilation_cache_hit'] is True`; `time.monotonic()` around the build.
    pub cache_hit: bool,
    pub build_seconds: f64,
    /// Slice mode only: `slice_plan['plan_digest']` and `node_slice_plan(plan, n)['node_plan_digest']`.
    pub slice_plan_digest: Option<String>,
    pub node_slice_plan_digests: Option<BTreeMap<String, String>>,
}

/// The compile has been accepted; the host runs the builder with `request`, then calls `finish`.
#[derive(Debug, Clone)]
pub struct PreparedCompile {
    pub request: BuildRequest,
    model: ModelContract,
    epoch: u64,
    slice_fetch: bool,
    split: Value,
    public_rows: Vec<Value>,
    max_disk: u64,
    max_rss: u64,
}

fn refuse_error(e: &Error, label: &str) -> Refusal {
    let message = match &e.detail {
        Some(d) => d.to_string(),
        None => messages::split_refusal(e.code, e.node_id.as_deref(), label),
    };
    Refusal {
        code: e.code.into(),
        message: Some(message),
    }
}

/// Python `json.dumps(value, sort_keys=True, allow_nan=False)` (default separators, ensure_ascii).
fn py_json(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => match py::number(n) {
            py::Num::Int(i) => out.push_str(&i.to_string()),
            py::Num::Float(f) => {
                out.push_str(&protocol::canonical::python_float_repr(f).unwrap_or_default())
            }
        },
        Value::String(s) => py_json_str(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_json(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_json_str(k, out);
                out.push_str(": ");
                py_json(&m[k.as_str()], out);
            }
            out.push('}');
        }
    }
}
fn py_json_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e && (c as u32) != 0x7f => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{u:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `compile_cache.cache_key` (compile_cache.py:17-30).
fn reuse_key(input: &PrepareInput<'_>, split: &Value) -> String {
    let m = input.model;
    let mut members: Vec<Value> = input
        .context_members
        .iter()
        .map(|m| {
            let mut o = Map::new();
            for k in [
                "node_id",
                "generation",
                "incarnation",
                "verification_key_digest",
                "endpoint_id",
            ] {
                o.insert(k.into(), m[k].clone());
            }
            Value::Object(o)
        })
        .collect();
    members.sort_by(|a, b| a["node_id"].as_str().cmp(&b["node_id"].as_str()));
    let facts: Map<String, Value> = input
        .facts
        .iter()
        .map(|(n, f)| {
            let mut f = f.as_object().cloned().unwrap_or_default();
            f.remove("staging_root");
            f.remove("source_root");
            (n.clone(), Value::Object(f))
        })
        .collect();
    let value = json!({
        "model": {"model_id": m.model_id, "resolved_commit": m.resolved_commit,
                  "source_representation": m.source_representation, "runtime_dtype": m.runtime_dtype,
                  "runtime_quantization": m.runtime_quantization},
        "manifest_digest": input.manifest_digest, "manifest": input.manifest,
        "members": members, "facts": facts, "split": split, "keys": input.keys,
        "slice_fetch": input.slice_mode,
    });
    let mut text = String::new();
    py_json(&value, &mut text);
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

/// `chain_compile.start_compile` up to the compile thread (see the module docs).
pub fn prepare(input: &PrepareInput<'_>) -> Result<PreparedCompile, Refusal> {
    if !input.owner_ready {
        return Err(Refusal::code("model_preparation_consent_required"));
    }
    let hosts = input
        .observation_hosts
        .ok_or_else(|| Refusal::code("two_distinct_hosts_required"))?;
    if hosts.len() > MAX_HOSTS || input.facts.len() > MAX_HOSTS {
        return Err(Refusal::code("host_count_exceeds_limit"));
    }
    let distinct: std::collections::BTreeSet<&String> = hosts.iter().collect();
    if hosts.len() < MIN_HOSTS || distinct.len() != hosts.len() || hosts.len() != input.facts.len()
    {
        return Err(Refusal::code("two_distinct_hosts_required"));
    }
    let node_ids: Vec<String> = input.facts.keys().cloned().collect();
    if input.already_started {
        return Err(Refusal::code("compiler_already_started"));
    }
    if input.resource_denied {
        return Err(Refusal::code("compiler_resource_policy_denied"));
    }
    let failed = || Refusal::code("chain_compile_failed");
    for f in input.facts.values() {
        let digest = f.get("manifest_digest").ok_or_else(failed)?;
        if !py::py_eq(digest, &json!(input.manifest_digest)) {
            return Err(Refusal::code("compiler_model_changed"));
        }
    }
    let mut current: BTreeMap<&str, &Value> = BTreeMap::new();
    for m in input.context_members {
        let node = m["node_id"].as_str().ok_or_else(failed)?;
        let record = input.current_members.get(node).ok_or_else(failed)?;
        let lease = record["lease_expires_at"].as_f64().ok_or_else(failed)?;
        if input.now >= lease
            || [
                "generation",
                "incarnation",
                "verification_key_digest",
                "endpoint_id",
            ]
            .iter()
            .any(|k| !py::py_eq(&record[*k], &m[*k]))
        {
            return Err(Refusal::code("compiler_member_changed"));
        }
        current.insert(node, record);
    }
    let reports = input
        .records
        .filter(|r| r.is_object())
        .ok_or_else(|| Refusal::code("member_capability_report_required"))?;
    // chain_compile._capability_rows: owner-verified, current, unexpired records only
    let mut rows = Vec::new();
    for node in &node_ids {
        let report = reports
            .get(node.as_str())
            .filter(|r| r.is_object())
            .ok_or_else(|| Refusal::code("member_capability_report_required"))?;
        let member = current.get(node.as_str()).ok_or_else(failed)?;
        let fresh = report["expires_at"]
            .as_f64()
            .filter(|_| report["expires_at"].is_number())
            .is_some_and(|e| e.is_finite() && input.now < e);
        if !py::py_eq(&report["generation"], &member["generation"]) || !fresh {
            return Err(Refusal::code("member_capability_report_stale"));
        }
        let mut row = Map::new();
        row.insert("node_id".into(), json!(node));
        row.insert("memory_bytes".into(), report["memory_bytes"].clone());
        for k in [
            "decode_tokens_per_second",
            "caps",
            "available_storage_bytes",
        ] {
            if let Some(v) = report.get(k) {
                row.insert(k.into(), v.clone());
            }
        }
        rows.push(Value::Object(row));
    }
    let m = input.model;
    let unregistered = || Refusal {
        code: "model_resources_unregistered".into(),
        message: Some(UNREGISTERED.into()),
    };
    let spec: ModelSpec =
        registered_model(&m.model_id, &m.resolved_commit).map_err(|_| unregistered())?;
    if m.runtime_dtype != "float32" {
        return Err(Refusal {
            code: "model_resources_dtype_unsupported".into(),
            message: None,
        });
    }
    let split_policy = input
        .policy
        .split
        .get(&m.runtime_quantization)
        .ok_or_else(|| Refusal::code("model_resources_quantization_unsupported"))?;
    let label = spec.shape.label.clone();
    let members =
        members_from_document(&Value::Array(rows)).map_err(|e| refuse_error(&e, &label))?;
    let planned = placement::plan_members(
        &spec,
        split_policy,
        &input.policy.resources,
        &members,
        &m.runtime_quantization,
        input.slice_mode,
        input.owner_node_id,
    )
    .map_err(|e| refuse_error(&e, &label))?;
    let split = planned["split"].clone();
    let stages = split["stages"].as_array().cloned().unwrap_or_default();
    if stages
        .iter()
        .map(|s| s["node_id"].clone())
        .collect::<Vec<_>>()
        != node_ids.iter().map(|n| json!(n)).collect::<Vec<_>>()
    {
        return Err(Refusal::code("member_capability_report_required"));
    }
    if input.slice_mode && !input.slice_staging {
        let architecture = if spec.shape.qk_norm { "qwen3" } else { "qwen2" };
        if !slice_fetch_permitted(input.source_bytes, architecture, true) {
            return Err(Refusal::code("slice_fetch_model_not_permitted"));
        }
    }
    // model_resources.aggregate_proposal / public_member_resources over the planned rows
    let resource_rows = planned["members"].as_array().cloned().unwrap_or_default();
    let max_of = |k: &str| {
        resource_rows
            .iter()
            .filter_map(|r| r[k].as_u64())
            .max()
            .unwrap_or(0)
    };
    let public_rows = resource_rows
        .iter()
        .map(|r| {
            let mut o = Map::new();
            for k in [
                "node_id",
                "start",
                "end",
                "stage_source_bytes",
                "resident_bytes",
                "kv_bytes",
                "load_peak_bytes",
                "max_rss_bytes",
                "max_disk_bytes",
                "disk_footprint_bytes",
                "basis",
            ] {
                o.insert(k.into(), r[k].clone());
            }
            Value::Object(o)
        })
        .collect();
    let max_disk = max_of("max_disk_bytes")
        .checked_mul(RUNNER_LIMIT_DIVISOR)
        .ok_or_else(failed)?;
    let max_rss = max_of("max_rss_bytes")
        .checked_mul(RUNNER_LIMIT_DIVISOR)
        .ok_or_else(failed)?;
    let key = reuse_key(input, &split);
    let ranges = stages
        .iter()
        .filter_map(|s| Some([s["start"].as_u64()? as usize, s["end"].as_u64()? as usize]))
        .collect();
    Ok(PreparedCompile {
        request: BuildRequest {
            model_id: m.model_id.clone(),
            resolved_commit: m.resolved_commit.clone(),
            deployment_id: input.deployment_id.into(),
            route_label: "a13",
            stage_sharded: input.slice_mode,
            compilation_cache_key: key,
            node_ids,
            managed_layer_ranges: ranges,
            layer_split: split.clone(),
            serving_context_tokens: input.policy.resources.context_tokens,
        },
        model: m.clone(),
        epoch: input.epoch,
        slice_fetch: input.slice_mode,
        split,
        public_rows,
        max_disk,
        max_rss,
    })
}

impl PreparedCompile {
    /// The compile thread's tail: the `_chain_compile_state` the chain reads once the build succeeded.
    pub fn finish(&self, out: &BuildOutput) -> Value {
        let m = &self.model;
        let stages: Vec<Value> = self.split["stages"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|s| {
                let mut o = Map::new();
                for k in [
                    "node_id",
                    "start",
                    "end",
                    "stage_bytes",
                    "cost_bytes",
                    "budget_bytes",
                ] {
                    o.insert(k.into(), s[k].clone());
                }
                Value::Object(o)
            })
            .collect();
        let mut state = json!({
            "state": "compiled", "route_ready": false, "epoch": self.epoch,
            "compilation_cache_hit": out.cache_hit, "build_seconds": out.build_seconds,
            "artifact_reuse_key": self.request.compilation_cache_key,
            "execution_proposal": {
                "model_id": m.model_id, "resolved_commit": m.resolved_commit,
                "source_representation": m.source_representation, "runtime_dtype": m.runtime_dtype,
                "runtime_quantization": m.runtime_quantization,
                "max_execution_download_bytes": 0,
                "max_additional_disk_bytes": self.max_disk, "max_combined_rss_bytes": self.max_rss,
                "max_wall_seconds": PROPOSED_WALL_SECONDS, "run_count": 1,
                "operator_plan_digest": out.operator_plan_digest,
                "physical_host_ids": out.physical_host_ids,
            },
            "resource_plan": {"members": self.public_rows},
            "layer_split": {"split_digest": self.split["split_digest"],
                            "weighting": self.split["weighting"], "stages": stages},
        });
        if self.slice_fetch {
            state["slice_plan_digest"] = json!(out.slice_plan_digest);
            state["node_slice_plan_digests"] = json!(out.node_slice_plan_digests);
        }
        state
    }
}

/// `_chain_compile_state` after the builder raised: `OwnerRunError('chain_compile_failed')` in the step.
pub fn failed_state() -> Value {
    json!({"state": "failed", "error_code": "chain_compile_failed", "route_ready": false})
}

/// The progress line `_step_compile` prints when the compile starts.
pub fn compile_progress(nodes: usize) -> String {
    format!("compiling the route over {nodes} Macs")
}

/// What `_step_compile` stores in the run context after a successful compile and admission.
#[derive(Debug, Clone)]
pub struct Admission {
    /// `ctx.state['proposal']` (the compiled `execution_proposal`).
    pub proposal: Value,
    /// `ctx.state['worker_wall_limits']` / `['wall_cap_fallback_nodes']`.
    pub worker_wall_limits: BTreeMap<String, u64>,
    pub wall_cap_fallback_nodes: Vec<String>,
    /// Second progress line, set when a joined Mac's time limit is unknown.
    pub progress: Option<String>,
    /// `ctx.state['compile_state']`: `{resource_plan, epoch, artifact_reuse_key}`.
    pub compile_state: Value,
    /// `ctx.result['compilation_cache_hit']` / `['build_seconds']`.
    pub compilation_cache_hit: bool,
    pub build_seconds: Value,
    /// `ctx.state['layers']`.
    pub layers: BTreeMap<String, [usize; 2]>,
}

/// `_step_compile` after the compile finished (`demo_chain.py:812-850`). `within_ceiling(proposal, ceiling)` is
/// `execution_admission.proposal_within_ceiling` (owner-run's `ceiling::proposal_within`). On
/// `member_wall_limit_invalid` the proposal was already stored in the context (`ctx.state['proposal']`).
pub fn admit(
    state: &Value,
    records: &Value,
    nodes: &[String],
    local: Option<&str>,
    ceiling: Option<&Value>,
    within_ceiling: &dyn Fn(&Value, &Value) -> bool,
) -> Result<Admission, Refusal> {
    if state["state"] != "compiled" {
        return Err(Refusal::code(
            state["error_code"]
                .as_str()
                .unwrap_or("chain_compile_failed"),
        ));
    }
    let proposal = state["execution_proposal"].clone();
    if let Some(c) = ceiling
        && !within_ceiling(&proposal, c)
    {
        return Err(Refusal::code("execution_proposal_outside_ceiling"));
    }
    // work_limits(app, proposal)['max_wall_seconds']
    let proposed = proposal["max_wall_seconds"]
        .as_u64()
        .ok_or_else(|| Refusal::code("chain_compile_failed"))?
        .min(WORKER_WALL_MAX_SECONDS);
    let mut limits = BTreeMap::new();
    let mut unknown = Vec::new();
    for node in nodes {
        let cap = records
            .get(node.as_str())
            .and_then(|r| r.get("caps"))
            .and_then(|c| c.get("max_wall_seconds"))
            .filter(|c| !c.is_null());
        let cap = match cap {
            None if local == Some(node.as_str()) => proposed,
            None => {
                unknown.push(node.clone());
                WALL_FALLBACK_SECONDS
            }
            Some(c) => match c.as_u64().filter(|_| is_int(c)) {
                Some(c) if (WALL_FALLBACK_SECONDS..=WORKER_WALL_MAX_SECONDS).contains(&c) => c,
                _ => return Err(Refusal::code("member_wall_limit_invalid")),
            },
        };
        limits.insert(node.clone(), proposed.min(cap));
    }
    let progress = (!unknown.is_empty()).then(|| {
        format!(
            "Time limit unknown for {} Macs; using {WALL_FALLBACK_SECONDS} seconds per run.",
            unknown.len()
        )
    });
    let layers = state["layer_split"]["stages"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|s| {
            Some((
                s["node_id"].as_str()?.to_owned(),
                [s["start"].as_u64()? as usize, s["end"].as_u64()? as usize],
            ))
        })
        .collect();
    Ok(Admission {
        proposal,
        worker_wall_limits: limits,
        wall_cap_fallback_nodes: unknown,
        progress,
        compile_state: json!({
            "resource_plan": state["resource_plan"], "epoch": state["epoch"],
            "artifact_reuse_key": state["artifact_reuse_key"],
        }),
        compilation_cache_hit: state["compilation_cache_hit"].as_bool().unwrap_or(false),
        build_seconds: state["build_seconds"].clone(),
        layers,
    })
}
