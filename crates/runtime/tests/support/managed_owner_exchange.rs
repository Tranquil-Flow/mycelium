// SPDX-License-Identifier: AGPL-3.0-only
//! Controlled signed IO, NOT a native worker or server mailbox.
use super::*;
use budgets::{ClockDomain, MonoInstant, Span};
use runtime::managed_owner::{OwnerError, OwnerHost};
use std::{collections::VecDeque, sync::Mutex};
struct Cell<T>(Mutex<T>);
impl<T: Copy> Cell<T> {
    fn new(value: T) -> Self {
        Self(Mutex::new(value))
    }
    fn get(&self) -> T {
        *self.0.lock().unwrap()
    }
    fn set(&self, value: T) {
        *self.0.lock().unwrap() = value;
    }
}
#[path = "managed_owner_sessions.rs"]
mod sessions;
struct Host {
    signer: Identity,
    root: PathBuf,
    wall: Cell<f64>,
    mono: Cell<u64>,
    nonce: u64,
    calls: Vec<Value>,
    lose: bool,
    fail_result: bool,
    lose_old: bool,
    states: VecDeque<&'static str>,
    revoke: bool,
    advance_after: Option<(&'static str, bool)>,
    forged: bool,
    member: Value,
    member_error: Option<&'static str>,
    member_reads: Vec<String>,
}
impl Host {
    fn new(r: &Root) -> Self {
        Self {
            signer: signer(),
            root: r.0.clone(),
            wall: Cell::new(1000.0),
            mono: Cell::new(100_000),
            nonce: 0,
            calls: Vec::new(),
            lose: false,
            fail_result: false,
            lose_old: false,
            states: VecDeque::new(),
            revoke: false,
            advance_after: None,
            forged: false,
            member_error: None,
            member_reads: Vec::new(),
            member: json!({"node_id":"member","endpoint_id":"member-endpoint","verification_key_digest":format!("sha256:{}","a".repeat(64)),"generation":1,"incarnation":"inc-1","lifecycle_state":"RUNNING","peer_class":"mac_mlx_iroh","lease_expires_at":1000000.0}),
        }
    }
    fn run(&mut self, o: &mut ManagedControlOwner) -> Result<Value, OwnerError> {
        o.exchange(self, &mut binding(), &command(), deadline())
    }
}
fn deadline() -> MonoInstant {
    MonoInstant::new(ClockDomain(1), 130_000)
}
impl transport::Clock for Host {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(1), self.mono.get())
    }
    fn wall_seconds(&self) -> f64 {
        self.wall.get()
    }
}
impl OwnerHost for Host {
    fn signer(&self) -> &Identity {
        &self.signer
    }
    fn current_member(&mut self, node: &str) -> Result<Value, OwnerError> {
        self.member_reads.push(node.to_owned());
        match self.member_error {
            Some(code) => Err(OwnerError(code)),
            None => Ok(self.member.clone()),
        }
    }
    fn nonce(&mut self) -> Result<String, OwnerError> {
        self.nonce += 1;
        Ok(format!("{:032x}", self.nonce))
    }
    fn wait(&mut self, span: Span, _: MonoInstant) -> Result<(), OwnerError> {
        self.mono.set(self.mono.get() + span.millis());
        Ok(())
    }
    fn exchange(&mut self, p: &Value, _: MonoInstant) -> Result<Value, OwnerError> {
        self.calls.push(p.clone());
        let s = &p["envelope"]["statement"];
        let enqueue = p["operation"] == "enqueue";
        if !enqueue && self.fail_result {
            return Err(OwnerError("managed_control_signature_invalid"));
        }
        if enqueue {
            let saved: Value = serde_json::from_slice(
                &fs::read(self.root.join("owner.json")).expect("intent before IO"),
            )
            .unwrap();
            assert_eq!(
                saved["commands"][s["native_command"]["command_id"].as_str().unwrap()]["enqueue_request"],
                *p,
                "exact signed intent must be durable before IO"
            );
            if self.lose || (self.lose_old && s["native_command"]["command_id"] == "old") {
                return Err(OwnerError("controlled_response_lost"));
            }
        }
        let state = if enqueue {
            "pending"
        } else if self.lose_old && s["native_command"]["command_id"] == "old" {
            "not_found"
        } else {
            self.states.pop_front().unwrap_or("completed")
        };
        let mut body = binding();
        for k in [
            "target_node_id",
            "target_endpoint_id",
            "target_key_digest",
            "membership_generation",
            "member_incarnation",
            "run_id",
            "deployment_id",
        ] {
            body[k] = s[k].clone();
        }
        body.as_object_mut().unwrap().extend(json!({"protocol":if enqueue {"mycelium.managed_control.enqueue_receipt.v1"}else{"mycelium.managed_control.result_receipt.v1"},"request_id":s["request_id"],"swarm_id":"swarm","owner_node_id":"owner","owner_endpoint_id":"owner-endpoint","issued_at":self.wall.get(),"expires_at":self.wall.get()+60.0,"command_id":s["native_command"]["command_id"],"command_digest":protocol::canonical::sha256_prefixed(&protocol::canonical::to_bytes(&s["native_command"]).unwrap()),"state":state}).as_object().unwrap().clone());
        if !enqueue {
            let native = if state == "completed" {
                json!({"protocol":protocol::native::CONTROL_PROTOCOL,"command_id":s["native_command"]["command_id"],"node_id":"member","ok":true,"route_ready":false,"result":{}})
            } else {
                Value::Null
            };
            body["native_response_digest"] = if native.is_null() {
                Value::Null
            } else {
                json!(protocol::canonical::sha256_prefixed(
                    &protocol::canonical::to_bytes(&native).unwrap()
                ))
            };
            body["native_response"] = native;
            if self.revoke {
                self.member["generation"] = json!(2);
                self.member["lifecycle_state"] = json!("STOPPED");
            }
        }
        if let Some((operation, live)) = self.advance_after
            && p["operation"] == operation
        {
            self.member["generation"] = json!(2);
            self.member["incarnation"] = json!("inc-2");
            self.member["lifecycle_state"] = json!(if live { "RUNNING" } else { "STOPPED" });
        }
        let signature = self.signer.sign(&body).unwrap();
        if self.forged {
            body["request_id"] = json!("forged");
        }
        Ok(
            json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"statement":body,"signature":signature,"verification_key":self.signer.public_key_record()}),
        )
    }
}
#[test]
fn uncertain_enqueue_restarts_with_exact_saved_signature_then_only_results() {
    let r = Root::new();
    let mut o = open(&r);
    let mut h = Host::new(&r);
    h.lose = true;
    assert_eq!(
        h.run(&mut o).unwrap_err().0,
        case("uncertain_enqueue")["outcome"]["error"]
            .as_str()
            .unwrap()
    );
    assert_eq!(disk(&r), case("uncertain_enqueue")["journal"]);
    let first = h.calls[0].clone();
    drop(o);
    let mut o = open(&r);
    h.lose = false;
    assert_eq!(h.run(&mut o).unwrap()["ok"], true);
    assert_eq!(h.calls[1], first);
    assert_eq!(disk(&r), case("restart_same_bytes")["journal"]);
    h.calls.clear();
    assert_eq!(h.run(&mut o).unwrap()["ok"], true);
    assert!(h.calls.iter().all(|v| v["operation"] == "result"));
    assert_eq!(disk(&r)["write_intents"], 1);
}
#[test]
fn expired_uncertain_intent_is_result_only_and_not_found_never_means_unexecuted() {
    for state in [
        "not_found",
        "pending",
        "claimed",
        "expired",
        "reconciliation_required",
    ] {
        let r = Root::new();
        let mut o = open(&r);
        let mut h = Host::new(&r);
        h.lose = true;
        let expected = case(&format!("expired_{state}"));
        h.nonce = u64::from_str_radix(
            expected["journal"]["commands"]["hello-1"]["enqueue_request"]["envelope"]["statement"]
                ["nonce"]
                .as_str()
                .unwrap(),
            16,
        )
        .unwrap()
            - 1;
        assert!(h.run(&mut o).is_err());
        h.calls.clear();
        h.wall.set(2000.0);
        h.states.push_back(state);
        let outcome = h.run(&mut o);
        if state == "not_found" {
            assert_eq!(
                outcome.unwrap_err().0,
                expected["outcome"]["error"].as_str().unwrap()
            );
        } else {
            assert_eq!(outcome.unwrap()["ok"], true);
        }
        assert!(h.calls.iter().all(|v| v["operation"] == "result"));
        let sequences: Vec<Value> = h
            .calls
            .iter()
            .map(|v| v["envelope"]["statement"]["sequence"].clone())
            .collect();
        assert_eq!(json!(sequences), expected["result_sequences"]);
        assert_eq!(disk(&r), expected["journal"]);
    }
}
#[test]
fn unpersisted_intent_never_reaches_io_and_receipt_signature_is_required() {
    let r = Root::new();
    let mut o = open(&r);
    let mut h = Host::new(&r);
    fs::rename(r.0.join("owner.json"), r.0.join("saved")).unwrap();
    fs::create_dir(r.0.join("owner.json")).unwrap();
    assert_eq!(
        h.run(&mut o).unwrap_err().0,
        "managed_owner_state_unavailable"
    );
    assert!(h.calls.is_empty());
    let r = Root::new();
    let mut o = open(&r);
    let mut h = Host::new(&r);
    h.forged = true;
    assert_eq!(
        h.run(&mut o).unwrap_err().0,
        "managed_control_reconciliation_required"
    );
    assert_eq!(disk(&r)["commands"]["hello-1"]["enqueue_confirmed"], false);
}
#[test]
fn revoked_member_fences_native_response_publication() {
    let r = Root::new();
    let mut o = open(&r);
    let mut h = Host::new(&r);
    h.revoke = true;
    assert_eq!(h.run(&mut o).unwrap_err().0, "managed_control_member_stale");
    assert_eq!(
        h.calls.len(),
        2,
        "reached real signed result before revocation"
    );
}

fn extra(name: &str) -> Value {
    let f: Value = serde_json::from_str(include_str!(
        "../fixtures/managed-owner-reconcile-oracle.json"
    ))
    .unwrap();
    f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == name)
        .unwrap()
        .clone()
}
#[test]
fn other_intents_reconcile_before_new_work_and_retire_exact_source_cases() {
    for name in ["fresh_same_run", "expired_same_run", "fresh_other_run"] {
        let expected = extra(name);
        let r = Root::new();
        let mut o = open(&r);
        let mut h = Host::new(&r);
        h.lose_old = true;
        let prepared = &expected["initial"]["commands"]["old"]["enqueue_request"];
        h.nonce = u64::from_str_radix(
            prepared["envelope"]["statement"]["nonce"].as_str().unwrap(),
            16,
        )
        .unwrap()
            - 1;
        let mut old_binding = expected["initial"]["commands"]["old"]["binding"].clone();
        let old_command = prepared["envelope"]["statement"]["native_command"].clone();
        assert!(
            o.exchange(&mut h, &mut old_binding, &old_command, deadline())
                .is_err()
        );
        assert_eq!(disk(&r), expected["initial"]);
        if name == "expired_same_run" {
            h.wall.set(2000.0);
        }
        let mut next = command();
        next["command_id"] = json!("new");
        next["command"] = json!("health");
        let result = o.exchange(&mut h, &mut binding(), &next, deadline());
        if name == "fresh_same_run" {
            assert_eq!(
                result.unwrap_err().0,
                "managed_control_reconciliation_required"
            );
        } else {
            assert_eq!(result.unwrap()["ok"], true);
        }
        let trace: Vec<Value> = h
            .calls
            .iter()
            .map(|v| {
                json!([
                    v["operation"],
                    v["envelope"]["statement"]["native_command"]["command_id"],
                    v["envelope"]["statement"]["sequence"]
                ])
            })
            .collect();
        assert_eq!(json!(trace), expected["calls"]);
        assert_eq!(disk(&r), expected["journal"]);
        if !expected["retired"].is_null() {
            let actual: Value = serde_json::from_slice(
                &fs::read(r.0.join("retired-intents.private.json")).unwrap(),
            )
            .unwrap();
            assert_eq!(actual, expected["retired"]);
        }
    }
}
#[test]
fn legacy_known_command_queries_without_inventing_old_signed_request() {
    let r = Root::new();
    drop(open(&r));
    let mut old = disk(&r);
    old["protocol"] = json!("mycelium.a13.owner_control_journal.v1");
    old["sequence"] = json!(9);
    old.as_object_mut().unwrap().remove("sequence_ceiling");
    old.as_object_mut().unwrap().remove("write_intents");
    let intent = &case("uncertain_enqueue")["journal"]["commands"]["hello-1"];
    old["commands"] = json!({"hello-1":{"binding":intent["binding"],"digest":intent["digest"]}});
    fs::write(
        r.0.join("owner.json"),
        protocol::canonical::to_bytes(&old).unwrap(),
    )
    .unwrap();
    let mut o = open(&r);
    let mut h = Host::new(&r);
    assert_eq!(h.run(&mut o).unwrap()["ok"], true);
    assert_eq!(h.calls.len(), 1);
    assert_eq!(h.calls[0]["operation"], "result");
    assert_eq!(disk(&r), extra("legacy_known_command")["journal"]);
}
#[test]
fn readonly_polling_crosses_sequence_blocks_without_consuming_write_quota() {
    let expected = extra("readonly_poll_quota");
    let r = Root::new();
    let mut limits = Limits::registered().unwrap();
    limits.max_commands = 2;
    limits.max_requests = 2;
    let mut o = ManagedControlOwner::open(&r.0, &signer(), "swarm", "owner", limits).unwrap();
    let mut h = Host::new(&r);
    let prep = &expected["journal"]["commands"]["poll"]["enqueue_request"]["envelope"]["statement"];
    h.nonce = u64::from_str_radix(prep["nonce"].as_str().unwrap(), 16).unwrap() - 1;
    h.states = std::iter::repeat_n("pending", 130).collect();
    let mut cmd = command();
    cmd["command_id"] = json!("poll");
    assert_eq!(
        o.exchange(&mut h, &mut binding(), &cmd, deadline())
            .unwrap()["ok"],
        true
    );
    assert_eq!(
        h.calls
            .iter()
            .filter(|c| c["operation"] == "result")
            .count() as u64,
        expected["polls"].as_u64().unwrap()
    );
    assert_eq!(disk(&r), expected["journal"]);
}

#[test]
fn live_advance_and_revocation_are_distinct_at_all_three_resolution_seams() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../fixtures/managed-owner-binding-oracle.json"
    ))
    .unwrap();
    for expected in fixture["cases"].as_array().unwrap() {
        let name = expected["name"].as_str().unwrap();
        let live = name.ends_with("_live");
        let r = Root::new();
        let mut owner = open(&r);
        let mut host = Host::new(&r);
        let mut admitted = binding();
        if let Some(nonce)=expected["journal"]["commands"]["hello-1"]["enqueue_request"]["envelope"]["statement"]["nonce"].as_str(){host.nonce=u64::from_str_radix(nonce,16).unwrap()-1;}
        if name.starts_with("initial") {
            host.member["generation"] = json!(2);
            host.member["incarnation"] = json!("inc-2");
            host.member["lifecycle_state"] = json!(if live { "RUNNING" } else { "STOPPED" });
        } else {
            host.advance_after = Some((
                if name.starts_with("before_result") {
                    "enqueue"
                } else {
                    "result"
                },
                live,
            ));
        }
        let response = owner.exchange(&mut host, &mut admitted, &command(), deadline());
        if live {
            assert_eq!(response.unwrap()["ok"], true);
        } else {
            assert_eq!(response.unwrap_err().0, expected["outcome"]["error"]);
        }
        assert_eq!(admitted, expected["binding"]);
        assert_eq!(disk(&r), expected["journal"]);
        let calls: Vec<Value> = host
            .calls
            .iter()
            .map(|p| json!([p["operation"], p["envelope"]["statement"]["sequence"]]))
            .collect();
        assert_eq!(json!(calls), expected["calls"]);
    }
}

#[test]
fn same_signer_foreign_scope_prepared_request_never_reaches_io() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../fixtures/managed-owner-authority-oracle.json"
    ))
    .unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let r = Root::new();
        drop(open(&r));
        fs::write(
            r.0.join("owner.json"),
            protocol::canonical::to_bytes(&case["journal"]).unwrap(),
        )
        .unwrap();
        let mut owner = open(&r);
        let mut host = Host::new(&r);
        let answer = host.run(&mut owner);
        assert!(
            host.calls.is_empty(),
            "signed {} from another authority must be refused BEFORE IO",
            case["field"]
        );
        assert_eq!(answer.unwrap_err().0, case["outcome"]["error"]);
        assert_eq!(disk(&r), case["journal"]);
    }
}
