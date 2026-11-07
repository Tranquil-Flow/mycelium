// SPDX-License-Identifier: AGPL-3.0-only
use xtask::lint::{Source, scan_sources};
#[test]
fn wildcard_propagation_and_recorded_best_effort_are_accepted() {
    let source = Source {
        path: "crates/runtime.rs".into(),
        text: r#"
fn cleanup() -> Result<(), E> { let _ = stop()?; Ok(()) }
fn best_effort() { if let Err(error) = stop() { record_error("STOP_FAILED", &error, "cleanup"); } }
"#
        .into(),
    };
    assert!(scan_sources(&[source]).unwrap().is_empty());
}
#[test]
fn cfg_disabled_sources_are_checked_and_comments_and_strings_are_not() {
    let sources = [Source {
        path: "crates/runtime.rs".into(),
        text: r#"
// let _ = stop();
#[cfg(any())] fn disabled() { let _ = stop(); }
fn strings() { let message = "Duration::from_secs(30)"; }
"#
        .into(),
    }];
    let result = scan_sources(&sources).unwrap();
    assert_eq!(result.len(), 1);
    assert_eq!(result[0].code, "SE001");
    assert_eq!(result[0].line, 3);
}
