// SPDX-License-Identifier: AGPL-3.0-only
//! The recipient-encrypted pairing handoff against the EXECUTED oracle
//! (`L10-evidence/r9-probe-handoff.py` -> `fixtures/oracle_handoff.json`, Python 3.14 +
//! `cryptography`, fixed entropy): X25519 / HKDF-SHA256 / ChaCha20-Poly1305 vectors, quota,
//! invite bundles, `approve_pairing_request`, `decrypt_invitation_handoff`,
//! `DemoAuthority.approve_pairing` (approvals store and idempotence) and
//! `RemoteMacMember._translate`. Every outcome - the bytes of a handoff included - must be equal.
use api::{
    handoff::{self, Coordinator, Entropy, crypto},
    schema::{PublicError, Raised},
    wire::Text,
};
use protocol::identity::Identity;
use serde_json::Value;
use std::{fs, os::unix::fs::PermissionsExt, path::Path};

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
fn array<const N: usize>(s: &str) -> [u8; N] {
    hex(s).try_into().unwrap()
}
fn fixture() -> Value {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_handoff.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    fixture
}
fn entropy(doc: &Value) -> Entropy {
    let keys = &doc["keys"];
    Entropy {
        sender_secret: array(keys["sender_private"].as_str().unwrap()),
        salt: array(keys["salt"].as_str().unwrap()),
        aead_nonce: array(keys["aead_nonce"].as_str().unwrap()),
    }
}
fn identity(doc: &Value, which: &str) -> Identity {
    let keys = &doc["keys"];
    Identity::from_secret(
        keys[format!("{which}_endpoint")].as_str().unwrap(),
        &array(keys[format!("{which}_secret")].as_str().unwrap()),
    )
    .unwrap()
}
fn now_of(value: &Value) -> Value {
    value.clone()
}
/// Compares a Rust outcome with the oracle's: value (by wire bytes when the oracle gave them) or
/// exception class/module/code.
fn same(
    got: &Result<Value, PublicError>,
    want: &Value,
    wire: Option<&Value>,
) -> Result<(), String> {
    match (got, want.get("ok")) {
        (Ok(value), Some(ok)) => {
            if value == ok {
                Ok(())
            } else {
                let _ = wire;
                Err(format!("value {value} != {ok}"))
            }
        }
        (Err(error), None) => {
            let (ty, module) = error.raised.identity();
            let err = &want["err"];
            let code = err["code"].as_str().unwrap_or("action_not_completed");
            if err["type"] == ty && err["module"] == module && error.code == code {
                Ok(())
            } else {
                Err(format!("{error:?} ({ty}, {module}) != {err}"))
            }
        }
        (got, _) => Err(format!("{got:?} vs oracle {want}")),
    }
}
fn collect(failures: Vec<String>, what: &str, total: usize) {
    assert!(
        failures.is_empty(),
        "{what}: {} of {total} differ:\n{}",
        failures.len(),
        failures
            .iter()
            .take(12)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}

#[test]
fn primitives_match_the_oracle_library() {
    let doc = fixture();
    let prim = &doc["primitives"];
    for case in prim["x25519"].as_array().unwrap() {
        let scalar: [u8; 32] = array(case["scalar"].as_str().unwrap());
        let point: [u8; 32] = array(case["peer_public"].as_str().unwrap());
        let shared = crypto::x25519(&scalar, &point)
            .map(|s| s.iter().map(|b| format!("{b:02x}")).collect::<String>());
        match (case.get("shared"), case.get("err")) {
            (Some(want), _) => assert_eq!(shared.as_deref(), want.as_str(), "{case}"),
            (None, Some(_)) => assert!(shared.is_none(), "low-order point must be refused: {case}"),
            _ => panic!("{case}"),
        }
        if let Some(public) = case.get("public_of_scalar") {
            let got: String = crypto::x25519_public(&scalar)
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect();
            assert_eq!(public, &Value::from(got));
        }
    }
    for case in prim["hkdf"].as_array().unwrap() {
        let okm = crypto::derive_key_32(
            &hex(case["ikm"].as_str().unwrap()),
            &hex(case["salt"].as_str().unwrap()),
            &hex(case["info"].as_str().unwrap()),
        );
        assert_eq!(okm.to_vec(), hex(case["okm"].as_str().unwrap()));
    }
    let aeads = prim["aead"].as_array().unwrap();
    assert!(aeads.len() >= 38);
    for case in aeads {
        let (key, nonce) = (
            array::<32>(case["key"].as_str().unwrap()),
            array::<12>(case["nonce"].as_str().unwrap()),
        );
        let (pt, aad) = (
            hex(case["plaintext"].as_str().unwrap()),
            hex(case["aad"].as_str().unwrap()),
        );
        let sealed = hex(case["sealed"].as_str().unwrap());
        assert_eq!(
            crypto::aead_seal(&key, &nonce, &pt, &aad),
            sealed,
            "{}",
            case["plaintext"].as_str().unwrap().len()
        );
        assert_eq!(crypto::aead_open(&key, &nonce, &sealed, &aad), Some(pt));
        let mut flipped = sealed.clone();
        let last = flipped.len() - 1;
        flipped[last] ^= 1;
        assert_eq!(crypto::aead_open(&key, &nonce, &flipped, &aad), None);
        let mut wrong_aad = aad.clone();
        wrong_aad.push(0);
        assert_eq!(crypto::aead_open(&key, &nonce, &sealed, &wrong_aad), None);
    }
}

#[test]
fn quota_and_invite_bundles_match_the_oracle() {
    let doc = fixture();
    let mut failures = Vec::new();
    for case in doc["quota_cases"].as_array().unwrap() {
        if let Err(e) = same(&handoff::validate_quota(&case["quota"]), case, None) {
            failures.push(format!("quota {}: {e}", case["label"]));
        }
    }
    for case in doc["invite_cases"].as_array().unwrap() {
        let got = handoff::verify_invite_bundle(&case["bundle"], &now_of(&case["now"])).map(
            |v| serde_json::json!({"payload": v.payload, "seed_key_digest": v.seed_key_digest}),
        );
        if let Err(e) = same(&got, case, None) {
            failures.push(format!("invite {} @ {}: {e}", case["label"], case["now"]));
        }
    }
    let seed = identity(&doc, "seed");
    for case in doc["mint_cases"].as_array().unwrap() {
        let args = case["args"].as_array().unwrap();
        let got = handoff::mint_invite_bundle(
            &seed,
            args[0].as_str().unwrap(),
            args[1].as_str().unwrap(),
            &args[2],
            args[3].as_str().unwrap(),
            args[4].as_f64().unwrap(),
        );
        if let Err(e) = same(&got, case, None) {
            failures.push(format!("mint {}: {e}", case["label"]));
        }
    }
    collect(failures, "quota/invite/mint", 0);
}

#[test]
fn approve_and_decrypt_match_the_oracle_byte_for_byte() {
    let doc = fixture();
    let entropy = entropy(&doc);
    let mut failures = Vec::new();
    // The headline vector: same inputs and entropy, same handoff bytes; the oracle's own
    // handoff decrypts to the oracle's payload.
    let made = handoff::approve_pairing_request(
        &doc["request"],
        &doc["bundle"],
        &doc["quota"],
        &doc["now"],
        &entropy,
    );
    match &made {
        Ok(json)
            if String::from_utf8(json.dumps(Text::Ascii)).unwrap()
                == doc["handoff_wire"].as_str().unwrap() => {}
        other => failures.push(format!("headline handoff bytes: {other:?}")),
    }
    let key: [u8; 32] = array(doc["keys"]["recipient_private"].as_str().unwrap());
    let opened = handoff::decrypt_invitation_handoff(
        &doc["request"],
        &doc["handoff"],
        Some(&key),
        &doc["now"],
    );
    if opened.as_ref().ok() != Some(&doc["payload"]) {
        failures.push(format!("headline payload: {opened:?}"));
    }
    for case in doc["approve_cases"].as_array().unwrap() {
        let got = handoff::approve_pairing_request(
            &case["request"],
            &case["bundle"],
            &case["quota"],
            &case["now"],
            &entropy,
        );
        let wire = got
            .as_ref()
            .ok()
            .map(|j| String::from_utf8(j.dumps(Text::Ascii)).unwrap());
        let as_value = got.map(|j| j.to_value());
        if let Err(e) = same(&as_value, case, None) {
            failures.push(format!("approve {}: {e}", case["label"]));
        } else if let Some(wire) = wire
            && Some(wire.as_str()) != case["wire"].as_str()
        {
            failures.push(format!("approve {}: wire {wire}", case["label"]));
        }
    }
    for case in doc["decrypt_cases"].as_array().unwrap() {
        let key = match &case["key"] {
            Value::String(h) if h.len() == 64 || h.len() == 62 => Some(hex(h)),
            _ => None,
        };
        let got = handoff::decrypt_invitation_handoff(
            &case["request"],
            &case["handoff"],
            key.as_deref(),
            &now_of(&case["now"]),
        );
        if let Err(e) = same(&got, case, None) {
            failures.push(format!("decrypt {}: {e}", case["label"]));
        }
    }
    collect(failures, "approve/decrypt", 0);
}

fn private_dir(root: &Path) {
    fs::create_dir_all(root.join("seed")).unwrap();
    fs::set_permissions(root.join("seed"), fs::Permissions::from_mode(0o700)).unwrap();
}
#[test]
fn authority_approvals_store_is_idempotent_and_exact() {
    let doc = fixture();
    let coordinator = Coordinator {
        signer: identity(&doc, "seed"),
        swarm_id: doc["swarm_id"].as_str().unwrap().to_owned(),
        seed_url: doc["seed_url"].as_str().unwrap().to_owned(),
    };
    let fixed = entropy(&doc);
    let mut failures = Vec::new();
    let cases = doc["authority_cases"].as_array().unwrap();
    assert!(cases.len() >= 29);
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let root = std::env::temp_dir().join(format!(
            "l10-approve-{}-{}",
            std::process::id(),
            label.replace(['\'', '.'], "_")
        ));
        fs::remove_dir_all(&root).unwrap_or(());
        private_dir(&root);
        // `pre`: a pre-seeded approval record (its text is in the oracle's file listing).
        if let Some(pre) = case.get("pre_text").and_then(Value::as_str) {
            let dir = root.join("seed/approvals");
            fs::create_dir_all(&dir).unwrap();
            fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
            let name = case["pre_name"].as_str().unwrap();
            fs::write(dir.join(name), pre).unwrap();
            fs::set_permissions(dir.join(name), fs::Permissions::from_mode(0o600)).unwrap();
        }
        let opts = &case["opts"];
        let started = opts.get("started").is_none_or(|v| v == true);
        let has_coordinator = opts.get("coordinator").is_none_or(|v| v == true);
        let clock = opts
            .get("clock")
            .and_then(Value::as_f64)
            .unwrap_or(doc["now"].as_f64().unwrap());
        for (n, step) in case["steps"].as_array().unwrap().iter().enumerate() {
            let got = handoff::approve_pairing(
                &root,
                started,
                has_coordinator.then_some(&coordinator),
                &step["request"],
                &step["ttl"],
                &step["quota"],
                clock,
                &|| Ok(fixed.clone()),
            );
            let wire = got
                .as_ref()
                .ok()
                .map(|j| String::from_utf8(j.dumps(Text::Ascii)).unwrap());
            let as_value = got.map(|j| j.to_value());
            if let Err(e) = same(&as_value, step, None) {
                failures.push(format!("{label} step {n}: {e}"));
            } else if let Some(wire) = wire
                && Some(wire.as_str()) != step["wire"].as_str()
            {
                failures.push(format!("{label} step {n}: wire {wire}"));
            }
        }
        let mut files = serde_json::Map::new();
        if let Ok(entries) = fs::read_dir(root.join("seed/approvals")) {
            let mut names: Vec<_> = entries
                .map(|e| e.unwrap().file_name().into_string().unwrap())
                .collect();
            names.sort();
            for name in names {
                let path = root.join("seed/approvals").join(&name);
                let mode = format!(
                    "0o{:o}",
                    fs::metadata(&path).unwrap().permissions().mode() & 0o777
                );
                files.insert(
                    name,
                    serde_json::json!({"text": fs::read_to_string(&path).unwrap(), "mode": mode}),
                );
            }
        }
        let want = if case["files"].is_null() {
            Value::Object(serde_json::Map::new())
        } else {
            case["files"].clone()
        };
        if Value::Object(files.clone()) != want {
            failures.push(format!("{label}: files {} != {want}", Value::Object(files)));
        }
        fs::remove_dir_all(&root).unwrap();
    }
    collect(failures, "authority approvals", cases.len());
}

#[test]
fn peer_translate_matches_the_oracle() {
    let doc = fixture();
    for case in doc["translate_cases"].as_array().unwrap() {
        let got = handoff::peer_translate(case["code"].as_str());
        assert_eq!(Some(got.as_str()), case["ok"].as_str(), "{case}");
    }
    let _ = Raised::Peer;
}
