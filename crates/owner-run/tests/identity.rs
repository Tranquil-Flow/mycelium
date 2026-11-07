// SPDX-License-Identifier: AGPL-3.0-only
//! Run identities (`owner_run.new_run_identity`, executed once per probe run: the oracle's shape
//! is recorded in the owner-run fixture) and the closed code shape.
use owner_run::machine::{closed_code, new_run_identity};
use serde_json::Value;

fn shape(run_id: &str, uuid: &str) -> (bool, bool) {
    let hex = |s: &str, n: usize| {
        s.len() == n
            && s.bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    };
    let parts: Vec<&str> = run_id.split('-').collect();
    let run_ok = parts.len() == 4
        && parts[0] == "a13"
        && hex(parts[1], 6)
        && parts[2] == "qwen"
        && parts[3].len() == 16
        && parts[3].as_bytes()[8] == b'T'
        && parts[3].ends_with('Z');
    let u: Vec<&str> = uuid.split('-').collect();
    let uuid_ok = u.len() == 5
        && [8, 4, 4, 4, 12].iter().zip(&u).all(|(n, p)| hex(p, *n))
        && u[2].starts_with('4')
        && matches!(u[3].as_bytes()[0], b'8' | b'9' | b'a' | b'b');
    (run_ok, uuid_ok)
}

/// Proves: B-RUN-14 (a run id is a13-<6 hex>-qwen-<UTC stamp>, the deployment id a UUID v4).
#[test]
fn identity_has_the_oracle_shape() {
    let fx: Value =
        serde_json::from_str(include_str!("fixtures/oracle_owner_run.json")).expect("fixture");
    let sample = &fx["identity"]["sample"];
    assert_eq!(
        shape(
            sample[0].as_str().expect("run"),
            sample[1].as_str().expect("uuid")
        ),
        (true, true),
        "the shape check accepts what the oracle produced"
    );
    let id = new_run_identity([0xab; 19], "20261029T010352Z");
    assert_eq!(shape(&id.run_id, &id.deployment_id), (true, true));
    assert_eq!(id.run_id, "a13-ababab-qwen-20261029T010352Z");
    let other = new_run_identity([0x01; 19], "20261029T010352Z");
    assert_ne!(id, other);
}

/// Proves: B-RUN-03 (`[a-z][a-z0-9_]{0,79}`, exactly).
#[test]
fn closed_codes() {
    for ok in [
        "a",
        "owner_run_active",
        "x9_",
        &format!("a{}", "b".repeat(79)),
    ] {
        assert!(closed_code(ok), "{ok}");
    }
    for bad in [
        "",
        "A",
        "9a",
        "_a",
        "a-b",
        "a b",
        "a\n",
        "é",
        &format!("a{}", "b".repeat(80)),
    ] {
        assert!(!closed_code(bad), "{bad:?}");
    }
}
