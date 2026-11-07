// SPDX-License-Identifier: AGPL-3.0-only
//! The native-hello observation (`runtime::observe`) against the EXECUTED v0.1.4 functions.
//! `fixtures/observe-oracle.json` (probe `L9-evidence/r15/r15-observe-probe.py`, python3.14 against
//! `managed_observations.collect_managed_hello_observations` and `chain_observation.
//! prepare_observation / ObservationContext` @ f4e366e9, session boundary and mailbox scripted,
//! every scenario run twice and identical) holds, per scenario, the result or closed code, the
//! ordered collaborator calls and the number of clock readings. The Rust port is driven by the
//! SAME scripts and must agree on all three.
//!
//! Proves: B-RUN-25 (native hello observed, not assumed: rejection matrix of 32+ responses, close
//! failure fails closed, run id invalid/stale, distinct hosts and boots, membership still current,
//! bootstrap offers published only after the whole batch validated, retire/collect order).
use runtime::observe::{
    CloseFailed, HelloSession, HelloSessions, ObservationError, ObservationHost, Offer,
    bootstrap_archive, collect_hello_observations, prepare_observation,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    sync::{Arc, Mutex},
};

type Log = Arc<Mutex<Vec<String>>>;

#[path = "support/observe_owner.rs"]
mod owner_adapter;

const RUN: &str = "fleet-qwen-20261029T040000Z";
const DEP: &str = "dep-1";
const DEFAULT_DIGEST: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/observe-oracle.json")).unwrap()
}
fn text<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}
fn host_id(c: &str) -> String {
    format!("host-{}", c.repeat(32))
}
fn boot_id(c: &str) -> String {
    format!("boot-{}", c.repeat(32))
}
fn ok_response(
    node: &str,
    command_id: &str,
    host: &str,
    boot: &str,
    run: &str,
    dep: &str,
) -> Value {
    json!({"protocol": "mycelium.physical_node_control.v1", "command_id": command_id,
        "node_id": node, "ok": true, "route_ready": false,
        "result": {"protocol": "mycelium.physical_node_control.v1", "node_id": node, "run_id": run,
            "deployment_id": dep, "route_ready": false, "state": "NEW",
            "host_id": host, "boot_id": boot}})
}

// ---- scripted sessions ------------------------------------------------------------------------

struct Session {
    log: Log,
    node: String,
    script: Value,
    run: String,
    dep: String,
}
impl HelloSession for Session {
    fn send_hello(&mut self, command_id: &str) -> Result<Value, Option<String>> {
        self.log
            .lock()
            .unwrap()
            .push(format!("send:{}:hello:{command_id}:{{}}", self.node));
        let send = self.script.get("send");
        if let Some(error) = send.and_then(|s| s.get("error")) {
            return Err(text(error, "code").map(str::to_owned));
        }
        if let Some(response) = send.and_then(|s| s.get("response")) {
            return Ok(response.clone());
        }
        Ok(ok_response(
            &self.node,
            command_id,
            text(&self.script, "host")
                .map_or_else(|| host_id("a"), str::to_owned)
                .as_str(),
            text(&self.script, "boot")
                .map_or_else(|| boot_id("b"), str::to_owned)
                .as_str(),
            &self.run,
            &self.dep,
        ))
    }
    fn close(&mut self) -> Result<(), CloseFailed> {
        self.log
            .lock()
            .unwrap()
            .push(format!("close:{}", self.node));
        if text(&self.script, "close") == Some("error") {
            Err(CloseFailed)
        } else {
            Ok(())
        }
    }
    fn cleanup_error(&self) -> bool {
        self.script.get("cleanup_error") == Some(&Value::Bool(true))
    }
    fn stop_response_ok(&self) -> Option<bool> {
        self.script
            .get("stop_response")
            .map(|response| response.get("ok") == Some(&Value::Bool(true)))
    }
}

/// `style`: the open line of the collect scenarios names the scope, the context scenarios do not.
struct Factory {
    log: Log,
    run: String,
    dep: String,
    nodes: BTreeSet<String>,
    scripts: Value,
    binding_after: Value,
    scoped_open_log: bool,
    fail_send: Arc<Mutex<Option<String>>>,
}
impl HelloSessions for Factory {
    fn run_id(&self) -> &str {
        &self.run
    }
    fn deployment_id(&self) -> &str {
        &self.dep
    }
    fn bound_nodes(&self) -> BTreeSet<String> {
        self.nodes.clone()
    }
    fn open(
        &mut self,
        node_id: &str,
        timeout_seconds: f64,
    ) -> Result<Box<dyn HelloSession + '_>, Option<String>> {
        let line = if self.scoped_open_log {
            format!(
                "open:{node_id}:():('{}', '{}'):{timeout_seconds:?}",
                self.run, self.dep
            )
        } else {
            format!("open:{node_id}")
        };
        self.log.lock().unwrap().push(line);
        let mut script = self
            .scripts
            .get(node_id)
            .cloned()
            .unwrap_or_else(|| json!({}));
        if !self.scoped_open_log {
            let digit = &node_id[node_id.len() - 1..];
            script = json!({"host": host_id(digit), "boot": boot_id(digit)});
            if self.fail_send.lock().unwrap().as_deref() == Some(node_id) {
                script["send"] = json!({"error": {"code": "managed_control_timeout"}});
            }
        }
        if let Some(error) = script.get("open_error") {
            return Err(text(error, "code").map(str::to_owned));
        }
        Ok(Box::new(Session {
            log: self.log.clone(),
            node: node_id.to_owned(),
            script,
            run: self.run.clone(),
            dep: self.dep.clone(),
        }))
    }
    fn binding_current(&mut self, node_id: &str) -> bool {
        self.log
            .lock()
            .unwrap()
            .push(format!("current_member:{node_id}"));
        !matches!(
            text(&self.binding_after, node_id),
            Some("missing" | "advanced")
        )
    }
}

fn strings(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect()
}

fn clock_of(values: Vec<Value>, reads: Arc<Mutex<usize>>) -> impl FnMut() -> f64 {
    move || {
        let index = *reads.lock().unwrap();
        *reads.lock().unwrap() += 1;
        match &values[index.min(values.len() - 1)] {
            Value::String(s) if s == "nan" => f64::NAN,
            Value::String(s) if s == "inf" => f64::INFINITY,
            other => other.as_f64().unwrap(),
        }
    }
}

fn outcome(result: Result<Value, ObservationError>) -> Value {
    match result {
        Ok(value) => json!({"ok": value}),
        Err(error) => json!({"error": error.code()}),
    }
}

#[test]
fn the_fixture_is_the_executed_oracle_table() {
    let f = fixture();
    let provenance = &f["provenance"];
    assert!(
        provenance["oracle"]
            .as_str()
            .unwrap()
            .contains("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    assert_eq!(provenance["cases"], 130);
    let kinds = |kind: &str| {
        f["cases"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["kind"] == kind)
            .count()
    };
    assert_eq!(
        (kinds("collect"), kinds("prepare"), kinds("post")),
        (102, 20, 8)
    );
}

#[test]
fn collect_matches_the_oracle_on_every_scripted_scenario() {
    let f = fixture();
    let mut checked = 0;
    for case in f["cases"].as_array().unwrap() {
        if case["kind"] != "collect" {
            continue;
        }
        let spec = &case["spec"];
        let name = text(spec, "name").unwrap();
        let log: Log = Arc::default();
        let run = text(spec, "run_id").unwrap_or(RUN);
        let dep = text(spec, "deployment_id").unwrap_or(DEP);
        let mut factory = Factory {
            log: log.clone(),
            run: text(spec, "factory_run").unwrap_or(run).to_owned(),
            dep: text(spec, "factory_dep").unwrap_or(dep).to_owned(),
            nodes: strings(spec.get("factory_nodes")).into_iter().collect(),
            scripts: spec.get("script").cloned().unwrap_or_else(|| json!({})),
            binding_after: spec
                .get("binding_after")
                .cloned()
                .unwrap_or_else(|| json!({})),
            scoped_open_log: true,
            fail_send: Arc::default(),
        };
        let reads = Arc::new(Mutex::new(0));
        let mut clock = clock_of(spec["clock"].as_array().unwrap().clone(), reads.clone());
        let tokens = Arc::new(Mutex::new(0_u64));
        let mut token = {
            let tokens = tokens.clone();
            move || {
                *tokens.lock().unwrap() += 1;
                format!("{:016x}", *tokens.lock().unwrap())
            }
        };
        let got = outcome(
            collect_hello_observations(
                &mut factory,
                run,
                dep,
                &strings(spec.get("node_ids")),
                &mut clock,
                &mut token,
            )
            .map(|document| document.to_value()),
        );
        assert_eq!(got, case["result"], "{name}: result");
        let calls: Vec<String> = case["calls"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c.as_str().unwrap().to_owned())
            .collect();
        assert_eq!(
            *log.lock().unwrap(),
            calls,
            "{name}: collaborator calls in order"
        );
        assert_eq!(
            *reads.lock().unwrap() as u64,
            case["clock_reads"].as_u64().unwrap(),
            "{name}: clock readings"
        );
        checked += 1;
    }
    assert_eq!(checked, 102);
}

// ---- the context ---------------------------------------------------------------------------

struct Host {
    log: Log,
    spec: Value,
    fail_send: Arc<Mutex<Option<String>>>,
}
fn member(node: &str) -> Value {
    json!({"node_id": node, "endpoint_id": format!("ep-{node}"), "generation": 1,
        "verification_key_digest": format!("sha256:{}", "0".repeat(64)),
        "incarnation": format!("inc-{node}")})
}
impl Host {
    fn fails(&self, key: &str, who: &str) -> bool {
        self.spec.get("mailbox_fail").and_then(|m| text(m, key)) == Some(who)
    }
}
impl ObservationHost for Host {
    fn is_owner(&self) -> bool {
        text(&self.spec, "role").unwrap_or("owner") == "owner"
    }
    fn running(&self) -> bool {
        self.spec.get("running") != Some(&Value::Bool(false))
    }
    fn prepared_manifest_digest(&mut self) -> Result<String, ObservationError> {
        self.log.lock().unwrap().push("prepared_source".into());
        Ok(text(&self.spec, "app_digest")
            .unwrap_or(DEFAULT_DIGEST)
            .to_owned())
    }
    fn swarm_id(&self) -> String {
        "swarm-1".into()
    }
    fn member(&self, node_id: &str) -> Option<Value> {
        let known = self.spec.get("known").map_or_else(
            || vec!["n1".to_owned(), "n2".to_owned()],
            |k| strings(Some(k)),
        );
        known.iter().any(|k| k == node_id).then(|| member(node_id))
    }
    fn verify_descriptor(
        &mut self,
        card: &Value,
        member: &Value,
        swarm_id: &str,
        run_id: &str,
        deployment_id: &str,
    ) -> Result<Value, ObservationError> {
        self.log.lock().unwrap().push(format!(
            "verify:{}:{swarm_id}:{run_id}:{deployment_id}",
            text(member, "node_id").unwrap()
        ));
        if card.get("bad") == Some(&Value::Bool(true)) {
            return Err(ObservationError::new("descriptor invalid"));
        }
        Ok(json!({"manifest_digest": text(card, "digest").unwrap_or(DEFAULT_DIGEST)}))
    }
    fn session_factory(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        members: &[Value],
    ) -> Result<Box<dyn HelloSessions>, ObservationError> {
        let nodes: Vec<&str> = members
            .iter()
            .map(|m| text(m, "node_id").unwrap())
            .collect();
        let listing = nodes
            .iter()
            .map(|n| format!("'{n}'"))
            .collect::<Vec<_>>()
            .join(", ");
        {
            let mut log = self.log.lock().unwrap();
            log.push("owner_channel".into());
            log.push(format!("factory:{run_id}:{deployment_id}:[{listing}]"));
        }
        Ok(Box::new(Factory {
            log: self.log.clone(),
            run: run_id.to_owned(),
            dep: deployment_id.to_owned(),
            nodes: nodes.iter().map(|n| (*n).to_owned()).collect(),
            scripts: json!({}),
            binding_after: json!({}),
            scoped_open_log: false,
            fail_send: self.fail_send.clone(),
        }))
    }
    fn publish_offer(
        &mut self,
        member: &Value,
        run_id: &str,
        deployment_id: &str,
        archive: &[u8],
    ) -> Result<Offer, ObservationError> {
        let node = text(member, "node_id").unwrap();
        let digest: String = Sha256::digest(archive)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        self.log.lock().unwrap().push(format!(
            "publish_offer:{node}:{run_id}:{deployment_id}:{digest}"
        ));
        if self.fails("publish", node) {
            return Err(ObservationError::new("publish failed"));
        }
        Ok(Offer {
            offer_id: format!("offer-{node}"),
            raw: Value::Null,
        })
    }
    fn retire_offer(&mut self, offer: &Offer) -> Result<Value, ObservationError> {
        self.log
            .lock()
            .unwrap()
            .push(format!("retire_offer:{}", offer.offer_id));
        if self.fails("retire", &offer.offer_id) {
            return Err(ObservationError::new("retire failed"));
        }
        Ok(Value::from(format!("retired-{}", offer.offer_id)))
    }
    fn request_stage_cleanup(&mut self, offer: &Offer) -> Result<(), ObservationError> {
        self.log
            .lock()
            .unwrap()
            .push(format!("request_stage_cleanup:{}", offer.offer_id));
        if self.fails("cleanup", &offer.offer_id) {
            return Err(ObservationError::new("cleanup failed"));
        }
        Ok(())
    }
}

fn new_host(spec: &Value, log: &Log) -> Host {
    Host {
        log: log.clone(),
        spec: spec.clone(),
        fail_send: Arc::default(),
    }
}

#[test]
fn prepare_matches_the_oracle_ladder_order_and_published_offers() {
    let f = fixture();
    let mut checked = 0;
    for case in f["cases"].as_array().unwrap() {
        if case["kind"] != "prepare" {
            continue;
        }
        let spec = &case["spec"];
        let name = text(spec, "name").unwrap();
        let log: Log = Arc::default();
        let mut host = new_host(spec, &log);
        let got = match prepare_observation(
            &mut host,
            text(spec, "run_id").unwrap_or(RUN),
            text(spec, "deployment_id").unwrap_or(DEP),
            &spec["descriptors"],
            spec.get("ack") != Some(&Value::Null) && spec.get("ack") != Some(&json!(1)),
        ) {
            Ok(context) => {
                let offers: serde_json::Map<String, Value> = context
                    .offers
                    .iter()
                    .map(|(node, offer)| (node.clone(), Value::from(offer.offer_id.as_str())))
                    .collect();
                json!({"ok": {"run_id": context.run_id, "deployment_id": context.deployment_id,
                    "members": context.members.iter().map(|m| text(m, "node_id").unwrap()).collect::<Vec<_>>(),
                    "facts": context.participants(), "offers": offers}})
            }
            Err(error) => json!({"error": error.code()}),
        };
        let mut want = case["result"].clone();
        if let Some(object) = want.as_object_mut() {
            object.remove("class");
        }
        assert_eq!(got, want, "{name}: result");
        let calls: Vec<String> = case["calls"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c.as_str().unwrap().to_owned())
            .collect();
        assert_eq!(*log.lock().unwrap(), calls, "{name}: calls in order");
        checked += 1;
    }
    assert_eq!(checked, 20);
}

#[test]
fn collect_retire_and_cleanup_follow_the_oracle_on_a_prepared_context() {
    let f = fixture();
    let mut checked = 0;
    for case in f["cases"].as_array().unwrap() {
        if case["kind"] != "post" {
            continue;
        }
        let spec = &case["spec"];
        let name = text(spec, "name").unwrap();
        let log: Log = Arc::default();
        let mut host = new_host(spec, &log);
        let descriptors = json!([{"statement": {"target_node_id": "n1"}}, {"statement": {"target_node_id": "n2"}}]);
        let mut context = prepare_observation(&mut host, RUN, DEP, &descriptors, true).unwrap();
        log.lock().unwrap().clear();
        let mut outcomes = Vec::new();
        for step in strings(spec.get("steps")) {
            let value = match step.as_str() {
                "collect_ok" | "collect_fail" => {
                    *host.fail_send.lock().unwrap() = (step == "collect_fail").then(|| "n2".into());
                    let start = f["provenance"]["run_start_unix"].as_f64().unwrap();
                    let mut index = 0;
                    let mut clock = move || {
                        index += 1;
                        if index == 1 { start + 5.0 } else { start + 6.0 }
                    };
                    let counter = log.clone();
                    let mut token = move || {
                        let sends = counter
                            .lock()
                            .unwrap()
                            .iter()
                            .filter(|l| l.starts_with("send:"))
                            .count();
                        format!("{:016x}", sends + 1)
                    };
                    context.collect(&mut host, &mut clock, &mut token)
                }
                "retire" => context
                    .retire_bootstrap(&mut host)
                    .map(|rows| Value::Object(rows.into_iter().collect())),
                other => panic!("unknown step {other}"),
            };
            let mut row = outcome(value);
            row["step"] = Value::from(step.as_str());
            outcomes.push(row);
        }
        assert_eq!(Value::Array(outcomes), case["result"], "{name}: results");
        let calls: Vec<String> = case["calls"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c.as_str().unwrap().to_owned())
            .collect();
        assert_eq!(*log.lock().unwrap(), calls, "{name}: calls in order");
        assert_eq!(
            context.observations.is_some(),
            case["observations_set"].as_bool().unwrap(),
            "{name}: observations held"
        );
        checked += 1;
    }
    assert_eq!(checked, 8);
}

#[test]
fn the_bootstrap_archive_is_the_tar_python_writes() {
    // The sha256 the oracle's mailbox saw for run/deployment (`prepare_ok_one`) is the archive.
    let f = fixture();
    let case = f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["spec"]["name"] == "prepare_ok_one")
        .unwrap();
    let want = case["calls"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_str().unwrap())
        .find(|c| c.starts_with("publish_offer:"))
        .unwrap()
        .rsplit(':')
        .next()
        .unwrap()
        .to_owned();
    let archive = bootstrap_archive(RUN, DEP);
    assert_eq!(archive.len() % 10_240, 0, "tarfile pads to its record size");
    let got: String = Sha256::digest(&archive)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(got, want);
}
