// SPDX-License-Identifier: AGPL-3.0-only
//! The phone's control channel (plan §2 row 10, §4.2): the stage-only subset
//! of `mycelium.physical_node_control.v1` that a route controller drives on
//! a middle placement, answered with phone-signed
//! `mycelium.physical_node_observation.v1` observations.
//!
//! The wire shapes are the Python node's (`physical_inference_node.py`):
//!
//! - a command is `{protocol, command_id, run_id, deployment_id, command,
//!   payload}` with exactly those fields (`dispatch`);
//! - a response is `{protocol, command_id, node_id, ok, route_ready: false,
//!   result | error: {code}}` (`_response`);
//! - a signed result is `{observation, signature, verification_key}` whose
//!   observation has the 13 fields the controller checks
//!   (`physical_inference_qualification.py`), signed with the
//!   `mycelium.ed25519_evidence_signature.v1` scheme ([`Identity::sign`]).
//!
//! The phone is stage-only: `infer_start`, `infer_decode` and every other
//! entry or A7/A10 command are refused with `stage_only_node`. `configure`
//! takes the phone profile (the phone's own applied v2 stage, never a file
//! path), `start` installs the route peers at the route generation, `stop`
//! drains. Request control (`bind_request_control`, `update_request_control`)
//! keeps the Python node's identity and generation rules. `infer_cancel_wait`
//! answers with a signed `inference_cancelled` observation whose
//! `request_cleanup` receipt commits the exact subject it tore down, built by
//! [`crate::node_cancel`] and reported truthfully by [`ControlEnv::cancel_wait`].
//!
//! Load identity. The phone counts its own stage loads ([`StageView::load_generation`]).
//! `configure` is refused with `load_generation_mismatch` unless the
//! controller names exactly that count, so the `load_generation` in a signed
//! `configured` observation is the phone's own fact for the stage it has
//! applied, bound to the process incarnation the observation also signs
//! (`process_id`). It proves an ordering of loads inside one incarnation and
//! nothing else: not which full-model manifest the stage came from (the
//! `manifest_digest` stays a controller claim and is listed as one), and
//! nothing across a restart, where the count starts again under a new
//! incarnation. `stage_load` returns the current count unsigned, as a hint a
//! controller needs before its first `configure`; it carries no authority.
//! A session lives on exactly the load it configured: when the node installs
//! another stage, a configured or running session is ended by the node and
//! its controller has to open and configure a new one.
//!
//! This module does no I/O: [`ControlEnv`] supplies the stage view, the
//! transport effects, the clock and the health readings.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::identity::Identity;

pub const NODE_CONTROL_PROTOCOL: &str = "mycelium.physical_node_control.v1";
pub const NODE_OBSERVATION_PROTOCOL: &str = "mycelium.physical_node_observation.v1";
/// The control ALPN on the phone's embedded endpoint (plan §4.2).
pub const CONTROL_ALPN: &[u8] = b"mycelium.node_control.v1";
/// `physical_inference_node.py` `MAX_COMMAND_BYTES`.
pub const MAX_COMMAND_BYTES: usize = 4 * 1024 * 1024;
/// Request controls held at once (each is a few hundred bytes).
pub const MAX_REQUEST_CONTROLS: usize = 1024;
pub const REPLAY_DECODE_MODE: &str = "complete_context_replay";

const COMMAND_FIELDS: [&str; 6] = ["protocol", "command_id", "run_id", "deployment_id", "command", "payload"];
const PEER_FIELDS: [&str; 4] = ["node_id", "endpoint_id", "endpoint_addr", "generation"];
/// `_REQUEST_CONTROL_FIELDS`.
const REQUEST_CONTROL_FIELDS: [&str; 12] = [
    "deployment_id",
    "deployment_epoch",
    "qualification_digest",
    "command_id",
    "publisher_generation",
    "absolute_deadline_ms",
    "request_attempt",
    "path_id",
    "path_attempt",
    "path_digest",
    "topology_generation",
    "cancellation_generation",
];
/// Commands only an entry (or an A7/A10 participant) answers.
const ENTRY_ONLY: [&str; 9] = [
    "infer_start",
    "infer_decode",
    "infer_cancel",
    "a7_snapshot",
    "a7_source_lost",
    "a10_target_verify",
    "a10_batch_policy",
    "rotate",
    "cancel",
];

/// The phone's applied v2 stage, as the control channel sees it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StageView {
    pub assignment_id: String,
    pub stage_id: String,
    pub pack_digest: String,
    pub backend: String,
    pub runtime_build_digest: String,
    pub golden_output_digest: String,
    /// The node's own stage-load sequence number for this applied stage: how
    /// many verified stage runtimes this node has installed so far, this one
    /// included. Never the controller's value, the route generation, the
    /// membership generation or the stage revision.
    pub load_generation: u64,
}

/// One route peer to install (`start`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PeerSpec {
    pub node_id: String,
    pub endpoint_id: String,
    pub endpoint_addr: Value,
    pub generation: u64,
}

/// What the control channel needs from the running node.
pub trait ControlEnv {
    /// The node's own iroh `EndpointAddr` document (`{"id": …, "addrs": …}`).
    fn endpoint_addr(&self) -> Value;
    /// The applied, golden-verified v2 stage, if any.
    fn stage(&self) -> Option<StageView>;
    /// Install the route peers at the local generation (ConfigurePeers).
    fn install_peers(&mut self, peers: &[PeerSpec], local_generation: u64) -> Result<(), String>;
    /// Health readings for the signed `health` observation.
    fn health(&self) -> Value;
    /// Runtime/transport counters and interruption contract owned by the node.
    fn qualification_snapshot(&self) -> Value;
    /// A `mycelium.host_resource_snapshot.v1`-shaped document.
    fn host_resources(&self) -> Value;
    /// Monotonic nanoseconds (the observation clock).
    fn monotonic_ns(&self) -> u64;
    /// Drain: stop serving Router frames.
    fn stop(&mut self);
    /// Map a separately bounded execution budget ONCE to the native monotonic clock.
    /// The control absolute deadline is opaque host identity, never Unix time.
    fn request_deadline(&mut self, _request_id: &str, _execution_budget_ms: u64) {}
    /// The exact teardown for one `infer_cancel_wait` subject: release the
    /// runtime replay results of the request's own path, retire that path from
    /// the stage node, and report the residue that is really left. The node
    /// never synthesizes a clean receipt: whatever this returns is signed.
    fn cancel_wait(&mut self, subject: &crate::node_cancel::CancelSubject) -> crate::node_cancel::Residue;
}

/// The node's control identity (`_identity`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlIdentity {
    pub node_id: String,
    pub host_id: String,
    pub process_id: String,
    pub endpoint_id: String,
    pub boot_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    New,
    Configured,
    Running,
    Stopping,
}

impl State {
    fn name(self) -> &'static str {
        match self {
            State::New => "NEW",
            State::Configured => "CONFIGURED",
            State::Running => "RUNNING",
            State::Stopping => "STOPPING",
        }
    }
}

struct Refusal(String);

fn refuse<T>(code: &str) -> Result<T, Refusal> {
    Err(Refusal(code.to_owned()))
}

fn require(ok: bool, code: &str) -> Result<(), Refusal> {
    if ok { Ok(()) } else { refuse(code) }
}

fn exact_fields<'a>(value: &'a Value, fields: &[&str], code: &str) -> Result<&'a Map<String, Value>, Refusal> {
    let map = value.as_object().ok_or_else(|| Refusal(code.to_owned()))?;
    let want: BTreeSet<&str> = fields.iter().copied().collect();
    require(map.len() == want.len() && map.keys().all(|k| want.contains(k.as_str())), code)?;
    Ok(map)
}

fn non_negative_int(value: &Value) -> Option<u64> {
    value.as_u64()
}

fn canonical_digest(value: &Value) -> String {
    let bytes = crate::canonical::to_bytes(value).unwrap_or_default();
    format!("sha256:{}", hex::encode(Sha256::digest(bytes)))
}

/// The phone side of one controller's session.
pub struct ControlNode {
    identity: ControlIdentity,
    /// (run_id, deployment_id), bound by the first `hello`.
    run: Option<(String, String)>,
    /// The authenticated controller (its iroh endpoint id) that opened this
    /// session with `hello`; no other controller drives it.
    controller: Option<String>,
    state: State,
    peer_generation: u64,
    /// The native load generation `configure` was signed for.
    configured_load: Option<u64>,
    request_control_graph: Option<Value>,
    pending_budgets: BTreeMap<String, u64>,
    // Never reuse an evicted receipt's request id within this session. The
    // bounded history fails closed instead of forgetting a cleanup fence.
    consumed_requests: BTreeSet<String>,
    request_controls: BTreeMap<String, Map<String, Value>>,
    /// Cancellations that arrived before their request control was bound
    /// (`_pending_cancellations`), by request id.
    pending_cancellations: BTreeMap<String, crate::node_cancel::CancelSubject>,
    /// Exact accepted cancellation subjects while cleanup is incomplete.
    cancellation_controls: BTreeMap<String, crate::node_cancel::CancelSubject>,
    /// Sealed receipts (`_request_cleanup_receipts`), oldest id first.
    cleanup_receipts: Vec<(String, crate::node_cancel::CancelReceipt)>,
}

impl ControlNode {
    pub fn new(identity: ControlIdentity) -> Self {
        Self {
            identity,
            run: None,
            controller: None,
            state: State::New,
            peer_generation: 0,
            configured_load: None,
            request_control_graph: None,
            pending_budgets: BTreeMap::new(),
            consumed_requests: BTreeSet::new(),
            request_controls: BTreeMap::new(),
            pending_cancellations: BTreeMap::new(),
            cancellation_controls: BTreeMap::new(),
            cleanup_receipts: Vec::new(),
        }
    }

    pub fn state(&self) -> &'static str {
        self.state.name()
    }

    /// The controller (iroh endpoint id) this session is bound to, if any.
    pub fn controller(&self) -> Option<&str> {
        self.controller.as_deref()
    }

    /// The native load generation this session was configured for, if it has
    /// configured. The node ends such a session when it installs another load.
    pub(crate) fn configured_load(&self) -> Option<u64> {
        self.configured_load
    }

    /// Whether this session has ended (`stop`): the node starts a new one.
    pub fn stopped(&self) -> bool {
        self.state == State::Stopping
    }

    /// Handle one command document from the authenticated controller `from`
    /// (the transport's remote endpoint id, never a field of the command);
    /// returns the canonical response bytes.
    pub fn handle(&mut self, from: &str, raw: &[u8], signer: &Identity, env: &mut dyn ControlEnv) -> Vec<u8> {
        let parsed: Option<Value> = (raw.len() <= MAX_COMMAND_BYTES).then(|| serde_json::from_slice(raw).ok()).flatten();
        let command_id = parsed.as_ref().and_then(|d| d["command_id"].as_str()).unwrap_or("").to_owned();
        let response = match parsed {
            None => self.response(&command_id, Err(Refusal("invalid_command_document".into()))),
            Some(document) => {
                let outcome = self.dispatch(from, &document, signer, env);
                self.response(&command_id, outcome)
            }
        };
        crate::canonical::to_bytes(&response).unwrap_or_default()
    }

    fn response(&self, command_id: &str, outcome: Result<Value, Refusal>) -> Value {
        let mut response = json!({
            "protocol": NODE_CONTROL_PROTOCOL,
            "command_id": command_id,
            "node_id": self.identity.node_id,
            "route_ready": false,
        });
        match outcome {
            Ok(result) => {
                response["ok"] = json!(true);
                response["result"] = result;
            }
            Err(Refusal(code)) => {
                response["ok"] = json!(false);
                response["error"] = json!({ "code": code });
            }
        }
        response
    }

    fn dispatch(&mut self, from: &str, document: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        let doc = exact_fields(document, &COMMAND_FIELDS, "invalid_command_fields")?;
        require(doc["protocol"] == NODE_CONTROL_PROTOCOL, "invalid_command_protocol")?;
        require(doc["command_id"].as_str().is_some_and(|s| !s.is_empty()), "invalid_command_id")?;
        let (Some(run_id), Some(deployment_id)) = (doc["run_id"].as_str(), doc["deployment_id"].as_str()) else {
            return refuse("run_id_mismatch");
        };
        let command = doc["command"].as_str().ok_or_else(|| Refusal("invalid_command".into()))?;
        let payload = &doc["payload"];
        require(payload.is_object(), "invalid_command_payload")?;
        // The first hello binds the session's run; every later command must
        // carry the same run and deployment (PIN binds them from its argv).
        // The session belongs to the controller that opened it.
        require(!from.is_empty() && self.controller.as_deref().is_none_or(|c| c == from), "controller_mismatch")?;
        match &self.run {
            None if command == "hello" && !run_id.is_empty() && !deployment_id.is_empty() => {
                self.run = Some((run_id.to_owned(), deployment_id.to_owned()));
                self.controller = Some(from.to_owned());
            }
            None => return refuse("run_id_mismatch"),
            Some((run, deployment)) => {
                require(run == run_id, "run_id_mismatch")?;
                require(deployment == deployment_id, "deployment_id_mismatch")?;
            }
        }
        if ENTRY_ONLY.contains(&command) {
            return refuse("stage_only_node");
        }
        match command {
            "hello" => self.hello(payload),
            "stage_load" => self.stage_load(payload, env),
            "configure" => self.configure(payload, signer, env),
            "start" => self.start(payload, signer, env),
            "health" => self.health(payload, signer, env),
            "snapshot" => self.snapshot(payload, signer, env),
            "bind_request_control" => self.bind_request_control(payload, signer, env),
            "update_request_control" => self.update_request_control(payload, signer, env),
            "infer_cancel_wait" => self.infer_cancel_wait(payload, signer, env),
            "stop" => self.stop(payload, signer, env),
            _ => refuse("unsupported_command"),
        }
    }

    fn identity_fields(&self) -> Map<String, Value> {
        let (run, deployment) = self.run.clone().unwrap_or_default();
        let mut m = Map::new();
        m.insert("run_id".into(), json!(run));
        m.insert("deployment_id".into(), json!(deployment));
        m.insert("node_id".into(), json!(self.identity.node_id));
        m.insert("host_id".into(), json!(self.identity.host_id));
        m.insert("process_id".into(), json!(self.identity.process_id));
        m.insert("endpoint_id".into(), json!(self.identity.endpoint_id));
        m.insert("peer_generation".into(), json!(self.peer_generation));
        m.insert("state".into(), json!(self.state.name()));
        m.insert("route_ready".into(), json!(false));
        m
    }

    /// `_signed_result`: the 13-field observation, its signature and key.
    fn signed(&self, event: &str, details: Value, signer: &Identity, env: &dyn ControlEnv) -> Result<Value, Refusal> {
        let mut statement = self.identity_fields();
        statement.insert("protocol".into(), json!(NODE_OBSERVATION_PROTOCOL));
        statement.insert("event".into(), json!(event));
        statement.insert("monotonic_ns".into(), json!(env.monotonic_ns()));
        statement.insert("details".into(), details);
        let statement = Value::Object(statement);
        let signature = signer.sign(&statement).map_err(|_| Refusal("signer_not_ready".into()))?;
        Ok(json!({ "observation": statement, "signature": signature, "verification_key": signer.public_key_record() }))
    }

    fn hello(&self, payload: &Value) -> Result<Value, Refusal> {
        exact_fields(payload, &[], "invalid_hello_fields")?;
        let mut result = self.identity_fields();
        result.insert("protocol".into(), json!(NODE_CONTROL_PROTOCOL));
        result.insert("boot_id".into(), json!(self.identity.boot_id));
        Ok(Value::Object(result))
    }

    /// The phone's current stage-load count, or null when no stage is applied
    /// (or it is checked out for compute). Unsigned on purpose: a hint for the
    /// controller's first `configure`, never evidence. Authority is only the
    /// signed `configured` observation.
    fn stage_load(&self, payload: &Value, env: &dyn ControlEnv) -> Result<Value, Refusal> {
        exact_fields(payload, &[], "invalid_stage_load_fields")?;
        Ok(json!({
            "protocol": NODE_CONTROL_PROTOCOL,
            "stage_load_generation": env.stage().map(|stage| stage.load_generation),
        }))
    }

    /// The applied load as the phone holds it now, for a signed snapshot.
    fn load_identity(stage: &StageView) -> Value {
        json!({
            "assignment_id": stage.assignment_id,
            "stage_id": stage.stage_id,
            "phone_stage_pack_digest": stage.pack_digest,
            "load_generation": stage.load_generation,
        })
    }

    /// Phone profile: `{assignment_id, stage_id, pack_digest,
    /// manifest_digest, load_generation}`; the stage is the phone's own
    /// applied, golden-verified v2 stage, never a file the controller wrote.
    /// `load_generation` must equal the phone's own count for that stage.
    fn configure(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        require(self.state == State::New, "invalid_state_for_configure")?;
        let mut base = payload.clone();
        let graph = base.as_object_mut().and_then(|m| m.remove("request_control_graph"));
        if let Some(graph) = &graph {
            let g = exact_fields(graph, &["deployment_id", "deployment_epoch", "topology_generation"], "invalid_request_control_graph")?;
            require(g["deployment_id"].as_str() == self.run.as_ref().map(|(_, d)| d.as_str())
                && g["deployment_epoch"].as_u64().is_some_and(|n| n > 0)
                && g["topology_generation"].as_u64().is_some_and(|n| n > 0), "invalid_request_control_graph")?;
        }
        let p = exact_fields(&base, &["assignment_id", "stage_id", "pack_digest", "manifest_digest", "load_generation"], "invalid_configure_fields")?;
        let claimed_load = non_negative_int(&p["load_generation"]).filter(|g| *g > 0).ok_or_else(|| Refusal("invalid_load_generation".into()))?;
        let text = |k: &str| p[k].as_str().filter(|s| !s.is_empty()).ok_or_else(|| Refusal("invalid_configure_fields".into()));
        let (assignment_id, stage_id, pack_digest, manifest_digest) =
            (text("assignment_id")?, text("stage_id")?, text("pack_digest")?, text("manifest_digest")?);
        let stage = env.stage().ok_or_else(|| Refusal("stage_not_loaded".into()))?;
        require(stage.backend == crate::qwen2::QWEN2_BACKEND, "stage_backend_mismatch")?;
        require(stage.assignment_id == assignment_id, "assignment_id_mismatch")?;
        require(stage.stage_id == stage_id, "stage_id_mismatch")?;
        require(stage.pack_digest == pack_digest, "stage_pack_digest_mismatch")?;
        // The controller does not choose this number: it must name the load
        // the phone itself counted, or nothing is signed.
        require(stage.load_generation == claimed_load, "load_generation_mismatch")?;
        self.configured_load = Some(stage.load_generation);
        self.request_control_graph = graph;
        self.state = State::Configured;
        let details = json!({
            "assignment_id": assignment_id,
            "stage_id": stage_id,
            "manifest_digest": manifest_digest,
            "endpoint_addr": env.endpoint_addr(),
            "runtime_mode": REPLAY_DECODE_MODE,
            "runtime_backend": stage.backend,
            "runtime_build_digest": stage.runtime_build_digest,
            "golden_output_digest": stage.golden_output_digest,
            "phone_stage_pack_digest": pack_digest,
            "load_generation": stage.load_generation,
            "load_identity_provenance": {
                "applied_fields":["assignment_id","stage_id","phone_stage_pack_digest","runtime_backend","runtime_build_digest","golden_output_digest","load_generation"],
                "controller_claim_fields":["manifest_digest"],
                "limitation":"native_full_model_manifest_digest_not_independently_observed",
                "load_generation_basis":"native_stage_load_sequence_within_process_incarnation"
            },
        });
        self.signed("configured", details, signer, env)
    }

    fn peer(&self, value: &Value) -> Result<PeerSpec, Refusal> {
        let p = exact_fields(value, &PEER_FIELDS, "invalid_peer_fields")?;
        let (Some(node_id), Some(endpoint_id), Some(generation)) =
            (p["node_id"].as_str(), p["endpoint_id"].as_str(), non_negative_int(&p["generation"]))
        else {
            return refuse("invalid_peer_binding");
        };
        require(!node_id.is_empty() && !endpoint_id.is_empty() && generation > 0, "invalid_peer_binding")?;
        require(p["endpoint_addr"]["id"] == endpoint_id, "invalid_peer_binding")?;
        require(node_id != self.identity.node_id, "self_peer_binding")?;
        Ok(PeerSpec {
            node_id: node_id.to_owned(),
            endpoint_id: endpoint_id.to_owned(),
            endpoint_addr: p["endpoint_addr"].clone(),
            generation,
        })
    }

    fn start(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        require(self.state == State::Configured, "invalid_state_for_start")?;
        let map = payload.as_object().ok_or_else(|| Refusal("invalid_start_fields".into()))?;
        let keys: BTreeSet<&str> = map.keys().map(String::as_str).collect();
        require(
            keys == BTreeSet::from(["peer", "local_generation"]) || keys == BTreeSet::from(["peer", "peers", "local_generation"]),
            "invalid_start_fields",
        )?;
        let local_generation =
            non_negative_int(&map["local_generation"]).filter(|g| *g > 0).ok_or_else(|| Refusal("invalid_local_generation".into()))?;
        let mut peers = vec![self.peer(&map["peer"])?];
        if let Some(more) = map.get("peers") {
            let more = more.as_array().ok_or_else(|| Refusal("invalid_peers".into()))?;
            require(more.len() <= 64, "invalid_peers")?;
            for p in more {
                peers.push(self.peer(p)?);
            }
        }
        let nodes: BTreeSet<&str> = peers.iter().map(|p| p.node_id.as_str()).collect();
        require(nodes.len() == peers.len(), "duplicate_peer_node_id")?;
        let endpoints: BTreeSet<&str> = peers.iter().map(|p| p.endpoint_id.as_str()).collect();
        require(endpoints.len() == peers.len(), "duplicate_peer_endpoint_id")?;
        // A stage reloaded or replaced since `configure` is another load: the
        // session was signed for the old one and may not start on the new.
        let stage = env.stage().ok_or_else(|| Refusal("stage_not_loaded".into()))?;
        require(self.configured_load == Some(stage.load_generation), "load_generation_mismatch")?;
        env.install_peers(&peers, local_generation).map_err(Refusal)?;
        self.peer_generation = local_generation;
        self.state = State::Running;
        let details = json!({
            "peer_node_id": peers[0].node_id,
            "peer_node_ids": nodes.into_iter().collect::<Vec<_>>(),
            "local_generation": local_generation,
        });
        self.signed("started", details, signer, env)
    }

    fn health(&self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        exact_fields(payload, &[], "invalid_health_fields")?;
        require(matches!(self.state, State::Configured | State::Running), "invalid_state_for_health")?;
        let mut details = json!({ "state": self.state.name() });
        if let (Value::Object(d), Value::Object(h)) = (&mut details, env.health()) {
            for (k, v) in h {
                d.entry(k).or_insert(v);
            }
        }
        self.signed("health", details, signer, env)
    }

    fn snapshot(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        if payload.get("cleanup_subject").is_some() {
            let fields = if payload.get("receipt_only").is_some() { vec!["cleanup_subject", "receipt_only"] } else { vec!["cleanup_subject"] };
            exact_fields(payload, &fields, "invalid_snapshot_fields")?;
            require(payload.get("receipt_only").is_none_or(Value::is_boolean), "invalid_snapshot_fields")?;
            require(matches!(self.state, State::Configured | State::Running), "invalid_state_for_snapshot")?;
            let mut flat = payload["cleanup_subject"].clone();
            let mut fields = crate::node_cancel::SUBJECT_FIELDS.to_vec();
            fields.push("request_id");
            exact_fields(&flat, &fields, "invalid_cleanup_subject")?;
            flat["deadline_budget_ms"] = json!(1);
            let (subject, _) = crate::node_cancel::subject_of_bound(&flat, "invalid_cleanup_subject").map_err(Refusal)?;
            if let Some((_, receipt)) = self.cleanup_receipts.iter().find(|(id, _)| *id == subject.request_id) {
                require(receipt.subject == subject, "cleanup_receipt_identity_mismatch")?;
                return self.signed("snapshot", json!({"request_cleanup":receipt.cleanup_json()}), signer, env);
            }
            // Only an already-consumed, exact command may trigger cleanup and
            // seal its delayed receipt. A snapshot is never a cancellation.
            if self.cancellation_controls.get(&subject.request_id) == Some(&subject)
                || self.pending_cancellations.get(&subject.request_id) == Some(&subject) {
                flat["deadline_budget_ms"] = json!(self.pending_budgets.get(&subject.request_id).copied().unwrap_or(1));
                let result = self.infer_cancel_wait(&flat, signer, env)?;
                return self.signed("snapshot", json!({"request_cleanup":result["observation"]["details"]["request_cleanup"]}), signer, env);
            }
            let mut cleanup = subject.to_json();
            cleanup["runtime_clean"] = json!(false);
            cleanup["transport_clean"] = json!(false);
            cleanup["cancellation_worker_complete"] = json!(false);
            cleanup["complete"] = json!(false);
            return self.signed("snapshot", json!({"request_cleanup":cleanup}), signer, env);
        }
        exact_fields(payload, &[], "invalid_snapshot_fields")?;
        require(matches!(self.state, State::Configured | State::Running), "invalid_state_for_snapshot")?;
        let mut resources = env.host_resources();
        // The phone replays complete context only (plan §5).
        resources["supported_decode_modes"] = json!([REPLAY_DECODE_MODE]);
        resources["route_ready"] = json!(false);
        if let Value::Object(m) = &mut resources {
            m.remove("resource_digest");
        }
        let digest = canonical_digest(&resources);
        resources["resource_digest"] = json!(digest);
        let mut details = env.qualification_snapshot();
        require(details.is_object(), "runtime_snapshot_unavailable")?;
        details["state"] = json!(self.state.name());
        details["host_resources"] = resources;
        // What is applied NOW next to what `configure` was signed for, so a
        // consumer can see a reload or replacement. Null while no stage is
        // applied (or it is checked out); never the configured value copied.
        details["load_identity"] = env.stage().as_ref().map(Self::load_identity).unwrap_or(Value::Null);
        details["configured_load_generation"] = json!(self.configured_load);
        self.signed("snapshot", details, signer, env)
    }

    fn request_control(&self, payload: &Value, code: &str) -> Result<(String, Map<String, Value>), Refusal> {
        // Both callers validate their closed outer schema first (bind also
        // carries the separately validated execution budget).
        let p = payload.as_object().ok_or_else(|| Refusal(code.into()))?;
        let request_id = p["request_id"].as_str().filter(|s| !s.is_empty()).ok_or_else(|| Refusal("invalid_request_id".into()))?;
        let control = exact_fields(&p["control"], &REQUEST_CONTROL_FIELDS, code)?;
        let mut flat = Value::Object(control.clone());
        flat["request_id"] = json!(request_id);
        flat["deadline_budget_ms"] = json!(1);
        crate::node_cancel::subject_of_bound(&flat, code).map_err(Refusal)?;
        self.validate_graph(&flat, code)?;
        Ok((request_id.to_owned(), control.clone()))
    }

    fn validate_graph(&self, subject: &Value, code: &str) -> Result<(), Refusal> {
        let graph = self.request_control_graph.as_ref().ok_or_else(|| Refusal(code.into()))?;
        require(["deployment_id", "deployment_epoch", "topology_generation"].iter().all(|k| subject[*k] == graph[*k]), code)
    }

    /// Persistent within the session, including sealed receipts: never resurrect
    /// execution after cleanup. The execution lane also keeps its own fence.
    pub(crate) fn request_blocked(&self, request_id: &str) -> bool {
        self.pending_cancellations.contains_key(request_id) || self.cancellation_controls.contains_key(request_id)
            || self.cleanup_receipts.iter().any(|(id, _)| id == request_id)
            || self.consumed_requests.contains(request_id)
    }

    fn bind_request_control(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        exact_fields(payload, &["request_id", "control", "execution_budget_ms"], "invalid_bind_request_control_fields")?;
        let budget = payload["execution_budget_ms"].as_u64().filter(|b| (1..=3_600_000).contains(b))
            .ok_or_else(|| Refusal("invalid_execution_budget_ms".into()))?;
        require(self.state == State::Running, "invalid_state_for_bind_request_control")?;
        let (request_id, mut control) = self.request_control(payload, "invalid_bind_request_control")?;
        require(control["cancellation_generation"] == 0, "invalid_bind_request_control")?;
        // A delayed original bind may acknowledge the sealed successor but
        // must never recreate a request control after cleanup (Python parity).
        if let Some((_, receipt)) = self.cleanup_receipts.iter().find(|(id, _)| *id == request_id) {
            let sealed = receipt.subject.to_json();
            require(REQUEST_CONTROL_FIELDS.iter().filter(|k| **k != "cancellation_generation").all(|k| sealed[*k] == control[*k])
                && control["cancellation_generation"].as_u64().and_then(|g| g.checked_add(1)) == Some(receipt.subject.cancellation_generation),
                "request_already_cleaned")?;
            control["cancellation_generation"] = json!(receipt.subject.cancellation_generation);
            return self.signed("request_control_bound", json!({"request_id":request_id, "control_digest":canonical_digest(&Value::Object(control))}), signer, env);
        }
        require(!self.consumed_requests.contains(&request_id), "request_already_cleaned")?;
        require(self.consumed_requests.len() + self.request_controls.len() + self.pending_cancellations.len() < MAX_REQUEST_CONTROLS
            || self.request_controls.contains_key(&request_id) || self.pending_cancellations.contains_key(&request_id), "request_control_table_full")?;
        // A pending cancellation is the authoritative identity for the request
        // (`_bind_request_control`, PIN:2413-2465): a later bind must carry its
        // exact immutable fields, is pinned one generation behind it, and
        // clears the pending record only through that fence. A bind may never
        // silently overwrite or coexist with a recorded cancellation.
        if let Some(pending) = self.pending_cancellations.get(&request_id) {
            let pending_matches = crate::node_cancel::subject_of_bound(&{
                let mut flat = serde_json::Map::new();
                flat.insert("request_id".into(), json!(request_id));
                flat.insert("deadline_budget_ms".into(), json!(1));
                for k in REQUEST_CONTROL_FIELDS {
                    flat.insert(k.into(), control[k].clone());
                }
                Value::Object(flat)
            }, "invalid_bind_request_control").ok().is_some_and(|(s, _)| {
                s.identity_json() == pending.identity_json()
                    && s.cancellation_generation.checked_add(1) == Some(pending.cancellation_generation)
            });
            require(pending_matches, "stale_infer_cancel_generation")?;
            require(self.request_controls.contains_key(&request_id) || self.request_controls.len() < MAX_REQUEST_CONTROLS, "request_control_table_full")?;
            control["cancellation_generation"] = json!(pending.cancellation_generation);
            self.cancellation_controls.insert(request_id.clone(), pending.clone());
            self.pending_cancellations.remove(&request_id);
            self.pending_budgets.remove(&request_id);
        }
        if let Some(current) = self.request_controls.get(&request_id) {
            let advanced_duplicate = REQUEST_CONTROL_FIELDS.iter().filter(|k| **k != "cancellation_generation").all(|k| current[*k] == control[*k])
                && control["cancellation_generation"].as_u64().and_then(|g| g.checked_add(1)) == current["cancellation_generation"].as_u64()
                && self.cancellation_controls.get(&request_id).is_some_and(|s| {
                    REQUEST_CONTROL_FIELDS.iter().all(|k| s.to_json()[*k] == current[*k])
                });
            require(*current == control || advanced_duplicate, "conflicting_request_control")?;
            control = current.clone();
        } else {
            require(self.request_controls.len() < MAX_REQUEST_CONTROLS, "request_control_table_full")?;
            self.request_controls.insert(request_id.clone(), control.clone());
            env.request_deadline(&request_id, budget);
        }
        let digest = canonical_digest(&Value::Object(control));
        self.signed("request_control_bound", json!({ "request_id": request_id, "control_digest": digest }), signer, env)
    }

    fn update_request_control(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        exact_fields(payload, &["request_id", "control"], "invalid_update_request_control_fields")?;
        require(self.state == State::Running, "invalid_state_for_update_request_control")?;
        let (request_id, control) = self.request_control(payload, "invalid_update_request_control")?;
        let current = self.request_controls.get(&request_id).ok_or_else(|| Refusal("request_control_unknown".into()))?;
        require(
            REQUEST_CONTROL_FIELDS.iter().filter(|k| **k != "publisher_generation").all(|k| current[*k] == control[*k]),
            "request_control_identity_mismatch",
        )?;
        let (old, new) = (current["publisher_generation"].as_u64().unwrap_or(0), control["publisher_generation"].as_u64().unwrap_or(0));
        let duplicate = old == new;
        if !duplicate {
            require(old.checked_add(1) == Some(new), "publisher_generation_cas_mismatch")?;
            self.request_controls.insert(request_id.clone(), control);
        }
        self.signed(
            "request_control_updated",
            json!({ "request_id": request_id, "publisher_generation": new, "duplicate": duplicate }),
            signer,
            env,
        )
    }

    /// `infer_cancel_wait`: fence the owner's exact request-control identity,
    /// tear the request's own path down inline on this reserved control
    /// worker, and return a signed `inference_cancelled` observation whose
    /// `request_cleanup` names the residue the teardown actually found
    /// (`_infer_cancel_wait`, PIN:4299-4358; receipt shape PIN:2920).
    ///
    /// The control worker performs only currently available cleanup and never
    /// waits behind CPU/network work. A busy runtime or outstanding publication
    /// yields an incomplete receipt immediately; a scoped snapshot can seal it
    /// later. A repeat of a sealed subject never moves proof backwards.
    fn infer_cancel_wait(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        let (subject, deadline_budget_ms) = crate::node_cancel::subject_of(payload, "invalid_infer_cancel_control").map_err(Refusal)?;
        self.validate_graph(payload, "invalid_infer_cancel_control")?;
        // A receipt already committed for this exact identity is replayed.
        if let Some((_, receipt)) = self.cleanup_receipts.iter().find(|(id, r)| *id == subject.request_id && r.subject == subject) {
            return self.signed("inference_cancelled", receipt.to_json(), signer, env);
        }
        require(!self.consumed_requests.contains(&subject.request_id), "request_already_cleaned")?;
        if let Some(budget) = self.pending_budgets.get(&subject.request_id) {
            require(*budget == deadline_budget_ms, "conflicting_pending_cancellation")?;
        }
        let bound = self.bound_subject(&subject.request_id);
        require(!self.request_controls.contains_key(&subject.request_id) || bound.is_some(), "invalid_infer_cancel_control")?;
        let retry = self.cancellation_controls.get(&subject.request_id) == Some(&subject);
        let decision = if retry { crate::node_cancel::Fence::Duplicate } else {
            crate::node_cancel::fence(bound.as_ref(), self.pending_cancellations.get(&subject.request_id), &subject)
        };
        match decision {
            crate::node_cancel::Fence::Refused(code) => return refuse(code),
            crate::node_cancel::Fence::Applied => {
                let control = self.request_controls.get_mut(&subject.request_id).expect("bound above");
                control["cancellation_generation"] = json!(subject.cancellation_generation);
                self.cancellation_controls.insert(subject.request_id.clone(), subject.clone());
            }
            // Cancel-before-start: the cancellation itself is the authoritative
            // pending identity; a later bind must match it exactly.
            crate::node_cancel::Fence::Pending => {
                require(self.consumed_requests.len() + self.request_controls.len() + self.pending_cancellations.len() < MAX_REQUEST_CONTROLS, "request_control_table_full")?;
                self.pending_cancellations.insert(subject.request_id.clone(), subject.clone());
                self.pending_budgets.insert(subject.request_id.clone(), deadline_budget_ms);
            }
            crate::node_cancel::Fence::Duplicate => {}
        }
        let pending_start = bound.is_none();
        // The exact teardown. Its residue is the receipt: nothing is invented.
        let residue = env.cancel_wait(&subject);
        // An unbound cancellation still owns its pending identity, even if runtime residue is empty.
        let cleanup_pending = pending_start || !residue.is_clean();
        let receipt = crate::node_cancel::CancelReceipt {
            request_id: subject.request_id.clone(),
            cancelled: true,
            status: if cleanup_pending { "CANCELLING" } else { "CANCELLED" },
            cleanup_pending,
            subject: subject.clone(),
            cancellation_worker_complete: !residue.runtime_busy,
            residue,
            pending_start,
            deadline_budget_ms,
        };
        if !cleanup_pending {
            self.consumed_requests.insert(subject.request_id.clone());
            self.pending_budgets.remove(&subject.request_id);
            self.request_controls.remove(&subject.request_id);
            self.pending_cancellations.remove(&subject.request_id);
            self.cancellation_controls.remove(&subject.request_id);
            while self.cleanup_receipts.len() >= crate::node_cancel::MAX_CLEANUP_RECEIPTS {
                self.cleanup_receipts.remove(0);
            }
            self.cleanup_receipts.push((subject.request_id.clone(), receipt.clone()));
        }
        self.signed("inference_cancelled", receipt.to_json(), signer, env)
    }

    /// The bound request control as a cancellation subject, if this session
    /// bound one for `request_id` (its identity fields, verbatim).
    fn bound_subject(&self, request_id: &str) -> Option<crate::node_cancel::CancelSubject> {
        let control = self.request_controls.get(request_id)?;
        let mut payload = serde_json::Map::new();
        payload.insert("request_id".into(), json!(request_id));
        payload.insert("deadline_budget_ms".into(), json!(1));
        for k in REQUEST_CONTROL_FIELDS {
            payload.insert(k.into(), control[k].clone());
        }
        crate::node_cancel::subject_of_bound(&Value::Object(payload), "invalid_infer_cancel_control").ok().map(|(s, _)| s)
    }

    fn stop(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        exact_fields(payload, &[], "invalid_stop_fields")?;
        self.state = State::Stopping;
        let signed = self.signed("stopping", json!({}), signer, env)?;
        env.stop();
        self.request_controls.clear();
        self.pending_cancellations.clear();
        self.pending_budgets.clear();
        self.cancellation_controls.clear();
        self.cleanup_receipts.clear();
        Ok(json!({ "state": self.state.name(), "final_observation": signed }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Env {
        stage: Option<StageView>,
        installed: Vec<(Vec<PeerSpec>, u64)>,
        stopped: bool,
        clock: u64,
        cancelled: Vec<crate::node_cancel::CancelSubject>,
        deadlines: Vec<(String,u64)>,
        residue: crate::node_cancel::Residue,
    }

    impl ControlEnv for Env {
        fn endpoint_addr(&self) -> Value {
            json!({"id": "endpoint-phone", "addrs": []})
        }
        fn stage(&self) -> Option<StageView> {
            self.stage.clone()
        }
        fn install_peers(&mut self, peers: &[PeerSpec], local_generation: u64) -> Result<(), String> {
            self.installed.push((peers.to_vec(), local_generation));
            Ok(())
        }
        fn health(&self) -> Value {
            json!({"transport_running": true})
        }
        fn qualification_snapshot(&self) -> Value {
            json!({"runtime":{"active_state_count":3,"applied_operation_count":7},
                "transport":{"remote_frames_sent":11,"remote_frames_received":13},
                "transport_fatal_error":"synthetic_failure", "interruptibility":{"maximum_total_cleanup_ms":null}})
        }
        fn host_resources(&self) -> Value {
            json!({"protocol": "mycelium.host_resource_snapshot.v1", "backend": crate::qwen2::QWEN2_BACKEND})
        }
        fn monotonic_ns(&self) -> u64 {
            self.clock
        }
        fn request_deadline(&mut self,r:&str,b:u64) { self.deadlines.push((r.into(),b)); }
        fn stop(&mut self) {
            self.stopped = true;
        }
        /// A staged residue the test sets, so the receipt is only complete when
        /// the teardown really found nothing.
        fn cancel_wait(&mut self, subject: &crate::node_cancel::CancelSubject) -> crate::node_cancel::Residue {
            self.cancelled.push(subject.clone());
            std::mem::take(&mut self.residue)
        }
    }

    fn env() -> Env {
        Env {
            stage: Some(StageView {
                assignment_id: "assignment-1".into(),
                stage_id: "stage-phone".into(),
                pack_digest: "sha256:pack".into(),
                backend: crate::qwen2::QWEN2_BACKEND.into(),
                runtime_build_digest: format!("sha256:{}", "b".repeat(64)),
                golden_output_digest: format!("sha256:{}", "a".repeat(64)),
                load_generation: 1,
            }),
            installed: vec![],
            stopped: false,
            clock: 7,
            cancelled: Vec::new(),
            deadlines: Vec::new(),
            residue: crate::node_cancel::Residue::default(),
        }
    }

    fn node() -> ControlNode {
        ControlNode::new(ControlIdentity {
            node_id: "node-phone".into(),
            host_id: "host-phone".into(),
            process_id: "process-1".into(),
            endpoint_id: "endpoint-phone".into(),
            boot_id: "boot-1".into(),
        })
    }

    fn signer() -> Identity {
        Identity::from_secret("endpoint-phone", &[9; 32]).unwrap()
    }

    fn send(node: &mut ControlNode, env: &mut Env, command: &str, payload: Value) -> Value {
        let doc = json!({"protocol": NODE_CONTROL_PROTOCOL, "command_id": format!("c-{command}"), "run_id": "run-1", "deployment_id": "dep-1", "command": command, "payload": payload});
        serde_json::from_slice(&node.handle("controller-1", &serde_json::to_vec(&doc).unwrap(), &signer(), env)).unwrap()
    }

    #[test]
    fn execution_budget_is_required_bounded_and_not_part_of_signed_identity() {
        let (mut n, mut e) = running();
        for budget in [Value::Null, json!(0), json!(-1), json!(true), json!(1.5), json!(3_600_001)] {
            let mut payload = json!({"request_id":"budget", "control":control(1)});
            if !budget.is_null() { payload["execution_budget_ms"] = budget; }
            assert_eq!(send(&mut n, &mut e, "bind_request_control", payload)["ok"], false);
            assert!(!n.request_controls.contains_key("budget"));
        }
        let subject = control(1);
        let r = send(&mut n, &mut e, "bind_request_control", json!({"request_id":"budget", "control":subject, "execution_budget_ms":3_600_000}));
        let o = assert_signed(&r, "request_control_bound");
        assert_eq!(o["details"]["control_digest"], canonical_digest(&subject));
    }

    fn assert_signed(response: &Value, event: &str) -> Value {
        assert_eq!(response["ok"], true, "{response}");
        let result = &response["result"];
        let observation = &result["observation"];
        let fields: BTreeSet<&str> = observation.as_object().unwrap().keys().map(String::as_str).collect();
        assert_eq!(
            fields,
            BTreeSet::from([
                "protocol",
                "event",
                "monotonic_ns",
                "run_id",
                "deployment_id",
                "node_id",
                "host_id",
                "process_id",
                "endpoint_id",
                "peer_generation",
                "state",
                "route_ready",
                "details"
            ])
        );
        assert_eq!(observation["event"], event);
        assert_eq!(observation["route_ready"], false);
        assert_eq!(result["signature"]["signer_endpoint_id"], "endpoint-phone");
        crate::identity::verify(observation, &result["signature"], &result["verification_key"]).unwrap();
        observation.clone()
    }

    fn peer(node_id: &str, endpoint: &str) -> Value {
        json!({"node_id": node_id, "endpoint_id": endpoint, "endpoint_addr": {"id": endpoint, "addrs": []}, "generation": 3})
    }

    fn configure_payload() -> Value {
        json!({"assignment_id": "assignment-1", "stage_id": "stage-phone", "pack_digest": "sha256:pack", "manifest_digest": "sha256:graph", "load_generation": 1, "request_control_graph":{"deployment_id":"dep-1", "deployment_epoch":1, "topology_generation":1}})
    }

    #[test]
    fn readiness_projection_signs_backend_and_has_runtime_counters() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        let configured = assert_signed(&send(&mut node, &mut env, "configure", configure_payload()), "configured");
        assert_eq!(configured["details"]["runtime_backend"], crate::qwen2::QWEN2_BACKEND);
        assert!(configured["details"]["runtime_build_digest"].as_str().is_some());
        assert!(configured["details"]["golden_output_digest"].as_str().is_some());
        let snapshot = assert_signed(&send(&mut node, &mut env, "snapshot", json!({})), "snapshot");
        assert!(snapshot["details"]["runtime"].is_object());
        assert!(snapshot["details"].get("transport_fatal_error").is_some());
        assert!(snapshot["details"]["interruptibility"].is_object());
        assert_eq!(snapshot["details"]["runtime"]["applied_operation_count"],7);
        assert_eq!(snapshot["details"]["runtime"]["active_state_count"],3);
        assert_eq!(snapshot["details"]["transport"]["remote_frames_sent"],11);
        assert_eq!(snapshot["details"]["transport_fatal_error"],"synthetic_failure");
    }

    #[test]
    fn a_session_runs_hello_configure_start_health_snapshot_and_stop_with_signed_observations() {
        let (mut node, mut env) = (node(), env());
        let hello = send(&mut node, &mut env, "hello", json!({}));
        assert_eq!((hello["ok"].clone(), hello["route_ready"].clone()), (json!(true), json!(false)));
        assert_eq!(hello["result"]["boot_id"], "boot-1");
        assert_eq!(hello["result"]["run_id"], "run-1");
        let configured = assert_signed(&send(&mut node, &mut env, "configure", configure_payload()), "configured");
        assert_eq!(configured["details"]["endpoint_addr"]["id"], "endpoint-phone");
        assert_eq!(configured["details"]["runtime_mode"], REPLAY_DECODE_MODE);
        let start = json!({"peer": peer("node-mac-final", "endpoint-final"), "peers": [peer("node-mac-entry", "endpoint-entry")], "local_generation": 5});
        let started = assert_signed(&send(&mut node, &mut env, "start", start), "started");
        assert_eq!(started["peer_generation"], 5);
        assert_eq!(started["state"], "RUNNING");
        assert_eq!(env.installed[0].0.len(), 2);
        assert_eq!(env.installed[0].1, 5);
        assert_signed(&send(&mut node, &mut env, "health", json!({})), "health");
        let snapshot = assert_signed(&send(&mut node, &mut env, "snapshot", json!({})), "snapshot");
        assert_eq!(snapshot["details"]["host_resources"]["supported_decode_modes"], json!([REPLAY_DECODE_MODE]));
        let stopped = send(&mut node, &mut env, "stop", json!({}));
        assert_eq!(stopped["result"]["state"], "STOPPING");
        assert!(env.stopped);
    }

    #[test]
    fn a_stage_only_phone_refuses_entry_commands_and_foreign_runs() {
        let (mut node, mut env) = (node(), env());
        // Nothing before hello binds the run.
        assert_eq!(send(&mut node, &mut env, "health", json!({}))["error"]["code"], "run_id_mismatch");
        send(&mut node, &mut env, "hello", json!({}));
        for command in ["infer_start", "infer_decode", "a7_snapshot", "a10_target_verify"] {
            let r = send(&mut node, &mut env, command, json!({}));
            assert_eq!((r["ok"].clone(), r["error"]["code"].clone()), (json!(false), json!("stage_only_node")), "{command}");
        }
        // A bare `infer_cancel_wait` (no identity) is the owner's data-plane
        // cancel, never the receipt command: it is refused, not answered.
        assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", json!({}))["error"]["code"], "invalid_infer_cancel_control");
        let foreign = json!({"protocol": NODE_CONTROL_PROTOCOL, "command_id": "x", "run_id": "run-2", "deployment_id": "dep-1", "command": "health", "payload": {}});
        let r: Value =
            serde_json::from_slice(&node.handle("controller-1", &serde_json::to_vec(&foreign).unwrap(), &signer(), &mut env)).unwrap();
        assert_eq!(r["error"]["code"], "run_id_mismatch");
        let extra = json!({"protocol": NODE_CONTROL_PROTOCOL, "command_id": "x", "run_id": "run-1", "deployment_id": "dep-1", "command": "health", "payload": {}, "more": 1});
        let r: Value =
            serde_json::from_slice(&node.handle("controller-1", &serde_json::to_vec(&extra).unwrap(), &signer(), &mut env)).unwrap();
        assert_eq!(r["error"]["code"], "invalid_command_fields");
        let r: Value = serde_json::from_slice(&node.handle("controller-1", b"not json", &signer(), &mut env)).unwrap();
        assert_eq!(r["error"]["code"], "invalid_command_document");
    }

    #[test]
    fn configure_requires_the_phones_own_applied_stage_and_start_refuses_bad_peers() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        let mut wrong = configure_payload();
        wrong["pack_digest"] = json!("sha256:other");
        assert_eq!(send(&mut node, &mut env, "configure", wrong)["error"]["code"], "stage_pack_digest_mismatch");
        env.stage = None;
        assert_eq!(send(&mut node, &mut env, "configure", configure_payload())["error"]["code"], "stage_not_loaded");
        let mut env2 = self::env();
        assert_eq!(
            send(&mut node, &mut env2, "start", json!({"peer": peer("a", "e"), "local_generation": 1}))["error"]["code"],
            "invalid_state_for_start"
        );
        send(&mut node, &mut env2, "configure", configure_payload());
        for (payload, code) in [
            (json!({"peer": peer("node-phone", "e"), "local_generation": 1}), "self_peer_binding"),
            (json!({"peer": peer("a", "e"), "peers": [peer("a", "f")], "local_generation": 1}), "duplicate_peer_node_id"),
            (json!({"peer": peer("a", "e"), "peers": [peer("b", "e")], "local_generation": 1}), "duplicate_peer_endpoint_id"),
            (json!({"peer": peer("a", "e"), "local_generation": 0}), "invalid_local_generation"),
            (
                json!({"peer": {"node_id": "a", "endpoint_id": "e", "endpoint_addr": {"id": "other"}, "generation": 1}, "local_generation": 1}),
                "invalid_peer_binding",
            ),
        ] {
            assert_eq!(send(&mut node, &mut env2, "start", payload)["error"]["code"], code);
        }
        assert!(env2.installed.is_empty(), "nothing installed on a refused start");
    }

    #[test]
    fn lifecycle_validation_rejects_python_invalid_controls() {
        for (field, value) in [
            ("deployment_epoch", json!(0)), ("request_attempt", json!(0)),
            ("qualification_digest", json!("sha256:q")), ("path_digest", json!(format!("sha256:{}", "G".repeat(64)))),
            ("publisher_generation", json!(0)), ("topology_generation", json!(0)),
            ("absolute_deadline_ms", json!(0)), ("path_attempt", json!(-1)),
            ("command_id", json!("é".repeat(129))), ("cancellation_generation", json!(1)),
        ] {
            let (mut node, mut env) = running();
            let mut c = control(1);
            c["qualification_digest"] = json!(format!("sha256:{}", "b".repeat(64)));
            c["request_attempt"] = json!(1);
            c[field] = value;
            let result = send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":c}));
            assert_eq!(result["error"]["code"], "invalid_bind_request_control", "{field}: {result}");
            assert!(node.request_controls.is_empty());
        }
    }

    #[test]
    fn lifecycle_graph_and_pending_budget_are_exact_and_receipts_never_reopen() {
        let (mut node, mut env) = running();
        for field in ["deployment_epoch", "topology_generation"] {
            let mut c = control(1); c[field] = json!(2);
            assert_eq!(send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":c}))["error"]["code"], "invalid_bind_request_control");
            let mut c = cancel_payload(1); c[field] = json!(2);
            assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", c)["error"]["code"], "invalid_infer_cancel_control");
        }
        assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        let mut retry = cancel_payload(1); retry["deadline_budget_ms"] = json!(2000);
        assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", retry)["error"]["code"], "conflicting_pending_cancellation");
        assert_signed(&send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":control(1)})), "request_control_bound");
        assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        let mut other = cancel_payload(1); other["command_id"] = json!("other");
        assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", other)["error"]["code"], "request_already_cleaned");
        node.cleanup_receipts.clear(); // simulate the bounded receipt cache's eviction
        assert_eq!(send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":control(1)}))["error"]["code"], "request_already_cleaned");
        assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1))["error"]["code"], "request_already_cleaned");
    }

    #[test]
    fn lifecycle_snapshot_only_seals_consumed_exact_cancellation() {
        let (mut node, mut env) = running();
        let mut subject = cancel_payload(1);
        subject.as_object_mut().unwrap().remove("deadline_budget_ms");
        let query = json!({"cleanup_subject":subject, "receipt_only":true});
        let unknown = assert_signed(&send(&mut node, &mut env, "snapshot", query.clone()), "snapshot");
        assert_eq!(unknown["details"]["request_cleanup"]["complete"], false);
        assert!(env.cancelled.is_empty(), "a query cannot cancel a request");
        assert_signed(&send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":control(1)})), "request_control_bound");
        env.residue.inflight = 1;
        let first = assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        assert_eq!(first["details"]["cleanup_pending"], true);
        let done = assert_signed(&send(&mut node, &mut env, "snapshot", query.clone()), "snapshot");
        assert_eq!(done["details"]["request_cleanup"]["complete"], true);
        let replay = assert_signed(&send(&mut node, &mut env, "snapshot", query.clone()), "snapshot");
        assert_eq!(replay["details"]["request_cleanup"], done["details"]["request_cleanup"]);
        let mut wrong = query;
        wrong["cleanup_subject"]["command_id"] = json!("unconsumed-command");
        assert_eq!(send(&mut node, &mut env, "snapshot", wrong)["error"]["code"], "cleanup_receipt_identity_mismatch");
    }

    fn control(publisher: u64) -> Value {
        json!({
            "deployment_id": "dep-1", "deployment_epoch": 1, "qualification_digest": format!("sha256:{}", "b".repeat(64)), "command_id": "cmd",
            "publisher_generation": publisher, "absolute_deadline_ms": 10, "request_attempt": 1, "path_id": "p",
            "path_attempt": 0, "path_digest": format!("sha256:{}", "a".repeat(64)), "topology_generation": 1, "cancellation_generation": 0,
        })
    }

    #[test]
    fn request_control_keeps_identity_and_advances_the_publisher_generation_by_one() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        send(&mut node, &mut env, "configure", configure_payload());
        send(&mut node, &mut env, "start", json!({"peer": peer("a", "e"), "local_generation": 1}));
        let bound = assert_signed(
            &send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id": "r", "control": control(1)})),
            "request_control_bound",
        );
        assert_eq!(bound["details"]["control_digest"], canonical_digest(&control(1)));
        assert_signed(
            &send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id": "r", "control": control(1)})),
            "request_control_bound",
        );
        assert_eq!(
            send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id": "r", "control": control(2)}))["error"]["code"],
            "conflicting_request_control"
        );
        let updated = assert_signed(
            &send(&mut node, &mut env, "update_request_control", json!({"request_id": "r", "control": control(2)})),
            "request_control_updated",
        );
        assert_eq!((updated["details"]["publisher_generation"].clone(), updated["details"]["duplicate"].clone()), (json!(2), json!(false)));
        assert_eq!(
            send(&mut node, &mut env, "update_request_control", json!({"request_id": "r", "control": control(4)}))["error"]["code"],
            "publisher_generation_cas_mismatch"
        );
        let mut other = control(3);
        other["path_id"] = json!("q");
        assert_eq!(
            send(&mut node, &mut env, "update_request_control", json!({"request_id": "r", "control": other}))["error"]["code"],
            "request_control_identity_mismatch"
        );
        assert_eq!(
            send(&mut node, &mut env, "update_request_control", json!({"request_id": "x", "control": control(2)}))["error"]["code"],
            "request_control_unknown"
        );
        assert_eq!(env.deadlines,vec![("r".into(),60_000)],"duplicate bind and publisher update cannot remap execution time");
        let mut foreign = control(1);
        foreign["deployment_id"] = json!("dep-2");
        assert_eq!(
            send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id": "s", "control": foreign}))["error"]["code"],
            "invalid_bind_request_control"
        );
    }

    fn cancel_payload(generation: u64) -> Value {
        json!({
            "request_id": "r", "deployment_id": "dep-1", "deployment_epoch": 1, "qualification_digest": format!("sha256:{}", "b".repeat(64)),
            "command_id": "cmd", "publisher_generation": 1, "absolute_deadline_ms": 10, "request_attempt": 1,
            "path_id": "p", "path_attempt": 0, "path_digest": format!("sha256:{}", "a".repeat(64)), "topology_generation": 1,
            "cancellation_generation": generation, "deadline_budget_ms": 4000,
        })
    }

    fn running() -> (ControlNode, Env) {
        let (mut node, mut env) = (node(), env());
        assert_eq!(send(&mut node, &mut env, "hello", json!({}))["ok"], true);
        assert_eq!(send(&mut node, &mut env, "configure", configure_payload())["ok"], true);
        assert_eq!(send(&mut node, &mut env, "start", json!({"peer": peer("a", "e"), "local_generation": 1}))["ok"], true);
        (node, env)
    }

    #[test]
    fn cancellation_regression_pending_bind_consumes_and_advances() {
        let (mut node, mut env) = running();
        assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        let bound = assert_signed(&send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":control(1)})), "request_control_bound");
        let mut advanced = control(1);
        advanced["cancellation_generation"] = json!(1);
        assert_eq!(bound["details"]["control_digest"], canonical_digest(&advanced));
        assert!(!node.pending_cancellations.contains_key("r"));
        assert_eq!(node.request_controls["r"]["cancellation_generation"], 1);
        assert_signed(&send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":control(1)})), "request_control_bound");
        let done = assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        assert_eq!(done["details"]["request_cleanup"]["complete"], true);
    }

    #[test]
    fn cancellation_regression_pending_bind_requires_exact_next_generation() {
        let (mut node, mut env) = running();
        assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(3)), "inference_cancelled");
        let mut bad = control(1);
        bad["cancellation_generation"] = json!(1);
        let result = send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":bad}));
        assert_eq!(result["error"]["code"], "invalid_bind_request_control");
        assert!(node.request_controls.is_empty());
        assert_eq!(node.pending_cancellations["r"].cancellation_generation, 3);
    }

    #[test]
    fn cancellation_regression_incomplete_bound_cleanup_can_finish() {
        let (mut node, mut env) = running();
        assert_signed(&send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":control(1)})), "request_control_bound");
        env.residue.inflight = 1;
        let first = assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        assert_eq!(first["details"]["cleanup_pending"], true);
        let done = assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        assert_eq!(done["details"]["request_cleanup"]["complete"], true);
        assert_eq!(env.cancelled.len(), 2);
    }

    #[test]
    fn cancellation_regression_sealed_cleanup_cannot_be_rebound() {
        let (mut node, mut env) = running();
        let bind = json!({"execution_budget_ms":60_000, "request_id":"r", "control":control(1)});
        assert_signed(&send(&mut node, &mut env, "bind_request_control", bind.clone()), "request_control_bound");
        assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        assert_signed(&send(&mut node, &mut env, "bind_request_control", bind), "request_control_bound");
        assert!(node.request_controls.is_empty(), "a late bind must not resurrect sealed cleanup");
        let mut other = control(1);
        other["path_id"] = json!("other");
        assert_eq!(send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id":"r", "control":other}))["error"]["code"], "request_already_cleaned");
    }

    /// The owner cancellation fan-out: a signed `inference_cancelled`
    /// observation whose `request_cleanup` commits the exact subject it tore
    /// down, with `cleanup_pending: false` only when nothing is left.
    #[test]
    fn infer_cancel_wait_signs_a_receipt_over_the_exact_torn_down_subject() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        send(&mut node, &mut env, "configure", configure_payload());
        send(&mut node, &mut env, "start", json!({"peer": peer("a", "e"), "local_generation": 1}));
        send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id": "r", "control": control(1)}));
        let observation = assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        let details = &observation["details"];
        assert_eq!(details["request_id"], "r");
        assert_eq!(details["cancelled"], true);
        assert_eq!(details["status"], "CANCELLED");
        assert_eq!(details["cleanup_pending"], false);
        let cleanup = &details["request_cleanup"];
        assert_eq!(cleanup["complete"], true);
        assert_eq!(cleanup["request_id"], "r");
        assert_eq!(cleanup["path_id"], "p");
        assert_eq!(cleanup["cancellation_generation"], 1);
        assert_eq!(cleanup["runtime_clean"], true);
        assert_eq!(cleanup["transport_clean"], true);
        assert_eq!(cleanup["cancellation_worker_complete"], true);
        assert_eq!(
            cleanup["residue"],
            json!({"live_paths": [], "pending_cancellations": 0, "inflight": 0, "replay_results": 0, "path_retained": false})
        );
        // The teardown saw exactly this subject, once.
        assert_eq!(env.cancelled.len(), 1);
        assert_eq!(env.cancelled[0].request_id, "r");
        assert_eq!(env.cancelled[0].cancellation_generation, 1);
        // The sealed receipt replays byte-for-byte and never re-tears-down.
        let replay = assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        assert_eq!(replay["details"], *details);
        assert_eq!(env.cancelled.len(), 1);
    }

    #[test]
    fn a_cancellation_reports_the_residue_it_found_and_never_claims_zero() {
        for residue in [
            crate::node_cancel::Residue { live_paths: vec!["p2".into()], ..Default::default() },
            crate::node_cancel::Residue { pending_cancellations: 1, ..Default::default() },
            crate::node_cancel::Residue { inflight: 1, ..Default::default() },
            crate::node_cancel::Residue { replay_results: 2, ..Default::default() },
            crate::node_cancel::Residue { path_retained: true, ..Default::default() },
        ] {
            let (mut node, mut env) = (node(), env());
            send(&mut node, &mut env, "hello", json!({}));
            send(&mut node, &mut env, "configure", configure_payload());
            send(&mut node, &mut env, "start", json!({"peer": peer("a", "e"), "local_generation": 1}));
            env.residue = residue.clone();
            // Cancel-before-start: no control bound, so this is the pending
            // identity and the receipt cannot be complete either.
            let observation = assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
            let details = &observation["details"];
            assert_eq!(details["pending_start"], true);
            assert_eq!(details["cleanup_pending"], true, "{details}");
            assert_eq!(details["status"], "CANCELLING");
            assert_eq!(details["request_cleanup"]["complete"], false, "{details}");
            assert_eq!(details["request_cleanup"]["residue"], residue.to_json(), "{details}");
            // A pending receipt is never sealed: a repeat re-observes.
            send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1));
            assert_eq!(env.cancelled.len(), 2);
        }
    }

    #[test]
    fn only_the_next_cancellation_generation_of_the_bound_control_is_applied() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        send(&mut node, &mut env, "configure", configure_payload());
        send(&mut node, &mut env, "start", json!({"peer": peer("a", "e"), "local_generation": 1}));
        // No control bound yet: the cancellation is a pending identity.
        let pending = assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        assert_eq!(pending["details"]["pending_start"], true);
        // A different immutable identity for the same request cannot join it.
        let mut other = cancel_payload(3);
        other["path_id"] = json!("q");
        assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", other)["error"]["code"], "conflicting_pending_cancellation");
        // A bound control at generation 0 advances only at generation 1.
        send(&mut node, &mut env, "bind_request_control", json!({"execution_budget_ms":60_000, "request_id": "r2", "control": control(1)}));
        let mut stale = cancel_payload(2);
        stale["request_id"] = json!("r2");
        assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", stale.clone())["error"]["code"], "stale_infer_cancel_generation");
        let mut wrong_attempt = stale.clone();
        wrong_attempt["path_attempt"] = json!(9);
        assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", wrong_attempt)["error"]["code"], "stale_infer_cancel_generation");
        let mut foreign_deployment = stale.clone();
        foreign_deployment["deployment_id"] = json!("dep-2");
        assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", foreign_deployment)["error"]["code"], "invalid_infer_cancel_control");
        // Nothing was torn down by any refused cancellation.
        assert_eq!(env.cancelled.len(), 1);
        // Generation 1 against a bound generation 0 is applied and sealed.
        let mut applied = stale.clone();
        applied["cancellation_generation"] = json!(1);
        applied["cancellation_generation"] = json!(1);
        // The bound control's own cancellation generation is 0, so this is next.
        let receipt = assert_signed(&send(&mut node, &mut env, "infer_cancel_wait", applied), "inference_cancelled");
        assert_eq!(receipt["details"]["request_cleanup"]["complete"], true);
        assert!(receipt["details"].get("pending_start").is_none());
    }

    #[test]
    fn a_malformed_cancellation_is_refused_before_any_teardown() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        send(&mut node, &mut env, "configure", configure_payload());
        send(&mut node, &mut env, "start", json!({"peer": peer("a", "e"), "local_generation": 1}));
        for (mutate, code) in [
            (
                Box::new(|p: &mut Value| { p.as_object_mut().unwrap().remove("request_id"); }) as Box<dyn Fn(&mut Value)>,
                "invalid_infer_cancel_control",
            ),
            (Box::new(|p: &mut Value| { p.as_object_mut().unwrap().remove("deadline_budget_ms"); }), "invalid_infer_cancel_control"),
            (Box::new(|p: &mut Value| { p.as_object_mut().unwrap().insert("extra".into(), json!(1)); }), "invalid_infer_cancel_control"),
            (Box::new(|p: &mut Value| p["deadline_budget_ms"] = json!(9_000)), "invalid_infer_cancel_control"),
            (Box::new(|p: &mut Value| p["cancellation_generation"] = json!(0)), "invalid_infer_cancel_control"),
            (Box::new(|p: &mut Value| p["publisher_generation"] = json!(0)), "invalid_infer_cancel_control"),
        ] {
            let mut payload = cancel_payload(1);
            mutate(&mut payload);
            assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", payload)["error"]["code"], code);
        }
        assert!(env.cancelled.is_empty(), "a refused cancellation never tears anything down");
    }

    #[test]
    fn stop_and_a_cancellation_receipt_do_not_outlive_the_session() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        send(&mut node, &mut env, "configure", configure_payload());
        send(&mut node, &mut env, "start", json!({"peer": peer("a", "e"), "local_generation": 1}));
        send(&mut node, &mut env, "infer_cancel_wait", cancel_payload(1));
        send(&mut node, &mut env, "stop", json!({}));
        // A new session cannot replay the old session's sealed receipt.
        let (mut fresh, mut env2) = (self::node(), self::env());
        send(&mut fresh, &mut env2, "hello", json!({}));
        send(&mut fresh, &mut env2, "configure", configure_payload());
        let replay = assert_signed(&send(&mut fresh, &mut env2, "infer_cancel_wait", cancel_payload(1)), "inference_cancelled");
        assert_eq!(replay["details"]["pending_start"], true, "{replay}");
        assert_eq!(env2.cancelled.len(), 1);
    }

    #[test]
    fn a_session_is_driven_only_by_the_controller_that_opened_it() {
        let (mut node, mut env) = (node(), env());
        let doc = |command: &str| {
            serde_json::to_vec(
                &json!({"protocol": NODE_CONTROL_PROTOCOL, "command_id": "x", "run_id": "run-1", "deployment_id": "dep-1", "command": command, "payload": {}}),
            )
            .unwrap()
        };
        let answer = |r: Vec<u8>| serde_json::from_slice::<Value>(&r).unwrap();
        assert_eq!(answer(node.handle("controller-1", &doc("hello"), &signer(), &mut env))["ok"], true);
        // Another (even allow-listed) controller, with the same run ids.
        for command in ["hello", "health", "stop"] {
            assert_eq!(answer(node.handle("controller-2", &doc(command), &signer(), &mut env))["error"]["code"], "controller_mismatch");
        }
        assert_eq!(answer(node.handle("", &doc("hello"), &signer(), &mut env))["error"]["code"], "controller_mismatch");
        assert!(!env.stopped && node.state() == "NEW");
    }

    fn v2_provenance() -> Value {
        json!({
            "applied_fields":["assignment_id","stage_id","phone_stage_pack_digest","runtime_backend","runtime_build_digest","golden_output_digest","load_generation"],
            "controller_claim_fields":["manifest_digest"],
            "limitation":"native_full_model_manifest_digest_not_independently_observed",
            "load_generation_basis":"native_stage_load_sequence_within_process_incarnation"
        })
    }

    #[test]
    fn configure_refuses_a_load_generation_the_phone_did_not_count() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        for claimed in [2u64, 17, u64::MAX] {
            let mut wrong = configure_payload();
            wrong["load_generation"] = json!(claimed);
            let refused = send(&mut node, &mut env, "configure", wrong);
            assert_eq!((refused["ok"].clone(), refused["error"]["code"].clone()), (json!(false), json!("load_generation_mismatch")), "{refused}");
            assert!(refused.get("result").is_none(), "a refused claim is never signed");
            assert_eq!(node.state(), "NEW");
        }
        // The other applied-stage refusals keep their own codes ahead of it.
        let mut both = configure_payload();
        both["pack_digest"] = json!("sha256:other");
        both["load_generation"] = json!(2);
        assert_eq!(send(&mut node, &mut env, "configure", both)["error"]["code"], "stage_pack_digest_mismatch");
        let configured = assert_signed(&send(&mut node, &mut env, "configure", configure_payload()), "configured");
        assert_eq!(configured["details"]["load_generation"], 1);
    }

    #[test]
    fn the_configured_observation_signs_the_closed_v2_load_descriptor_and_never_the_old_one() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        let response = send(&mut node, &mut env, "configure", configure_payload());
        let configured = assert_signed(&response, "configured");
        assert_eq!(configured["details"]["load_identity_provenance"], v2_provenance());
        let raw = response.to_string();
        assert!(!raw.contains("native_manifest_load_generation_not_independently_observed"), "old descriptor emitted");
        assert!(!raw.contains("\"controller_claim_fields\":[\"manifest_digest\",\"load_generation\"]"), "old claim list emitted");
        // The manifest digest stays what it is: the controller's claim, echoed.
        assert_eq!(configured["details"]["manifest_digest"], "sha256:graph");
    }

    #[test]
    fn stage_load_is_an_unsigned_hint_and_null_without_an_applied_stage() {
        let (mut node, mut env) = (node(), env());
        // Like every command, it follows the hello that binds the run.
        assert_eq!(send(&mut node, &mut env, "stage_load", json!({}))["error"]["code"], "run_id_mismatch");
        send(&mut node, &mut env, "hello", json!({}));
        let hint = send(&mut node, &mut env, "stage_load", json!({}));
        assert_eq!(hint["ok"], true, "{hint}");
        assert_eq!(hint["result"], json!({"protocol": NODE_CONTROL_PROTOCOL, "stage_load_generation": 1}));
        assert_eq!(send(&mut node, &mut env, "stage_load", json!({"x":1}))["error"]["code"], "invalid_stage_load_fields");
        env.stage = None;
        assert_eq!(send(&mut node, &mut env, "stage_load", json!({}))["result"]["stage_load_generation"], Value::Null);
        assert_eq!(node.state(), "NEW", "a hint changes no state");
    }

    #[test]
    fn a_reload_after_configure_is_refused_at_start_and_visible_in_the_signed_snapshot() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        assert_signed(&send(&mut node, &mut env, "configure", configure_payload()), "configured");
        let before = assert_signed(&send(&mut node, &mut env, "snapshot", json!({})), "snapshot");
        assert_eq!(before["details"]["load_identity"], json!({"assignment_id":"assignment-1","stage_id":"stage-phone","phone_stage_pack_digest":"sha256:pack","load_generation":1}));
        assert_eq!(before["details"]["configured_load_generation"], 1);
        // The node reloads the same assignment: its own count advances.
        env.stage.as_mut().unwrap().load_generation = 2;
        let after = assert_signed(&send(&mut node, &mut env, "snapshot", json!({})), "snapshot");
        assert_eq!(after["details"]["load_identity"]["load_generation"], 2);
        assert_eq!(after["details"]["configured_load_generation"], 1, "the configured load is not rewritten");
        let start = json!({"peer": peer("a", "e"), "local_generation": 1});
        assert_eq!(send(&mut node, &mut env, "start", start.clone())["error"]["code"], "load_generation_mismatch");
        assert!(env.installed.is_empty() && node.state() == "CONFIGURED");
        env.stage = None;
        assert_eq!(send(&mut node, &mut env, "start", start)["error"]["code"], "stage_not_loaded");
        assert_eq!(assert_signed(&send(&mut node, &mut env, "snapshot", json!({})), "snapshot")["details"]["load_identity"], Value::Null);
    }

    #[test]
    fn a_new_session_must_name_the_current_load_and_a_new_incarnation_is_signed_into_it() {
        let mut env = env();
        env.stage.as_mut().unwrap().load_generation = 2;
        let mut second = node();
        send(&mut second, &mut env, "hello", json!({}));
        // The profile signed for load 1 is the old one now.
        assert_eq!(send(&mut second, &mut env, "configure", configure_payload())["error"]["code"], "load_generation_mismatch");
        let mut current = configure_payload();
        current["load_generation"] = json!(2);
        let configured = assert_signed(&send(&mut second, &mut env, "configure", current), "configured");
        assert_eq!((configured["details"]["load_generation"].clone(), configured["process_id"].clone()), (json!(2), json!("process-1")));
        // A restarted process counts from one again, under another incarnation:
        // the same number is a different signed identity.
        let mut restarted = ControlNode::new(ControlIdentity {
            node_id: "node-phone".into(), host_id: "host-phone".into(), process_id: "process-2".into(),
            endpoint_id: "endpoint-phone".into(), boot_id: "boot-2".into(),
        });
        let mut fresh = self::env();
        send(&mut restarted, &mut fresh, "hello", json!({}));
        let again = assert_signed(&send(&mut restarted, &mut fresh, "configure", configure_payload()), "configured");
        assert_eq!((again["details"]["load_generation"].clone(), again["process_id"].clone()), (json!(1), json!("process-2")));
    }
}
