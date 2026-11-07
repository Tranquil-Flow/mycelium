// SPDX-License-Identifier: AGPL-3.0-only
//! The signed package documents (`desktop::docs`), verified as the v0.1.4 installer does
//! (`mycelium_a13_packaging/manifest.py`, `install_source.py`, `mycelium_a13_macos/*`,
//! `mycelium_qualification/signing.py` at f4e366e9). Fixtures are signed with throw-away TEST
//! keys (fixed seeds); the production signer is never read. Oracle vectors below were computed
//! once with the oracle's own code outside this repository.
use desktop::{
    docs::{self, FinalizeSpec, Signer},
    install::{self, Observed},
};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
};

static SEQ: AtomicUsize = AtomicUsize::new(0);
const ISSUED: f64 = 1_790_000_000.0;
const NOW: f64 = ISSUED + 10.0;

fn scratch() -> PathBuf {
    let p = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "v02-l11-docs-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir_all(&p).unwrap();
    p
}
fn mac() -> Observed {
    Observed {
        system: "Darwin".into(),
        machine: "arm64".into(),
        macos_version: "26.5.1".into(),
    }
}
fn signer(seed: u8) -> Signer {
    Signer::from_seed([seed; 32], docs::SIGNER_ENDPOINT_ID)
}
/// A finalized bundle directory (app payload + signed documents) for `version`.
fn bundle(seed: u8, version: &str, floor: &str) -> PathBuf {
    let dir = scratch();
    let exe = dir.join("Mycelium A13 Demo.app/Contents/MacOS/mycelium");
    fs::create_dir_all(exe.parent().unwrap()).unwrap();
    fs::write(&exe, b"exe").unwrap();
    docs::finalize(
        &dir,
        &signer(seed),
        &FinalizeSpec {
            version,
            rollback_floor: floor,
            source_revision: "0123456789abcdef0123456789abcdef01234567",
            issued_at: ISSUED,
            minimum_os: "15.6.1",
        },
    )
    .unwrap();
    dir
}
fn read(dir: &Path, name: &str) -> Value {
    serde_json::from_slice(&fs::read(dir.join(name)).unwrap()).unwrap()
}
fn write(dir: &Path, name: &str, value: &Value) {
    fs::write(dir.join(name), serde_json::to_vec_pretty(value).unwrap()).unwrap();
}
/// Re-sign `manifest` (minus its signature) with `seed`'s key: isolates structure checks from
/// the signature check.
fn resign(manifest: &mut Value, seed: u8) {
    let mut statement = manifest.clone();
    statement.as_object_mut().unwrap().remove("signature");
    manifest["signature"] = signer(seed).sign(&statement).unwrap();
}
fn verify(dir: &Path) -> Result<docs::Verified, &'static str> {
    docs::verify_documents(dir, &mac(), None, NOW).map_err(|e| e.0)
}
fn pin(seed: u8) -> String {
    signer(seed).verification_key_digest()
}

// ---- canonical JSON and the signature against the oracle ------------------------------------------

#[test]
fn python_float_repr_vectors_match_the_oracle() {
    for (value, expected) in [
        (1_792_838_370.475_772, "1792838370.475772"),
        (1_790_000_000.0, "1790000000.0"),
        (1e16, "1e+16"),
        (1e-5, "1e-05"),
        (0.0001, "0.0001"),
        (123_456_789_012_345_678.0, "1.2345678901234568e+17"),
        (1.5e-7, "1.5e-07"),
        (5e-324, "5e-324"),
        (0.5, "0.5"),
        (100.0, "100.0"),
        (1e22, "1e+22"),
        (9_999_999_999_999_998.0, "9999999999999998.0"),
        (-2.5e-10, "-2.5e-10"),
        (1.797_693_134_862_315_7e308, "1.7976931348623157e+308"),
        (12_345_678.0, "12345678.0"),
    ] {
        assert_eq!(docs::canonical_float(value), expected, "{value:e}");
    }
}

fn nasty() -> Value {
    json!({
        "b": 1,
        "a": [1.5, "é\u{1}\"\\\n", null, true, false, 1_000_000_000_000_000_i64],
        "c": {"z": 0.1, "é": -0.0},
        "issued_at": 1_790_000_000.0,
    })
}
#[test]
fn canonical_json_matches_the_oracle_bytes() {
    assert_eq!(
        String::from_utf8(docs::canonical_json(&nasty()).unwrap()).unwrap(),
        "{\"a\":[1.5,\"é\\u0001\\\"\\\\\\n\",null,true,false,1000000000000000],\"b\":1,\"c\":{\"z\":0.1,\"é\":-0.0},\"issued_at\":1790000000.0}"
    );
}
#[test]
fn canonical_json_refuses_what_the_oracle_refuses() {
    let mut deep = json!(0);
    for _ in 0..49 {
        deep = json!([deep]);
    }
    assert!(docs::canonical_json(&deep).is_none(), "depth 49 > 48");
    let mut ok = json!(0);
    for _ in 0..48 {
        ok = json!([ok]);
    }
    assert!(docs::canonical_json(&ok).is_some(), "depth 48 is allowed");
    assert!(
        docs::canonical_json(&Value::Array(vec![json!(0); 100_000])).is_none(),
        "100001 nodes"
    );
    assert!(
        docs::canonical_json(&Value::Array(vec![json!(0); 99_999])).is_some(),
        "100000 nodes"
    );
}
#[test]
fn ed25519_signing_equals_the_oracles_deterministic_signature() {
    // `Ed25519EvidenceSigner` at f4e366e9, seed = 32 x 0x07, over `nasty()`.
    let signer = Signer::from_seed([7; 32], "mycelium-macos-release");
    assert_eq!(
        signer.public_key_record(),
        json!({
            "algorithm": "ed25519", "encoding": "base64",
            "verification_key": "6kpsY+KcUgq+9VB7Ey7F+ZVHdq6+vnuSQh7qaRRG0iw=",
            "verification_key_digest": "sha256:fe812c12f3ab4ce6ac5db69ac352f906cb1b11ef43fb33e252ef7ff552263889",
        })
    );
    assert_eq!(
        signer.sign(&nasty()).unwrap(),
        json!({
            "algorithm": "ed25519", "signer_endpoint_id": "mycelium-macos-release",
            "verification_key_digest": "sha256:fe812c12f3ab4ce6ac5db69ac352f906cb1b11ef43fb33e252ef7ff552263889",
            "signed_statement_digest": "sha256:e3221c8f4e915f5a0a1229c9dd07b8be9e0263485d7ba682a5999984cb63f11d",
            "signature": "VulwT2MMP45SDB2E3lIMylQN9/kGrVx75BY9hjHmUNjzQddf9gNpGQJsgXF1LiYnnOICeVhbH6RqpseL/eBxCA==",
        })
    );
}

// ---- the real v0.1.2 release bundle ----------------------------------------------------------------

fn release() -> PathBuf {
    PathBuf::from(std::env::var("MYCELIUM_V01X_BUNDLE").expect("MYCELIUM_V01X_BUNDLE"))
}
/// Only the two documents of the real release, copied to scratch for mutation.
fn release_documents() -> PathBuf {
    let dir = scratch();
    for name in [docs::MANIFEST_NAME, docs::SOURCE_NAME] {
        fs::copy(release().join(name), dir.join(name)).unwrap();
    }
    dir
}
fn real_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

#[test]
#[ignore = "reads a real extracted v0.1.x release bundle; set MYCELIUM_V01X_BUNDLE (read only)"]
fn the_real_v012_release_signature_verifies_with_the_pinned_publisher_key() {
    let dir = release();
    let verified = docs::verify_documents(&dir, &mac(), None, real_now())
        .map_err(|e| e.0)
        .unwrap();
    assert_eq!(verified.version, "0.1.2");
    assert_eq!(verified.publisher_pin_source, docs::FIRST_USE_PIN);
    assert_eq!(
        verified.publisher_key_digest,
        "sha256:582670de045ef8d5ef30252e26f90ee0a3aa0f7963f5343f800bea611b7da1e8"
    );
    assert_eq!(verified.rollback_floor, "0.1.0");
    assert_eq!(
        verified.source_revision,
        "f8488106ebd56b1ab3285ed8b83796c0c918d0d3"
    );
    assert_eq!(verified.developer_id_team, docs::DEVELOPER_ID_UNSIGNED);
    let digest = install::bundle_digest(&dir.join(install::PAYLOAD_DIRECTORY)).unwrap();
    assert_eq!(verified.expected_digests, [digest.clone(), digest]);
    // Pinned to the same key (a later install/rollback): also accepted.
    assert!(
        docs::verify_documents(
            &dir,
            &mac(),
            Some((
                &verified.publisher_key_digest,
                "first_use_owner_private_media",
                "0.1.0"
            )),
            real_now()
        )
        .is_ok()
    );
}

#[test]
#[ignore = "reads a real extracted v0.1.x release bundle; set MYCELIUM_V01X_BUNDLE (read only)"]
fn tampered_or_wrong_key_copies_of_the_real_release_are_refused_with_v014s_codes() {
    let verify_real = |dir: &Path, installed: Option<(&str, &str, &str)>, now: f64| {
        docs::verify_documents(dir, &mac(), installed, now)
            .map_err(|e| e.0)
            .map(|v| v.version)
    };
    // Any byte of a signed field.
    let dir = release_documents();
    let mut manifest = read(&dir, docs::MANIFEST_NAME);
    manifest["source_revision"] = "f8488106ebd56b1ab3285ed8b83796c0c918d0d4".into();
    write(&dir, docs::MANIFEST_NAME, &manifest);
    assert_eq!(
        verify_real(&dir, None, real_now()),
        Err("package_signature_invalid")
    );
    // A different package digest bound under the same signature.
    let dir = release_documents();
    let mut manifest = read(&dir, docs::MANIFEST_NAME);
    manifest["package_digest"] = format!("sha256:{}", "0".repeat(64)).into();
    write(&dir, docs::MANIFEST_NAME, &manifest);
    assert_eq!(
        verify_real(&dir, None, real_now()),
        Err("package_signature_invalid")
    );
    // The signature replaced by another key's signature, same statement: wrong key.
    let dir = release_documents();
    let mut manifest = read(&dir, docs::MANIFEST_NAME);
    let intruder = signer(9);
    manifest["publisher_identity"]["verification_key_record"] = intruder.public_key_record();
    resign(&mut manifest, 9);
    write(&dir, docs::MANIFEST_NAME, &manifest);
    // Correctly signed by the intruder, but the install source still names the genuine key... the
    // manifest alone is self-consistent, so the PIN decides: first use would pin the intruder,
    // an installed record pinned to the genuine release key refuses.
    assert_eq!(
        verify_real(
            &dir,
            Some((
                "sha256:582670de045ef8d5ef30252e26f90ee0a3aa0f7963f5343f800bea611b7da1e8",
                "first_use_owner_private_media",
                "0.1.0"
            )),
            real_now()
        ),
        Err("package_publisher_mismatch")
    );
    // Wrong key record whose digest does not match its bytes.
    let dir = release_documents();
    let mut manifest = read(&dir, docs::MANIFEST_NAME);
    manifest["publisher_identity"]["verification_key_record"]["verification_key"] =
        intruder.public_key_record()["verification_key"].clone();
    write(&dir, docs::MANIFEST_NAME, &manifest);
    assert_eq!(
        verify_real(&dir, None, real_now()),
        Err("package_publisher_invalid")
    );
    // Expired (one second past `expires_at` 1800614370.475772).
    let dir = release_documents();
    assert_eq!(
        verify_real(&dir, None, 1_800_614_371.0),
        Err("package_expired")
    );
    // Rollback floor pin above the package's floor.
    assert_eq!(
        verify_real(
            &dir,
            Some((
                "sha256:582670de045ef8d5ef30252e26f90ee0a3aa0f7963f5343f800bea611b7da1e8",
                "first_use_owner_private_media",
                "0.1.1"
            )),
            real_now()
        ),
        Err("package_downgrade_rejected")
    );
    // The install source: any edit leaves the manifest valid but the source refused.
    let dir = release_documents();
    let mut source = read(&dir, docs::SOURCE_NAME);
    source["install_mode"] = "windows_service".into();
    write(&dir, docs::SOURCE_NAME, &source);
    assert_eq!(
        verify_real(&dir, None, real_now()),
        Err("install_source_install_mode_invalid")
    );
}

// ---- manifest structure, in the oracle's order ---------------------------------------------------------

#[test]
fn a_fresh_signed_bundle_verifies_and_reports_the_version_from_the_artifact_name() {
    let dir = bundle(1, "0.2.0", "0.1.0");
    let v = verify(&dir).unwrap();
    assert_eq!(
        (v.version.as_str(), v.rollback_floor.as_str()),
        ("0.2.0", "0.1.0")
    );
    assert_eq!(v.publisher_key_digest, pin(1));
    assert_eq!(v.publisher_pin_source, docs::FIRST_USE_PIN);
    let digest = install::bundle_digest(&dir.join(install::PAYLOAD_DIRECTORY)).unwrap();
    assert_eq!(v.expected_digests, [digest.clone(), digest]);
    // The same signed bytes verify identically twice (no clock or randomness in verification).
    assert_eq!(verify(&dir).unwrap().version, "0.2.0");
}

#[test]
fn manifest_mutations_report_the_oracles_codes() {
    type Edit = fn(&mut Value);
    let cases: Vec<(&str, Edit, &str)> = vec![
        (
            "extra field",
            |m| m["surprise"] = 1.into(),
            "package_manifest_shape_invalid",
        ),
        (
            "missing field",
            |m| {
                m.as_object_mut().unwrap().remove("privacy_notice");
            },
            "package_manifest_shape_invalid",
        ),
        (
            "protocol",
            |m| m["protocol"] = "mycelium.other.v1".into(),
            "package_manifest_protocol_invalid",
        ),
        (
            "embedded secret key name",
            |m| m["build_inputs"]["Invite"] = 1.into(),
            "package_embedded_secret",
        ),
        (
            "hidden URL",
            |m| m["privacy_notice"] = "see https://x.example".into(),
            "package_hidden_download_url",
        ),
        (
            "short source revision",
            |m| m["source_revision"] = "abc".into(),
            "package_source_revision_invalid",
        ),
        (
            "build inputs",
            |m| m["build_inputs"]["extra"] = 1.into(),
            "package_build_inputs_invalid",
        ),
        (
            "build digest",
            |m| m["build_inputs"]["build_digest"] = "nope".into(),
            "package_build_inputs_invalid",
        ),
        (
            "platform keys",
            |m| m["platform_architecture"]["extra"] = 1.into(),
            "package_platform_invalid",
        ),
        (
            "platform system",
            |m| m["platform_architecture"]["system"] = "linux".into(),
            "package_platform_invalid",
        ),
        (
            "platform machine",
            |m| m["platform_architecture"]["machine"] = "x86_64".into(),
            "package_platform_invalid",
        ),
        (
            "platform version",
            |m| m["platform_architecture"]["macos_version"] = "26".into(),
            "package_platform_invalid",
        ),
        (
            "package digest",
            |m| m["package_digest"] = "sha256:ABC".into(),
            "package_digest_invalid",
        ),
        (
            "publisher keys",
            |m| m["publisher_identity"]["extra"] = 1.into(),
            "package_publisher_invalid",
        ),
        (
            "publisher id",
            |m| m["publisher_identity"]["publisher_id"] = "-bad".into(),
            "package_publisher_invalid",
        ),
        (
            "developer team",
            |m| m["publisher_identity"]["developer_id_team"] = "abc".into(),
            "package_publisher_invalid",
        ),
        (
            "key record",
            |m| m["publisher_identity"]["verification_key_record"]["encoding"] = "hex".into(),
            "package_publisher_invalid",
        ),
        (
            "minimum os",
            |m| m["minimum_os"] = "macos 15".into(),
            "package_minimum_os_invalid",
        ),
        (
            "descriptor",
            |m| m["service_descriptor"]["lifecycle_support"] = false.into(),
            "package_lifecycle_support_missing",
        ),
        (
            "descriptor mode",
            |m| m["service_descriptor"]["install_mode"] = "launchd_system".into(),
            "package_lifecycle_support_missing",
        ),
        (
            "channel",
            |m| m["update_channel"] = "bad channel".into(),
            "package_update_channel_invalid",
        ),
        (
            "floor",
            |m| m["rollback_floor"] = "1.x".into(),
            "package_rollback_floor_invalid",
        ),
        (
            "capability",
            |m| m["capability_protocol"] = "other.v1".into(),
            "package_capability_protocol_invalid",
        ),
        (
            "privacy",
            |m| m["privacy_notice"] = "".into(),
            "package_privacy_notice_invalid",
        ),
        (
            "expiry policy",
            |m| m["expiry_revocation_policy"] = "a b".into(),
            "package_expiry_policy_invalid",
        ),
        (
            "times",
            |m| m["expires_at"] = (ISSUED - 1.0).into(),
            "package_manifest_shape_invalid",
        ),
        (
            "time type",
            |m| m["issued_at"] = "x".into(),
            "package_manifest_shape_invalid",
        ),
        (
            "signature shape",
            |m| m["signature"] = json!({"algorithm": "ed25519"}),
            "package_signature_invalid",
        ),
        (
            "signature bytes",
            |m| m["signature"]["signature"] = "AAAA".into(),
            "package_signature_invalid",
        ),
        (
            "signature algorithm",
            |m| m["signature"]["algorithm"] = "rsa".into(),
            "package_signature_invalid",
        ),
        (
            "publisher label (signed, so only the label is wrong)",
            |m| m["publisher_identity"]["publisher_id"] = "someone-else".into(),
            "package_publisher_mismatch",
        ),
    ];
    for (name, edit, expected) in cases {
        let dir = bundle(1, "0.2.0", "0.1.0");
        let mut manifest = read(&dir, docs::MANIFEST_NAME);
        edit(&mut manifest);
        // Structure/value checks precede the signature, so most cases need no re-signing; the
        // ones that must reach later checks are re-signed over the edited statement.
        if matches!(name, s if s.starts_with("publisher label")) {
            resign(&mut manifest, 1);
        }
        write(&dir, docs::MANIFEST_NAME, &manifest);
        assert_eq!(verify(&dir).map(|v| v.version), Err(expected), "{name}");
    }
}

#[test]
fn an_edit_to_any_signed_binding_without_re_signing_breaks_the_signature() {
    for (field, value) in [
        (
            "source_revision",
            json!("ffffffffffffffffffffffffffffffffffffffff"),
        ),
        (
            "package_digest",
            json!(format!("sha256:{}", "1".repeat(64))),
        ),
        ("update_channel", json!("signed_update_channel")),
        ("rollback_floor", json!("0.0.1")),
        ("minimum_os", json!("macos-15.6.0")),
        ("privacy_notice", json!("other-notice")),
        ("expires_at", json!(ISSUED + 999_999_999.0)),
        ("issued_at", json!(ISSUED - 1.0)),
    ] {
        let dir = bundle(1, "0.2.0", "0.1.0");
        let mut manifest = read(&dir, docs::MANIFEST_NAME);
        manifest[field] = value;
        write(&dir, docs::MANIFEST_NAME, &manifest);
        assert_eq!(
            verify(&dir).map(|v| v.version),
            Err("package_signature_invalid"),
            "{field}"
        );
    }
}

#[test]
fn signature_clock_publisher_pin_platform_and_floor_checks_follow_the_oracles_order() {
    let dir = bundle(1, "0.2.0", "0.1.0");
    // expiry: 90 days
    let expires = ISSUED + 90.0 * 86400.0;
    assert!(
        docs::verify_documents(&dir, &mac(), None, expires).is_ok(),
        "now == expires_at is valid"
    );
    assert_eq!(
        docs::verify_documents(&dir, &mac(), None, expires + 0.5)
            .map(|v| v.version)
            .map_err(|e| e.0),
        Err("package_expired")
    );
    // pinned to another key
    assert_eq!(
        docs::verify_documents(
            &dir,
            &mac(),
            Some((&pin(2), "first_use_owner_private_media", "0.1.0")),
            NOW
        )
        .map(|v| v.version)
        .map_err(|e| e.0),
        Err("package_publisher_mismatch")
    );
    // a bad floor pin value in the record
    assert_eq!(
        docs::verify_documents(
            &dir,
            &mac(),
            Some((&pin(1), "first_use_owner_private_media", "x")),
            NOW
        )
        .map(|v| v.version)
        .map_err(|e| e.0),
        Err("package_rollback_floor_invalid")
    );
    // pin above the package floor
    assert_eq!(
        docs::verify_documents(
            &dir,
            &mac(),
            Some((&pin(1), "first_use_owner_private_media", "0.1.1")),
            NOW
        )
        .map(|v| v.version)
        .map_err(|e| e.0),
        Err("package_downgrade_rejected")
    );
    // platform
    for observed in [
        Observed {
            macos_version: "15.6.0".into(),
            ..mac()
        },
        Observed {
            macos_version: "26.5.0".into(),
            ..mac()
        },
        Observed {
            macos_version: String::new(),
            ..mac()
        },
    ] {
        assert_eq!(
            docs::verify_documents(&dir, &observed, None, NOW)
                .map(|v| v.version)
                .map_err(|e| e.0),
            Err("package_platform_unsupported"),
            "{observed:?}"
        );
    }
    for observed in [
        Observed {
            machine: "x86_64".into(),
            ..mac()
        },
        Observed {
            system: "Linux".into(),
            ..mac()
        },
    ] {
        assert_eq!(
            docs::verify_documents(&dir, &observed, None, NOW)
                .map(|v| v.version)
                .map_err(|e| e.0),
            Err("platform_unsupported")
        );
    }
    for version in ["26.5.1", "15.6.1"] {
        let observed = Observed {
            macos_version: version.into(),
            ..mac()
        };
        assert!(
            docs::verify_documents(&dir, &observed, None, NOW).is_ok(),
            "{version}"
        );
    }
}

// ---- install source ------------------------------------------------------------------------------------

#[test]
fn install_source_mutations_report_the_oracles_codes() {
    type Edit = fn(&mut Value);
    let cases: Vec<(&str, Edit, &str)> = vec![
        (
            "extra field",
            |s| s["extra"] = 1.into(),
            "install_source_shape_invalid",
        ),
        (
            "protocol",
            |s| s["protocol"] = "x".into(),
            "install_source_protocol_invalid",
        ),
        (
            "hidden url",
            |s| s["minimum_os"] = "macos://x".into(),
            "install_source_hidden_url",
        ),
        (
            "channel",
            |s| s["channel"] = "beta".into(),
            "install_source_channel_invalid",
        ),
        (
            "unhashable channel",
            |s| s["channel"] = json!(["x"]),
            "installer_failed",
        ),
        (
            "package class",
            |s| s["package_class"] = "zip".into(),
            "install_source_package_class_invalid",
        ),
        (
            "artifact name",
            |s| s["artifact_name"] = "a/b".into(),
            "install_source_artifact_invalid",
        ),
        (
            "artifact digest",
            |s| s["artifact_digest"] = "sha256:1".into(),
            "install_source_digest_invalid",
        ),
        (
            "publisher keys",
            |s| s["publisher_identity"]["x"] = 1.into(),
            "install_source_publisher_invalid",
        ),
        (
            "publisher endpoint",
            |s| s["publisher_identity"]["endpoint_id"] = "a b".into(),
            "install_source_publisher_invalid",
        ),
        (
            "team on owner channel",
            |s| s["publisher_identity"]["developer_id_team"] = "ABCDEFGHIJ".into(),
            "install_source_publisher_invalid",
        ),
        (
            "key record algorithm",
            |s| s["publisher_identity"]["verification_key_record"]["algorithm"] = "rsa".into(),
            "install_source_publisher_invalid",
        ),
        (
            "key record digest",
            |s| {
                s["publisher_identity"]["verification_key_record"]["verification_key_digest"] =
                    "x".into()
            },
            "install_source_publisher_invalid",
        ),
        (
            "minimum os",
            |s| s["minimum_os"] = "".into(),
            "install_source_minimum_os_invalid",
        ),
        (
            "architecture",
            |s| s["platform_architecture"] = "x86_64".into(),
            "install_source_platform_unsupported",
        ),
        (
            "service manager",
            |s| s["service_manager"] = "systemd".into(),
            "install_source_lifecycle_support_missing",
        ),
        (
            "install mode",
            |s| s["install_mode"] = "x".into(),
            "install_source_install_mode_invalid",
        ),
        (
            "floor",
            |s| s["rollback_floor"] = "a".into(),
            "install_source_rollback_floor_invalid",
        ),
        (
            "lifecycle",
            |s| s["lifecycle_support"] = false.into(),
            "install_source_lifecycle_support_missing",
        ),
        (
            "expert tools",
            |s| s["expert_tools_required"] = json!(["ssh"]),
            "install_source_expert_tools_required",
        ),
        (
            "publisher label",
            |s| s["publisher_identity"]["publisher_id"] = "other".into(),
            "install_source_publisher_mismatch",
        ),
        (
            "source floor above the manifest floor",
            |s| s["rollback_floor"] = "0.3.0".into(),
            "install_source_downgrade_rejected",
        ),
        (
            "artifact name shape",
            |s| s["artifact_name"] = "mycelium-1.0-macos-arm64".into(),
            "artifact_name_invalid",
        ),
    ];
    for (name, edit, expected) in cases {
        let dir = bundle(1, "0.2.0", "0.1.0");
        let mut source = read(&dir, docs::SOURCE_NAME);
        edit(&mut source);
        write(&dir, docs::SOURCE_NAME, &source);
        assert_eq!(verify(&dir).map(|v| v.version), Err(expected), "{name}");
    }
}

#[test]
fn install_source_with_a_floor_below_the_pin_is_refused() {
    let dir = bundle(1, "0.2.0", "0.1.0");
    let mut source = read(&dir, docs::SOURCE_NAME);
    source["rollback_floor"] = "0.0.1".into();
    write(&dir, docs::SOURCE_NAME, &source);
    // First use: pin "0.0.0" is below 0.0.1: fine. With the manifest's own pin 0.1.0 it is not.
    assert!(verify(&dir).is_ok());
    assert_eq!(
        docs::verify_documents(
            &dir,
            &mac(),
            Some((&pin(1), "first_use_owner_private_media", "0.1.0")),
            NOW
        )
        .map(|v| v.version)
        .map_err(|e| e.0),
        Err("install_source_downgrade_rejected")
    );
}

// ---- reading the documents ---------------------------------------------------------------------------------

#[test]
fn missing_linked_or_malformed_documents_are_bundle_incomplete() {
    for name in [docs::MANIFEST_NAME, docs::SOURCE_NAME] {
        let dir = bundle(1, "0.2.0", "0.1.0");
        fs::remove_file(dir.join(name)).unwrap();
        assert_eq!(
            verify(&dir).map(|v| v.version),
            Err("bundle_incomplete"),
            "missing {name}"
        );
        let dir = bundle(1, "0.2.0", "0.1.0");
        fs::write(dir.join(name), b"{nope").unwrap();
        assert_eq!(
            verify(&dir).map(|v| v.version),
            Err("bundle_incomplete"),
            "malformed {name}"
        );
        let dir = bundle(1, "0.2.0", "0.1.0");
        fs::write(dir.join(name), b"[]").unwrap();
        assert_eq!(
            verify(&dir).map(|v| v.version),
            Err("bundle_incomplete"),
            "not an object {name}"
        );
        let dir = bundle(1, "0.2.0", "0.1.0");
        let copy = dir.join("elsewhere.json");
        fs::rename(dir.join(name), &copy).unwrap();
        std::os::unix::fs::symlink(&copy, dir.join(name)).unwrap();
        assert_eq!(
            verify(&dir).map(|v| v.version),
            Err("bundle_incomplete"),
            "linked {name}"
        );
    }
}

#[test]
fn the_manifest_signature_cannot_be_replayed_onto_another_payload() {
    // A bundle whose payload differs from the signed digest is caught by the installer
    // (`package_digest_mismatch`, tested in install.rs); here the documents of bundle A paired with
    // bundle B's key cannot be re-bound without B's private key.
    let a = bundle(1, "0.2.0", "0.1.0");
    let b = bundle(2, "0.2.0", "0.1.0");
    let mut manifest = read(&a, docs::MANIFEST_NAME);
    manifest["signature"] = read(&b, docs::MANIFEST_NAME)["signature"].clone();
    write(&a, docs::MANIFEST_NAME, &manifest);
    assert_eq!(
        verify(&a).map(|v| v.version),
        Err("package_signature_invalid")
    );
}

#[test]
fn signing_key_files_must_hold_exactly_32_raw_bytes_and_are_never_echoed() {
    let dir = scratch();
    let key = dir.join("test.key");
    fs::write(&key, [5u8; 31]).unwrap();
    assert_eq!(
        Signer::from_key_file(&key, "e").err().map(|e| e.0),
        Some("signing_key_file_must_hold_32_raw_private_bytes")
    );
    fs::write(&key, [5u8; 32]).unwrap();
    let signer = Signer::from_key_file(&key, "e").unwrap();
    assert_eq!(
        signer.verification_key_digest(),
        Signer::from_seed([5; 32], "e").verification_key_digest()
    );
    assert_eq!(
        Signer::from_key_file(&dir.join("absent"), "e")
            .err()
            .map(|e| e.0),
        Some("signing_key_file_unreadable")
    );
}

#[test]
fn the_install_command_asset_is_the_oracles_script_with_one_executable_name_changed() {
    use sha2::{Digest, Sha256};
    let ported = docs::INSTALL_COMMAND;
    assert_eq!(ported.matches("Contents/MacOS/mycelium\"").count(), 1);
    let oracle = ported.replace(
        "Contents/MacOS/mycelium\"",
        "Contents/MacOS/Mycelium A13 Demo\"",
    );
    // `_INSTALL_COMMAND` of finalize_a13_macos.py at f4e366e9 == `Install Mycelium.command` of the
    // real v0.1.2 release (sha256 below).
    assert_eq!(
        format!("{:x}", Sha256::digest(oracle.as_bytes())),
        "668f9752013f60adc41fa773b2a44dbbc88fb9dc0300c3bc92ecae3dbf30e82e"
    );
    assert_eq!(
        format!("{:x}", Sha256::digest(docs::README.as_bytes())),
        "701c27d9f294038ec60b7b7181ca9f8ea32ad7b4cc5922e81aa4624fa28d8e15"
    );
}

#[test]
fn a_well_formed_signature_that_is_not_the_signers_fails_the_ed25519_check() {
    // Every field the verifier can compare without cryptography is consistent: the claimed key is
    // the pinned one, the claimed statement digest is the true one, the bytes are 64 and valid
    // base64. Only the Ed25519 verification can refuse these.
    for kind in ["other_key", "flipped_byte", "other_statement"] {
        let dir = bundle(1, "0.2.0", "0.1.0");
        let mut manifest = read(&dir, docs::MANIFEST_NAME);
        let mut statement = manifest.clone();
        statement.as_object_mut().unwrap().remove("signature");
        match kind {
            "other_key" => {
                let mut forged = signer(9).sign(&statement).unwrap();
                forged["verification_key_digest"] = pin(1).into();
                manifest["signature"] = forged;
            }
            "flipped_byte" => {
                let text = manifest["signature"]["signature"]
                    .as_str()
                    .unwrap()
                    .to_string();
                let first = if text.starts_with('A') { "B" } else { "A" };
                manifest["signature"]["signature"] = format!("{first}{}", &text[1..]).into();
            }
            _ => {
                let other = signer(1).sign(&json!({"another": "statement"})).unwrap();
                manifest["signature"]["signature"] = other["signature"].clone();
            }
        }
        write(&dir, docs::MANIFEST_NAME, &manifest);
        assert_eq!(
            verify(&dir).map(|v| v.version),
            Err("package_signature_invalid"),
            "{kind}"
        );
    }
}
