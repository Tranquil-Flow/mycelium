// SPDX-License-Identifier: AGPL-3.0-only
//! The canonical https public origin (`mycelium_internet.bootstrap.canonical_https_origin`) lives in
//! `protocol::origin`, and the seed server enforces it on `public_origin` exactly where v0.1.4 does
//! (`SeedHTTPServer.__init__`: `canonical_https_origin(public_seed_url)` before anything is bound).
//!
//! Executed oracle FX-L5-origin-r14 (`round14-origin-probe.py` -> `fixtures/oracle_origin_r14.json`, v0.1.4
//! f4e366e9, Python 3.14.8): 71 values; for each the verdict of `canonical_https_origin`, of
//! `LocalTLSSeedServer(public_origin=...)` and of `SeedHTTPServer(public_seed_url=...)` (accepted -> the seed URL
//! the coordinator is bound to; refused -> `ValueError("origin is invalid")` with the coordinator left unbound),
//! and the check order (a bad port is reported before a bad origin).
//!
//! Behaviour: B-SWARM-28 (a malformed advertised URL is refused before the seed serves anything; this test covers
//! the https-origin validation of the advertised/public origin only) and the CL-L5-03 strict-parsing claim.
use protocol::origin::canonical_https_origin;
use seed::{Coordinator, Options, server::Server, tls};
use serde_json::Value;
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf, sync::Arc};

const FIXTURE_CA: &[u8] = include_bytes!("../fixtures/v014_tls_material/ca.pem");
const FIXTURE_SERVER: &[u8] = include_bytes!("../fixtures/v014_tls_material/server.pem");
const FIXTURE_KEY: &[u8] = include_bytes!("../fixtures/v014_tls_material/server.key");

fn oracle() -> Value {
    let doc: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_origin_r14.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    assert_eq!(doc["cases"].as_array().unwrap().len(), 71);
    doc
}

fn scratch(name: &str) -> PathBuf {
    // The store refuses a symlink on the path and macOS keeps `/var` -> `/private/var`.
    let base = fs::canonicalize(std::env::temp_dir()).unwrap();
    let dir = base.join(format!("l5r14-origin-{}-{name}", std::process::id()));
    fs::remove_dir_all(&dir).unwrap_or(());
    fs::create_dir_all(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    dir
}

fn material(name: &str) -> (PathBuf, tls::Material) {
    let root = scratch(name);
    let dir = root.join("tls");
    fs::create_dir(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    for (file, body) in [
        ("ca.pem", FIXTURE_CA),
        ("server.pem", FIXTURE_SERVER),
        ("server.key", FIXTURE_KEY),
    ] {
        fs::write(dir.join(file), body).unwrap();
        fs::set_permissions(dir.join(file), fs::Permissions::from_mode(0o600)).unwrap();
    }
    let material = tls::ensure_local_tls_material(&dir).unwrap();
    (root, material)
}

fn coordinator(root: &std::path::Path, index: usize) -> Arc<Coordinator> {
    let signer = protocol::identity::Identity::from_secret("seed-endpoint", &[21u8; 32]).unwrap();
    let options = Options::new("swarm-authority", "owner-node", "seed-boot-1").unwrap();
    Arc::new(
        Coordinator::open(
            &root.join(format!("seed-{index}.sqlite")),
            signer,
            options,
            Box::new(|| 1_700_000_000.0),
            Box::new(|| "id".to_owned()),
        )
        .unwrap_or_else(|e| panic!("open: {}", e.code)),
    )
}

/// The value of one recorded case as the Rust API takes it: text, or something that is not text.
fn value_of(case: &Value) -> Value {
    match case["kind"].as_str().unwrap() {
        "text" => Value::String(case["text"].as_str().unwrap().to_owned()),
        "none" => Value::Null,
        "int" => Value::from(5),
        "bytes" | "list" => {
            Value::Array(vec![Value::String("https://seed.mycelium.now".to_owned())])
        }
        other => panic!("unknown kind {other}"),
    }
}

/// `protocol::origin::canonical_https_origin` answers every recorded value as the oracle's function does: the
/// same canonical text for an accepted value, a refusal otherwise.
#[test]
fn the_protocol_validator_equals_the_oracle_on_every_recorded_value() {
    let doc = oracle();
    let mut accepted = 0;
    for case in doc["cases"].as_array().unwrap() {
        let label = case["label"].as_str().unwrap();
        let verdict = canonical_https_origin(&value_of(case));
        let recorded = &case["canonical"];
        if recorded["ok"] == true {
            let canonical = verdict.unwrap_or_else(|_| panic!("{label}: refused"));
            assert_eq!(canonical, recorded["value"].as_str().unwrap(), "{label}");
            accepted += 1;
        } else {
            assert!(verdict.is_err(), "{label}: accepted");
            assert_eq!(recorded["message"], "origin is invalid", "{label}");
        }
    }
    assert_eq!(accepted, 18);
}

/// The TLS server and the plain server refuse exactly the values the oracle's servers refuse, with the oracle's
/// text (`ValueError("origin is invalid")`, an accident without a closed code), before binding anything; they
/// bind the coordinator to an accepted origin; `None` still means "the server's own URL".
#[test]
fn the_seed_servers_enforce_the_canonical_origin_as_the_oracle_does() {
    let doc = oracle();
    let (root, material) = material("servers");
    let mut checked = 0;
    for (index, case) in doc["cases"].as_array().unwrap().iter().enumerate() {
        let label = case["label"].as_str().unwrap();
        let origin = match case["kind"].as_str().unwrap() {
            "text" => Some(case["text"].as_str().unwrap()),
            "none" => None,
            // Not text: the typed `Option<&str>` cannot express them (checked on the validator above).
            _ => continue,
        };
        for (variant, recorded) in [("tls", &case["tls"]), ("plain", &case["plain"])] {
            let seed = coordinator(&root, index * 2 + usize::from(variant == "plain"));
            let got = if variant == "tls" {
                Server::bind_tls(seed.clone(), &material, origin)
            } else {
                Server::bind_plain(seed.clone(), origin)
            };
            if recorded["ok"] == true {
                let server = got.unwrap_or_else(|e| panic!("{label} {variant}: {}", e.code));
                assert_eq!(
                    seed.seed_url().as_deref(),
                    Some(origin.unwrap_or(server.base_url())),
                    "{label} {variant}"
                );
                server.close();
            } else {
                let error = got
                    .err()
                    .unwrap_or_else(|| panic!("{label} {variant}: bound"));
                assert_eq!(error.code, recorded["message"].as_str().unwrap(), "{label}");
                assert!(
                    error.accident,
                    "{label} {variant}: a ValueError has no code"
                );
                assert_eq!(seed.seed_url(), None, "{label} {variant}: left unbound");
            }
            checked += 1;
        }
    }
    assert_eq!(checked, 2 * 68);
}

/// `LocalTLSSeedServer` reports a bad port before it looks at the origin.
#[test]
fn a_bad_port_is_reported_before_a_bad_origin() {
    let doc = oracle();
    let recorded = &doc["ordering"]["bad_port_and_bad_origin"];
    assert_eq!(recorded["code"], "local_tls_port_invalid");
    let (root, material) = material("ordering");
    let seed = coordinator(&root, 0);
    let error = Server::bind_tls_on(seed.clone(), &material, Some("http://x"), Some(80))
        .err()
        .expect("refused");
    assert_eq!(error.code, recorded["code"].as_str().unwrap());
    assert_eq!(seed.seed_url(), None);
}
