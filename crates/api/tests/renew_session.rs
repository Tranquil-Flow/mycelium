// SPDX-License-Identifier: AGPL-3.0-only
//! The member heartbeat / lease-renewal half of `NodeMembershipSession` against the EXECUTED oracle
//! (FX-L10-RENEW-SESSION: `L10-evidence/r13-probe-session.py` -> `fixtures/oracle_renew_session.json`,
//! v0.1.4 f4e366e9, Python 3.14): the real session driven op by op with deterministic Ed25519 keys, a scripted clock
//! and renewals forged with the real `sign_membership_message` under a deterministic seed key. Every op's result
//! (the signed envelope BYTES or the exception class/module/code), the lease afterwards and the number of ids drawn
//! from the id source must be the Rust session's too.
use api::member::{MemberError, Session};
use protocol::identity::Identity;
use serde_json::Value;
use std::cell::Cell;

fn secret(hex: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap();
    }
    out
}
fn instant(value: &Value) -> f64 {
    match value.as_str() {
        Some("nan") => f64::NAN,
        Some("inf") => f64::INFINITY,
        Some("-inf") => f64::NEG_INFINITY,
        _ => value.as_f64().unwrap(),
    }
}
/// What the oracle raised, as `(class, module, code)`; the Rust error's closed identity.
fn identity_of(error: &MemberError) -> (&'static str, &'static str, String) {
    match error {
        MemberError::Membership(code) => (
            "NodeMembershipError",
            "mycelium_node.membership",
            code.clone(),
        ),
        MemberError::Contract(code) => (
            "MembershipContractError",
            "mycelium_membership.contracts",
            code.clone(),
        ),
        // `_segment` / the heartbeat shape check: a `ValueError` whose text is not a closed code.
        MemberError::Value => ("ValueError", "builtins", String::new()),
    }
}

#[test]
fn heartbeat_and_lease_renewal_equal_the_executed_oracle() {
    let doc: Value =
        serde_json::from_str(include_str!("fixtures/oracle_renew_session.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let signer = Identity::from_secret(
        doc["member_endpoint"].as_str().unwrap(),
        &secret(doc["member_secret"].as_str().unwrap()),
    )
    .unwrap();
    let seed_digest = doc["seed_key_digest"].as_str().unwrap();
    let cases = doc["cases"].as_array().unwrap();
    assert!(cases.len() >= 55, "{} cases", cases.len());
    let (mut ops_run, mut failures) = (0usize, Vec::new());
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let ids: Vec<String> = case["ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|i| i.as_str().unwrap().to_owned())
            .collect();
        let clock = Cell::new(1000.0f64);
        let drawn = Cell::new(0usize);
        let source = || {
            let id = ids[drawn.get()].clone();
            drawn.set(drawn.get() + 1);
            id
        };
        let now = || clock.get();
        let mut session = Session::new(
            doc["node_id"].as_str().unwrap(),
            doc["swarm_id"].as_str().unwrap(),
            doc["seed_node_id"].as_str().unwrap(),
            &signer,
            doc["incarnation"].as_str().unwrap(),
        );
        for (index, op) in case["ops"].as_array().unwrap().iter().enumerate() {
            ops_run += 1;
            let kind = op["op"].as_str().unwrap();
            let mut problems = Vec::new();
            let outcome: Result<Option<Value>, MemberError> = match kind {
                "forge_refused" => continue,
                "join_request" => {
                    clock.set(instant(&op["now"]));
                    drawn.set(1); // the join message id is the source's first draw; the API takes it explicitly
                    session
                        .join_request(
                            &signer,
                            instant(&op["now"]),
                            op["id"].as_str().unwrap(),
                            op["nonce"].as_str().unwrap(),
                            &["https://member-endpoint-r13.invalid/control".to_owned()],
                        )
                        .map(Some)
                }
                "accept_join" => session
                    .accept_join(&op["envelope"], instant(&op["now"]), seed_digest)
                    .map(Some),
                "heartbeat" => {
                    clock.set(instant(&op["now"]));
                    session
                        .heartbeat(
                            &signer,
                            &now,
                            &source,
                            op["lifecycle"].as_str().unwrap(),
                            op["active"].as_u64().unwrap(),
                        )
                        .map(Some)
                }
                "accept_renewal" => {
                    clock.set(instant(&op["now"]));
                    session
                        .accept_lease_renewal(&op["envelope"], &now, op["hb_id"].as_str().unwrap())
                        .map(Some)
                }
                other => panic!("unknown op {other}"),
            };
            match (&outcome, op.get("result"), op.get("error")) {
                (Ok(Some(got)), Some(want), None) if got == want => {}
                (Err(e), None, Some(want)) => {
                    let (class, module, code) = identity_of(e);
                    let named = want["code"].as_str().unwrap();
                    // A `ValueError` carries free text, not a closed code: class and module identify it.
                    let value_error = class == "ValueError";
                    if (class, module)
                        != (
                            want["type"].as_str().unwrap(),
                            want["module"].as_str().unwrap(),
                        )
                        || (!value_error && code != named)
                    {
                        problems.push(format!("raised {class}/{code} != {want}"));
                    }
                }
                (got, want, error) => problems.push(format!(
                    "outcome {got:?} vs result {want:?} / error {error:?}"
                )),
            }
            if let Some(lease) = op.get("lease") {
                let got = session.lease_expires_at().map(Value::from);
                if got.as_ref() != Some(lease) && !(lease.is_null() && got.is_none()) {
                    problems.push(format!("lease {got:?} != {lease}"));
                }
            }
            if let Some(draws) = op.get("draws")
                && drawn.get() as u64 != draws.as_u64().unwrap()
            {
                problems.push(format!("id draws {} != {draws}", drawn.get()));
            }
            if !problems.is_empty() {
                failures.push(format!(
                    "{label}#{index} ({kind}): {}",
                    problems.join(" | ")
                ));
            }
        }
    }
    assert!(ops_run >= 218, "{ops_run} ops");
    assert!(
        failures.is_empty(),
        "{} ops differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
