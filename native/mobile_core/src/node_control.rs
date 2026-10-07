// SPDX-License-Identifier: AGPL-3.0-or-later
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
//! and its sealed cleanup receipts are M4-host work (see the proposal) and
//! are refused with `unsupported_command` until then.
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
const ENTRY_ONLY: [&str; 9] =
    ["infer_start", "infer_decode", "infer_cancel", "a7_snapshot", "a7_source_lost", "a10_target_verify", "a10_batch_policy", "rotate", "cancel"];

/// The phone's applied v2 stage, as the control channel sees it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StageView {
    pub assignment_id: String,
    pub stage_id: String,
    pub pack_digest: String,
    pub backend: String,
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
    /// A `mycelium.host_resource_snapshot.v1`-shaped document.
    fn host_resources(&self) -> Value;
    /// Monotonic nanoseconds (the observation clock).
    fn monotonic_ns(&self) -> u64;
    /// Drain: stop serving Router frames.
    fn stop(&mut self);
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
    request_controls: BTreeMap<String, Map<String, Value>>,
}

impl ControlNode {
    pub fn new(identity: ControlIdentity) -> Self {
        Self { identity, run: None, controller: None, state: State::New, peer_generation: 0, request_controls: BTreeMap::new() }
    }

    pub fn state(&self) -> &'static str {
        self.state.name()
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
            "configure" => self.configure(payload, signer, env),
            "start" => self.start(payload, signer, env),
            "health" => self.health(payload, signer, env),
            "snapshot" => self.snapshot(payload, signer, env),
            "bind_request_control" => self.bind_request_control(payload, signer, env),
            "update_request_control" => self.update_request_control(payload, signer, env),
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

    /// Phone profile: `{assignment_id, stage_id, pack_digest,
    /// manifest_digest, load_generation}`; the stage is the phone's own
    /// applied, golden-verified v2 stage, never a file the controller wrote.
    fn configure(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        require(self.state == State::New, "invalid_state_for_configure")?;
        let p = exact_fields(payload, &["assignment_id", "stage_id", "pack_digest", "manifest_digest", "load_generation"], "invalid_configure_fields")?;
        require(non_negative_int(&p["load_generation"]).is_some_and(|g| g > 0), "invalid_load_generation")?;
        let text = |k: &str| p[k].as_str().filter(|s| !s.is_empty()).ok_or_else(|| Refusal("invalid_configure_fields".into()));
        let (assignment_id, stage_id, pack_digest, manifest_digest) =
            (text("assignment_id")?, text("stage_id")?, text("pack_digest")?, text("manifest_digest")?);
        let stage = env.stage().ok_or_else(|| Refusal("stage_not_loaded".into()))?;
        require(stage.backend == crate::qwen2::QWEN2_BACKEND, "stage_backend_mismatch")?;
        require(stage.assignment_id == assignment_id, "assignment_id_mismatch")?;
        require(stage.stage_id == stage_id, "stage_id_mismatch")?;
        require(stage.pack_digest == pack_digest, "stage_pack_digest_mismatch")?;
        self.state = State::Configured;
        let details = json!({
            "assignment_id": assignment_id,
            "stage_id": stage_id,
            "manifest_digest": manifest_digest,
            "endpoint_addr": env.endpoint_addr(),
            "runtime_mode": REPLAY_DECODE_MODE,
            "phone_stage_pack_digest": pack_digest,
            "load_generation": p["load_generation"],
        });
        self.signed("configured", details, signer, env)
    }

    fn peer(&self, value: &Value) -> Result<PeerSpec, Refusal> {
        let p = exact_fields(value, &PEER_FIELDS, "invalid_peer_fields")?;
        let (Some(node_id), Some(endpoint_id), Some(generation)) = (p["node_id"].as_str(), p["endpoint_id"].as_str(), non_negative_int(&p["generation"]))
        else {
            return refuse("invalid_peer_binding");
        };
        require(!node_id.is_empty() && !endpoint_id.is_empty() && generation > 0, "invalid_peer_binding")?;
        require(p["endpoint_addr"]["id"] == endpoint_id, "invalid_peer_binding")?;
        require(node_id != self.identity.node_id, "self_peer_binding")?;
        Ok(PeerSpec { node_id: node_id.to_owned(), endpoint_id: endpoint_id.to_owned(), endpoint_addr: p["endpoint_addr"].clone(), generation })
    }

    fn start(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        require(self.state == State::Configured, "invalid_state_for_start")?;
        let map = payload.as_object().ok_or_else(|| Refusal("invalid_start_fields".into()))?;
        let keys: BTreeSet<&str> = map.keys().map(String::as_str).collect();
        require(keys == BTreeSet::from(["peer", "local_generation"]) || keys == BTreeSet::from(["peer", "peers", "local_generation"]), "invalid_start_fields")?;
        let local_generation = non_negative_int(&map["local_generation"]).filter(|g| *g > 0).ok_or_else(|| Refusal("invalid_local_generation".into()))?;
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

    fn snapshot(&self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
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
        self.signed("snapshot", json!({ "state": self.state.name(), "host_resources": resources }), signer, env)
    }

    fn request_control(&self, payload: &Value, code: &str) -> Result<(String, Map<String, Value>), Refusal> {
        let p = exact_fields(payload, &["request_id", "control"], code)?;
        let request_id = p["request_id"].as_str().filter(|s| !s.is_empty()).ok_or_else(|| Refusal("invalid_request_id".into()))?;
        let control = exact_fields(&p["control"], &REQUEST_CONTROL_FIELDS, code)?;
        let run = self.run.as_ref().map(|(_, d)| d.as_str());
        require(control["deployment_id"].as_str() == run, code)?;
        for k in [
            "publisher_generation",
            "cancellation_generation",
            "request_attempt",
            "path_attempt",
            "topology_generation",
            "absolute_deadline_ms",
            "deployment_epoch",
        ] {
            require(control[k].is_u64(), code)?;
        }
        for k in ["qualification_digest", "command_id", "path_id", "path_digest"] {
            require(control[k].as_str().is_some_and(|s| !s.is_empty()), code)?;
        }
        Ok((request_id.to_owned(), control.clone()))
    }

    fn bind_request_control(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        exact_fields(payload, &["request_id", "control"], "invalid_bind_request_control_fields")?;
        require(self.state == State::Running, "invalid_state_for_bind_request_control")?;
        let (request_id, control) = self.request_control(payload, "invalid_bind_request_control")?;
        if let Some(current) = self.request_controls.get(&request_id) {
            require(*current == control, "conflicting_request_control")?;
        } else {
            require(self.request_controls.len() < MAX_REQUEST_CONTROLS, "request_control_table_full")?;
            self.request_controls.insert(request_id.clone(), control.clone());
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
        self.signed("request_control_updated", json!({ "request_id": request_id, "publisher_generation": new, "duplicate": duplicate }), signer, env)
    }

    fn stop(&mut self, payload: &Value, signer: &Identity, env: &mut dyn ControlEnv) -> Result<Value, Refusal> {
        exact_fields(payload, &[], "invalid_stop_fields")?;
        self.state = State::Stopping;
        let signed = self.signed("stopping", json!({}), signer, env)?;
        env.stop();
        self.request_controls.clear();
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
        fn host_resources(&self) -> Value {
            json!({"protocol": "mycelium.host_resource_snapshot.v1", "backend": crate::qwen2::QWEN2_BACKEND})
        }
        fn monotonic_ns(&self) -> u64 {
            self.clock
        }
        fn stop(&mut self) {
            self.stopped = true;
        }
    }

    fn env() -> Env {
        Env {
            stage: Some(StageView {
                assignment_id: "assignment-1".into(),
                stage_id: "stage-phone".into(),
                pack_digest: "sha256:pack".into(),
                backend: crate::qwen2::QWEN2_BACKEND.into(),
            }),
            installed: vec![],
            stopped: false,
            clock: 7,
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
        json!({"assignment_id": "assignment-1", "stage_id": "stage-phone", "pack_digest": "sha256:pack", "manifest_digest": "sha256:graph", "load_generation": 1})
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
        assert_eq!(send(&mut node, &mut env, "infer_cancel_wait", json!({}))["error"]["code"], "unsupported_command");
        let foreign =
            json!({"protocol": NODE_CONTROL_PROTOCOL, "command_id": "x", "run_id": "run-2", "deployment_id": "dep-1", "command": "health", "payload": {}});
        let r: Value = serde_json::from_slice(&node.handle("controller-1", &serde_json::to_vec(&foreign).unwrap(), &signer(), &mut env)).unwrap();
        assert_eq!(r["error"]["code"], "run_id_mismatch");
        let extra = json!({"protocol": NODE_CONTROL_PROTOCOL, "command_id": "x", "run_id": "run-1", "deployment_id": "dep-1", "command": "health", "payload": {}, "more": 1});
        let r: Value = serde_json::from_slice(&node.handle("controller-1", &serde_json::to_vec(&extra).unwrap(), &signer(), &mut env)).unwrap();
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
        assert_eq!(send(&mut node, &mut env2, "start", json!({"peer": peer("a", "e"), "local_generation": 1}))["error"]["code"], "invalid_state_for_start");
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

    fn control(publisher: u64) -> Value {
        json!({
            "deployment_id": "dep-1", "deployment_epoch": 1, "qualification_digest": "sha256:q", "command_id": "cmd",
            "publisher_generation": publisher, "absolute_deadline_ms": 10, "request_attempt": 0, "path_id": "p",
            "path_attempt": 0, "path_digest": "sha256:p", "topology_generation": 1, "cancellation_generation": 0,
        })
    }

    #[test]
    fn request_control_keeps_identity_and_advances_the_publisher_generation_by_one() {
        let (mut node, mut env) = (node(), env());
        send(&mut node, &mut env, "hello", json!({}));
        send(&mut node, &mut env, "configure", configure_payload());
        send(&mut node, &mut env, "start", json!({"peer": peer("a", "e"), "local_generation": 1}));
        let bound =
            assert_signed(&send(&mut node, &mut env, "bind_request_control", json!({"request_id": "r", "control": control(0)})), "request_control_bound");
        assert_eq!(bound["details"]["control_digest"], canonical_digest(&control(0)));
        assert_signed(&send(&mut node, &mut env, "bind_request_control", json!({"request_id": "r", "control": control(0)})), "request_control_bound");
        assert_eq!(
            send(&mut node, &mut env, "bind_request_control", json!({"request_id": "r", "control": control(1)}))["error"]["code"],
            "conflicting_request_control"
        );
        let updated =
            assert_signed(&send(&mut node, &mut env, "update_request_control", json!({"request_id": "r", "control": control(1)})), "request_control_updated");
        assert_eq!((updated["details"]["publisher_generation"].clone(), updated["details"]["duplicate"].clone()), (json!(1), json!(false)));
        assert_eq!(
            send(&mut node, &mut env, "update_request_control", json!({"request_id": "r", "control": control(3)}))["error"]["code"],
            "publisher_generation_cas_mismatch"
        );
        let mut other = control(2);
        other["path_id"] = json!("q");
        assert_eq!(
            send(&mut node, &mut env, "update_request_control", json!({"request_id": "r", "control": other}))["error"]["code"],
            "request_control_identity_mismatch"
        );
        assert_eq!(
            send(&mut node, &mut env, "update_request_control", json!({"request_id": "x", "control": control(1)}))["error"]["code"],
            "request_control_unknown"
        );
        let mut foreign = control(0);
        foreign["deployment_id"] = json!("dep-2");
        assert_eq!(
            send(&mut node, &mut env, "bind_request_control", json!({"request_id": "s", "control": foreign}))["error"]["code"],
            "invalid_bind_request_control"
        );
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
}
