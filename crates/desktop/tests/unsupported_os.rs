// SPDX-License-Identifier: AGPL-3.0-only
//! An unsupported Mac refuses with v0.1.4's words (CL-L11-05). Expected values: the executed
//! v0.1.4 launcher (`mycelium_a13_macos.__main__.main()` with `platform.*` patched), FX-L11-unsupported-os
//! (`fixtures/unsupported-os-oracle.json`, probe `unsupported_os_probe.py` sha256 inside the
//! round-11 evidence dir, oracle f4e366e9): for every case, whether the launch is admitted, the
//! `launcher_refused` stderr line byte for byte (json.dumps default separators), the exit code
//! 2, and the `osascript` argv a Finder launch shows. Admitted cases print nothing and show nothing.
use desktop::entry::{alert_script, platform_refusal};
use serde_json::Value;

const FIXTURE: &str = include_str!("fixtures/unsupported-os-oracle.json");

fn cases() -> Vec<Value> {
    let rows: Vec<Value> = serde_json::from_str(FIXTURE).unwrap();
    assert_eq!(rows.len(), 19, "the recorded oracle cases");
    rows
}
fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v[k].as_str().unwrap()
}

#[test]
fn every_oracle_case_is_admitted_or_refused_alike() {
    let mut refused = 0;
    let mut admitted = 0;
    for case in cases() {
        let (system, machine, version) = (
            s(&case, "system"),
            s(&case, "machine"),
            s(&case, "os_version"),
        );
        let ours = platform_refusal(system, machine, version);
        let oracle_admits = case["preflight"]["exit"] == 0;
        assert_eq!(
            ours.is_none(),
            oracle_admits,
            "{system} {machine} {version:?}: oracle exit {}",
            case["preflight"]["exit"]
        );
        if oracle_admits {
            admitted += 1;
        } else {
            refused += 1;
        }
    }
    // The fixture really exercises both sides (15.6.1 exact exception, 26.x) and the refusals.
    assert_eq!((admitted, refused), (3, 16));
}

#[test]
fn the_refusal_line_exit_code_and_alert_are_v014s_byte_for_byte() {
    for case in cases() {
        if case["preflight"]["exit"] == 0 {
            continue;
        }
        let (system, machine, version) = (
            s(&case, "system"),
            s(&case, "machine"),
            s(&case, "os_version"),
        );
        let refusal = platform_refusal(system, machine, version).expect("refused");
        let launch = &case["launch"];
        assert_eq!(launch["exit"], 2);
        assert_eq!(refusal.exit_code, 2, "{machine} {version:?}");
        assert_eq!(
            format!("{}\n", refusal.event),
            s(launch, "stderr"),
            "{machine} {version:?}"
        );
        let calls = launch["osascript_calls"].as_array().unwrap();
        assert_eq!(calls.len(), 1, "one dialog");
        let argv: Vec<&str> = calls[0]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a.as_str().unwrap())
            .collect();
        assert_eq!(&argv[..2], ["/usr/bin/osascript", "-e"]);
        assert_eq!(
            alert_script(&refusal.message),
            argv[2],
            "{machine} {version:?}"
        );
        // `--preflight` (a probe, not a Finder launch) never shows the dialog.
        assert_eq!(
            case["preflight"]["osascript_calls"]
                .as_array()
                .unwrap()
                .len(),
            0
        );
    }
}

#[test]
fn the_two_user_words_and_the_unknown_version_cap_are_the_oracles() {
    let intel = platform_refusal("Darwin", "x86_64", "15.6.1").unwrap();
    assert!(
        intel
            .message
            .starts_with("This Mac has an Intel processor (macOS 15.6.1).")
    );
    let old = platform_refusal("Darwin", "arm64", "14.5").unwrap();
    assert!(
        old.message
            .starts_with("This Mac runs macOS 14.5. Mycelium A13 Demo needs macOS 26.0 or later.")
    );
    // v0.1.4 shows at most 16 characters of the version, else the word `unknown`.
    let long = platform_refusal("Darwin", "arm64", &format!("{}.0", "9".repeat(17))).unwrap();
    assert!(long.message.starts_with("This Mac runs macOS unknown."));
}
