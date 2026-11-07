// SPDX-License-Identifier: AGPL-3.0-only
// O:contract:mycelium.membership.signed_message.v1; unregistered managed progress seam.
use protocol::{ContractError, TimeLimits, canonical, progress};
use serde_json::{Value, json};
fn vectors() -> Vec<Value> {
    serde_json::from_str(include_str!("../fixtures/f4-progress-vectors.json")).unwrap()
}
fn verify(row: &Value, env: &Value, now: f64) -> Result<progress::VerifiedDecision, ContractError> {
    let c = &row["context"];
    progress::verify_response(
        env,
        &progress::ExpectedProgress {
            owner_key: &c["key"],
            request_id: c["request_id"].as_str().unwrap(),
            swarm_id: c["swarm_id"].as_str().unwrap(),
            owner_node_id: c["owner_node_id"].as_str().unwrap(),
            binding: &c["binding"],
            command_id: c["command_id"].as_str().unwrap(),
            receipt: &c["receipt"],
            now,
        },
        TimeLimits {
            max_ttl_seconds: 300.0,
            clock_skew_seconds: 30.0,
        },
    )
}
#[test]
fn signed_progress_ack_and_retirement_match_external_positive_negative_vectors() {
    for row in vectors() {
        let decision = verify(&row, &row["value"], 1000.0).unwrap();
        assert_eq!(
            decision.envelope_bytes(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        assert_eq!(decision.is_cancelled(), row["name"] == progress::CANCELLED);
        for case in row["cases"].as_array().unwrap() {
            let got = verify(&row, &case["value"], 1000.0);
            assert_eq!(
                got.is_ok(),
                case["accepted"].as_bool().unwrap(),
                "{} / {}: {got:?}",
                row["name"],
                case["name"]
            );
            if let Some(code) = case["error"]
                .as_str()
                .filter(|s| s.starts_with("managed_control_"))
            {
                assert_eq!(got.unwrap_err().0, code, "{}", case["name"]);
            }
        }
        for case in row["time_cases"].as_array().unwrap() {
            assert_eq!(
                verify(&row, &row["value"], case["now"].as_f64().unwrap()).is_ok(),
                case["accepted"].as_bool().unwrap()
            );
        }
    }
}
#[test]
fn forwarding_retirement_preserves_prefix_without_native_ack_or_terminal() {
    let traces: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/f4-progress-traces.json")).unwrap();
    for trace in traces {
        let row = vectors()
            .into_iter()
            .find(|r| r["name"] == trace["protocol"])
            .unwrap();
        let mut pending =
            progress::PendingForward::new("command-1", &row["context"]["receipt"]).unwrap();
        let decision = verify(&row, &row["value"], 1000.0).unwrap();
        let mut persisted = None;
        pending
            .record(decision, |v| {
                persisted = Some(v.clone());
                Ok(())
            })
            .unwrap();
        assert_eq!(pending.snapshot(), &trace["row"]);
        assert_eq!(persisted.as_ref(), Some(&trace["row"]));
        assert_eq!(
            pending.ready_to_ack(trace["phase"].as_str()),
            trace["ready_to_ack"].as_bool().unwrap()
        );
        assert_eq!(
            pending.native_ack().is_none(),
            trace["protocol"] == progress::CANCELLED
        );
        assert_eq!(pending.snapshot()["terminal"], false);
        assert_eq!(pending.snapshot()["pending"], row["context"]["receipt"]);
        // A crash after row persistence but before outbound clear repeats the
        // same signed decision. It must not strand cleanup delivery.
        let retry = verify(&row, &row["value"], 1000.0).unwrap();
        pending.record(retry, |_| Ok(())).unwrap();
        assert_eq!(pending.snapshot(), &trace["row"]);
        // No completion/cleanup success is synthesized from a stale native refusal.
        let refusal = json!({"protocol": "mycelium.physical_node_control.v1", "command_id":"command-1", "node_id":"node-entry", "ok":false, "route_ready":false, "error":{"code":"stale_infer_decode_generation"}});
        protocol::native::validate_response(&refusal, "command-1", "node-entry", "failed").unwrap();
        assert!(
            protocol::native::validate_response(&refusal, "command-1", "node-entry", "completed")
                .is_err()
        );
    }
}
#[test]
fn refusal_and_failed_persistence_cannot_retire_or_discard_pending_receipt() {
    let row = vectors().pop().unwrap();
    let receipt = &row["context"]["receipt"];
    let mut pending = progress::PendingForward::new("command-1", receipt).unwrap();
    let before = pending.snapshot().clone();
    for refusal in [
        json!({"error":"token_progress_disabled"}),
        json!({"error":"stale_infer_decode_generation"}),
    ] {
        assert!(verify(&row, &refusal, 1000.0).is_err());
        assert_eq!(pending.snapshot(), &before);
    }
    let decision = verify(&row, &row["value"], 1000.0).unwrap();
    assert_eq!(
        pending
            .record(decision, |_| Err(ContractError("persist_failed")))
            .unwrap_err()
            .0,
        "persist_failed"
    );
    assert_eq!(pending.snapshot(), &before);
    let mut wrong = receipt.clone();
    wrong["statement"]["sequence"] = json!(99);
    let mut other = progress::PendingForward::new("command-1", &wrong).unwrap();
    let decision = verify(&row, &row["value"], 1000.0).unwrap();
    assert_eq!(
        other
            .record(decision, |_| panic!("mismatch must not persist"))
            .unwrap_err()
            .0,
        "token_progress_state_invalid"
    );
}
#[test]
fn owner_cancellation_subject_requires_exact_claim_and_never_accepts_receipts() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/f4-owner-retirement-vectors.json")).unwrap();
    for case in cases {
        let v = &case["value"];
        let mut command: Value =
            serde_json::from_str(v["claim"]["envelope_json"].as_str().unwrap()).unwrap();
        command = command["statement"]["native_command"].take();
        let got = progress::retirement_subject(
            &progress::RetirementContext {
                cancelled: v["cancelled"].as_bool().unwrap(),
                closed: v["closed"].as_bool().unwrap(),
                command_id: "command-1",
                grant_digest: v["grant_digest"].as_str().unwrap(),
                target_endpoint_id: v["target_endpoint_id"].as_str().unwrap(),
                claim: &v["claim"],
                native_command: &command,
            },
            &v["statement"],
        );
        assert_eq!(
            got.is_ok(),
            case["accepted"].as_bool().unwrap(),
            "{}: {got:?}",
            case["name"]
        );
        if case["accepted"] == true {
            assert_eq!(
                got.unwrap().unwrap_or(Value::Null),
                case["output"],
                "{}",
                case["name"]
            );
        } else if let Some(code) = case["error"].as_str() {
            assert_eq!(got.unwrap_err().0, code, "{}", case["name"]);
        }
    }
}
#[test]
fn raw_progress_boundary_rejects_duplicate_keys_and_size_overflow() {
    let row = vectors().pop().unwrap();
    let raw = canonical::to_bytes(&row["value"]).unwrap();
    let parsed = progress::parse_response(&raw).unwrap();
    assert!(verify(&row, &parsed, 1000.0).unwrap().is_cancelled());
    assert!(progress::parse_response(b"{\"statement\":{},\"statement\":{}}").is_err());
    assert!(progress::parse_response(&vec![b' '; protocol::managed::MAX_FRAME_BYTES + 1]).is_err());
}
