// SPDX-License-Identifier: AGPL-3.0-only
//! Direct-helper length overrides restrict magnitude claims; no wire-bypass assertion.
use budget_model::inventory::adjudication as adj;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
#[test]
fn executed_length_overrides_prevent_universal_container_magnitude_claims() {
    let fx = include_str!("../data/fx-l1b-r17-container-domain.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(fx.as_bytes()),
        "cae2e501d0cd0403783046f79076ea386d8c83a37650649ed7c7cc4fa3aa4792"
    );
    let path = "mycelium_platform_neutral/primitives.py";
    let blobs = oracle_blobs::blobs(&[path.to_owned()]);
    assert_eq!(
        oracle_blobs::sha256_hex(blobs[path].as_bytes()),
        "a0a13c28620968a884b586db147a5cf5a2811f294da712f0adf2e10e7866d886"
    );
    let rows: Vec<_> = fx
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .collect();
    assert_eq!(rows.len(), 6);
    for row in rows {
        assert!(matches!(row[0], "nullable_text" | "bounded_list"));
        if row[1].starts_with("builtin-") {
            assert_eq!((row[2], row[3], row[4]), ("9", "refused", "-"));
        } else {
            assert!(matches!(
                row[1],
                "str-subclass" | "list-subclass" | "tuple-subclass"
            ));
            assert_eq!((row[2], row[3], row[4]), ("1", "accepted", "9"));
        }
    }
    let keys: Vec<_> = include_str!("../data/python-r17-misc.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .filter(|f| matches!(f[19], "nullable_text" | "bounded_list"))
        .map(|f| f[0])
        .collect();
    assert_eq!(keys.len(), 4);
    let p = adj::parse(&adj::TABLES).unwrap();
    for key in keys {
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        assert!(
            e.rationale
                .contains("successful helper return on builtin str/list/tuple inputs only"),
            "{key}"
        );
        assert!(
            e.rationale.contains("length-overriding subclasses"),
            "{key}"
        );
    }
}
