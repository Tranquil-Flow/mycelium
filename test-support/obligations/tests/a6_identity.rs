// SPDX-License-Identifier: AGPL-3.0-only
use obligations::a6;
use serde_json::json;
fn valid() -> (a6::Binding, a6::Manifest, obligations::Registry) {
    (
        a6::checked_in_binding().unwrap(),
        a6::accepted_manifest().unwrap(),
        obligations::checked_in().unwrap().0,
    )
}
#[test]
fn honest_additive_identity_is_not_retirement() {
    let (b, m, h) = valid();
    a6::validate_binding(&b, &m, &h).unwrap();
    assert_eq!(m.files.len(), 1347);
    assert_eq!(b.paths.len(), 10);
    assert_eq!(b.test_groups.len(), 10);
    assert_eq!(b.obligations.len(), 14);
    assert!(
        b.body_rows
            .iter()
            .all(|r| r["retired"] == false && r["proof_state"] == "unproven")
    );
}
#[test]
fn old_state_and_missing_members_are_refused() {
    let (b, m, h) = valid();
    for which in 0..5 {
        let mut x = b.clone();
        match which {
            0 => {
                x.paths.clear();
            }
            1 => {
                x.paths.pop();
            }
            2 => {
                x.test_groups.pop();
            }
            3 => {
                x.obligations.pop();
            }
            _ => {
                x.body_rows.pop();
            }
        }
        assert!(
            a6::validate_binding(&x, &m, &h).is_err(),
            "missing population {which}"
        );
    }
}
#[test]
fn source_hash_size_path_and_manifest_members_are_binding() {
    let (b, m, h) = valid();
    for which in 0..5 {
        let mut x = m.clone();
        match which {
            0 => x.files[0].sha256 = "sha256:".to_string() + &"0".repeat(64),
            1 => x.files[0].size_bytes += 1,
            2 => x.files[0].path = "other.py".into(),
            3 => {
                x.files.pop();
            }
            _ => x.files.push(x.files[0].clone()),
        }
        assert!(
            a6::validate_binding(&b, &x, &h).is_err(),
            "source identity {which}"
        );
    }
}
#[test]
fn historical_records_cannot_be_edited_or_deleted() {
    let (b, m, h) = valid();
    for which in 0..5 {
        let mut x = h.clone();
        match which {
            0 => x.paths[0].citation = "backdated".into(),
            1 => {
                x.paths.pop();
            }
            2 => x.test_groups[0]
                .evidence
                .insert("source_tests".into(), json!([]))
                .map(|_| ())
                .unwrap_or(()),
            3 => x.obligations[0].description = "altered".into(),
            _ => x.source_sha = b.source_sha.clone(),
        }
        assert!(a6::validate_binding(&b, &m, &x).is_err(), "history {which}");
    }
}
#[test]
fn crosswalk_body_group_and_proof_tampering_are_refused() {
    let (b, m, h) = valid();
    for which in 0..6 {
        let mut x = b.clone();
        match which {
            0 => x.body_rows[0]["retired"] = json!(true),
            1 => x.body_rows[1]["source_tests"] = json!([]),
            2 => x.test_groups[0].id = "duplicate-new-identity".into(),
            3 => x.body_rows[10]["historical_group"]["id"] = json!("new-tb-group"),
            4 => x.paths[0].obligation_id = "not-declared".into(),
            _ => x.history_files[0].sha256 = "0".repeat(64),
        }
        assert!(
            a6::validate_binding(&x, &m, &h).is_err(),
            "crosswalk {which}"
        );
    }
}
#[test]
fn provisional_authority_and_contract_successor_tamper_are_refused() {
    let (b, m, h) = valid();
    for which in 0..3 {
        let mut x = b.clone();
        match which {
            0 => x.source_sha = h.source_sha.clone(),
            1 => x.manifest.sha256 = "0".repeat(64),
            _ => x
                .history_files
                .retain(|f| f.path != "accepted-contract-binding.json"),
        }
        assert!(a6::validate_binding(&x, &m, &h).is_err());
    }
}
#[test]
fn literal_a6_digest_encoding_is_not_silently_repaired() {
    let (_, m, _) = valid();
    let p = &m.files[0];
    assert!(a6::file_pin(p).is_ok());
    for sha in [
        p.sha256[7..].to_string(),
        p.sha256.to_uppercase(),
        "sha256:bad".into(),
    ] {
        let mut p = p.clone();
        p.sha256 = sha;
        assert!(a6::file_pin(&p).is_err());
    }
}
