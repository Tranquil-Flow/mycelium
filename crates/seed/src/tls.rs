// SPDX-License-Identifier: AGPL-3.0-only
//! Installation-local TLS material (`mycelium_a13_macos/tls.py` `ensure_local_tls_material` /
//! `_generate_material` / `_validate_material` @ f4e366e9): one private directory holding `ca.pem` (a CA named
//! "Mycelium local installation CA"), `server.pem` ("Mycelium loopback seed", SAN `localhost` + `127.0.0.1`,
//! server-auth) and `server.key` (PKCS#8), all owner-only (0600) and created together or not at all.
//!
//! RU-027: this is persisted state, so it is v0.1.4's, not an approximation. v0.1.4 generates it with the Python
//! `cryptography` library (no system tool): RSA-2048 keys (public exponent 65537), SHA-256 / PKCS#1 v1.5
//! signatures, certificates backdated 5 minutes, CA valid 3650 days and server 825 days, a random 159-bit serial,
//! the extensions in the source's order (CA: basicConstraints, SKI, AKI, keyUsage; server: SAN, basicConstraints,
//! SKI, AKI, extendedKeyUsage, keyUsage; SKI = SHA-1 of the key bits, AKI = the CA's SKI). The audited RustSec
//! counterparts do the same work here: `rsa` (key generation, signing) and `x509-cert` (typed X.509 structures
//! and encoding); there is no hand-assembled DER in this file. An existing set is loaded and validated, never
//! regenerated (a set written by v0.1.4 is served as is).
//!
//! `rsa` is subject to RUSTSEC-2023-0071 (timing side channel on private-key operations). It is used ONLY here,
//! at first-time local generation (a key and two signatures, offline, nobody can time them). Every TLS
//! signature is made by `ring` through rustls. `tests/tls_material.rs::rsa_is_only_used_by_material_generation`
//! keeps that true; see `L5-evidence/round11-lock-revision.md`.
use rand_core::{OsRng, RngCore};
use rsa::{
    RsaPrivateKey,
    pkcs1v15::SigningKey,
    pkcs8::{EncodePrivateKey, LineEnding},
    sha2::Sha256,
    signature::Keypair,
};
use rustls::{ServerConfig, crypto::ring as ring_provider};
use rustls_pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
use std::{
    fs,
    io::Write,
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use x509_cert::{
    Certificate,
    attr::AttributeTypeAndValue,
    builder::{Builder, CertificateBuilder, Profile},
    der::{
        Any, DecodePem, Encode, EncodePem, Length, Writer,
        asn1::{Ia5String, OctetString, SetOfVec, Utf8StringRef},
        oid::{
            AssociatedOid, ObjectIdentifier,
            db::{rfc4519::CN, rfc5280::ID_KP_SERVER_AUTH},
        },
        referenced::OwnedToRef,
    },
    ext::{
        AsExtension, Extension,
        pkix::{
            AuthorityKeyIdentifier, BasicConstraints, ExtendedKeyUsage, KeyUsage, KeyUsages,
            SubjectAltName, SubjectKeyIdentifier,
            name::{GeneralName, GeneralNames},
        },
    },
    name::{Name, RdnSequence, RelativeDistinguishedName},
    serial_number::SerialNumber,
    spki::SubjectPublicKeyInfoOwned,
    time::{Time, Validity},
};

const OWNER_DIR_MODE: u32 = 0o700;
const OWNER_FILE_MODE: u32 = 0o600;
const MODE_BITS: u32 = 0o777;
/// `_generate_material` (`tls.py:157-250`): `public_exponent=65_537, key_size=2_048`.
const RSA_BITS: usize = 2_048;
/// Validity as in `_generate_material`: backdated 5 minutes; CA 3650 days; server 825 days.
/// SE-L5-TLS-BUDGETS: these three durations have no L1 registry id yet.
const BACKDATE_SECONDS: u64 = 5 * 60;
const CA_VALID_SECONDS: u64 = 3650 * 86_400;
const SERVER_VALID_SECONDS: u64 = 825 * 86_400;
/// `x509.random_serial_number()`: 160 random bits shifted right by one = 159 bits.
const SERIAL_BYTES: usize = 20;
const CA_NAME: &str = "Mycelium local installation CA";
const SERVER_NAME: &str = "Mycelium loopback seed";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct TlsError(pub &'static str);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Material {
    pub ca_certificate: PathBuf,
    pub server_certificate: PathBuf,
    pub server_private_key: PathBuf,
}

fn refuse<T>(code: &'static str) -> Result<T, TlsError> {
    Err(TlsError(code))
}

// ---- generation ------------------------------------------------------------------------------------------

/// Python's `cryptography` raises its own exceptions on a generation failure (no stable code); this port says so
/// with one code instead of crashing.
const GENERATION_FAILED: TlsError = TlsError("local_tls_material_generation_failed");

fn generation<T, E>(result: Result<T, E>) -> Result<T, TlsError> {
    result.map_err(|_| GENERATION_FAILED)
}

/// `x509.Name([NameAttribute(COMMON_NAME, text)])`: one RDN, a UTF8String value (what `cryptography` encodes).
fn common_name(text: &str) -> Result<Name, TlsError> {
    let value = generation(Any::encode_from(&generation(Utf8StringRef::new(text))?))?;
    let attribute = AttributeTypeAndValue { oid: CN, value };
    let rdn = RelativeDistinguishedName::from(generation(SetOfVec::try_from(vec![attribute]))?);
    Ok(RdnSequence::from(vec![rdn]))
}

/// A random positive serial of at most 159 bits (never zero).
fn serial() -> Result<SerialNumber, TlsError> {
    loop {
        let mut bytes = [0u8; SERIAL_BYTES];
        generation(OsRng.try_fill_bytes(&mut bytes))?;
        bytes[0] &= 0x7f;
        if bytes.iter().any(|b| *b != 0) {
            return generation(SerialNumber::new(&bytes));
        }
    }
}

/// An extension with the criticality v0.1.4 gave it (`add_extension(.., critical=...)`), not the builder's
/// default for its type: the v0.1.4 flags are what the oracle test pins.
struct Flagged<T>(T, bool);

impl<T: AssociatedOid> AssociatedOid for Flagged<T> {
    const OID: ObjectIdentifier = T::OID;
}

impl<T: Encode> Encode for Flagged<T> {
    fn encoded_len(&self) -> x509_cert::der::Result<Length> {
        self.0.encoded_len()
    }

    fn encode(&self, writer: &mut impl Writer) -> x509_cert::der::Result<()> {
        self.0.encode(writer)
    }
}

impl<T: AssociatedOid + Encode> AsExtension for Flagged<T> {
    fn critical(&self, _subject: &Name, _extensions: &[Extension]) -> bool {
        self.1
    }
}

/// `not_valid_before(now - 5 min)`, `not_valid_after(now + lifetime)`.
fn validity(now: u64, lifetime: u64) -> Result<Validity, TlsError> {
    let at = |seconds: u64| generation(Time::try_from(UNIX_EPOCH + Duration::from_secs(seconds)));
    Ok(Validity {
        not_before: at(now.saturating_sub(BACKDATE_SECONDS))?,
        not_after: at(now + lifetime)?,
    })
}

/// One certificate with the source's extension list, in the source's order. `Profile::Manual` is the builder's
/// "no default extensions" profile: the extensions are the explicit list below, nothing is added behind it. The
/// oracle test compares order, criticality and value of each with v0.1.4's executed output.
fn certificate(
    subject: Name,
    issuer: Option<Name>,
    subject_key: &SigningKey<Sha256>,
    issuer_key: &SigningKey<Sha256>,
    window: Validity,
    server: bool,
) -> Result<Vec<u8>, TlsError> {
    let spki = generation(SubjectPublicKeyInfoOwned::from_key(
        subject_key.verifying_key(),
    ))?;
    let issuer_spki = generation(SubjectPublicKeyInfoOwned::from_key(
        issuer_key.verifying_key(),
    ))?;
    let subject_id = generation(SubjectKeyIdentifier::try_from(spki.owned_to_ref()))?;
    let authority_id = generation(AuthorityKeyIdentifier::try_from(issuer_spki.owned_to_ref()))?;
    let mut builder = generation(CertificateBuilder::new(
        Profile::Manual { issuer },
        serial()?,
        window,
        subject,
        spki,
        issuer_key,
    ))?;
    if server {
        let names = GeneralNames::from(vec![
            GeneralName::DnsName(generation(Ia5String::new("localhost"))?),
            GeneralName::IpAddress(generation(OctetString::new([127u8, 0, 0, 1]))?),
        ]);
        generation(builder.add_extension(&Flagged(SubjectAltName(names), false)))?;
        generation(builder.add_extension(&Flagged(
            BasicConstraints {
                ca: false,
                path_len_constraint: None,
            },
            true,
        )))?;
        generation(builder.add_extension(&Flagged(subject_id.clone(), false)))?;
        generation(builder.add_extension(&Flagged(authority_id.clone(), false)))?;
        generation(
            builder.add_extension(&Flagged(ExtendedKeyUsage(vec![ID_KP_SERVER_AUTH]), false)),
        )?;
        generation(builder.add_extension(&Flagged(
            KeyUsage(KeyUsages::DigitalSignature | KeyUsages::KeyEncipherment),
            true,
        )))?;
    } else {
        generation(builder.add_extension(&Flagged(
            BasicConstraints {
                ca: true,
                path_len_constraint: Some(0),
            },
            true,
        )))?;
        generation(builder.add_extension(&Flagged(subject_id.clone(), false)))?;
        generation(builder.add_extension(&Flagged(authority_id.clone(), false)))?;
        generation(builder.add_extension(&Flagged(
            KeyUsage(KeyUsages::DigitalSignature | KeyUsages::KeyCertSign | KeyUsages::CRLSign),
            true,
        )))?;
    }
    let built: Certificate = generation(builder.build::<rsa::pkcs1v15::Signature>())?;
    generation(built.to_pem(LineEnding::LF)).map(String::into_bytes)
}

/// The three PEM bodies in install order: `ca.pem`, `server.pem`, `server.key`.
pub type Bodies = (Vec<u8>, Vec<u8>, Vec<u8>);

/// `_generate_material` at unix time `now` (the caller's wall clock in `ensure_local_tls_material`; the tests
/// pass the clock the oracle probe froze).
pub fn generate_material(now: u64) -> Result<Bodies, TlsError> {
    let ca_key = SigningKey::<Sha256>::new(generation(RsaPrivateKey::new(&mut OsRng, RSA_BITS))?);
    let server_private = generation(RsaPrivateKey::new(&mut OsRng, RSA_BITS))?;
    let server_key = SigningKey::<Sha256>::new(server_private.clone());
    let ca_name = common_name(CA_NAME)?;
    let ca = certificate(
        ca_name.clone(),
        None,
        &ca_key,
        &ca_key,
        validity(now, CA_VALID_SECONDS)?,
        false,
    )?;
    let server = certificate(
        common_name(SERVER_NAME)?,
        Some(ca_name),
        &server_key,
        &ca_key,
        validity(now, SERVER_VALID_SECONDS)?,
        true,
    )?;
    let key = generation(server_private.to_pkcs8_pem(LineEnding::LF))?;
    Ok((ca, server, key.as_bytes().to_vec()))
}

// ---- files ----------------------------------------------------------------------------------------------

/// `_private_directory(create=True)`.
fn private_directory(root: &Path) -> Result<PathBuf, TlsError> {
    let invalid = || TlsError("local_tls_path_invalid");
    let absolute = std::path::absolute(root).map_err(|_| invalid())?;
    if fs::symlink_metadata(&absolute).is_err() {
        // Python `mkdir(parents=True, mode=0o700)`: the mode applies to the leaf only; new parents take the default.
        if let Some(parent) = absolute.parent() {
            fs::create_dir_all(parent).map_err(|_| invalid())?;
        }
        fs::DirBuilder::new()
            .mode(OWNER_DIR_MODE)
            .create(&absolute)
            .map_err(|_| invalid())?;
        fs::set_permissions(&absolute, fs::Permissions::from_mode(OWNER_DIR_MODE))
            .map_err(|_| invalid())?;
    }
    let metadata = fs::symlink_metadata(&absolute).map_err(|_| invalid())?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != rustix::process::getuid().as_raw()
        || metadata.mode() & MODE_BITS != OWNER_DIR_MODE
    {
        return refuse("local_tls_path_invalid");
    }
    Ok(absolute)
}

/// `_owner_file`.
fn owner_file(path: &Path) -> Result<(), TlsError> {
    let metadata =
        fs::symlink_metadata(path).map_err(|_| TlsError("local_tls_material_incomplete"))?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.uid() != rustix::process::getuid().as_raw()
        || metadata.nlink() != 1
        || metadata.mode() & MODE_BITS != OWNER_FILE_MODE
    {
        return refuse("local_tls_material_invalid");
    }
    Ok(())
}

fn present(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}

/// `_validate_material`: owner-only files, the key serves the certificate, the CA loads, and the server
/// certificate's issuer equals the CA's subject (v0.1.4 compares the names, not the signature).
pub fn validate(material: &Material) -> Result<(), TlsError> {
    for path in [
        &material.ca_certificate,
        &material.server_certificate,
        &material.server_private_key,
    ] {
        owner_file(path)?;
    }
    let invalid = || TlsError("local_tls_material_invalid");
    let ca = fs::read(&material.ca_certificate).map_err(|_| invalid())?;
    let server = fs::read(&material.server_certificate).map_err(|_| invalid())?;
    server_config(material)?;
    let ca = Certificate::from_pem(&ca).map_err(|_| invalid())?;
    let server = Certificate::from_pem(&server).map_err(|_| invalid())?;
    if server.tbs_certificate.issuer != ca.tbs_certificate.subject {
        return refuse("local_tls_material_invalid");
    }
    Ok(())
}

/// The rustls server configuration for this material (TLS 1.2 and 1.3, no client authentication).
pub fn server_config(material: &Material) -> Result<Arc<ServerConfig>, TlsError> {
    let invalid = || TlsError("local_tls_material_invalid");
    let certificates = CertificateDer::pem_file_iter(&material.server_certificate)
        .map_err(|_| invalid())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| invalid())?;
    let key = PrivateKeyDer::from_pem_file(&material.server_private_key).map_err(|_| invalid())?;
    let config = ServerConfig::builder_with_provider(Arc::new(ring_provider::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|_| invalid())?
        .with_no_client_auth()
        .with_single_cert(certificates, key)
        .map_err(|_| invalid())?;
    Ok(Arc::new(config))
}

fn write_temporary(path: &Path, body: &[u8], salt: u64) -> Result<PathBuf, TlsError> {
    let name = path
        .file_name()
        .map_or(String::new(), |n| n.to_string_lossy().into_owned());
    let temporary = path.with_file_name(format!(".{name}.{salt:016x}.tmp"));
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(OWNER_FILE_MODE)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(&temporary)
        .map_err(|_| TlsError("local_tls_material_write_failed"))?;
    file.set_permissions(fs::Permissions::from_mode(OWNER_FILE_MODE))
        .map_err(|_| TlsError("local_tls_material_write_failed"))?;
    file.write_all(body)
        .and_then(|()| file.sync_all())
        .map_err(|_| TlsError("local_tls_material_write_failed"))?;
    Ok(temporary)
}

/// `ensure_local_tls_material`: load one complete local TLS identity or atomically create its first
/// generation.
pub fn ensure_local_tls_material(root: &Path) -> Result<Material, TlsError> {
    let directory = private_directory(root)?;
    let material = Material {
        ca_certificate: directory.join("ca.pem"),
        server_certificate: directory.join("server.pem"),
        server_private_key: directory.join("server.key"),
    };
    let paths = [
        &material.ca_certificate,
        &material.server_certificate,
        &material.server_private_key,
    ];
    let have: Vec<bool> = paths.iter().map(|p| present(p)).collect();
    if have.iter().any(|x| *x) {
        if !have.iter().all(|x| *x) {
            return refuse("local_tls_material_incomplete");
        }
        validate(&material)?;
        return Ok(material);
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| TlsError("local_tls_material_write_failed"))?
        .as_secs();
    let (ca, server, key) = generate_material(now)?;
    let salt = OsRng.next_u64();
    let bodies = [ca, server, key];
    let mut temporaries: Vec<PathBuf> = Vec::new();
    let mut installed: Vec<&PathBuf> = Vec::new();
    let result = (|| -> Result<(), TlsError> {
        for (path, body) in paths.iter().zip(&bodies) {
            temporaries.push(write_temporary(path, body, salt)?);
        }
        if paths.iter().any(|p| present(p)) {
            return refuse("local_tls_material_incomplete");
        }
        for (temporary, path) in temporaries.iter().zip(paths) {
            fs::rename(temporary, path).map_err(|_| TlsError("local_tls_material_write_failed"))?;
            installed.push(path);
        }
        fs::File::open(&directory)
            .and_then(|d| d.sync_all())
            .map_err(|_| TlsError("local_tls_material_write_failed"))
    })();
    for temporary in &temporaries {
        if present(temporary) {
            fs::remove_file(temporary).map_err(|_| TlsError("local_tls_material_write_failed"))?;
        }
    }
    if let Err(error) = result {
        for path in installed {
            fs::remove_file(path).map_err(|_| TlsError("local_tls_material_write_failed"))?;
        }
        return Err(error);
    }
    validate(&material)?;
    Ok(material)
}
