// SPDX-License-Identifier: AGPL-3.0-only
use std::{fs, path::Path};
use xtask::lint::{Diagnostic, Source, scan_sources};

fn fixtures(kind: &str) -> Vec<Source> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("fixtures")
        .join(kind);
    let mut sources = fs::read_dir(dir)
        .unwrap()
        .map(|entry| {
            let path = entry.unwrap().path();
            Source {
                path: format!(
                    "crates/example/{}",
                    path.file_name().unwrap().to_str().unwrap()
                ),
                text: fs::read_to_string(path).unwrap(),
            }
        })
        .collect::<Vec<_>>();
    sources.sort_by(|a, b| a.path.cmp(&b.path));
    assert!(!sources.is_empty());
    sources
}

#[test]
fn reject_fixtures_have_exact_codes_and_lines() {
    let sources = fixtures("reject");
    let mut expected = Vec::new();
    for source in &sources {
        let mut count = 0;
        for (index, line) in source.text.lines().enumerate() {
            if let Some((_, codes)) = line.split_once("// expect: ") {
                for code in codes.split_whitespace() {
                    expected.push(Diagnostic {
                        path: source.path.clone(),
                        line: index + 1,
                        code: code.into(),
                    });
                    count += 1;
                }
            }
        }
        assert!(count > 0, "fixture lacks oracle: {}", source.path);
    }
    expected.sort();
    let actual = scan_sources(&sources).unwrap();
    assert_eq!(actual, expected);
    let codes = actual
        .iter()
        .map(|d| d.code.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(
        codes,
        [
            "SE001", "SE002", "SE003", "SE004", "SE005", "TM001", "TM002"
        ]
        .into_iter()
        .collect()
    );
}

#[test]
fn accept_fixtures_are_clean() {
    assert_eq!(scan_sources(&fixtures("accept")).unwrap(), vec![]);
}

#[test]
fn budget_exemption_is_component_scoped_and_only_for_time() {
    let text = "fn demo() { let wait = Duration::from_secs(30); let _ = stop(); }";
    for (path, codes) in [
        ("crates/budgets/src/lib.rs", vec!["SE001"]),
        ("crates/budgets_extra/src/lib.rs", vec!["SE001", "TM001"]),
        ("test-support/budgets/src/lib.rs", vec!["SE001", "TM001"]),
    ] {
        let result = scan_sources(&[Source {
            path: path.into(),
            text: text.into(),
        }])
        .unwrap();
        assert_eq!(
            result.iter().map(|d| d.code.as_str()).collect::<Vec<_>>(),
            codes
        );
    }
}

#[test]
fn parse_failure_is_not_a_green_skip() {
    assert!(
        scan_sources(&[Source {
            path: "bad.rs".into(),
            text: "fn {".into()
        }])
        .is_err()
    );
}

#[test]
fn parameter_names_are_resolved_across_source_files() {
    let sources = [
        Source {
            path: "crates/api.rs".into(),
            text: "fn wait(bytes: u64, stop_timeout: u64) {}".into(),
        },
        Source {
            path: "crates/caller.rs".into(),
            text: "fn caller() { wait(1024, 30); }".into(),
        },
    ];
    assert_eq!(
        scan_sources(&sources).unwrap(),
        vec![Diagnostic {
            path: "crates/caller.rs".into(),
            line: 1,
            code: "TM002".into()
        }]
    );
}
