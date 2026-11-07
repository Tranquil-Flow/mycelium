// SPDX-License-Identifier: AGPL-3.0-only
use std::{collections::BTreeMap, ffi::OsStr, path::Path};
use xtask::{policy::Date, preflight::Preflight};
fn today() -> Date {
    Date::parse("2026-10-26").unwrap()
}
fn manifest() -> serde_json::Value {
    serde_json::json!({"version":1,"tools":[{"name":"fixture", "detect":{"kind":"file", "path":"Cargo.toml"}}], "groups":[
        {"id":"unit", "kind":"unit", "mandatory":true, "needs":["fixture"], "after":[], "command":["cargo","test","-p","xtask"]},
        {"id":"docs", "kind":"doctest", "mandatory":true, "needs":[], "after":["unit"], "command":["cargo","test","--doc"]},
        {"id":"custom", "kind":"custom", "mandatory":true, "needs":[], "after":[], "command":["cargo","xtask","lint"]}]})
}
fn parse(m: serde_json::Value, q: serde_json::Value) -> Result<Preflight, String> {
    Preflight::parse(&m.to_string(), &q.to_string(), today())
}
fn empty() -> serde_json::Value {
    serde_json::json!({"version":1,"entries":[]})
}
fn root() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
}
#[test]
fn missing_prerequisite_blocks_transitive_groups_not_green_skip() {
    let mut m = manifest();
    m["tools"][0]["detect"]["path"] = "absent-prerequisite-marker".into();
    let p = parse(m, empty()).unwrap();
    let statuses = p
        .evaluate(root(), OsStr::new(""), &BTreeMap::new(), &[])
        .unwrap();
    assert_eq!(statuses.len(), 3);
    assert_eq!(
        statuses[0].render(),
        "unit: NOT_RUN(BLOCKED: fixture: missing file)"
    );
    assert!(statuses[1].render().contains("NOT_RUN(BLOCKED:"));
    assert_eq!(statuses[2].render(), "custom: RUN");
}
#[test]
fn admitted_groups_include_doctests_and_custom_jobs() {
    let p = parse(manifest(), empty()).unwrap();
    let statuses = p
        .evaluate(root(), OsStr::new(""), &BTreeMap::new(), &[])
        .unwrap();
    assert_eq!(
        statuses.iter().map(|s| s.render()).collect::<Vec<_>>(),
        ["unit: RUN", "docs: RUN", "custom: RUN"]
    );
    let selected = p
        .evaluate(root(), OsStr::new(""), &BTreeMap::new(), &["docs".into()])
        .unwrap();
    assert_eq!(selected.len(), 2);
    assert!(
        p.evaluate(
            root(),
            OsStr::new(""),
            &BTreeMap::new(),
            &["unknown".into()]
        )
        .is_err()
    );
}
#[test]
fn manifest_unknown_prerequisites_cycles_and_empty_selection_fail() {
    let mut unknown = manifest();
    unknown["groups"][0]["needs"][0] = "typo".into();
    assert!(parse(unknown, empty()).is_err());
    let mut cycle = manifest();
    cycle["groups"][0]["after"] = serde_json::json!(["docs"]);
    assert!(parse(cycle, empty()).is_err());
    let mut no_groups = manifest();
    no_groups["groups"] = serde_json::json!([]);
    assert!(parse(no_groups, empty()).is_err());
    let mut unsafe_probe = manifest();
    unsafe_probe["tools"][0]["detect"] = serde_json::json!({"kind":"command","program":"curl"});
    assert!(parse(unsafe_probe, empty()).is_err());
}
fn quarantine() -> serde_json::Value {
    serde_json::json!({"group":"unit","owner":"test-team","reason":"tracked adapter flake","expiry":"2026-11-26","replacement_coverage":["custom"]})
}
#[test]
fn quarantine_expiry_missing_owner_and_missing_coverage_fail() {
    for (field, value) in [("expiry", "2026-10-26"), ("owner", ""), ("reason", "")] {
        let mut q = quarantine();
        q[field] = value.into();
        assert!(parse(manifest(), serde_json::json!({"version":1,"entries":[q]})).is_err());
    }
    for coverage in [
        serde_json::json!([]),
        serde_json::json!(["missing"]),
        serde_json::json!(["unit"]),
    ] {
        let mut q = quarantine();
        q["replacement_coverage"] = coverage;
        assert!(parse(manifest(), serde_json::json!({"version":1,"entries":[q]})).is_err());
    }
}
#[test]
fn valid_quarantine_is_visible_and_does_not_admit_mandatory_group() {
    let p = parse(
        manifest(),
        serde_json::json!({"version":1,"entries":[quarantine()]}),
    )
    .unwrap();
    let statuses = p
        .evaluate(root(), OsStr::new(""), &BTreeMap::new(), &[])
        .unwrap();
    assert!(
        statuses[0]
            .render()
            .contains("NOT_RUN(BLOCKED: quarantined")
    );
    assert!(statuses[1].reason.is_some());
    assert!(statuses[2].reason.is_none());
}
