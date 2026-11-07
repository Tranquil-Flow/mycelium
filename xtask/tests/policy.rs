// SPDX-License-Identifier: AGPL-3.0-only
use xtask::{
    lint::{Diagnostic, Source},
    policy::{Allowlist, Date},
};
fn today() -> Date {
    Date::parse("2026-10-26").unwrap()
}
fn entry() -> serde_json::Value {
    serde_json::json!({"path":"crates/example/src/lib.rs", "line":1, "rule":"SE001", "source_line":"fn run() { let _ = stop(); }", "owner":"runtime-team", "reason":"legacy adapter migration", "expiry":"2026-11-26", "replacement_plan":"propagate stop failures in runtime adapter"})
}
fn parse(value: serde_json::Value) -> Result<Allowlist, String> {
    Allowlist::parse(
        &serde_json::json!({"version":1,"entries":[value]}).to_string(),
        today(),
    )
}
#[test]
fn exception_must_be_complete_known_and_unexpired() {
    for (field, value) in [
        ("owner", ""),
        ("reason", " "),
        ("replacement_plan", ""),
        ("rule", "UNKNOWN"),
        ("expiry", "2026-10-26"),
        ("expiry", "2026-10-25"),
        ("expiry", "2026-02-30"),
        ("path", "../outside.rs"),
        ("path", "*.rs"),
    ] {
        let mut value_entry = entry();
        value_entry[field] = value.into();
        assert!(
            parse(value_entry).is_err(),
            "accepted invalid {field}={value}"
        );
    }
    let mut missing = entry();
    missing.as_object_mut().unwrap().remove("owner");
    assert!(parse(missing).is_err());
    let mut unknown = entry();
    unknown["extra"] = true.into();
    assert!(parse(unknown).is_err());
}
#[test]
fn exception_is_exact_and_stale_entries_fail() {
    let policy = parse(entry()).unwrap();
    let source = Source {
        path: "crates/example/src/lib.rs".into(),
        text: "fn run() { let _ = stop(); }".into(),
    };
    let diagnostic = Diagnostic {
        path: source.path.clone(),
        line: 1,
        code: "SE001".into(),
    };
    assert_eq!(
        policy
            .apply(vec![diagnostic.clone()], std::slice::from_ref(&source))
            .unwrap(),
        vec![]
    );
    assert!(policy.apply(vec![], std::slice::from_ref(&source)).is_err());
    let changed = Source {
        text: "fn run() { let _ = close(); }".into(),
        ..source
    };
    assert!(policy.apply(vec![diagnostic], &[changed]).is_err());
}
#[test]
fn duplicate_entries_and_schema_drift_fail() {
    let duplicate = serde_json::json!({"version":1,"entries":[entry(),entry()]});
    assert!(Allowlist::parse(&duplicate.to_string(), today()).is_err());
    for invalid in ["{}", "[]", "{\"version\":2,\"entries\":[]}"] {
        assert!(Allowlist::parse(invalid, today()).is_err());
    }
}
#[test]
fn dates_validate_calendar_and_utc_epoch() {
    assert!(Date::parse("2000-02-29").is_ok());
    for value in [
        "1900-02-29",
        "2026-13-01",
        "2026-01-00",
        "2026-1-01",
        "abcd-ef-gh",
    ] {
        assert!(Date::parse(value).is_err());
    }
    assert_eq!(Date::from_epoch_days(0).unwrap(), "1970-01-01");
    assert_eq!(Date::from_epoch_days(20752).unwrap(), "2026-10-26");
}
