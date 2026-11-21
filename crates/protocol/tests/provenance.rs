// SPDX-License-Identifier: AGPL-3.0-only
use serde_json::Value;
use sha2::{Digest, Sha256};
#[test]
fn fixture_seal_and_coverage_crosswalk_are_complete() {
    let base = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures");
    let seal: Value = serde_json::from_str(include_str!("../fixtures/fixture-seal.json")).unwrap();
    let entries = seal["files"].as_array().unwrap();
    assert!(!entries.is_empty());
    let mut paths = std::collections::BTreeSet::new();
    for e in entries {
        let path = e["path"].as_str().unwrap();
        assert!(paths.insert(path));
        assert!(
            std::path::Path::new(path)
                .components()
                .all(|c| matches!(c, std::path::Component::Normal(_)))
        );
        let raw = std::fs::read(base.join(path)).unwrap();
        assert_eq!(raw.len() as u64, e["size_bytes"].as_u64().unwrap());
        assert_eq!(
            hex::encode(Sha256::digest(&raw)),
            e["sha256"].as_str().unwrap(),
            "{path}"
        );
    }
    fn fixture_paths(
        root: &std::path::Path,
        path: &std::path::Path,
        out: &mut std::collections::BTreeSet<String>,
    ) {
        for entry in std::fs::read_dir(path).unwrap() {
            let entry = entry.unwrap();
            let kind = entry.file_type().unwrap();
            assert!(
                !kind.is_symlink(),
                "fixture seal must not escape through a symlink"
            );
            if kind.is_dir() {
                fixture_paths(root, &entry.path(), out);
            } else {
                assert!(kind.is_file());
                let relative = entry
                    .path()
                    .strip_prefix(root)
                    .unwrap()
                    .to_str()
                    .unwrap()
                    .to_owned();
                if relative != "fixture-seal.json" {
                    out.insert(relative);
                }
            }
        }
    }
    let mut present = std::collections::BTreeSet::new();
    fixture_paths(&base, &base, &mut present);
    assert_eq!(
        present,
        paths.iter().map(|p| (*p).to_owned()).collect(),
        "unsealed or stale fixture files"
    );
    let inventory: Value =
        serde_json::from_str(include_str!("../fixtures/inventory.json")).unwrap();
    let coverage: Value = serde_json::from_str(include_str!("../fixtures/coverage.json")).unwrap();
    let source: std::collections::BTreeSet<_> = inventory["contracts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["protocol"].as_str().unwrap())
        .collect();
    let recorded: std::collections::BTreeSet<_> = coverage["contracts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["protocol"].as_str().unwrap())
        .collect();
    assert_eq!(source, recorded);
    assert_eq!(source.len(), 87);
    assert_eq!(
        coverage["contracts"].as_array().unwrap().len(),
        source.len()
    );
    let assignments: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/round3-assignment-vectors.json")).unwrap();
    let outputs: std::collections::BTreeSet<_> = assignments
        .iter()
        .filter_map(|r| match r["name"].as_str().unwrap() {
            "assignment" => Some(
                r["value"]["assignment"]["protocol"]
                    .as_str()
                    .unwrap()
                    .to_owned(),
            ),
            "report" => Some(
                r["value"]["report"]["protocol"]
                    .as_str()
                    .unwrap()
                    .to_owned(),
            ),
            "audit" => {
                let v: Value = serde_json::from_str(r["canonical_utf8"].as_str().unwrap()).unwrap();
                Some(v["protocol"].as_str().unwrap().to_owned())
            }
            _ => None,
        })
        .collect();
    let registered: std::collections::BTreeSet<_> = coverage["contracts"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["coverage"] == "assignment-report-audit-oracle-corpus")
        .map(|r| r["protocol"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(outputs, registered);
    assert_eq!(outputs.len(), 3);
    let projections: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/round3-projection-vectors.json")).unwrap();
    let outputs: std::collections::BTreeSet<_> = projections
        .iter()
        .map(|r| {
            let v: Value = serde_json::from_str(r["canonical_utf8"].as_str().unwrap()).unwrap();
            v.get("protocol")
                .unwrap_or(&v["incidents"][0]["protocol"])
                .as_str()
                .unwrap()
                .to_owned()
        })
        .collect();
    let registered: std::collections::BTreeSet<_> = coverage["contracts"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["coverage"] == "operator-projection-oracle-corpus")
        .map(|r| r["protocol"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(outputs, registered);
    assert_eq!(outputs.len(), 3);
    let seeds: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/round3-seed-vectors.json")).unwrap();
    let outputs: std::collections::BTreeSet<_> = seeds
        .iter()
        .map(|r| {
            let v: Value = serde_json::from_str(r["canonical_utf8"].as_str().unwrap()).unwrap();
            v["protocol"].as_str().unwrap().to_owned()
        })
        .collect();
    let registered: std::collections::BTreeSet<_> = coverage["contracts"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["coverage"] == "seed-boundary-projection-oracle-corpus")
        .map(|r| r["protocol"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(outputs, registered);
    assert_eq!(outputs.len(), 3);
    let vectors: Value =
        serde_json::from_str(include_str!("../fixtures/membership-vectors.json")).unwrap();
    let mut implemented: std::collections::BTreeSet<_> = vectors
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["envelope"]["message"]["protocol"].as_str().unwrap())
        .collect();
    implemented.insert("mycelium.membership.signed_message.v1");
    let covered: std::collections::BTreeSet<_> = coverage["contracts"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["coverage"] == "membership-wire-schema-signature")
        .map(|r| r["protocol"].as_str().unwrap())
        .collect();
    assert_eq!(implemented, covered);
    let history: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/continued-history-vectors.json")).unwrap();
    assert!(history.iter().any(|r| r["accepted"] == false));
    let outputs: std::collections::BTreeSet<_> = history
        .iter()
        .filter(|r| r["accepted"] == true)
        .map(|r| {
            let output: Value =
                serde_json::from_str(r["canonical_utf8"].as_str().unwrap()).unwrap();
            output["protocol"].as_str().unwrap().to_owned()
        })
        .collect();
    let registered: std::collections::BTreeSet<_> = coverage["contracts"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["coverage"] == "history-derivation-oracle-corpus")
        .map(|r| r["protocol"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(outputs, registered);
    assert_eq!(outputs.len(), 1);
    for (raw, level) in [
        (
            include_str!("../fixtures/remaining-snapshot-vectors.json"),
            "planner-snapshot-derivation-oracle-corpus",
        ),
        (
            include_str!("../fixtures/remaining-load-proof-vectors.json"),
            "loader-authentication-oracle-corpus",
        ),
    ] {
        let snapshots: Vec<Value> = serde_json::from_str(raw).unwrap();
        let outputs: std::collections::BTreeSet<_> = snapshots
            .iter()
            .map(|r| {
                let v: Value = serde_json::from_str(r["canonical_utf8"].as_str().unwrap()).unwrap();
                v["protocol"].as_str().unwrap().to_owned()
            })
            .collect();
        let registered: std::collections::BTreeSet<_> = coverage["contracts"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["coverage"] == level)
            .map(|r| r["protocol"].as_str().unwrap().to_owned())
            .collect();
        assert_eq!(outputs, registered);
        assert_eq!(outputs.len(), 1);
    }
    for (fixture, level) in [
        (
            include_str!("../fixtures/remaining-route-vectors.json"),
            "route-parser-oracle-corpus",
        ),
        (
            concat!(
                include_str!("../fixtures/remaining-public-vectors.json.part-00"),
                include_str!("../fixtures/remaining-public-vectors.json.part-01"),
                include_str!("../fixtures/remaining-public-vectors.json.part-02"),
                include_str!("../fixtures/remaining-public-vectors.json.part-03"),
                include_str!("../fixtures/remaining-public-vectors.json.part-04")
            ),
            "remaining-public-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/remaining-gossip-bundle-vectors.json"),
            "gossip-bundle-oracle-corpus",
        ),
        (
            include_str!("../fixtures/remaining-gossip-signed-vectors.json"),
            "gossip-signature-oracle-corpus",
        ),
        (
            include_str!("../fixtures/remaining-gossip-link-vectors.json"),
            "gossip-link-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continued-promotion-vectors.json"),
            "promotion-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continued-projection-vectors.json"),
            "projection-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continued-observatory-vectors.json"),
            "observatory-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continued-capacity-vectors.json"),
            "capacity-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continued-performance-vectors.json"),
            "performance-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continued-product-vectors.json"),
            "product-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continued-recovery-vectors.json"),
            "recovery-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continued-wire-vectors.json"),
            "router-wire-oracle-corpus",
        ),
        (
            include_str!("../fixtures/expanded-bootstrap-vectors.json"),
            "bootstrap-primary-schema-corpus",
        ),
        (
            include_str!("../fixtures/expanded-manual-vectors.json"),
            "manual-route-oracle-corpus",
        ),
        (
            include_str!("../fixtures/expanded-preparation-vectors.json"),
            "preparation-derivation-oracle-corpus",
        ),
        (
            include_str!("../fixtures/expanded-graph-vectors.json"),
            "execution-graph-oracle-corpus",
        ),
        (
            include_str!("../fixtures/expanded-replica-vectors.json"),
            "replica-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/expanded-public-vectors.json"),
            "public-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continuation-schema-vectors.json"),
            "closed-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/continuation-request-vectors.json"),
            "request-event-schema-oracle-corpus",
        ),
        (
            include_str!("../fixtures/f4-seed-vectors.json"),
            "seed-transition-dual-signature-oracle-corpus",
        ),
        (
            include_str!("../fixtures/f4-manifest-vectors.json"),
            "path-manifest-schema-binding-oracle-corpus",
        ),
        (
            include_str!("../fixtures/resume-evidence-vectors.json"),
            "evidence-schema-digest-oracle-corpus",
        ),
    ] {
        let rows: Vec<Value> = serde_json::from_str(fixture).unwrap();
        let corpus: std::collections::BTreeSet<_> = rows
            .iter()
            .map(|r| r["value"]["protocol"].as_str().unwrap())
            .collect();
        let registered: std::collections::BTreeSet<_> = coverage["contracts"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["coverage"] == level)
            .map(|r| r["protocol"].as_str().unwrap())
            .collect();
        assert_eq!(corpus, registered);
        assert!(corpus.iter().all(|p| source.contains(p)));
    }
}
