// SPDX-License-Identifier: AGPL-3.0-only
//! Source-specific managed exchange RESPONSE allowance, not a request or input cap.
use budgets::table::{Provenance, Purpose, Value};
use budgets::{BudgetId, Entries};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
const FX: &str = include_str!("../data/fx-l1b-r17-control-response.tsv");
const PATH: &str = "mycelium_a13_macos/managed_control.py";
#[test]
fn response_allowance_is_named_typed_and_distinct_from_request_ttl() {
    let id = BudgetId::from_name("ManagedControlExchangeResponseTtl")
        .expect("missing source-specific response allowance");
    assert_eq!(id.span(Entries::new(0)).unwrap().millis(), 60_000);
    assert_eq!(id.entry().purpose, Purpose::Policy);
    assert!(matches!(id.entry().value, Value::Duration(_)));
    assert!(
        matches!(id.entry().provenance,Provenance::Source(p) if p=="mycelium_a13_macos/managed_control.py:1006 @ f4e366e9; _response initializer min(60.0, MAX_TTL_SECONDS); fields override is outside this allowance; not an external-envelope ceiling or measured p99")
    );
    assert_ne!(id, BudgetId::ControlTtl);
    assert_ne!(
        id.span(Entries::new(0)),
        BudgetId::ControlTtl.span(Entries::new(0))
    );
}
#[test]
fn source_domain_pins_initializer_and_subsequent_override_without_inventing_cap() {
    let blobs = oracle_blobs::blobs(&[PATH.to_owned()]);
    let source = &blobs[PATH];
    assert_eq!(
        oracle_blobs::sha256_hex(source.as_bytes()),
        "6b8b4cab5dc6598007ffaf30453ca8407bb57e13c8d29f1ceb04fe7ac52f0457"
    );
    assert_eq!(
        oracle_blobs::line_of(source, 96).unwrap(),
        "MAX_TTL_SECONDS = 300.0"
    );
    assert_eq!(
        oracle_blobs::line_of(source, 1006).unwrap().trim(),
        "\"expires_at\": now + min(60.0, MAX_TTL_SECONDS),"
    );
    assert_eq!(
        oracle_blobs::line_of(source, 1007).unwrap().trim(),
        "**dict(fields),"
    );
    let span = source
        .lines()
        .skip(996)
        .take(13)
        .collect::<Vec<_>>()
        .join("\n");
    assert_eq!(
        oracle_blobs::sha256_hex(span.as_bytes()),
        "8323347e1edd67ac67650df38f3df9f54b4b742fd0b55ce03ca5ccdbc4194f4f"
    );
}
#[test]
fn real_isolated_producer_fixture_retains_sixty_seconds_and_private_bypass() {
    assert_eq!(
        oracle_blobs::sha256_hex(FX.as_bytes()),
        "cb69c86be253d9dbad65cec32bae43848d4b86819889847b45b42427d026ec3a"
    );
    let rows: Vec<_> = FX
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .collect();
    assert_eq!(rows.len(), 4);
    let id = BudgetId::from_name("ManagedControlExchangeResponseTtl").unwrap();
    for r in &rows[..3] {
        assert_eq!(r[2], "300.0");
        assert_eq!(r[3], "60.0");
        assert_eq!(
            r[4].parse::<u64>().unwrap(),
            id.span(Entries::new(0)).unwrap().millis()
        );
    }
    assert_eq!(rows[3][0], "private-override");
    assert_eq!(rows[3][4], "uncapped-private-override");
    assert!(rows[3][3].parse::<f64>().unwrap() > 300.0);
}
