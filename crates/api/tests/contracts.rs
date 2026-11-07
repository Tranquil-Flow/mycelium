// SPDX-License-Identifier: AGPL-3.0-only
use api::{assets, schema::STATUS_FIELDS};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, fs, path::Path};
#[test]
fn unchanged_assets_match_immutable_source_hashes() {
    let manifest: Value = serde_json::from_str(include_str!("../contracts/assets.json")).unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    for asset in manifest["assets"].as_array().unwrap() {
        let bytes = fs::read(root.join(asset["path"].as_str().unwrap())).unwrap();
        assert_eq!(bytes.len() as u64, asset["bytes"].as_u64().unwrap());
        assert_eq!(
            format!("{:x}", Sha256::digest(bytes)),
            asset["sha256"].as_str().unwrap()
        );
        assert_eq!(asset["commit"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    }
    let page = assets::desktop("/").unwrap();
    let html = std::str::from_utf8(&page.body).unwrap();
    assert!(html.contains("<option value=\"4000000000\" selected>"));
    assert!(html.contains("<option value=\"600\">10 minutes</option>"));
    assert!(assets::desktop("/../private").is_none());
    assert!(assets::directory("/swarms").is_some());
}
#[test]
fn continuation_dispatch_review_has_no_pending_rows_or_false_closure_claims() {
    let j: Value = serde_json::from_str(include_str!("../contracts/routes.json")).unwrap();
    let rows = j["routes"].as_array().unwrap();
    for row in rows {
        assert_ne!(
            row["semantic_review"], "pending",
            "{} {} {}",
            row["surface"], row["method"], row["path"]
        );
        if row["semantic_review"] == "dispatch_reviewed" {
            let review = &row["dispatch_review"];
            for field in [
                "dispatch_target",
                "request",
                "response",
                "auth",
                "errors",
                "redaction",
            ] {
                assert!(
                    review[field].as_str().is_some_and(|v| !v.is_empty()),
                    "{field}: {row}"
                );
            }
            assert!(!review["sources"].as_array().unwrap().is_empty());
            assert!(!review["remaining_closure"].as_array().unwrap().is_empty());
            assert_eq!(review["transitive_closure"], false);
            assert_eq!(row["coverage"], "inventoried_source_only");
        }
    }
}
#[test]
fn route_inventory_is_unique_provenanced_and_never_claims_fake_acceptance() {
    let j: Value = serde_json::from_str(include_str!("../contracts/routes.json")).unwrap();
    let rows = j["routes"].as_array().unwrap();
    let mut seen = BTreeSet::new();
    for row in rows {
        let key = (
            row["surface"].as_str().unwrap(),
            row["method"].as_str().unwrap(),
            row["path"].as_str().unwrap(),
        );
        assert!(seen.insert(key), "duplicate {key:?}");
        assert!(matches!(
            key.1,
            "GET" | "POST" | "HEAD" | "DELETE" | "OPTIONS"
        ));
        for name in [
            "auth",
            "request_schema",
            "response_schema",
            "errors",
            "ordering",
            "privacy",
            "provenance",
        ] {
            assert!(!row[name].is_null(), "missing {name} {key:?}");
        }
        assert_eq!(row["coverage"], "inventoried_source_only");
        for p in row["provenance"].as_array().unwrap() {
            let s = &p["dispatch"];
            assert_eq!(s["commit"], j["oracle_commit"]);
            assert!(s["start"].as_u64().unwrap() <= s["end"].as_u64().unwrap());
            assert_eq!(s["sha256"].as_str().unwrap().len(), 64);
        }
    }
    for (method, path) in [
        ("GET", "/"),
        ("GET", "/api/status"),
        ("POST", "/api/adaptive/resume"),
        ("POST", "/api/swarm/publish"),
    ] {
        assert!(seen.contains(&("desktop", method, path)));
    }
    assert!(seen.contains(&("compatibility", "POST", "/v1/messages")));
    assert!(seen.contains(&(
        "browser_gateway",
        "DELETE",
        "/api/v1/inference/{request_id}/cancel"
    )));
    let frozen: Value = serde_json::from_str(include_str!("../contracts/schemas.json")).unwrap();
    let frozen_fields = frozen["desktop_status"]["required_top_level_fields"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect::<BTreeSet<_>>();
    assert_eq!(
        STATUS_FIELDS.iter().copied().collect::<BTreeSet<_>>(),
        frozen_fields
    );
    let example: Value =
        serde_json::from_str(include_str!("../contracts/status-example.json")).unwrap();
    assert_eq!(
        STATUS_FIELDS.iter().copied().collect::<BTreeSet<_>>(),
        example
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect()
    );
}
