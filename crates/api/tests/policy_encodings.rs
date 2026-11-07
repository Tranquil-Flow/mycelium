// SPDX-License-Identifier: AGPL-3.0-only
//! Integrator ruling (c): `resource-policy.json` is accepted in exactly the encodings v0.1.4
//! accepts. `read_resource_policy` does `json.loads(os.read(fd, 4097))` on bytes, so utf-8,
//! utf-8-sig, utf-16 and utf-32 (either byte order, with and without BOM, per
//! `json.detect_encoding`) all load. Expected outcomes are the EXECUTED oracle's
//! (`r8-probe-policy-encodings.py`, 74 files).
use api::preferences::resource_policy;
use serde_json::Value;
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf};

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
#[test]
fn every_encoding_and_malformed_file_matches_read_resource_policy() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_policy_encodings.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let cases = fixture["cases"].as_array().unwrap();
    assert!(cases.len() >= 70);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir: PathBuf =
            std::env::temp_dir().join(format!("l10-enc-{label}-{}", std::process::id()));
        fs::remove_dir_all(&dir).unwrap_or(());
        fs::create_dir_all(&dir).unwrap();
        let file = dir.join(resource_policy::FILE);
        fs::write(&file, hex(case["hex"].as_str().unwrap())).unwrap();
        fs::set_permissions(&file, fs::Permissions::from_mode(0o600)).unwrap();
        let got = resource_policy::read(&dir);
        let ok = match (&got, case.get("ok")) {
            (Ok(Some(value)), Some(want)) => value == want,
            (Err(e), None) => {
                let want = &case["err"];
                u64::from(e.status) == want["status"].as_u64().unwrap()
                    && e.code == want["code"].as_str().unwrap()
            }
            _ => false,
        };
        if !ok {
            failures.push(format!("{label}: got {got:?}, oracle {}", case));
        }
        fs::remove_dir_all(&dir).unwrap();
    }
    assert!(
        failures.is_empty(),
        "{} of {} differ; first: {:?}",
        failures.len(),
        cases.len(),
        failures
            .first()
            .map(|f| f.chars().take(260).collect::<String>())
    );
}
