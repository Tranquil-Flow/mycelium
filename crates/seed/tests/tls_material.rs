// SPDX-License-Identifier: AGPL-3.0-only
//! RU-027: the seed's local TLS material equals v0.1.4's (FX-L5-tls-material-r11: `round11-tls-material-probe.py`
//! -> `fixtures/oracle_tls_material.json` + `fixtures/v014_tls_material/`, the files the REAL v0.1.4
//! `mycelium_a13_macos.tls.ensure_local_tls_material` wrote under a frozen clock, f4e366e9, Python 3.14.8,
//! cryptography 46.0.5). Expected values are the oracle's executed output; nothing is typed from the Rust side.
use protocol::identity::Identity;
use rsa::{
    BigUint, RsaPrivateKey, RsaPublicKey,
    pkcs1v15::{Signature, VerifyingKey},
    pkcs8::{DecodePrivateKey, DecodePublicKey},
    sha2::Sha256,
    signature::Verifier,
    traits::PublicKeyParts,
};
use rustls::{
    ClientConfig, ClientConnection, RootCertStore, crypto::ring as ring_provider,
    time_provider::TimeProvider,
};
use rustls_pki_types::{CertificateDer, ServerName, UnixTime, pem::PemObject};
use seed::{Coordinator, Options, server::Server, tls};
use serde_json::{Value, json};
use sha1::{Digest, Sha1};
use std::{
    fs,
    io::{Read, Write},
    net::TcpStream,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{Arc, OnceLock},
};
use x509_cert::{
    Certificate,
    der::{DecodePem, Encode},
    time::Time,
};

/// The clock the oracle probe froze (`NOW` in the probe).
const ORACLE_NOW: u64 = 1_700_000_000;
const FIXTURE_CA: &[u8] = include_bytes!("../fixtures/v014_tls_material/ca.pem");
const FIXTURE_SERVER: &[u8] = include_bytes!("../fixtures/v014_tls_material/server.pem");
const FIXTURE_KEY: &[u8] = include_bytes!("../fixtures/v014_tls_material/server.key");

fn oracle() -> Value {
    let doc: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_tls_material.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    assert_eq!(doc["frozen_now"], ORACLE_NOW);
    doc
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn scratch(name: &str) -> PathBuf {
    // The store refuses a symlink on the path and macOS keeps `/var` -> `/private/var`.
    let base = fs::canonicalize(std::env::temp_dir()).unwrap();
    let dir = base.join(format!("l5r11-{}-{name}", std::process::id()));
    fs::remove_dir_all(&dir).unwrap_or(());
    fs::create_dir_all(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    dir
}

fn write_owner_file(path: &Path, body: &[u8]) {
    fs::write(path, body).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}

/// The v0.1.4-generated set, copied into a 0700 directory with 0600 files.
fn v014_set(name: &str) -> PathBuf {
    let dir = scratch(name).join("tls");
    fs::create_dir(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    write_owner_file(&dir.join("ca.pem"), FIXTURE_CA);
    write_owner_file(&dir.join("server.pem"), FIXTURE_SERVER);
    write_owner_file(&dir.join("server.key"), FIXTURE_KEY);
    dir
}

type Bodies = (Vec<u8>, Vec<u8>, Vec<u8>);

/// Two Rust generations under the oracle's frozen clock (RSA key generation is slow: done once per process).
fn generated(index: usize) -> &'static Bodies {
    static SETS: [OnceLock<Bodies>; 2] = [OnceLock::new(), OnceLock::new()];
    SETS[index].get_or_init(|| tls::generate_material(ORACLE_NOW).unwrap())
}

fn certificate(pem: &[u8]) -> Certificate {
    Certificate::from_pem(pem).unwrap()
}

fn bit_length(bytes: &[u8]) -> usize {
    let trimmed: Vec<u8> = bytes.iter().copied().skip_while(|b| *b == 0).collect();
    match trimmed.first() {
        None => 0,
        Some(first) => trimmed.len() * 8 - first.leading_zeros() as usize,
    }
}

fn spki_key_bits(cert: &Certificate) -> Vec<u8> {
    cert.tbs_certificate
        .subject_public_key_info
        .subject_public_key
        .raw_bytes()
        .to_vec()
}

fn time_seconds(time: &Time) -> u64 {
    time.to_unix_duration().as_secs()
}

fn time_tag(time: &Time) -> u8 {
    match time {
        Time::UtcTime(_) => 23,
        Time::GeneralTime(_) => 24,
    }
}

/// The oracle summary's shape, minus the fields that are random per generation (serial, keys and the
/// identifiers derived from them), which are checked by their relations below.
fn describe(cert: &Certificate) -> Value {
    let tbs = &cert.tbs_certificate;
    let spki = &tbs.subject_public_key_info;
    let key = RsaPublicKey::from_public_key_der(&spki.to_der().unwrap()).unwrap();
    let extensions: Vec<Value> = tbs
        .extensions
        .as_ref()
        .unwrap()
        .iter()
        .map(|e| {
            let oid = e.extn_id.to_string();
            let random = oid == "2.5.29.14" || oid == "2.5.29.35";
            json!({
                "oid": oid,
                "critical": e.critical,
                "value_der_hex": if random { Value::Null } else { Value::String(hex(e.extn_value.as_bytes())) },
            })
        })
        .collect();
    json!({
        "version": format!("{:?}", tbs.version).to_lowercase(),
        "signature_algorithm_der_hex": hex(&tbs.signature.to_der().unwrap()),
        "outer_signature_algorithm_der_hex": hex(&cert.signature_algorithm.to_der().unwrap()),
        "issuer_der_hex": hex(&tbs.issuer.to_der().unwrap()),
        "subject_der_hex": hex(&tbs.subject.to_der().unwrap()),
        "not_before": time_seconds(&tbs.validity.not_before),
        "not_after": time_seconds(&tbs.validity.not_after),
        "validity_tags": [time_tag(&tbs.validity.not_before), time_tag(&tbs.validity.not_after)],
        "public_key_bits": key.n().bits(),
        "public_exponent": key.e().to_bytes_be().iter().fold(0u64, |a, b| a << 8 | u64::from(*b)),
        "spki_algorithm_der_hex": hex(&spki.algorithm.to_der().unwrap())[4..],
        "extensions": extensions,
    })
}

fn oracle_shape(summary: &Value) -> Value {
    let mut extensions = summary["extensions"].clone();
    for e in extensions.as_array_mut().unwrap() {
        if e["oid"] == "2.5.29.14" || e["oid"] == "2.5.29.35" {
            e["value_der_hex"] = Value::Null;
        }
    }
    let version = summary["version"].as_str().unwrap().to_lowercase();
    json!({
        "version": version,
        "signature_algorithm_der_hex": summary["signature_algorithm_der_hex"],
        "outer_signature_algorithm_der_hex": summary["signature_algorithm_der_hex"],
        "issuer_der_hex": summary["issuer_der_hex"],
        "subject_der_hex": summary["subject_der_hex"],
        "not_before": summary["not_before"],
        "not_after": summary["not_after"],
        "validity_tags": summary["validity_tags"],
        "public_key_bits": summary["public_key_bits"],
        "public_exponent": summary["public_exponent"],
        "spki_algorithm_der_hex": summary["spki_algorithm_der_hex"],
        "extensions": extensions,
    })
}

fn verify_signature(cert: &Certificate, issuer: &Certificate) {
    let key = RsaPublicKey::from_public_key_der(
        &issuer
            .tbs_certificate
            .subject_public_key_info
            .to_der()
            .unwrap(),
    )
    .unwrap();
    let signature = Signature::try_from(cert.signature.raw_bytes()).unwrap();
    VerifyingKey::<Sha256>::new(key)
        .verify(&cert.tbs_certificate.to_der().unwrap(), &signature)
        .expect("PKCS#1 v1.5 SHA-256 signature");
}

// ---- generation equals the oracle --------------------------------------------------------------------------

#[test]
fn generated_certificates_have_the_v014_profile() {
    let doc = oracle();
    let (ca_pem, server_pem, _) = generated(0);
    let ca = certificate(ca_pem);
    let server = certificate(server_pem);
    for (rust, name) in [(&ca, "ca"), (&server, "server")] {
        assert_eq!(
            describe(rust),
            oracle_shape(&doc[name]),
            "{name} certificate vs the executed v0.1.4 output"
        );
    }
    // The random fields: serial (v0.1.4 `x509.random_serial_number()`: 159 random bits, never 0), the key
    // identifiers (RFC 5280 4.2.1.2 method 1: SHA-1 of the subjectPublicKey bits), AKI = the CA's SKI.
    assert!(doc["serial_max_bit_length"].as_u64().unwrap() == 159);
    for cert in [&ca, &server] {
        let bits = bit_length(cert.tbs_certificate.serial_number.as_bytes());
        assert!((1..=159).contains(&bits), "serial bit length {bits}");
    }
    let sha1_hex = |c: &Certificate| hex(&Sha1::digest(spki_key_bits(c)));
    let value = |c: &Certificate, oid: &str| {
        c.tbs_certificate
            .extensions
            .as_ref()
            .unwrap()
            .iter()
            .find(|e| e.extn_id.to_string() == oid)
            .map(|e| hex(e.extn_value.as_bytes()))
            .unwrap()
    };
    assert_eq!(
        doc["ca"]["subject_key_id_is_sha1_of_key_bits"], true,
        "oracle relation"
    );
    assert_eq!(
        doc["server"]["subject_key_id_is_sha1_of_key_bits"], true,
        "oracle relation"
    );
    assert_eq!(doc["server_aki_is_ca_ski"], true, "oracle relation");
    assert_eq!(value(&ca, "2.5.29.14"), format!("0414{}", sha1_hex(&ca)));
    assert_eq!(
        value(&server, "2.5.29.14"),
        format!("0414{}", sha1_hex(&server))
    );
    assert_eq!(
        value(&ca, "2.5.29.35"),
        format!("30168014{}", sha1_hex(&ca))
    );
    assert_eq!(
        value(&server, "2.5.29.35"),
        format!("30168014{}", sha1_hex(&ca))
    );
    // Signed by the CA key (SHA-256, PKCS#1 v1.5); the CA is self-signed.
    verify_signature(&server, &ca);
    verify_signature(&ca, &ca);
    assert_eq!(
        doc["server_signature_verifies_under_ca_key_pkcs1v15_sha256"],
        true
    );
    assert_eq!(doc["ca_self_signed_issuer_equals_subject"], true);
    assert_eq!(ca.tbs_certificate.issuer, ca.tbs_certificate.subject);
    assert_eq!(doc["server_issuer_equals_ca_subject"], true);
    assert_eq!(server.tbs_certificate.issuer, ca.tbs_certificate.subject);
}

#[test]
fn generated_private_key_is_the_v014_pkcs8_rsa_2048() {
    let doc = oracle();
    let (_, server_pem, key_pem) = generated(0);
    let text = std::str::from_utf8(key_pem).unwrap();
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines[0], doc["server_key"]["pem_first_line"]);
    assert_eq!(*lines.last().unwrap(), doc["server_key"]["pem_last_line"]);
    assert_eq!(
        text.ends_with('\n'),
        doc["server_key"]["pem_ends_with_newline"]
    );
    let key = RsaPrivateKey::from_pkcs8_pem(text).unwrap();
    assert_eq!(
        key.n().bits(),
        doc["server_key"]["bits"].as_u64().unwrap() as usize
    );
    assert_eq!(
        key.e(),
        &BigUint::from(doc["server_key"]["exponent"].as_u64().unwrap())
    );
    let cert = certificate(server_pem);
    let certificate_key = RsaPublicKey::from_public_key_der(
        &cert
            .tbs_certificate
            .subject_public_key_info
            .to_der()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        key.to_public_key() == certificate_key,
        doc["server_key"]["matches_server_certificate"]
    );
}

#[test]
fn generated_pem_files_are_laid_out_like_the_oracles() {
    // Line width and framing of the files v0.1.4 wrote (fixtures) against ours.
    for (ours, theirs) in [
        (&generated(0).0, FIXTURE_CA),
        (&generated(0).1, FIXTURE_SERVER),
        (&generated(0).2, FIXTURE_KEY),
    ] {
        let ours = std::str::from_utf8(ours).unwrap();
        let theirs = std::str::from_utf8(theirs).unwrap();
        let widths = |s: &str| -> (String, String, usize, bool, bool) {
            let lines: Vec<&str> = s.lines().collect();
            (
                lines[0].to_owned(),
                (*lines.last().unwrap()).to_owned(),
                lines[1..lines.len() - 1]
                    .iter()
                    .map(|l| l.len())
                    .max()
                    .unwrap(),
                lines[1..lines.len() - 2].iter().all(|l| l.len() == 64),
                s.ends_with('\n') && !s.contains('\r'),
            )
        };
        assert_eq!(widths(ours), widths(theirs));
    }
    assert_eq!(
        generated(0)
            .0
            .windows(18)
            .filter(|w| *w == b"BEGIN CERTIFICATE-")
            .count(),
        oracle()["pem_certificates_in_file"]["ca"]
    );
}

#[test]
fn two_generations_differ_in_keys_serials_and_identifiers() {
    let (ca_a, server_a, key_a) = generated(0);
    let (ca_b, server_b, key_b) = generated(1);
    assert_ne!(key_a, key_b);
    assert_ne!(ca_a, ca_b);
    assert_ne!(server_a, server_b);
    assert_ne!(
        certificate(ca_a).tbs_certificate.serial_number,
        certificate(ca_b).tbs_certificate.serial_number
    );
    assert_ne!(
        certificate(ca_a).tbs_certificate.serial_number,
        certificate(server_a).tbs_certificate.serial_number
    );
}

// ---- reuse of a v0.1.4 set ---------------------------------------------------------------------------------

fn fixture_digests() -> Vec<(&'static str, Vec<u8>)> {
    vec![
        ("ca.pem", Sha1::digest(FIXTURE_CA).to_vec()),
        ("server.key", Sha1::digest(FIXTURE_KEY).to_vec()),
        ("server.pem", Sha1::digest(FIXTURE_SERVER).to_vec()),
    ]
}

fn directory_digests(dir: &Path) -> Vec<(String, Vec<u8>)> {
    let mut out: Vec<(String, Vec<u8>)> = fs::read_dir(dir)
        .unwrap()
        .map(|e| {
            let e = e.unwrap();
            (
                e.file_name().to_string_lossy().into_owned(),
                Sha1::digest(fs::read(e.path()).unwrap()).to_vec(),
            )
        })
        .collect();
    out.sort();
    out
}

#[test]
fn a_v014_material_directory_is_reused_byte_for_byte() {
    let dir = v014_set("reuse");
    let material = tls::ensure_local_tls_material(&dir).unwrap();
    assert_eq!(material.ca_certificate, dir.join("ca.pem"));
    assert_eq!(material.server_certificate, dir.join("server.pem"));
    assert_eq!(material.server_private_key, dir.join("server.key"));
    let expected: Vec<(String, Vec<u8>)> = fixture_digests()
        .into_iter()
        .map(|(n, d)| (n.to_owned(), d))
        .collect();
    assert_eq!(
        directory_digests(&dir),
        expected,
        "no file rewritten, none added"
    );
    assert_eq!(oracle()["reuse"]["unchanged"], true);
    // A second call is a no-op as well.
    tls::ensure_local_tls_material(&dir).unwrap();
    assert_eq!(directory_digests(&dir), expected);
}

/// The fixture was generated under a frozen clock (its server certificate expires 825 days after it), so the
/// verifying client reads the same frozen time plus one day instead of today's date.
#[derive(Debug)]
struct FrozenClock;

impl TimeProvider for FrozenClock {
    fn current_time(&self) -> Option<UnixTime> {
        Some(UnixTime::since_unix_epoch(std::time::Duration::from_secs(
            ORACLE_NOW + 86_400,
        )))
    }
}

fn coordinator(dir: &Path) -> Arc<Coordinator> {
    let signer = Identity::from_secret("seed-endpoint", &[21u8; 32]).unwrap();
    let options = Options::new("swarm-authority", "owner-node", "seed-boot-1").unwrap();
    Arc::new(
        Coordinator::open(
            &dir.join("seed.sqlite"),
            signer,
            options,
            Box::new(|| 1_700_000_000.0),
            Box::new(|| "id".to_owned()),
        )
        .unwrap_or_else(|e| panic!("open: {}", e.code)),
    )
}

#[test]
fn the_rust_seed_serves_the_v014_certificate_and_it_verifies() {
    let dir = v014_set("serve");
    let material = tls::ensure_local_tls_material(&dir).unwrap();
    let server = Server::bind_tls(coordinator(dir.parent().unwrap()), &material, None)
        .unwrap_or_else(|e| panic!("bind: {}", e.code));
    let mut roots = RootCertStore::empty();
    for ca in CertificateDer::pem_slice_iter(FIXTURE_CA) {
        roots.add(ca.unwrap()).unwrap();
    }
    let config = Arc::new(
        ClientConfig::builder_with_details(
            Arc::new(ring_provider::default_provider()),
            Arc::new(FrozenClock),
        )
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_root_certificates(roots)
        .with_no_client_auth(),
    );
    let mut connection = ClientConnection::new(
        config,
        ServerName::IpAddress(std::net::IpAddr::from([127, 0, 0, 1]).into()),
    )
    .unwrap();
    let mut socket = TcpStream::connect(server.address()).unwrap();
    let mut stream = rustls::Stream::new(&mut connection, &mut socket);
    stream
        .write_all(b"GET /seed/identity HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .unwrap();
    let mut answer = Vec::new();
    // The server closes with close_notify; an abrupt EOF would surface as an error here.
    stream.read_to_end(&mut answer).unwrap();
    assert!(
        answer.starts_with(b"HTTP/1.") && answer.windows(5).any(|w| w == b" 200 "),
        "seed answered {:?}",
        String::from_utf8_lossy(&answer[..answer.len().min(80)])
    );
    let presented = connection.peer_certificates().unwrap().to_vec();
    let from_file = CertificateDer::from_pem_slice(FIXTURE_SERVER).unwrap();
    assert_eq!(
        presented,
        vec![from_file],
        "the SAME certificate v0.1.4 generated"
    );
    server.close();
}

// ---- file set, modes, refusals -----------------------------------------------------------------------------

fn mode(path: &Path) -> String {
    format!("0o{:o}", fs::symlink_metadata(path).unwrap().mode() & 0o777)
}

#[test]
fn fresh_directories_get_the_v014_file_set_and_modes() {
    let doc = oracle();
    // An existing empty 0700 directory.
    let dir = scratch("fresh").join("tls");
    fs::create_dir(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    tls::ensure_local_tls_material(&dir).unwrap();
    let mut names: Vec<String> = fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    assert_eq!(
        json!(names),
        doc["files"]["names"],
        "no temporary left behind"
    );
    assert_eq!(json!(names), doc["fresh_empty_dir"]["created"]);
    assert_eq!(json!(mode(&dir)), doc["files"]["dir_mode"]);
    for name in &names {
        assert_eq!(json!(mode(&dir.join(name))), doc["files"]["modes"][name]);
    }
    // A nested root: the leaf is 0700, its new parents are created with the default mode (Python
    // `mkdir(parents=True, mode=0o700)` applies the mode to the leaf only).
    let base = scratch("nested");
    let nested = base.join("a").join("b").join("tls");
    tls::ensure_local_tls_material(&nested).unwrap();
    let default_dir = base.join("plain-default");
    fs::create_dir(&default_dir).unwrap();
    assert_eq!(json!(mode(&nested)), doc["nested_root_created"]["dir_mode"]);
    assert_eq!(mode(nested.parent().unwrap()), mode(&default_dir));
    assert_eq!(
        mode(nested.parent().unwrap().parent().unwrap()),
        mode(&default_dir)
    );
}

fn outcome(root: &Path) -> Value {
    match tls::ensure_local_tls_material(root) {
        Ok(_) => json!({"ok": true}),
        Err(e) => json!({"ok": false, "code": e.0}),
    }
}

#[test]
fn every_refusal_of_the_oracle_is_reproduced() {
    let doc = oracle();
    let (other_ca, _, other_key) = generated(1);
    let expected = doc["refusals"].as_object().unwrap();
    let mut seen = Vec::new();
    for (case, want) in expected {
        let dir = v014_set(case);
        let root = match case.as_str() {
            "one_file_missing" => {
                fs::remove_file(dir.join("server.key")).unwrap();
                dir
            }
            "only_ca_present" => {
                fs::remove_file(dir.join("server.key")).unwrap();
                fs::remove_file(dir.join("server.pem")).unwrap();
                dir
            }
            "server_certificate_mode_0644" => {
                fs::set_permissions(dir.join("server.pem"), fs::Permissions::from_mode(0o644))
                    .unwrap();
                dir
            }
            "server_key_mode_0640" => {
                fs::set_permissions(dir.join("server.key"), fs::Permissions::from_mode(0o640))
                    .unwrap();
                dir
            }
            "ca_mode_0400" => {
                fs::set_permissions(dir.join("ca.pem"), fs::Permissions::from_mode(0o400)).unwrap();
                dir
            }
            "directory_mode_0755" => {
                fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();
                dir
            }
            "ca_is_a_symlink" => {
                fs::rename(dir.join("ca.pem"), dir.join("ca.real")).unwrap();
                std::os::unix::fs::symlink(dir.join("ca.real"), dir.join("ca.pem")).unwrap();
                dir
            }
            "ca_has_two_links" => {
                fs::hard_link(dir.join("ca.pem"), dir.join("ca.link")).unwrap();
                dir
            }
            "server_key_of_another_set" => {
                write_owner_file(&dir.join("server.key"), other_key);
                dir
            }
            // Same name, other key: v0.1.4 compares names only (the oracle's own result is `ok`).
            "ca_of_another_set_same_name" | "ca_of_another_set_is_accepted_by_name_only" => {
                write_owner_file(&dir.join("ca.pem"), other_ca);
                dir
            }
            // The server certificate installed as the CA: its subject is not the server certificate's issuer.
            "ca_is_the_server_certificate" => {
                write_owner_file(&dir.join("ca.pem"), FIXTURE_SERVER);
                dir
            }
            "ca_garbage" => {
                write_owner_file(&dir.join("ca.pem"), b"not a pem\n");
                dir
            }
            "server_key_garbage" => {
                write_owner_file(
                    &dir.join("server.key"),
                    b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
                );
                dir
            }
            "server_certificate_empty" => {
                write_owner_file(&dir.join("server.pem"), b"");
                dir
            }
            "root_is_a_symlink" => {
                let link = dir.parent().unwrap().join("link_dir");
                std::os::unix::fs::symlink(&dir, &link).unwrap();
                link
            }
            "root_is_a_file" => {
                let file = dir.parent().unwrap().join("plain_file");
                fs::write(&file, b"x").unwrap();
                file
            }
            other => panic!("oracle case {other:?} has no transcription"),
        };
        assert_eq!(&outcome(&root), want, "refusal case {case}");
        seen.push(case.clone());
    }
    assert_eq!(seen.len(), expected.len());
    assert_eq!(seen.len(), 17, "the oracle recorded 17 cases");
}

// ---- the audited-crate rule ---------------------------------------------------------------------------------

#[test]
fn rsa_is_only_used_by_material_generation() {
    // RUSTSEC-2023-0071 (Marvin) is mitigated by never using `rsa` for a private-key operation an attacker can
    // drive or time: TLS signatures are made by ring through rustls. See `round11-lock-revision.md`.
    let mut offenders = Vec::new();
    // Every source file under `src/`, nested modules (`src/operator/ops.rs`) included.
    let mut pending = vec![Path::new(env!("CARGO_MANIFEST_DIR")).join("src")];
    let mut scanned = 0;
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(&directory).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                pending.push(path);
                continue;
            }
            let text = fs::read_to_string(&path).unwrap();
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            scanned += 1;
            if name != "tls.rs" && (text.contains("rsa::") || text.contains("use rsa")) {
                offenders.push(name);
            }
        }
    }
    assert!(scanned >= 8, "the scan saw {scanned} source files");
    assert!(
        offenders.is_empty(),
        "rsa used outside tls.rs: {offenders:?}"
    );
    // And inside tls.rs only the generation path names it (no `fn` on the serving path mentions it).
    let tls_source =
        fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/tls.rs")).unwrap();
    for needle in ["server_config", "pub fn validate"] {
        let start = tls_source.find(needle).unwrap();
        let end = tls_source[start..].find("\n}\n").unwrap() + start;
        assert!(
            !tls_source[start..end].contains("rsa"),
            "{needle} mentions rsa"
        );
    }
}

/// SE-L10-SEED-PORT, executed oracle FX-L5-seed-port-r13 (`round13-port-probe.py` -> `fixtures/oracle_port_r13.json`,
/// v0.1.4 f4e366e9): `LocalTLSSeedServer(port=...)`. Integer ports below 49152 or above 65535 are
/// `local_tls_port_invalid` (before anything is bound), a busy port is `local_tls_bind_failed`, a free port serves
/// on exactly that port, and the coordinator is left unbound after every refusal. A bool, a float or a text port is
/// refused by the oracle too; the typed `Option<i64>` cannot express them, so those recorded cases are only checked
/// to be refusals in the fixture.
#[test]
fn an_explicit_port_is_validated_and_bound_as_the_oracle_does() {
    let doc: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_port_r13.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let dir = v014_set("explicit-port");
    let material = tls::ensure_local_tls_material(&dir).unwrap();
    let mut replayed = 0;
    for case in doc["cases"].as_array().unwrap() {
        let label = case["label"].as_str().unwrap();
        let kind = case["port_kind"].as_str().unwrap();
        let mut held = None;
        let port: Option<i64> = match kind {
            "free" => {
                let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
                let mut port = i64::from(probe.local_addr().unwrap().port());
                drop(probe);
                // The oracle only accepts the dynamic range; retry until the kernel hands one out.
                while port < 49152 {
                    let again = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
                    port = i64::from(again.local_addr().unwrap().port());
                }
                Some(port)
            }
            "busy" => {
                let mut busy = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
                while busy.local_addr().unwrap().port() < 49152 {
                    busy = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
                }
                let port = i64::from(busy.local_addr().unwrap().port());
                held = Some(busy);
                Some(port)
            }
            "True" | "49152.0" | "'49152'" => {
                assert_eq!(case["ok"], false, "{label}");
                assert_eq!(case["code"], "local_tls_port_invalid", "{label}");
                continue;
            }
            number => Some(number.parse().unwrap()),
        };
        let public_origin = case["label"]
            .as_str()
            .unwrap()
            .contains("public-origin")
            .then_some("https://seed.example.test");
        let seed = coordinator(dir.parent().unwrap());
        let got = Server::bind_tls_on(seed.clone(), &material, public_origin, port);
        if case["ok"] == true {
            let server = got.unwrap_or_else(|e| panic!("{label}: {}", e.code));
            assert_eq!(
                server.base_url(),
                format!("https://127.0.0.1:{}", port.unwrap()),
                "{label}"
            );
            assert_eq!(server.address().port(), port.unwrap() as u16, "{label}");
            assert_eq!(
                seed.seed_url().as_deref(),
                Some(public_origin.unwrap_or(server.base_url())),
                "{label}"
            );
            server.close();
        } else {
            let error = got.err().unwrap_or_else(|| panic!("{label}: bound"));
            assert_eq!(error.code, case["code"].as_str().unwrap(), "{label}");
            assert_eq!(
                seed.seed_url(),
                None,
                "{label}: the coordinator stays unbound"
            );
        }
        drop(held);
        replayed += 1;
    }
    assert_eq!(replayed, 8);
}
