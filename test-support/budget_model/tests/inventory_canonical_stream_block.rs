// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/artifact_transport.py.
//! Canonical adjudication of the B-reported census omission `artifact_transport.py:44`.
use budget_model::inventory::{SOURCE_SHA, candidates, checked_in, validate};
use budgets::BudgetId;
use budgets::table::{Provenance, Value};

const PATH: &str = "mycelium_live/artifact_transport.py";
const ID: &str = "PyMyceliumLiveArtifactTransportStreamBlockBytesL44";

#[test]
fn stream_block_declaration_is_a_reviewed_exact_byte_mapping() {
    let all = candidates().unwrap();
    let site = all
        .iter()
        .find(|c| c.path == PATH && c.line == 44 && c.kind == "assignment")
        .expect("canonical census candidate");
    assert_eq!(site.symbol, "_STREAM_BLOCK_BYTES");
    assert_eq!(site.expression, "64 * 1024");
    let rows = checked_in().unwrap();
    let row = rows
        .iter()
        .find(|r| r.key == site.key)
        .expect("the declaration is reviewed, not pending");
    assert_eq!((row.unit.as_str(), row.value), ("bytes", Some(65_536)));
    assert_eq!(row.budget, ID);
    assert!(row.disposition.is_empty());
    assert_eq!(row.source_sha, SOURCE_SHA);
    // The table value and its exact site provenance agree with the row.
    let entry = BudgetId::from_name(ID).expect("registered id").entry();
    assert!(matches!(entry.value, Value::Storage(n) if n.get() == 65_536));
    assert!(matches!(
        entry.provenance,
        Provenance::Source(s) if s == format!("{PATH}:44 @ {}", &SOURCE_SHA[..8])
    ));
    // It is a byte stride: a time or count claim is rejected, and so is a same-unit,
    // same-value id whose provenance is another site (the :471 derived operand).
    for unit in ["milliseconds", "entries"] {
        let mut bad = row.clone();
        bad.unit = unit.into();
        assert!(validate(&[bad]).is_err(), "{unit}");
    }
    let other = BudgetId::from_name("PyMyceliumLiveArtifactTransportRateLimiterBlockBytesL471")
        .expect("same-unit same-value id");
    assert!(matches!(other.entry().value, Value::Storage(n) if n.get() == 65_536));
    assert_ne!(other.entry().provenance, entry.provenance);
    // Only the two slices in Handler.do_POST consume the stride; it is no wait budget.
    for word in ["timeout", "sleep", "wait", "deadline"] {
        assert!(!site.source.contains(word), "{word}");
    }
    validate(&rows).unwrap();
}
