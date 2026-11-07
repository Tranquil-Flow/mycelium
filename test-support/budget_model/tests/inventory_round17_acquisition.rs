// SPDX-License-Identifier: AGPL-3.0-only
//! Three private acquisition forwarding operands: source call paths, not raw Mapping inputs.
use budget_model::inventory::{self, adjudication as adj};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
const WITNESS: &str = include_str!("../data/python-r17-acquisition.tsv");
const FX: &str = include_str!("../data/fx-l1b-r17-acquisition.tsv");
const PATH: &str = "mycelium_a13_macos/acquisition.py";
fn records(t: &str) -> Vec<Vec<&str>> {
    t.lines().skip(1).map(|l| l.split('\t').collect()).collect()
}
#[test]
fn acquisition_operands_derive_only_from_exact_validated_manifest_ceiling() {
    assert_eq!(
        oracle_blobs::sha256_hex(WITNESS.as_bytes()),
        "f7e970acb145be05c6555b30b1cbfb286fd449899de17fe21a76c5bd7a611a1a"
    );
    assert_eq!(records(WITNESS).len(), 3);
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let candidates: BTreeMap<_, _> = inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let blobs = oracle_blobs::blobs(&[PATH.to_owned()]);
    for f in records(WITNESS) {
        let e = parsed
            .entries
            .iter()
            .find(|e| e.key == f[0])
            .expect("missing acquisition review");
        assert_eq!(
            (&*e.verdict, &*e.target, &*e.reviewer, e.partition),
            ("derived", f[1], "L1b-r17-acquisition", 'A')
        );
        assert!(e.rationale.contains("Static scoped call-path proof only"));
        assert!(
            e.rationale
                .contains("direct private-method invocation is not claimed bounded")
        );
        let v = adj::registry(&e.target).unwrap();
        assert_eq!(
            (v.unit, v.value),
            ("bytes", Some(9_223_372_036_854_775_807))
        );
        assert_eq!(
            v.source.as_deref(),
            Some("mycelium_a13_macos/acquisition.py:85 @ ef9ae61d")
        );
        assert_eq!(v.value, Some(f[2].parse().unwrap()));
        let c = &candidates[f[0]];
        assert_eq!(
            (
                &*c.path,
                c.line,
                c.column,
                &*c.scope,
                &*c.symbol,
                &*c.expression,
                &*c.kind,
                &*c.source
            ),
            (
                f[5],
                f[6].parse().unwrap(),
                f[7].parse().unwrap(),
                f[8],
                f[9],
                f[10],
                f[11],
                f[12]
            )
        );
        assert_eq!(oracle_blobs::sha256_hex(blobs[PATH].as_bytes()), f[3]);
        assert_eq!(
            oracle_blobs::sha256_hex(
                oracle_blobs::line_of(&blobs[PATH], c.line)
                    .unwrap()
                    .as_bytes()
            ),
            f[4]
        );
        assert!(adj::row_of(&parsed, e, &candidates, &adj::registry).is_ok());
    }
}
#[test]
fn every_scoped_private_call_is_the_pinned_execute_path() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let paths = parsed.scope.keys().cloned().collect::<Vec<_>>();
    let blobs = oracle_blobs::blobs(&paths);
    let mut calls = BTreeSet::new();
    for (path, s) in &blobs {
        for (i, l) in s.lines().enumerate() {
            for name in ["_resolve_manifest", "_scan_stage", "_download_file"] {
                if l.contains(&format!(".{name}(")) {
                    calls.insert((path.as_str(), i + 1, name));
                }
            }
        }
    }
    assert_eq!(
        calls,
        BTreeSet::from([
            (PATH, 1097, "_resolve_manifest"),
            (PATH, 1112, "_scan_stage"),
            (PATH, 1139, "_download_file")
        ])
    );
    let s = &blobs[PATH];
    for (line, text) in [
        (
            1065,
            "_validate_manifest(self._injected_manifest, model=self.model)",
        ),
        (
            1074,
            "_validate_manifest(source.resolve(self.model), model=self.model)",
        ),
        (1097, "self._resolve_manifest()"),
        (1224, "for record in manifest[\"files\"]:"),
        (1268, "maximum=record[\"size_bytes\"]"),
    ] {
        assert!(oracle_blobs::line_of(s, line).unwrap().contains(text));
    }
    assert!(s.contains("not 0 < size <= _MAX_FILE_BYTES"));
    assert!(s.contains("if total > _MAX_FILE_BYTES:"));
}
#[test]
fn acquisition_wrong_same_unit_target_and_missing_origin_are_refused() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let census = inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for f in records(WITNESS) {
        let e = parsed.entries.iter().find(|e| e.key == f[0]).unwrap();
        let mut wrong = e.clone();
        wrong.target = "PyR16FenceOwnerFileBytes".into();
        assert_eq!(adj::registry(&wrong.target).unwrap().unit, "bytes");
        assert!(
            adj::row_of(&parsed, &wrong, &census, &adj::registry)
                .unwrap_err()
                .starts_with("target-not-bound-by-citation:")
        );
        wrong = e.clone();
        wrong.citations = wrong
            .citations
            .split(" | ")
            .filter(|c| !c.starts_with("mycelium_a13_macos/acquisition.py:85 @"))
            .collect::<Vec<_>>()
            .join(" | ");
        assert!(
            adj::row_of(&parsed, &wrong, &census, &adj::registry)
                .unwrap_err()
                .starts_with("target-not-bound-by-citation:")
        );
    }
}
#[test]
fn real_manifest_validator_edges_are_not_download_or_capacity_calibration() {
    assert_eq!(
        oracle_blobs::sha256_hex(FX.as_bytes()),
        "5886bef90e966c2f5969b845d7fca4fb2d668e094c462d698af5b82062dfaaa4"
    );
    let rows = records(FX);
    assert_eq!(rows.len(), 6);
    for f in rows {
        let accepted = matches!(f[0], "aggregate-edge" | "small");
        assert_eq!(f[2], if accepted { "accepted" } else { "refused" });
        if f[0] == "aggregate-edge" {
            assert_eq!(f[3], "9223372036854775807");
        }
    }
}
