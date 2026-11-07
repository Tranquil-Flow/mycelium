// SPDX-License-Identifier: AGPL-3.0-only
use control::*;
use protocol::identity::Identity;
use serde_json::{Value, json};
fn fixture() -> Value {
    serde_json::from_str::<Value>(include_str!("../../protocol/fixtures/managed-vectors.json"))
        .unwrap()[0]
        .clone()
}
fn binding() -> MemberBinding {
    MemberBinding {
        target_node_id: "node-member".into(),
        target_endpoint_id: "endpoint-member".into(),
        target_key_digest: format!("sha256:{}", "a".repeat(64)),
        membership_generation: "2".into(),
        member_incarnation: "incarnation-2".into(),
        run_id: "run-1".into(),
        deployment_id: "deployment-1".into(),
    }
}
fn context<'a>(f: &'a Value, b: &'a MemberBinding) -> ManagedContext<'a> {
    ManagedContext {
        protocol: "mycelium.managed_control.owner_command.v1",
        signer_key: &f["context"]["expected_key_record"],
        signer_endpoint: "endpoint-owner",
        swarm_id: "swarm-fixture",
        owner_node_id: "node-owner",
        binding: b,
    }
}

#[test]
fn wrong_generation_incarnation_audience_and_scope_refuse() {
    let f = fixture();
    let b = binding();
    let c = context(&f, &b);
    let limits = managed_time_limits().unwrap();
    let valid = authenticate_request(&f["envelope"], &c, 1000.0, limits).unwrap();
    assert_eq!(valid.statement(), &f["envelope"]["statement"]);
    for field in [
        "target_node_id",
        "target_endpoint_id",
        "target_key_digest",
        "membership_generation",
        "member_incarnation",
        "run_id",
        "deployment_id",
        "swarm_id",
        "owner_node_id",
    ] {
        let mut bad = f["envelope"].clone();
        bad["statement"][field] = if field == "membership_generation" {
            json!(3)
        } else if field == "target_key_digest" {
            json!(format!("sha256:{}", "b".repeat(64)))
        } else {
            json!("wrong")
        };
        let signer = Identity::from_secret("endpoint-owner", &[0x11; 32]).unwrap();
        bad["signature"] = signer.sign(&bad["statement"]).unwrap();
        let expected = match field {
            "swarm_id" | "owner_node_id" => "managed_control_authority_binding_invalid",
            "run_id" | "deployment_id" => "managed_control_run_binding_mismatch",
            _ => "managed_control_member_stale",
        };
        assert_eq!(
            authenticate_request(&bad, &c, 1000.0, limits)
                .unwrap_err()
                .0,
            expected,
            "{field}"
        );
    }
}

/// Deliberately only an in-memory adapter contract, NOT durable F2 qualification.
#[derive(Default, Clone)]
struct History {
    nonces: std::collections::BTreeSet<String>,
    requests: std::collections::BTreeSet<String>,
    cached: Vec<Value>,
    unavailable: bool,
}
impl ReplayStore for History {
    fn claim(&mut self, r: &AuthenticatedRequest) -> Result<ReplayOutcome, ReplayError> {
        if self.unavailable {
            return Err(ReplayError::HistoryUnavailable);
        }
        let s = r.statement();
        let nonce = s["nonce"].as_str().unwrap().to_owned();
        let id = s["request_id"].as_str().unwrap().to_owned();
        if self.cached.contains(s) {
            return Ok(ReplayOutcome::CachedResponse(b"receipt".to_vec()));
        }
        if self.nonces.contains(&nonce) || self.requests.contains(&id) {
            return Err(ReplayError::Duplicate);
        }
        self.nonces.insert(nonce);
        self.requests.insert(id);
        self.cached.push(s.clone());
        Ok(ReplayOutcome::FirstSeen)
    }
}
#[test]
fn f2_replay_seam_retains_history_after_response_prune() {
    let f = fixture();
    let b = binding();
    let c = context(&f, &b);
    let limits = managed_time_limits().unwrap();
    let request = authenticate_request(&f["envelope"], &c, 1000.0, limits).unwrap();
    let mut history = History::default();
    assert_eq!(history.claim(&request), Ok(ReplayOutcome::FirstSeen));
    assert_eq!(
        history.claim(&request),
        Ok(ReplayOutcome::CachedResponse(b"receipt".to_vec()))
    );
    history.cached.clear();
    let mut reopened = history.clone();
    assert_eq!(reopened.claim(&request), Err(ReplayError::Duplicate));
    let mut newer = f["envelope"].clone();
    newer["statement"]["sequence"] = 2.into();
    newer["signature"] = Identity::from_secret("endpoint-owner", &[0x11; 32])
        .unwrap()
        .sign(&newer["statement"])
        .unwrap();
    let request = authenticate_request(&newer, &c, 1000.0, limits).unwrap();
    assert_eq!(reopened.claim(&request), Err(ReplayError::Duplicate));
    reopened.unavailable = true;
    assert_eq!(
        reopened.claim(&request),
        Err(ReplayError::HistoryUnavailable)
    );
}
