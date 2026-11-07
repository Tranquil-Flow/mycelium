// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{ContractError, canonical, identity::Identity, token};
use serde_json::{Value, json};
fn signed(v: &Value, seed: u8, endpoint: &str) -> Result<Value, ContractError> {
    let who = Identity::from_secret(endpoint, &[seed; 32]).unwrap();
    Ok(
        json!({"statement":v,"signature":who.sign(v).map_err(|_|ContractError("invalid_signed_statement"))?}),
    )
}
fn admit(env: &Value, c: &Value) -> Result<token::AdmittedGrant, ContractError> {
    admit_with(&mut token::GrantRegistry::new(4096)?, env, c)
}
fn admit_with(
    registry: &mut token::GrantRegistry,
    env: &Value,
    c: &Value,
) -> Result<token::AdmittedGrant, ContractError> {
    let n = |k: &str| {
        c[k].as_u64()
            .ok_or(ContractError("token_path_grant_invalid"))
    };
    let f = |k: &str| {
        c[k].as_f64()
            .ok_or(ContractError("token_path_grant_invalid"))
    };
    let expected = token::GrantExpectation {
        scope: &c["expected_scope"],
        owner_key: &c["owner_key"],
        max_new_tokens: n("max_new_tokens")?,
        stop_token_ids: &c["stop_token_ids"],
        window: token::GrantWindow {
            now_unix_ms: n("now_unix_ms")?,
            now_monotonic_s: f("now_monotonic_s")?,
            process_deadline_monotonic_s: f("process_deadline_monotonic_s")?,
            lease_deadline_monotonic_s: f("lease_deadline_monotonic_s")?,
            clock_uncertainty_ms: n("clock_uncertainty_ms")?,
        },
    };
    registry.admit(env, &expected)
}
fn check(r: &Value, case: &Value, got: Result<(), ContractError>) {
    assert_eq!(
        got.is_ok(),
        case["accepted"].as_bool().unwrap(),
        "{} / {}: {got:?}, oracle={}",
        r["name"],
        case["name"],
        case["error"]
    );
    if let Some(e) = case["error"]
        .as_str()
        .filter(|e| e.starts_with("token_path_"))
    {
        assert_eq!(got.unwrap_err().0, e, "{} / {}", r["name"], case["name"]);
    }
}
#[test]
fn token_grants_and_receipts_are_byte_exact_and_match_oracle_refusals() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continuation-token-vectors.json")).unwrap();
    for r in rows {
        let receipt = r["kind"] == "token_receipt";
        let env = signed(
            &r["value"],
            if receipt { 18 } else { 17 },
            if receipt {
                "endpoint-entry"
            } else {
                "endpoint-owner"
            },
        )
        .unwrap();
        assert_eq!(
            canonical::to_bytes(&env).unwrap(),
            r["envelope_utf8"].as_str().unwrap().as_bytes()
        );
        let c = &r["context"];
        let validate = |v: &Value| -> Result<(), ContractError> {
            if receipt {
                let grant = admit(&c["grant"], &c["grant_context"])?;
                let mut cursor = token::ReceiptCursor::new(grant, c["entry_key"].clone());
                cursor.accept(&signed(v, 18, "endpoint-entry")?, || (10.0, true))?;
            } else {
                let grant = admit(&signed(v, 17, "endpoint-owner")?, c)?;
                assert_eq!(
                    grant.deadline_monotonic_s(),
                    r["deadline"].as_f64().unwrap()
                );
            }
            Ok(())
        };
        validate(&r["value"]).unwrap();
        for case in r["cases"].as_array().unwrap() {
            check(&r, case, validate(&case["value"]));
        }
        if !receipt {
            assert_eq!(
                admit(&env, c).unwrap().digest(),
                r["digest"].as_str().unwrap()
            );
            for case in r["context_cases"].as_array().unwrap() {
                check(&r, case, admit(&env, &case["context"]).map(|_| ()));
            }
        }
    }
}

fn rows() -> Vec<Value> {
    serde_json::from_str(include_str!("../fixtures/continuation-token-vectors.json")).unwrap()
}
#[test]
fn process_registry_refuses_replay_without_eviction_and_claim_is_once() {
    let r = rows().remove(0);
    let env = &r["envelope"];
    let c = &r["context"];
    let mut registry = token::GrantRegistry::new(1).unwrap();
    let mut bad = env.clone();
    bad["signature"]["signature"] = json!("bad");
    assert_eq!(
        admit_with(&mut registry, &bad, c).err().unwrap().0,
        "token_path_grant_signature"
    );
    let mut grant = admit_with(&mut registry, env, c).unwrap();
    grant.claim_execution().unwrap();
    assert_eq!(
        grant.claim_execution().unwrap_err().0,
        "token_path_grant_replay"
    );
    assert_eq!(
        admit_with(&mut registry, env, c).err().unwrap().0,
        "token_path_grant_replay"
    );
    let mut other = env["statement"].clone();
    other["scope"]["request_id"] = json!("request-2");
    let mut context = c.clone();
    context["expected_scope"] = other["scope"].clone();
    let other = signed(&other, 17, "endpoint-owner").unwrap();
    assert_eq!(
        admit_with(&mut registry, &other, &context).err().unwrap().0,
        "token_path_grant_capacity"
    );
    // No TTL pruning or capacity eviction erases the first replay fence.
    assert_eq!(
        admit_with(&mut registry, env, c).err().unwrap().0,
        "token_path_grant_replay"
    );
    assert!(token::GrantRegistry::new(0).is_err());
    assert!(token::GrantRegistry::new(4097).is_err());
}
#[test]
fn receipt_failures_never_advance_and_terminal_cannot_reverse() {
    let r = rows()
        .into_iter()
        .find(|r| r["name"] == "DECODING-2")
        .unwrap();
    let c = &r["context"];
    let grant = admit(&c["grant"], &c["grant_context"]).unwrap();
    let mut cursor = token::ReceiptCursor::new(grant, c["entry_key"].clone());
    let first = &r["envelope"];
    for (field, value, code) in [
        ("sequence", json!(1), "token_path_receipt_order"),
        ("start_index", json!(1), "token_path_receipt_order"),
        (
            "previous_receipt_digest",
            json!("wrong"),
            "token_path_receipt_chain",
        ),
        ("grant_digest", json!("wrong"), "token_path_receipt_subject"),
        ("token_ids", json!([2, 11]), "token_path_receipt_invalid"),
    ] {
        let mut b = r["value"].clone();
        b[field] = value;
        assert_eq!(
            cursor
                .accept(&signed(&b, 18, "endpoint-entry").unwrap(), || (10.0, true))
                .unwrap_err()
                .0,
            code
        );
    }
    assert_eq!(
        cursor.accept(first, || (10.0, false)).unwrap_err().0,
        "token_path_grant_stale"
    );
    for now in [90.0, 91.0, f64::NAN, f64::INFINITY, -1.0] {
        assert_eq!(
            cursor.accept(first, || (now, true)).unwrap_err().0,
            "token_path_grant_expired"
        );
    }
    let mut bad = first.clone();
    bad["signature"]["signer_endpoint_id"] = json!("wrong");
    assert_eq!(
        cursor
            .accept(&bad, || panic!(
                "authentication must precede currentness sampling"
            ))
            .unwrap_err()
            .0,
        "token_path_receipt_signature"
    );
    assert_eq!(
        cursor.accept(first, || (89.999, true)).unwrap(),
        vec![10, 11]
    );
    assert_eq!(
        cursor.accept(first, || (10.0, true)).unwrap_err().0,
        "token_path_receipt_order"
    );
    let mut end = r["value"].clone();
    end["sequence"] = json!(1);
    end["start_index"] = json!(2);
    end["end_index"] = json!(2);
    end["token_ids"] = json!([]);
    end["status"] = json!("CANCELLED");
    end["previous_receipt_digest"] = first["signature"]["signed_statement_digest"].clone();
    let terminal = signed(&end, 18, "endpoint-entry").unwrap();
    assert!(
        cursor
            .accept(&terminal, || (10.0, true))
            .unwrap()
            .is_empty()
    );
    assert_eq!(cursor.terminal(), Some("CANCELLED"));
    assert_eq!(
        cursor.accept(&terminal, || (10.0, true)).unwrap_err().0,
        "token_path_receipt_terminal"
    );
    assert_eq!(cursor.terminal(), Some("CANCELLED"));
}
#[test]
fn verified_progress_retains_exact_sealable_receipt_identity() {
    let r = rows()
        .into_iter()
        .find(|r| r["name"] == "DECODING-2")
        .unwrap();
    let c = &r["context"];
    let grant = admit(&c["grant"], &c["grant_context"]).unwrap();
    let mut cursor = token::ReceiptCursor::new(grant, c["entry_key"].clone());
    let mut frame = json!({"protocol":protocol::native::PROGRESS_PROTOCOL,"command_id":"command-1","node_id":"node-entry","receipt":r["envelope"]});
    // Routing is not authentication, and a foreign command cannot consume cursor state.
    let raw = canonical::to_bytes(&frame).unwrap();
    assert_eq!(
        cursor
            .accept_progress(&raw, "other-command", "node-entry", || panic!(
                "foreign routing must refuse first"
            ))
            .err()
            .unwrap()
            .0,
        "token_progress_frame_invalid"
    );
    assert_eq!(
        cursor
            .accept_progress(&raw, "command-1", "other-node", || panic!(
                "foreign routing must refuse first"
            ))
            .err()
            .unwrap()
            .0,
        "token_progress_frame_invalid"
    );
    // Duplicate keys refuse at the raw ingress, before serde Value could erase them.
    let duplicate = format!(
        "{{\"command_id\":\"foreign\",{}",
        std::str::from_utf8(&raw)
            .unwrap()
            .strip_prefix('{')
            .unwrap()
    );
    assert_eq!(
        cursor
            .accept_progress(duplicate.as_bytes(), "command-1", "node-entry", || panic!(
                "duplicate ingress"
            ))
            .err()
            .unwrap()
            .0,
        "token_progress_frame_invalid"
    );
    let receipt = cursor
        .accept_progress(&raw, "command-1", "node-entry", || (10.0, true))
        .unwrap();
    assert_eq!(receipt.sequence(), 0);
    assert_eq!(receipt.token_ids(), &[10, 11]);
    assert_eq!(receipt.status(), "DECODING");
    assert_eq!(receipt.error_code(), None);
    assert_eq!(
        receipt.receipt_digest(),
        r["envelope"]["signature"]["signed_statement_digest"]
            .as_str()
            .unwrap()
    );
    assert_eq!(
        receipt.envelope_bytes(),
        r["envelope_utf8"].as_str().unwrap().as_bytes()
    );
    frame["receipt"]["statement"]["token_ids"] = json!([999]);
    assert_eq!(receipt.token_ids(), &[10, 11]);
    assert_eq!(
        receipt.envelope_bytes(),
        r["envelope_utf8"].as_str().unwrap().as_bytes()
    );
    assert_eq!(
        cursor
            .accept_progress(&raw, "command-1", "node-entry", || (10.0, true))
            .err()
            .unwrap()
            .0,
        "token_path_receipt_order"
    );
}

#[test]
fn verified_receipt_terminal_metadata_matches_each_oracle_variant() {
    for r in rows().into_iter().filter(|r| r["kind"] == "token_receipt") {
        let c = &r["context"];
        let grant = admit(&c["grant"], &c["grant_context"]).unwrap();
        let mut cursor = token::ReceiptCursor::new(grant, c["entry_key"].clone());
        let got = cursor
            .accept_verified(&r["envelope"], || (10.0, true))
            .unwrap();
        assert_eq!(
            got.envelope_bytes(),
            r["envelope_utf8"].as_str().unwrap().as_bytes()
        );
        assert_eq!(got.status(), r["value"]["status"].as_str().unwrap());
        assert_eq!(got.error_code(), r["value"]["error_code"].as_str());
        assert_eq!(
            got.receipt_digest(),
            r["envelope"]["signature"]["signed_statement_digest"]
                .as_str()
                .unwrap()
        );
    }
}

#[test]
fn expiry_is_the_minimum_of_signed_process_and_membership_authority() {
    let r = rows().remove(0);
    for (field, value, expected) in [
        ("process_deadline_monotonic_s", json!(20.0), 20.0),
        ("lease_deadline_monotonic_s", json!(15.0), 15.0),
        ("now_unix_ms", json!(1090000), 19.0),
        ("clock_uncertainty_ms", json!(99999), 10.001),
    ] {
        let mut c = r["context"].clone();
        c[field] = value;
        assert_eq!(
            admit(&r["envelope"], &c).unwrap().deadline_monotonic_s(),
            expected
        );
    }
}
