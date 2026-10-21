// SPDX-License-Identifier: AGPL-3.0-only
//! Phone stage-only node vs the real Python Router (golden vectors in
//! `contracts/phone-stage-wire-golden`, produced by running
//! `mycelium_router.router.Router` as the middle node): the Rust rules must
//! admit, refuse, position, forward, report and cancel exactly as Python does,
//! byte for byte where a frame is sent.

use mycelium_mobile_core::stage_wire::{HopLedger, LedgerHit, LockedPath, payload_fingerprint};
use mycelium_mobile_core::wire::{self, Activation};
use serde_json::Value;
use sha2::{Digest, Sha256};

const PHONE: &str = "node-phone";

/// `MYCELIUM_PHONE_WIRE_GOLDEN` points a cross-compiled binary at a pushed copy.
fn dir() -> std::path::PathBuf {
    std::env::var_os("MYCELIUM_PHONE_WIRE_GOLDEN")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contracts/phone-stage-wire-golden"))
}

fn index() -> Value {
    serde_json::from_str(&std::fs::read_to_string(dir().join("index.json")).unwrap()).unwrap()
}

fn raw(name: &Value) -> Vec<u8> {
    std::fs::read(dir().join(name.as_str().unwrap())).unwrap()
}

fn frame(name: &Value) -> wire::Frame {
    wire::decode_frame(&raw(name)).unwrap()
}

fn case<'a>(index: &'a Value, name: &str) -> &'a Value {
    index["cases"].as_array().unwrap().iter().find(|c| c["case"] == name).unwrap()
}

fn path(index: &Value) -> LockedPath {
    let locked = frame(&case(index, "manifest_locked")["input"]);
    assert_eq!(locked.message_type, "ManifestLocked");
    LockedPath::from_manifest_locked(&locked.body).unwrap()
}

#[test]
fn every_frame_matches_its_digest_and_reencodes_identically() {
    let index = index();
    for entry in index["frames"].as_array().unwrap() {
        let bytes = raw(&entry["file"]);
        assert_eq!(hex::encode(Sha256::digest(&bytes)), entry["frame_sha256"].as_str().unwrap());
        let f = wire::decode_frame(&bytes).unwrap();
        assert_eq!(wire::encode_frame(&f.message_type, &f.body, &f.payload).unwrap(), bytes, "{}", entry["file"]);
    }
}

#[test]
fn manifest_locked_yields_the_route_the_python_middle_node_registers() {
    let index = index();
    let e = &case(&index, "manifest_locked")["expect"];
    let p = path(&index);
    assert_eq!(p.request_id, e["request_id"].as_str().unwrap());
    assert_eq!(p.path_id, e["path_id"].as_str().unwrap());
    assert_eq!(p.path_attempt, e["path_attempt"].as_i64().unwrap());
    assert_eq!(p.topology_version, e["topology_version"].as_i64().unwrap());
    assert_eq!(p.entry_node_id, e["entry_node_id"].as_str().unwrap());
    assert_eq!(p.prompt_length as u64, e["prompt_length"].as_u64().unwrap());
    assert_eq!(p.hidden_size as u64, e["hidden_size"].as_u64().unwrap());
    let h = p.local_hop(PHONE).unwrap();
    assert_eq!(h as u64, e["local_hop_index"].as_u64().unwrap());
    assert_eq!(p.hops[h].placement_id, e["local_placement_id"].as_str().unwrap());
    assert_eq!(p.hops[h].runtime_backend, "mobile-rust-qwen2");
    assert_eq!(p.hops[h + 1].placement_id, e["next_placement_id"].as_str().unwrap());
    assert_eq!(p.hops[h + 1].node_id, e["next_node_id"].as_str().unwrap());
    assert_eq!(p.hops[h - 1].node_id, e["previous_node_id"].as_str().unwrap());
    // A stage-only phone is never the entry or the final placement.
    assert_eq!(p.local_hop("node-mac-entry").unwrap_err().0, "stage_only_node_not_middle");
    assert_eq!(p.local_hop("node-mac-final").unwrap_err().0, "stage_only_node_not_middle");
    assert_eq!(p.local_hop("node-stranger").unwrap_err().0, "destination_not_local");
}

#[test]
fn admitted_hops_run_the_python_work_item_and_forward_byte_identical_frames() {
    let index = index();
    let p = path(&index);
    for name in ["hop_prefill", "hop_decode"] {
        let c = case(&index, name);
        let inbound = frame(&c["input"]);
        assert_eq!(inbound.message_type, "HopHeader");
        let hop = p.admit(PHONE, &inbound.body, "node-mac-entry", &inbound.payload).unwrap();
        let w = &c["work_item"];
        assert_eq!(hop.idempotency_key, w["idempotency_key"].as_str().unwrap());
        assert_eq!(hop.position as u64, w["position"].as_u64().unwrap(), "{name}");
        assert_eq!(hop.placement_id, w["placement_id"].as_str().unwrap());
        assert_eq!(hop.phase, w["phase"].as_str().unwrap());
        assert_eq!(hop.token_index, w["token_index"].as_i64().unwrap());
        assert_eq!(hop.payload_fingerprint, c["input_payload_fingerprint"].as_str().unwrap());
        // The inbound activation is MYAC f32 (1, seq, hidden); complete-context
        // replay: seq = position + 1 on decode, the prompt on prefill.
        let act = Activation::decode(&inbound.payload).unwrap();
        assert_eq!(act.shape, vec![1, c["input_seq"].as_u64().unwrap() as u32, p.hidden_size as u32]);
        if hop.phase == "DECODE" {
            assert_eq!(hop.position + 1, act.shape[1] as usize);
        }
        // Forward: Python's exact frame, given the stage output payload.
        let expected = raw(&c["forward"]);
        let expected_frame = wire::decode_frame(&expected).unwrap();
        assert_eq!(hex::encode(Sha256::digest(&expected_frame.payload)), c["stage_output_payload_sha256"].as_str().unwrap());
        let (body, next) = p.forward_header(&hop, &inbound.body).unwrap();
        assert_eq!(next.node_id, c["forward_destination_node_id"].as_str().unwrap());
        let ours = wire::encode_frame("HopHeader", &body, &expected_frame.payload).unwrap();
        assert_eq!(ours, expected, "{name}: forwarded frame differs from the Python Router's");
    }
}

#[test]
fn a_stage_failure_originates_the_python_failure_report() {
    let index = index();
    let p = path(&index);
    let c = case(&index, "stage_failure");
    let inbound = frame(&c["input"]);
    let hop = p.admit(PHONE, &inbound.body, "node-mac-entry", &inbound.payload).unwrap();
    let body = p.failure_report(&hop, PHONE, "stage_runtime_failed");
    assert_eq!(wire::encode_frame("FailureReport", &body, b"").unwrap(), raw(&c["originate"]));
    assert_eq!(p.entry_node_id, c["originate_destination_node_id"].as_str().unwrap());
}

#[test]
fn only_the_entry_cancels_and_a_late_hop_after_cancellation_is_refused() {
    let index = index();
    let p = path(&index);
    let c = case(&index, "path_cancellation");
    let cancel = frame(&c["input"]);
    assert_eq!(cancel.message_type, "PathCancellation");
    assert!(p.cancellation_matches(&cancel.body, c["accepted_from_node_id"].as_str().unwrap()));
    assert!(!p.cancellation_matches(&cancel.body, "node-mac-final"));
    let mut stale = cancel.body.clone();
    stale["path_attempt"] = serde_json::json!(1);
    assert!(!p.cancellation_matches(&stale, "node-mac-entry"));
    // After cancellation the node forgets the path: Python answers unknown_path
    // and executes nothing. With no registered path there is nothing to admit.
    assert_eq!(c["late_hop_reason"], "unknown_path");
    assert_eq!(c["late_hop_executed"], 0);
}

#[test]
fn refusals_carry_the_python_codes_and_retries_hit_the_ledger() {
    let index = index();
    let p = path(&index);
    let c = case(&index, "rejections");
    for r in c["rejections"].as_array().unwrap() {
        let f = frame(&r["frame"]);
        let reason = r["reason"].as_str().unwrap();
        if reason == "unknown_path" {
            // The phone looks the path up by path_id first; another id is unknown.
            assert_ne!(f.body["path_id"], p.path_id.as_str());
        }
        let got = p.admit(PHONE, &f.body, r["source_node_id"].as_str().unwrap(), &f.payload).unwrap_err();
        assert_eq!(got.0, reason, "{}", r["frame"]);
    }
    let first = frame(&c["replay_first"]);
    let conflict = frame(&c["replay_conflict"]);
    let mut ledger: HopLedger<Vec<u8>> = HopLedger::new(4);
    let hop = p.admit(PHONE, &first.body, "node-mac-entry", &first.payload).unwrap();
    assert!(ledger.check(&hop).is_none());
    ledger.record(&hop, b"forwarded".to_vec());
    let retry = p.admit(PHONE, &first.body, "node-mac-entry", &first.payload).unwrap();
    assert_eq!(ledger.check(&retry), Some(LedgerHit::Cached(b"forwarded".to_vec())));
    let other = p.admit(PHONE, &conflict.body, "node-mac-entry", &conflict.payload).unwrap();
    assert_eq!(ledger.check(&other), Some(LedgerHit::Conflict));
    assert_eq!(c["replay_conflict_reason"], "idempotency_payload_mismatch");
    ledger.forget_path(&p.request_id, &p.path_id);
    assert!(ledger.is_empty());
    assert_eq!(payload_fingerprint(b""), {
        let mut h = Sha256::new();
        h.update(b"y\0\0\0\0\0\0\0\0");
        hex::encode(h.finalize())
    });
}
