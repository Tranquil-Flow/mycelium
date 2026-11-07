// SPDX-License-Identifier: AGPL-3.0-only
use xtask::lint::{Source, scan_sources};
fn source(path: &str, text: &str) -> Source {
    Source {
        path: path.into(),
        text: text.into(),
    }
}
fn collision(now_parameter: &str) -> Vec<Source> {
    vec![
        source(
            "crates/device-harness/src/keeper.rs",
            &format!(
                "pub fn check(io: &mut Io, {now_parameter}: u64, swarm: &str, limits: Limits) {{}}"
            ),
        ),
        source(
            "crates/runtime/src/iroh_receive.rs",
            "impl RouterReceiver { fn check(&self, deadline: u64) {} }",
        ),
        source(
            "crates/device-harness/tests/collision.rs",
            "use device_harness::keeper::*;\nfn test() { check(&mut io, 100, \"swarm\", limits); device_harness::keeper::check(&mut io, 100, \"swarm\", limits); }",
        ),
    ]
}
#[test]
fn unrelated_ufcs_deadline_cannot_reclassify_keeper_clock_observations() {
    assert_eq!(scan_sources(&collision("now")).unwrap(), vec![]);
}
#[test]
fn genuine_deadline_in_identical_import_and_qualified_shapes_still_fails() {
    let diagnostics = scan_sources(&collision("deadline")).unwrap();
    assert_eq!(diagnostics.len(), 1); // findings deduplicate by line/rule, not call
    assert_eq!(
        diagnostics[0].path,
        "crates/device-harness/tests/collision.rs"
    );
    assert_eq!(diagnostics[0].line, 2);
    assert_eq!(diagnostics[0].code, "TM002");
}
#[test]
fn modules_aliases_receivers_and_ufcs_are_scoped_not_bare_names() {
    let sources = [source(
        "crates/example/src/lib.rs",
        r#"
mod clock { pub fn check(io: &Io, now: u64) {} pub struct Watch; impl Watch { pub fn check(&self, now: u64) {} } }
mod timed { pub fn check(io: &Io, deadline: u64) {} pub struct Watch; impl Watch { pub fn check(&self, deadline: u64) {} } }
mod safe { use crate::clock::check as inspect; fn run() { inspect(&io, 30); } }
mod unsafe_calls { use crate::timed::check as inspect; fn run() { inspect(&io, 30); } }
fn run(a: clock::Watch, b: timed::Watch) {
    clock::check(&io, 30);
    timed::check(&io, 30);
    a.check(30);
    b.check(30);
    clock::Watch::check(&a, 30);
    timed::Watch::check(&b, 30);
}
"#,
    )];
    let got = scan_sources(&sources).unwrap();
    assert_eq!(
        got.iter()
            .map(|d| (d.line, d.code.as_str()))
            .collect::<Vec<_>>(),
        vec![(5, "TM002"), (8, "TM002"), (10, "TM002"), (12, "TM002")]
    );
}
#[test]
fn lexical_imports_and_annotated_local_receivers_do_not_leak_between_scopes() {
    let sources = [source(
        "crates/example/src/lib.rs",
        r#"
mod clock { pub fn check(io: &Io, now: u64) {} pub struct Watch; impl Watch { pub fn check(&self, now: u64) {} } }
mod timed { pub fn check(io: &Io, deadline: u64) {} pub struct Watch; impl Watch { pub fn check(&self, deadline: u64) {} } }
fn first() { use crate::timed::check as inspect; inspect(&io, 30); }
fn second() { use crate::clock::check as inspect; inspect(&io, 30); }
fn nested() {
    use crate::clock::check as inspect;
    { use crate::timed::check as inspect; inspect(&io, 30); }
    inspect(&io, 30);
    let w: clock::Watch = make();
    w.check(30);
    { let w: timed::Watch = make(); w.check(30); }
    w.check(30);
}
"#,
    )];
    let got = scan_sources(&sources).unwrap();
    assert_eq!(
        got.iter()
            .map(|d| (d.line, d.code.as_str()))
            .collect::<Vec<_>>(),
        vec![(4, "TM002"), (8, "TM002"), (12, "TM002")]
    );
}
#[test]
fn unresolved_globs_terminate_and_external_time_fallback_still_fires() {
    let source = source(
        "crates/a/src/lib.rs",
        "use external_unknown::*;\nfn run() { opaque(30); }\nfn timed() { wait_timeout(30); }",
    );
    let got = scan_sources(&[source]).unwrap();
    assert_eq!(
        got.iter()
            .map(|d| (d.line, d.code.as_str()))
            .collect::<Vec<_>>(),
        vec![(3, "TM002")]
    );
}
#[test]
fn imported_receiver_with_inferred_type_is_conservatively_checked() {
    let sources = [
        source(
            "crates/a/src/lib.rs",
            "pub struct Watch; impl Watch { pub fn check(&self, deadline: u64) {} }",
        ),
        source(
            "crates/b/src/lib.rs",
            "use a::Watch;\nfn run() { let w = Watch::new(); w.check(30); }",
        ),
    ];
    let got = scan_sources(&sources).unwrap();
    assert_eq!(
        got.iter()
            .map(|d| (d.path.as_str(), d.line, d.code.as_str()))
            .collect::<Vec<_>>(),
        vec![("crates/b/src/lib.rs", 2, "TM002")]
    );
}
#[test]
fn cross_file_imports_super_paths_and_type_aliases_retain_real_deadline_checks() {
    let sources = [
        source(
            "crates/a/src/clock.rs",
            "pub fn check(io: &Io, now: u64) {}",
        ),
        source(
            "crates/a/src/timed.rs",
            "pub fn check(io: &Io, deadline: u64) {} pub struct Watch; impl Watch { pub fn check(&self, deadline: u64) {} }",
        ),
        source(
            "crates/a/src/consumer.rs",
            "use super::timed::{check as inspect, Watch};\ntype Alias = Watch;\nfn run(a: Alias) { inspect(&io, 30); }\nfn method(a: Alias) { a.check(30); }\nfn ufcs(a: Alias) { Alias::check(&a, 30); }",
        ),
    ];
    let got = scan_sources(&sources).unwrap();
    assert_eq!(
        got.iter()
            .map(|d| (d.line, d.code.as_str()))
            .collect::<Vec<_>>(),
        vec![(3, "TM002"), (4, "TM002"), (5, "TM002")]
    );
}
#[test]
fn unknown_receiver_fallback_is_per_crate_and_repeatable_with_indexed_lookup() {
    // Same method name in two crates; an untyped receiver stays conservative in the
    // caller's own crate only. Repeated identical calls must resolve identically.
    let sources = [
        source(
            "crates/alpha/src/lib.rs",
            "pub struct W; impl W { pub fn wait(&self, timeout: u64) {} }\nfn a(x: Unknown) {\n    x.wait(30);\n    x.wait(30);\n}",
        ),
        source(
            "crates/beta/src/lib.rs",
            "pub struct W; impl W { pub fn wait(&self, now: u64) {} }\nfn b(x: Unknown) {\n    x.wait(30);\n    x.wait(30);\n}",
        ),
    ];
    let found: Vec<_> = scan_sources(&sources)
        .unwrap()
        .into_iter()
        .map(|d| (d.path, d.line, d.code))
        .collect();
    assert_eq!(
        found,
        vec![
            ("crates/alpha/src/lib.rs".to_owned(), 3, "TM002".to_owned()),
            ("crates/alpha/src/lib.rs".to_owned(), 4, "TM002".to_owned()),
        ]
    );
}
