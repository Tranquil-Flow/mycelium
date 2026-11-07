// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 installation package manifest (`mycelium_a13_packaging.manifest` shared core with the Linux, Windows
//! and macOS profiles and their `verify_*_package` entry points) on cases recorded by executing the oracle
//! (`round9-package-manifest-probe.py`, scratch publisher keys): the validated / built / verified output
//! byte for byte, or the exact code; a Python accident must be a refusal. Non-ASCII decimal digits are the one
//! documented divergence (`known_divergence`): the oracle accepts them (Python `isdigit` / `\d`), this port
//! refuses them, and the test pins that refusal.
use protocol::identity::Identity;
use protocol::package_manifest::{
    Expect, Platform, PlatformExpect, build_manifest, validate_manifest, verify_package,
};
use serde_json::Value;

fn platform_of(name: &str) -> Platform {
    match name {
        "linux" => Platform::Linux,
        "windows" => Platform::Windows,
        "macos" => Platform::MacOs,
        other => panic!("unknown platform {other}"),
    }
}

fn s<'a>(args: &'a Value, key: &str) -> &'a str {
    args[key].as_str().unwrap()
}

#[test]
fn package_manifests_match_the_executed_oracle() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round9-package-manifest-vectors.json"
    ))
    .unwrap();
    let signer = Identity::from_secret(
        v["signer"]["endpoint"].as_str().unwrap(),
        &[u8::try_from(v["signer"]["byte"].as_u64().unwrap()).unwrap(); 32],
    )
    .unwrap();
    let (mut accepted, mut refused, mut accidents, mut skipped_now) = (0, 0, 0, 0);
    for case in v["cases"].as_array().unwrap() {
        let at = format!("{}:{}:{}", case["platform"], case["kind"], case["name"]);
        let platform = platform_of(case["platform"].as_str().unwrap());
        let want = &case["result"];
        let got: Result<Value, &'static str> = match case["kind"].as_str().unwrap() {
            "validate" => validate_manifest(&case["document"], platform).map_err(|e| e.0),
            "build" => build_manifest(
                platform,
                &signer,
                &case["issued_at"],
                &case["expires_at"],
                case["bindings"].as_object().unwrap(),
            )
            .map_err(|e| e.0),
            "verify" => {
                let a = &case["args"];
                let Some(now) = a["now"].as_f64() else {
                    assert_eq!(want["code"], "package_manifest_shape_invalid", "{at}");
                    skipped_now += 1;
                    continue;
                };
                let expect = Expect {
                    expected_publisher: s(a, "expected_publisher"),
                    publisher_key_digest_pin: s(a, "publisher_key_digest_pin"),
                    installed_version: s(a, "installed_version"),
                    rollback_floor_pin: s(a, "rollback_floor_pin"),
                    now,
                };
                let platform_expect = match platform {
                    Platform::Linux => PlatformExpect::Linux {
                        architecture: s(a, "expected_architecture"),
                        distribution: s(a, "expected_distribution"),
                    },
                    Platform::Windows => PlatformExpect::Windows {
                        architecture: s(a, "expected_architecture"),
                        authenticode_subject: s(a, "expected_authenticode_subject"),
                        windows_version: s(a, "expected_windows_version"),
                        windows_build: s(a, "expected_windows_build"),
                    },
                    Platform::MacOs => PlatformExpect::MacOs {
                        architecture: s(a, "expected_architecture"),
                        developer_id_team: s(a, "expected_developer_id_team"),
                        observed_macos_version: s(a, "observed_macos_version"),
                    },
                };
                verify_package(&case["document"], &expect, &platform_expect).map_err(|e| e.0)
            }
            other => panic!("{at}: unknown kind {other}"),
        };
        if want["accepted"] == true {
            let got = got.unwrap_or_else(|code| panic!("{at}: refused with {code}"));
            let key = if want.get("document").is_some() {
                "document"
            } else {
                "facts"
            };
            assert_eq!(got, want[key], "{at}");
            accepted += 1;
        } else if let Some(code) = want["code"].as_str() {
            assert_eq!(got.map(|_| ()), Err(code), "{at}");
            refused += 1;
        } else {
            assert!(
                got.is_err(),
                "{at}: oracle crashed ({}); v0.2 must refuse",
                want["python_exception"]
            );
            accidents += 1;
        }
    }
    assert_eq!(
        (accepted, refused, accidents, skipped_now),
        (152, 1578, 19, 9)
    );
}

#[test]
fn non_ascii_decimal_digits_are_refused_where_the_oracle_accepts_them() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round9-package-manifest-vectors.json"
    ))
    .unwrap();
    let divergences = v["known_divergence"].as_array().unwrap();
    assert_eq!(divergences.len(), 5);
    let mut oracle_accepts = 0;
    for case in divergences {
        let platform = platform_of(case["platform"].as_str().unwrap());
        assert!(
            validate_manifest(&case["document"], platform).is_err(),
            "{}",
            case["name"]
        );
        if case["result"]["accepted"] == true {
            oracle_accepts += 1;
        }
    }
    assert_eq!(
        oracle_accepts, 4,
        "the superscript is an oracle accident, the rest are accepted"
    );
}
