// SPDX-License-Identifier: AGPL-3.0-only
//! Literal comparator policies are not universal caps on Python int subclasses.
use budget_model::inventory::adjudication as adj;
use std::collections::BTreeSet;
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
#[test]
fn executed_subclass_counterexamples_limit_the_reviewed_magnitude_domain() {
    let fx = include_str!("../data/fx-l1b-r17-integer-domain.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(fx.as_bytes()),
        "5841e21791aff1e0b6ced4917380cfaa37b2e6c8b5da02068d1a4504b2eb92a8"
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
    assert_eq!(rows.len(), 8);
    for row in rows {
        assert!(matches!(row[0], "bounded_int" | "nullable_bounded_int"));
        assert!(matches!(row[2], "-1" | "8")); // outside the executed [0, 7] range
        match row[1] {
            "builtin" => assert_eq!((row[3], row[4], row[5]), ("refused", "-", "-")),
            "comparison-override" => {
                assert_eq!((row[3], row[4], row[5]), ("accepted", row[2], "true"))
            }
            other => panic!("unknown executed domain {other}"),
        }
    }
    let keys: BTreeSet<_> = [
        include_str!("../data/python-r17-numeric.tsv"),
        include_str!("../data/python-r17-integers.tsv"),
    ]
    .into_iter()
    .flat_map(|text| text.lines().skip(1))
    .map(|l| l.split('\t').collect::<Vec<_>>())
    .filter(|r| matches!(r[19], "bounded_int" | "nullable_bounded_int"))
    .map(|r| r[0])
    .collect();
    assert_eq!(keys.len(), 61);
    let parsed = adj::parse(&adj::TABLES).unwrap();
    for key in keys {
        let e = parsed.entries.iter().find(|e| e.key == key).unwrap();
        assert!(
            e.rationale
                .contains("builtin integer successful helper return"),
            "{key}"
        );
        assert!(
            e.rationale
                .contains("int subclasses can override comparisons"),
            "{key}"
        );
    }
}
