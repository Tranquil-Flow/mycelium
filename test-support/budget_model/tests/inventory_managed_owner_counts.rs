// SPDX-License-Identifier: AGPL-3.0-only
//! Named owner policies, not global sequence or all-row ledger ceilings.
use budgets::table::{Provenance, Purpose, Value};
use budgets::{BudgetId, Entries};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
const PATH: &str = "mycelium_a13_macos/control_session.py";
const FX: &str = include_str!("../data/fx-l1b-r17-owner-counts.tsv");
#[test]
fn owner_counts_have_exact_names_units_values_and_source_domains() {
    for (name, count, provenance) in [
        (
            "ManagedOwnerSequenceReservationBlock",
            64,
            "mycelium_a13_macos/control_session.py:38 @ f4e366e9; _SEQUENCE_BLOCK crash-safe reservation increment, not maximum sequence",
        ),
        (
            "ManagedOwnerConfirmedLedgerTail",
            256,
            "mycelium_a13_macos/control_session.py:130 @ f4e366e9; _COMMAND_LEDGER_TAIL confirmed-retention target plus reserved row; unresolved rows never pruned; not an all-row ledger ceiling",
        ),
    ] {
        let id = BudgetId::from_name(name).expect("missing exact owner policy");
        assert_eq!(id.count().unwrap(), Entries::new(count));
        assert_eq!(id.entry().purpose, Purpose::Policy);
        assert!(matches!(id.entry().value, Value::Count(_)));
        assert_eq!(id.entry().provenance, Provenance::Source(provenance));
        assert_ne!(
            id,
            BudgetId::PyMyceliumA13MacosControlSessionMaxOwnerCommandsL126
        );
    }
}
#[test]
fn source_scope_preserves_unresolved_rows_and_sequence_high_water() {
    let blobs = oracle_blobs::blobs(&[PATH.to_owned()]);
    let s = &blobs[PATH];
    assert_eq!(
        oracle_blobs::sha256_hex(s.as_bytes()),
        "aac49ba38c2b247ef4c1af80dd50f1bde997123545f9d8df8c55a916caccf613"
    );
    for (n, text) in [
        (
            38,
            "_SEQUENCE_BLOCK = 64  # crash-safe reservation: sequences may be skipped, never reused",
        ),
        (130, "_COMMAND_LEDGER_TAIL = 256"),
        (
            268,
            "unresolved = [key for key, row in commands.items() if not row['enqueue_confirmed']]",
        ),
        (269, "if unresolved:"),
        (272, "return"),
        (
            273,
            "excess = len(commands) - (_COMMAND_LEDGER_TAIL + 1)  # +1 reserves the new row",
        ),
        (
            298,
            "self._state['sequence_ceiling'] = self._volatile + _SEQUENCE_BLOCK",
        ),
    ] {
        assert_eq!(oracle_blobs::line_of(s, n).unwrap().trim(), text);
    }
}
#[test]
fn executed_constant_assignments_match_typed_registry_without_native_claims() {
    assert_eq!(
        oracle_blobs::sha256_hex(FX.as_bytes()),
        "a2ecb6643d96eadb6eab86456636451816b6bae8de07b1c8643e5f27390b96b1"
    );
    let rows: Vec<_> = FX
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .collect();
    assert_eq!(rows.len(), 2);
    for f in rows {
        assert_eq!(
            BudgetId::from_name(f[0]).unwrap().count().unwrap().get(),
            f[3].parse::<u64>().unwrap()
        );
        assert!(!f[4].is_empty());
    }
}
