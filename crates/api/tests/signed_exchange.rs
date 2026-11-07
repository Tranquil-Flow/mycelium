// SPDX-License-Identifier: AGPL-3.0-only
use api::{
    run_exchange::{Announce, RunExchange},
    run_exchange_routes::*,
};
use protocol::{TimeLimits, identity::Identity, managed};
use serde_json::{Value, json};
use std::io::Read;
fn identity() -> Identity {
    let mut key = [0_u8; 32];
    std::fs::File::open("/dev/urandom")
        .unwrap()
        .read_exact(&mut key)
        .unwrap();
    Identity::from_secret("owner-fresh", &key).unwrap()
}
/// Proves the signed demo-run slice of B-AUTH-01; F-AUTH-16 is exercised by the retained skew mutant.
#[test]
fn real_signed_report_routes_match_executed_seed_mailbox_oracle() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_signed_exchange.json")).unwrap();
    let rows = fixture["rows"].as_array().unwrap();
    let now = rows[0]["now"].as_f64().unwrap();
    let exchange = RunExchange::new(Box::new(move || now));
    let node = rows[0]["member"]["node_id"].as_str().unwrap();
    exchange.announce(Announce { phase:"collect".into(),run_id:Some("run-1".into()),deployment_id:Some("dep-1".into()),per_node:json!({node:{"bootstrap_offer_id":"offer-one"},"other-node":{"bootstrap_offer_id":"offer-secret"}}).as_object().unwrap().clone(),..Default::default() }).unwrap();
    let signer = identity();
    let observed = std::cell::RefCell::new(Vec::new());
    let observe =
        |n: &str, t: f64, r: Option<f64>| observed.borrow_mut().push((n.to_owned(), t, r));
    let routes = SignedRoutes {
        exchange: &exchange,
        signer: &signer,
        swarm_id: fixture["swarm_id"].as_str().unwrap(),
        owner_node_id: fixture["owner_node_id"].as_str().unwrap(),
        observe_clock: &observe,
        response_ttl: budgets::Span::from_millis(60_000),
    };
    for row in rows {
        let result = routes.dispatch_guarded(
            row["path"].as_str().unwrap(),
            &row["envelope"],
            &row["member"],
            now,
        );
        let got = match result {
            Ok(envelope) => {
                let mut statement = managed::verify(
                    &envelope,
                    RESPONSE,
                    &signer.public_key_record(),
                    signer.endpoint_id(),
                    now,
                    TimeLimits {
                        max_ttl_seconds: 300.0,
                        clock_skew_seconds: 30.0,
                    },
                )
                .unwrap();
                statement["announcement"]
                    .as_object_mut()
                    .unwrap()
                    .remove("owner_incarnation");
                // Owner identity is freshly generated in Rust; the callback must use that borrowed signer.
                assert_eq!(statement["owner_endpoint_id"], signer.endpoint_id());
                statement["owner_endpoint_id"] = row["expected"]["ok"]["owner_endpoint_id"].clone();
                json!({"ok":statement})
            }
            Err(e) => json!({"error":e.0}),
        };
        assert_eq!(got, row["expected"], "{}", row["name"]);
        assert_eq!(exchange.reports(), row["reports"], "{}", row["name"]);
    }
}
