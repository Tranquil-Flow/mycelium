// SPDX-License-Identifier: AGPL-3.0-only
//! Observed current-workflow count policy; not generation quota or native enforcement.
use budgets::BudgetId;
use budgets::table::{Provenance, Purpose, Value};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
const NAME: &str = "ManagedDemoObservedVerifiedTokenCountMax";
const PROVENANCE: &str = "mycelium_a13_macos/demo_chain.py:1012 @ f4e366e9; current-workflow observed verified_token_count exact-int upper guard; not a generation quota or renewal duration";
fn exact(id: BudgetId) -> bool {
    Some(id) == BudgetId::from_name(NAME)
        && id.entry().purpose == Purpose::Policy
        && matches!(id.entry().value, Value::Count(n) if n.get() == 4096)
        && matches!(id.entry().provenance, Provenance::Source(p) if p == PROVENANCE)
}
#[test]
fn observed_count_has_its_own_source_policy_not_a_generic_equal_number() {
    let id = BudgetId::from_name(NAME).expect("missing source-specific observed-count policy");
    assert!(exact(id));
    let wrong = BudgetId::PyFinalMyceliumInteractiveSwarmL329C52Comparison;
    assert!(matches!(wrong.entry().value, Value::Count(n) if n.get() == 4096));
    assert_eq!(wrong.entry().purpose, Purpose::Policy);
    assert!(!exact(wrong));
}
#[test]
fn isolated_callback_controls_pin_type_boundaries_and_current_identity() {
    let path = "mycelium_a13_macos/demo_chain.py";
    let blobs = oracle_blobs::blobs(&[path.to_owned()]);
    let source = &blobs[path];
    assert_eq!(
        oracle_blobs::sha256_hex(source.as_bytes()),
        "604a2cf25b35ce7d4be8b5f9cb1f69eb8933a2f5453e8f8f5ca05904fecb4b0c"
    );
    assert_eq!(
        oracle_blobs::line_of(source, 1012).unwrap().trim(),
        "if type(count) is int and 0 < count <= 4096:"
    );
    let span = source
        .lines()
        .skip(992)
        .take(22)
        .collect::<Vec<_>>()
        .join("\n");
    assert_eq!(
        oracle_blobs::sha256_hex(span.as_bytes()),
        "9498e7f219520d93f84a59e895f1f1cfc1a3e29b7b6a70c268f7eb08d7a20493"
    );
    let fx = include_str!("../data/fx-l1b-r17-demo-verified-count.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(fx.as_bytes()),
        "a4e083fb1c19d350cb7f146e50ef41980707ea0a47c25fde2152d9c618e293d6"
    );
    let rows: Vec<_> = fx
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .collect();
    assert_eq!(rows.len(), 15);
    for row in rows {
        match row[0] {
            "one" => assert_eq!((row[3], row[4]), ("present", "1")),
            "near-upper" => assert_eq!((row[3], row[4]), ("present", "4095")),
            "upper" => assert_eq!((row[3], row[4]), ("present", "4096")),
            "negative" | "zero" | "over-upper" | "bool" | "float" | "string" | "int-subclass"
            | "stale-identity" | "stopped" | "epoch-float" | "epoch-mismatch"
            | "absent-workflow" => assert_eq!((row[3], row[4]), ("omitted", "-")),
            other => panic!("unrecorded source control {other}"),
        }
    }
}
