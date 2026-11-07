// SPDX-License-Identifier: AGPL-3.0-only
//! The owner-only seed key rotation operator layer of v0.1.4 (`mycelium_seed/operator.py`
//! `begin_seed_key_rotation` / `seed_key_rotation_status` / `complete_seed_key_rotation`, with the identity key
//! files of `mycelium_node/identity.py` and `mycelium_seed/authority.py` they use) over a private state root:
//!
//! ```text
//! <state_root>/                0700, owned by the operator, no symlink on the path
//!   state.sqlite3              0600, the durable coordinator store
//!   identity/                  0700
//!     seed.key                 32 raw Ed25519 secret bytes, 0600, one link
//!     seed.next.key            the pending key (begin creates it, complete renames it over seed.key)
//!     seed.rotation.json       the canonical dual-signed `mycelium.seed_key_transition.v1` envelope
//!     product.pseudonym.key    32 bytes derived from the seed key (`derive_product_pseudonym_salt`)
//! ```
//!
//! Every function validates its arguments first, then opens the authority exactly as `_load_authority` does
//! (private-directory lease, `state.sqlite3` owner file, durable binding, the one key file bound to the stored
//! key digest) and reports a stable `seed_operator_*` code. Error CLASSES matter as in the source:
//! [`ErrorClass::Authority`] (`SeedAuthorityError`) and [`ErrorClass::NodeIdentity`] (`NodeIdentityError`) are
//! not caught by `begin`/`complete`, so they reach the caller with their own code.
//!
//! Cryptography is `protocol::identity` (ed25519-dalek) and `protocol::seed_transition`; nothing is
//! hand-written here. The random next key is supplied by the caller (production: [`os_random_secret`]).
use crate::{Coordinator, Options, store};
use protocol::{canonical, identity::Identity, membership, seed_authority, seed_transition};
use rand_core::{OsRng, RngCore};
use rustix::fs::{AtFlags, Mode, OFlags, fchmod, fstat, mkdirat, openat, statat};
use serde_json::{Value, json};
use std::{
    fs::{self, File},
    io::{Read, Write},
    os::{
        fd::{AsFd, OwnedFd},
        unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    },
    path::{Component, Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

mod ops;
pub use ops::{
    backup_seed_state, default_backup_id, restore_seed_state, revoke_seed_member, seed_inventory,
};

pub const SEED_OPERATOR_ROTATION_PROTOCOL: &str = "mycelium.seed_operator_rotation.v1";
const IDENTITY_DIR: &str = "identity";
const DATABASE: &str = "state.sqlite3";
const ACTIVE_KEY: &str = "seed.key";
const NEXT_KEY: &str = "seed.next.key";
const TRANSITION_FILE: &str = "seed.rotation.json";
const PSEUDONYM_FILE: &str = "product.pseudonym.key";
const KEY_BYTES: usize = 32;
const OWNER_FILE: u32 = 0o600;
const OWNER_DIR: u32 = 0o700;
const MODE_BITS: u32 = 0o7777;

/// The Python exception class the source raised (what a caller can still tell apart).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorClass {
    /// `SeedOperatorError`.
    Operator,
    /// `SeedAuthorityError` (`mycelium_seed/authority.py`).
    Authority,
    /// `NodeIdentityError` (`mycelium_node/identity.py`).
    NodeIdentity,
    /// A Python `ValueError`; [`OperatorError::code`] holds its message.
    Value,
    /// A Python `OverflowError`; [`OperatorError::code`] holds its message.
    Overflow,
    /// `InviteError` (`mycelium_invite`): the invitation registry's table could not be prepared; the code is its
    /// closed code.
    Invite,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{code}")]
pub struct OperatorError {
    pub code: String,
    pub class: ErrorClass,
}

fn operator(code: &str) -> OperatorError {
    OperatorError {
        code: code.to_owned(),
        class: ErrorClass::Operator,
    }
}
fn authority(code: &str) -> OperatorError {
    OperatorError {
        code: code.to_owned(),
        class: ErrorClass::Authority,
    }
}
fn value_error(message: &str) -> OperatorError {
    OperatorError {
        code: message.to_owned(),
        class: ErrorClass::Value,
    }
}
fn overflow_error(message: &str) -> OperatorError {
    OperatorError {
        code: message.to_owned(),
        class: ErrorClass::Overflow,
    }
}
fn identity_error(code: &str) -> OperatorError {
    OperatorError {
        code: code.to_owned(),
        class: ErrorClass::NodeIdentity,
    }
}

/// A Python argument as the operator functions see it (`isinstance` checks decide).
#[derive(Debug, Clone, PartialEq)]
pub enum Arg {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
}

/// 32 bytes from the operating system's generator: the next seed key (`Ed25519PrivateKey.generate()`).
pub fn os_random_secret() -> Result<[u8; KEY_BYTES], OperatorError> {
    let mut secret = [0u8; KEY_BYTES];
    OsRng
        .try_fill_bytes(&mut secret)
        .map_err(|_| identity_error("node_identity_write_failed"))?;
    Ok(secret)
}

pub fn system_clock() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

// ---------------------------------------------------------------- private-directory lease

fn uid() -> u32 {
    rustix::process::getuid().as_raw()
}

/// `_validate_walk_component`: a directory owned by root or by us that others cannot write (a root-owned sticky
/// directory such as `/tmp` is the one trusted exception).
fn walk_component_valid(mode: u32, owner: u32) -> bool {
    let is_dir = mode & 0o170_000 == 0o040_000;
    let others_write = mode & 0o022 != 0;
    let root_sticky = owner == 0 && mode & 0o1000 != 0;
    is_dir && (owner == 0 || owner == uid()) && (!others_write || root_sticky)
}

fn dir_flags() -> OFlags {
    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC
}

fn normalised(path: &Path) -> Option<PathBuf> {
    if !path.is_absolute() {
        return None;
    }
    let mut out = PathBuf::from("/");
    for part in path.components() {
        match part {
            Component::RootDir | Component::CurDir | Component::Prefix(_) => {}
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(name) => out.push(name),
        }
    }
    Some(out)
}

/// What `private_directory_lease(path, create=False)` returned or raised.
enum Lease {
    Missing,
    Held(Held),
}

struct Held {
    path: PathBuf,
    device: u64,
    inode: u64,
    _directory: OwnedFd,
}

/// `Err(())` is the source's `ValueError` / `OSError` (the caller reports `seed_operator_state_invalid`).
fn open_lease(root: &Path) -> Result<Lease, ()> {
    open_lease_with(root, false)
}

/// `private_directory_lease(path, create=...)`: with `create` every missing component is made `0700` (owned by
/// us) and the lease always exists.
fn open_lease_with(root: &Path, create: bool) -> Result<Lease, ()> {
    let path = normalised(root).ok_or(())?;
    if path == Path::new("/") {
        return Err(());
    }
    let mut fd = openat(rustix::fs::CWD, "/", dir_flags(), Mode::empty()).map_err(|_| ())?;
    let start = fstat(&fd).map_err(|_| ())?;
    if !walk_component_valid(start.st_mode.into(), start.st_uid) {
        return Err(());
    }
    let parts: Vec<_> = path
        .components()
        .filter_map(|c| match c {
            Component::Normal(n) => Some(n.to_owned()),
            _ => None,
        })
        .collect();
    for (index, name) in parts.iter().enumerate() {
        let last = index + 1 == parts.len();
        let child = match openat(&fd, name, dir_flags(), Mode::empty()) {
            Ok(child) => child,
            Err(e) if e == rustix::io::Errno::NOENT && create => {
                let made = match mkdirat(&fd, name, Mode::RWXU) {
                    Ok(()) => true,
                    Err(e) if e == rustix::io::Errno::EXIST => false,
                    Err(_) => return Err(()),
                };
                let child = openat(&fd, name, dir_flags(), Mode::empty()).map_err(|_| ())?;
                if made {
                    let meta = fstat(&child).map_err(|_| ())?;
                    let mode: u32 = meta.st_mode.into();
                    if mode & 0o170_000 != 0o040_000 || meta.st_uid != uid() {
                        return Err(());
                    }
                    fchmod(&child, Mode::RWXU).map_err(|_| ())?;
                }
                child
            }
            Err(e) if e == rustix::io::Errno::NOENT => {
                // `FileNotFoundError` anywhere in the walk with `create=False`: the lease does not exist,
                // provided the directory reached so far is writable.
                let meta = fstat(&fd).map_err(|_| ())?;
                let mode: u32 = meta.st_mode.into();
                let writable = if rustix::process::geteuid().is_root() {
                    true
                } else if meta.st_uid == uid() {
                    mode & 0o300 == 0o300
                } else {
                    mode & 0o003 == 0o003
                };
                return if writable {
                    Ok(Lease::Missing)
                } else {
                    Err(())
                };
            }
            Err(_) => return Err(()),
        };
        let meta = fstat(&child).map_err(|_| ())?;
        let mode: u32 = meta.st_mode.into();
        if last {
            if mode & 0o170_000 != 0o040_000
                || meta.st_uid != uid()
                || mode & MODE_BITS != OWNER_DIR
            {
                return Err(());
            }
            let held = Held {
                path: path.clone(),
                device: meta.st_dev as u64,
                inode: meta.st_ino,
                _directory: child,
            };
            revalidate(&held)?;
            return Ok(Lease::Held(held));
        }
        if !walk_component_valid(mode, meta.st_uid) {
            return Err(());
        }
        fd = child;
    }
    Err(())
}

/// `PrivateDirectoryLease.revalidate`: the path still names the leased inode, owner-only.
fn revalidate(held: &Held) -> Result<(), ()> {
    let meta = fs::symlink_metadata(&held.path).map_err(|_| ())?;
    if !meta.is_dir()
        || meta.file_type().is_symlink()
        || meta.uid() != uid()
        || meta.mode() & MODE_BITS != OWNER_DIR
        || meta.dev() != held.device
        || meta.ino() != held.inode
    {
        return Err(());
    }
    Ok(())
}

// ---------------------------------------------------------------- identity key files (mycelium_node/identity.py)

/// `_owner_file`: a single-link regular 0600 file of ours that is not a symlink.
fn owner_file(path: &Path, code: &str) -> Result<(), OperatorError> {
    let meta = fs::symlink_metadata(path).map_err(|_| operator(code))?;
    if !meta.is_file()
        || meta.file_type().is_symlink()
        || meta.nlink() != 1
        || meta.uid() != uid()
        || meta.mode() & MODE_BITS != OWNER_FILE
    {
        return Err(operator(code));
    }
    Ok(())
}

fn signer_for(secret: &[u8; KEY_BYTES]) -> Result<Identity, OperatorError> {
    let first = Identity::from_secret("derive", secret)
        .map_err(|_| identity_error("node_identity_invalid"))?;
    let digest = first.verification_key_digest();
    Identity::from_secret(&format!("node-identity-{}", &digest[7..39]), secret)
        .map_err(|_| identity_error("node_identity_invalid"))
}

/// `_open_private_parent`: walk to the key file's parent without following links; the parent must be exactly
/// 0700 and ours.
fn open_private_parent(key_path: &Path) -> Result<OwnedFd, OperatorError> {
    let path_invalid = || identity_error("node_identity_path_invalid");
    let path = normalised(key_path).ok_or_else(path_invalid)?;
    let parent = path.parent().ok_or_else(path_invalid)?;
    let mut fd =
        openat(rustix::fs::CWD, "/", dir_flags(), Mode::empty()).map_err(|_| path_invalid())?;
    for part in parent.components() {
        if let Component::Normal(name) = part {
            fd = openat(&fd, name, dir_flags(), Mode::empty()).map_err(|_| path_invalid())?;
        }
    }
    let meta = fstat(&fd).map_err(|_| path_invalid())?;
    let mode: u32 = meta.st_mode.into();
    if meta.st_uid != uid() || mode & MODE_BITS != OWNER_DIR {
        return Err(identity_error("node_identity_permissions_invalid"));
    }
    Ok(fd)
}

fn file_name(path: &Path) -> Result<std::ffi::OsString, OperatorError> {
    path.file_name()
        .map(ToOwned::to_owned)
        .ok_or_else(|| identity_error("node_identity_path_invalid"))
}

/// `_load_existing(..., allow_incomplete)`: `Ok(None)` is the source's `None` (missing or short key while a
/// concurrent first start may still be writing).
fn load_existing(path: &Path, allow_incomplete: bool) -> Result<Option<Identity>, OperatorError> {
    let path_invalid = || identity_error("node_identity_path_invalid");
    let parent = open_private_parent(path)?;
    let name = file_name(path)?;
    let meta = match statat(&parent, &name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(meta) => meta,
        Err(e) if e == rustix::io::Errno::NOENT => {
            return if allow_incomplete {
                Ok(None)
            } else {
                Err(identity_error("node_identity_missing"))
            };
        }
        Err(_) => return Err(path_invalid()),
    };
    let mode: u32 = meta.st_mode.into();
    if mode & 0o170_000 != 0o100_000 || meta.st_nlink != 1 || meta.st_uid != uid() {
        return Err(path_invalid());
    }
    if mode & MODE_BITS != OWNER_FILE {
        return Err(identity_error("node_identity_permissions_invalid"));
    }
    let opened = openat(
        &parent,
        &name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|_| path_invalid())?;
    let before = fstat(&opened).map_err(|_| path_invalid())?;
    if (before.st_dev, before.st_ino) != (meta.st_dev, meta.st_ino) {
        return Err(path_invalid());
    }
    let mut bytes = Vec::new();
    let mut file = File::from(opened);
    (&mut file)
        .take(KEY_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| path_invalid())?;
    let after = fstat(file.as_fd()).map_err(|_| path_invalid())?;
    let key = |s: &rustix::fs::Stat| (s.st_dev, s.st_ino, s.st_size, s.st_mtime, s.st_ctime);
    if key(&before) != key(&after) {
        return Err(path_invalid());
    }
    if bytes.len() < KEY_BYTES && allow_incomplete {
        return Ok(None);
    }
    let secret: [u8; KEY_BYTES] = bytes
        .try_into()
        .map_err(|_| identity_error("node_identity_invalid"))?;
    signer_for(&secret).map(Some)
}

/// `load_node_signer`: an existing owner-only key, never creating anything.
pub fn load_node_signer(path: &Path) -> Result<Identity, OperatorError> {
    load_existing(path, false)?.ok_or_else(|| identity_error("node_identity_invalid"))
}

/// `_prepare_private_parent` (POSIX): no symlink among the ancestors, missing directories created 0700, the
/// parent not accessible to group/other and ours.
fn prepare_private_parent(key_path: &Path) -> Result<(), OperatorError> {
    let path_invalid = || identity_error("node_identity_path_invalid");
    let permissions = || identity_error("node_identity_permissions_invalid");
    let path = normalised(key_path).ok_or_else(path_invalid)?;
    let parent = path.parent().ok_or_else(path_invalid)?.to_owned();
    let mut missing = Vec::new();
    let mut nearest = parent.clone();
    while fs::symlink_metadata(&nearest).is_err() {
        missing.push(nearest.clone());
        match nearest.parent() {
            Some(up) if up != nearest => nearest = up.to_owned(),
            _ => break,
        }
    }
    for ancestor in nearest.ancestors() {
        let meta = fs::symlink_metadata(ancestor).map_err(|_| path_invalid())?;
        if meta.file_type().is_symlink() {
            return Err(path_invalid());
        }
    }
    let meta = fs::symlink_metadata(&nearest).map_err(|_| path_invalid())?;
    if !meta.is_dir() {
        return Err(path_invalid());
    }
    for directory in missing.into_iter().rev() {
        fs::create_dir(&directory).map_err(|_| path_invalid())?;
        fs::set_permissions(&directory, fs::Permissions::from_mode(OWNER_DIR))
            .map_err(|_| path_invalid())?;
    }
    let meta = fs::symlink_metadata(&parent).map_err(|_| path_invalid())?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return Err(path_invalid());
    }
    if meta.mode() & 0o077 != 0 || meta.uid() != uid() {
        return Err(permissions());
    }
    Ok(())
}

/// `_write_new`: create the key exclusively (0600) under the private parent; `Ok(None)` when it already exists.
fn write_new(path: &Path, secret: &[u8; KEY_BYTES]) -> Result<Option<Identity>, OperatorError> {
    let parent = open_private_parent(path)?;
    let name = file_name(path)?;
    let created = openat(
        &parent,
        &name,
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::RUSR | Mode::WUSR,
    );
    let fd = match created {
        Ok(fd) => fd,
        Err(e) if e == rustix::io::Errno::EXIST => return Ok(None),
        Err(_) => return Err(identity_error("node_identity_path_invalid")),
    };
    let mut file = File::from(fd);
    let written = file
        .set_permissions(fs::Permissions::from_mode(OWNER_FILE))
        .and_then(|()| file.write_all(secret))
        .and_then(|()| file.sync_all())
        .and_then(|()| File::from(parent.as_fd().try_clone_to_owned()?).sync_all());
    if written.is_err() {
        // `identity.py:_write_new` removes the partial key and raises `node_identity_write_failed` whether or not the
        // removal worked.
        if rustix::fs::unlinkat(&parent, &name, AtFlags::empty()).is_err() {
            return Err(identity_error("node_identity_write_failed"));
        }
        return Err(identity_error("node_identity_write_failed"));
    }
    signer_for(secret).map(Some)
}

/// `load_or_create_node_signer`: load the durable signer or atomically create it from `secret`.
pub fn load_or_create_node_signer(
    path: &Path,
    secret: &mut dyn FnMut() -> Result<[u8; KEY_BYTES], OperatorError>,
) -> Result<Identity, OperatorError> {
    prepare_private_parent(path)?;
    if fs::symlink_metadata(path).is_ok() {
        if let Some(existing) = load_existing(path, true)? {
            return Ok(existing);
        }
    } else if let Some(created) = write_new(path, &secret()?)? {
        return Ok(created);
    }
    let retries = budgets::BudgetId::PyMyceliumNodeIdentityCreateRetriesL25
        .count()
        .map_or(0, budgets::Entries::get);
    let pause = budgets::BudgetId::PyMyceliumNodeIdentityCreateRetrySecondsL26
        .span(budgets::Entries::ZERO)
        .map_or(0, |s| s.millis());
    for _ in 0..retries {
        if let Some(existing) = load_existing(path, true)? {
            return Ok(existing);
        }
        std::thread::sleep(std::time::Duration::from_millis(pause));
    }
    Err(identity_error("node_identity_invalid"))
}

// ---------------------------------------------------------------- mycelium_seed/authority.py

fn load_pseudonym(path: &Path) -> Result<[u8; 32], OperatorError> {
    let invalid = || authority("seed_product_pseudonym_key_invalid");
    let meta = match fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(authority("seed_product_pseudonym_key_missing"));
        }
        Err(_) => return Err(invalid()),
    };
    if !meta.is_file()
        || meta.file_type().is_symlink()
        || meta.nlink() != 1
        || meta.uid() != uid()
        || meta.mode() & MODE_BITS != OWNER_FILE
    {
        return Err(invalid());
    }
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags((OFlags::NOFOLLOW | OFlags::CLOEXEC).bits() as i32)
        .open(path)
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                authority("seed_product_pseudonym_key_missing")
            } else {
                invalid()
            }
        })?;
    let mut value = Vec::new();
    file.take(33)
        .read_to_end(&mut value)
        .map_err(|_| invalid())?;
    value.try_into().map_err(|_| invalid())
}

/// `ensure_product_pseudonym_salt`: load the salt, or derive it from the seed key and write it exclusively.
fn ensure_pseudonym(
    identity_root: &Path,
    signer: &Identity,
    swarm_id: &str,
) -> Result<(), OperatorError> {
    let path = identity_root.join(PSEUDONYM_FILE);
    if fs::symlink_metadata(&path).is_ok() {
        return load_pseudonym(&path).map(|_| ());
    }
    let value = seed_authority::derive_product_pseudonym_salt(signer, swarm_id)
        .map_err(|_| authority("seed_product_pseudonym_key_write_failed"))?;
    let created = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(OWNER_FILE)
        .custom_flags((OFlags::NOFOLLOW | OFlags::CLOEXEC).bits() as i32)
        .open(&path);
    let mut file = match created {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            return load_pseudonym(&path).map(|_| ());
        }
        Err(_) => return Err(authority("seed_product_pseudonym_key_write_failed")),
    };
    file.set_permissions(fs::Permissions::from_mode(OWNER_FILE))
        .and_then(|()| file.write_all(&value))
        .and_then(|()| file.sync_all())
        .map_err(|_| authority("seed_product_pseudonym_key_write_failed"))
}

/// `load_bound_seed_signer`: the one key file (`seed.key`, `seed.next.key`) whose digest is the stored one.
fn load_bound_seed_signer(
    identity_root: &Path,
    expected_digest: &str,
) -> Result<Identity, OperatorError> {
    let mut matches = Vec::new();
    let mut observed = false;
    for name in [ACTIVE_KEY, NEXT_KEY] {
        let path = identity_root.join(name);
        if fs::symlink_metadata(&path).is_err() {
            continue;
        }
        observed = true;
        let signer = load_node_signer(&path).map_err(|e| authority(&e.code))?;
        if signer.verification_key_digest() == expected_digest {
            matches.push(signer);
        }
    }
    match matches.len() {
        1 => Ok(matches.remove(0)),
        0 => Err(authority(if observed {
            "seed_authority_key_mismatch"
        } else {
            "seed_authority_key_missing"
        })),
        _ => Err(authority("seed_authority_key_ambiguous")),
    }
}

// ---------------------------------------------------------------- _load_authority

struct Loaded {
    lease: Held,
    coordinator: Coordinator,
    seed_key_digest: String,
    swarm_id: String,
    seed_node_id: String,
}

fn random_id() -> String {
    let mut raw = [0u8; 16];
    OsRng.fill_bytes(&mut raw);
    raw[6] = raw[6] & 0x0f | 0x40;
    raw[8] = raw[8] & 0x3f | 0x80;
    let hex: String = raw.iter().map(|b| format!("{b:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    )
}

fn load_authority(state_root: &Path, incarnation: &str) -> Result<Loaded, OperatorError> {
    let state_invalid = || operator("seed_operator_state_invalid");
    let lease = match open_lease(state_root) {
        Ok(Lease::Held(held)) => held,
        Ok(Lease::Missing) => return Err(operator("seed_operator_state_missing")),
        Err(()) => return Err(state_invalid()),
    };
    revalidate(&lease).map_err(|()| state_invalid())?;
    let database = lease.path.join(DATABASE);
    owner_file(&database, "seed_operator_database_invalid")?;
    let binding = store::read_binding(&database).map_err(|_| state_invalid())?;
    let signer = load_bound_seed_signer(&lease.path.join(IDENTITY_DIR), &binding.seed_key_digest)
        .map_err(|e| {
        operator(match e.code.as_str() {
            "node_identity_permissions_invalid"
            | "node_identity_path_invalid"
            | "node_identity_invalid" => "seed_operator_identity_invalid",
            "seed_authority_key_mismatch" => "seed_operator_identity_mismatch",
            _ => "seed_operator_state_invalid",
        })
    })?;
    let options = Options::new(&binding.swarm_id, &binding.seed_node_id, incarnation)
        .map_err(|_| state_invalid())?;
    let coordinator = Coordinator::open(
        &database,
        signer,
        options,
        Box::new(system_clock),
        Box::new(random_id),
    )
    .map_err(|_| state_invalid())?;
    revalidate(&lease).map_err(|()| state_invalid())?;
    Ok(Loaded {
        lease,
        coordinator,
        seed_key_digest: binding.seed_key_digest,
        swarm_id: binding.swarm_id,
        seed_node_id: binding.seed_node_id,
    })
}

// ---------------------------------------------------------------- helpers

fn reason_valid(arg: &Arg) -> bool {
    matches!(arg, Arg::Text(r) if !r.is_empty()
        && r.len() <= 64
        && r.as_bytes()[0].is_ascii_lowercase()
        && r.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_'))
}

fn max_overlap_seconds() -> Result<f64, OperatorError> {
    budgets::BudgetId::PyMyceliumSeedOperatorMaxRotationOverlapSecondsL43
        .span(budgets::Entries::ZERO)
        .map(|s| s.millis() as f64 / 1000.0)
        .map_err(|_| operator("seed_operator_rotation_overlap_invalid"))
}

fn millis(seconds: f64) -> i64 {
    (seconds * 1000.0) as i64
}

fn json_float(value: f64) -> Value {
    serde_json::Number::from_f64(value).map_or(Value::Null, Value::Number)
}

/// Python's `==` over JSON documents: numbers compare by value (`1 == 1.0`), everything else structurally.
fn py_equal(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => match (x.as_i64(), y.as_i64()) {
            (Some(i), Some(j)) => i == j,
            _ => x.as_f64() == y.as_f64(),
        },
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_equal(p, q))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(k, v)| y.get(k).is_some_and(|w| py_equal(v, w)))
        }
        _ => a == b,
    }
}

fn write_private(path: &Path, body: &[u8]) -> Result<(), OperatorError> {
    let failed = || operator("seed_operator_write_failed");
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(OWNER_FILE)
        .custom_flags((OFlags::NOFOLLOW | OFlags::CLOEXEC).bits() as i32)
        .open(path)
        .map_err(|_| failed())?;
    file.set_permissions(fs::Permissions::from_mode(OWNER_FILE))
        .and_then(|()| file.write_all(body))
        .and_then(|()| file.sync_all())
        .map_err(|_| failed())
}

fn sync_directory(path: &Path) -> Result<(), OperatorError> {
    File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(|_| operator("seed_operator_write_failed"))
}

fn exists_or_link(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}

fn finite(instant: f64) -> Result<f64, OperatorError> {
    if instant.is_finite() {
        Ok(instant)
    } else {
        Err(operator("seed_operator_clock_invalid"))
    }
}

// ---------------------------------------------------------------- the operator calls

/// `begin_seed_key_rotation`: create and durably bind a dual-signed pending seed-key transition.
pub fn begin_seed_key_rotation(
    state_root: &Path,
    reason: &Arg,
    overlap_seconds: &Arg,
    now: &dyn Fn() -> f64,
    new_secret: &mut dyn FnMut() -> Result<[u8; KEY_BYTES], OperatorError>,
) -> Result<Value, OperatorError> {
    if !reason_valid(reason) {
        return Err(operator("seed_operator_reason_invalid"));
    }
    let overlap = match overlap_seconds {
        Arg::Int(i) => *i as f64,
        Arg::Float(f) => *f,
        _ => return Err(operator("seed_operator_rotation_overlap_invalid")),
    };
    if !overlap.is_finite() || overlap <= 0.0 || overlap > max_overlap_seconds()? {
        return Err(operator("seed_operator_rotation_overlap_invalid"));
    }
    let Arg::Text(reason) = reason else {
        return Err(operator("seed_operator_reason_invalid"));
    };
    let loaded = load_authority(state_root, "seed-operator-rotate-begin")?;
    let identity_root = loaded.lease.path.join(IDENTITY_DIR);
    let next_key = identity_root.join(NEXT_KEY);
    let transition_path = identity_root.join(TRANSITION_FILE);
    let mut created_key = false;
    let mut created_transition = false;
    let cleanup = |key: bool, transition: bool| -> Result<(), OperatorError> {
        // `Path.unlink(missing_ok=True)`: an absent file is fine, any other failure surfaces.
        let remove = |path: &Path| match fs::remove_file(path) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
                Err(operator("seed_operator_rotation_failed"))
            }
            _ => Ok(()),
        };
        if transition {
            remove(&transition_path)?;
        }
        if key {
            remove(&next_key)?;
        }
        Ok(())
    };
    let result = (|| -> Result<Value, OperatorError> {
        if exists_or_link(&next_key) || exists_or_link(&transition_path) {
            return Err(operator("seed_operator_rotation_pending"));
        }
        let state = loaded
            .coordinator
            .authority_state()
            .map_err(|_| operator("seed_operator_rotation_failed"))?;
        if state["rotation"]["status"] == "PENDING" {
            return Err(operator("seed_operator_rotation_pending"));
        }
        ensure_pseudonym(
            &identity_root,
            loaded.coordinator.signer(),
            &loaded.swarm_id,
        )?;
        let new_signer = load_or_create_node_signer(&next_key, new_secret)?;
        created_key = true;
        let instant = finite(now())?;
        let previous = state["authority_generation"]
            .as_u64()
            .ok_or_else(|| operator("seed_operator_rotation_failed"))?;
        let new_digest = new_signer.verification_key_digest();
        let failed = |_| operator("seed_operator_rotation_failed");
        let transition = json!({
            "swarm_id": loaded.swarm_id,
            "seed_node_id": loaded.seed_node_id,
            "previous_generation": previous,
            "authority_generation": previous + 1,
            "old_seed_key_digest": loaded.seed_key_digest,
            "new_seed_key_digest": new_digest,
            "initiated_at": json_float(instant),
            "effective_at": json_float(instant),
            "overlap_expires_at": json_float(instant + overlap),
            "reason": reason,
        });
        let envelope = json!({
            "protocol": seed_transition::PROTOCOL,
            "transition": transition,
            "old_signature": loaded.coordinator.signer().sign(&transition).map_err(failed)?,
            "old_verification_key": loaded.coordinator.signer().public_key_record(),
            "new_signature": new_signer.sign(&transition).map_err(failed)?,
            "new_verification_key": new_signer.public_key_record(),
        });
        seed_transition::verify(
            &envelope,
            &json_float(instant),
            Some(&loaded.seed_key_digest),
        )
        .map_err(|e| operator(e.0))?;
        let body = canonical::to_bytes(&envelope)
            .map_err(|_| operator("seed_operator_rotation_failed"))?;
        write_private(&transition_path, &body)?;
        created_transition = true;
        sync_directory(&identity_root)?;
        loaded
            .coordinator
            .begin_authority_rotation(&transition)
            .map_err(|_| operator("seed_operator_rotation_failed"))?;
        Ok(json!({
            "protocol": SEED_OPERATOR_ROTATION_PROTOCOL,
            "event": "rotation_pending",
            "swarm_id": loaded.swarm_id,
            "previous_generation": previous,
            "authority_generation": previous + 1,
            "old_seed_key_digest": loaded.seed_key_digest,
            "new_seed_key_digest": new_digest,
            "effective_at_unix_ms": millis(instant),
            "overlap_expires_at_unix_ms": millis(instant + overlap),
            "route_ready": false,
        }))
    })();
    if let Err(error) = &result {
        // `except SeedOperatorError` / `except (OSError, SeedStateError, ...)` clean up; an authority or
        // node-identity error is not caught by the source and leaves what it found.
        if error.class == ErrorClass::Operator {
            cleanup(created_key, created_transition)?;
        }
    }
    result
}

/// `seed_key_rotation_status`: the public state of the latest durable authority transition.
pub fn seed_key_rotation_status(state_root: &Path) -> Result<Value, OperatorError> {
    let loaded = load_authority(state_root, "seed-operator-rotate-status")?;
    let invalid = || operator("seed_operator_rotation_state_invalid");
    let state = loaded
        .coordinator
        .authority_state()
        .map_err(|_| invalid())?;
    let rotation = &state["rotation"];
    if rotation.is_null() {
        return Ok(json!({
            "protocol": SEED_OPERATOR_ROTATION_PROTOCOL,
            "event": "rotation_absent",
            "swarm_id": loaded.swarm_id,
            "authority_generation": state["authority_generation"],
            "seed_key_digest": loaded.seed_key_digest,
            "route_ready": false,
        }));
    }
    let generation = rotation["authority_generation"]
        .as_u64()
        .ok_or_else(invalid)?;
    let real = |name: &str| rotation[name].as_f64().ok_or_else(invalid);
    let acknowledged: Vec<Value> = loaded
        .coordinator
        .rotation_acknowledgements(generation)
        .iter()
        .map(|item| {
            Ok(json!({
                "node_id": item["node_id"],
                "member_generation": item["member_generation"],
                "acknowledged_at_unix_ms": millis(item["acknowledged_at"].as_f64().ok_or_else(invalid)?),
            }))
        })
        .collect::<Result<_, OperatorError>>()?;
    Ok(json!({
        "protocol": SEED_OPERATOR_ROTATION_PROTOCOL,
        "event": format!("rotation_{}", rotation["status"].as_str().ok_or_else(invalid)?.to_lowercase()),
        "swarm_id": loaded.swarm_id,
        "previous_generation": rotation["previous_generation"],
        "authority_generation": generation,
        "old_seed_key_digest": rotation["old_seed_key_digest"],
        "new_seed_key_digest": rotation["new_seed_key_digest"],
        "effective_at_unix_ms": millis(real("effective_at")?),
        "overlap_expires_at_unix_ms": millis(real("overlap_expires_at")?),
        "acknowledged_members": acknowledged,
        "route_ready": false,
    }))
}

/// `complete_seed_key_rotation`: promote a verified pending key and fence the previous authority key.
pub fn complete_seed_key_rotation(
    state_root: &Path,
    authority_generation: &Arg,
    now: &dyn Fn() -> f64,
) -> Result<Value, OperatorError> {
    let generation = match authority_generation {
        Arg::Int(g) if *g >= 2 => *g as u64,
        _ => return Err(operator("seed_operator_rotation_generation_invalid")),
    };
    let loaded = load_authority(state_root, "seed-operator-rotate-complete")?;
    let failed = || operator("seed_operator_rotation_failed");
    let state = loaded.coordinator.authority_state().map_err(|_| failed())?;
    let rotation = &state["rotation"];
    if rotation.is_null() || rotation["authority_generation"].as_u64() != Some(generation) {
        return Err(operator("seed_operator_rotation_unknown"));
    }
    let identity_root = loaded.lease.path.join(IDENTITY_DIR);
    let transition_path = identity_root.join(TRANSITION_FILE);
    owner_file(&transition_path, "seed_operator_rotation_record_invalid")?;
    let envelope: Value = fs::read(&transition_path)
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .ok_or_else(|| operator("seed_operator_rotation_record_invalid"))?;
    let instant = finite(now())?;
    let text = |name: &str| rotation[name].as_str().ok_or_else(failed);
    let (old_digest, new_digest) = (text("old_seed_key_digest")?, text("new_seed_key_digest")?);
    let transition = seed_transition::verify(&envelope, &json_float(instant), Some(old_digest))
        .map_err(|e| operator(e.0))?;
    if !py_equal(&transition, &rotation["transition"]) {
        return Err(operator("seed_operator_rotation_record_mismatch"));
    }
    let next_key = identity_root.join(NEXT_KEY);
    let active_key = identity_root.join(ACTIVE_KEY);
    let status = text("status")?;
    if status == "PENDING" {
        let real = |name: &str| rotation[name].as_f64().ok_or_else(failed);
        if instant < real("effective_at")? {
            return Err(operator("seed_operator_rotation_not_effective"));
        }
        if instant > real("overlap_expires_at")? {
            return Err(operator("seed_operator_rotation_overlap_expired"));
        }
        owner_file(&next_key, "seed_operator_rotation_key_invalid")?;
        let next_signer = load_node_signer(&next_key)?;
        if next_signer.verification_key_digest() != new_digest
            || loaded.seed_key_digest != old_digest
        {
            return Err(operator("seed_operator_rotation_key_mismatch"));
        }
        let transition_digest = canonical::sha256_prefixed(
            &canonical::to_bytes(&rotation["transition"]).map_err(|_| failed())?,
        );
        let acknowledgements = loaded.coordinator.rotation_acknowledgements(generation);
        let missing = loaded.coordinator.member_rows().iter().any(|member| {
            let required = member.lease_expires_at > instant
                && matches!(member.lifecycle_state.as_str(), "CONFIGURED" | "RUNNING")
                && membership::peer_runtime_is_activation_eligible(
                    &json!(member.peer_class),
                    &member.runtime_capability,
                )
                .unwrap_or(false);
            required
                && !acknowledgements.iter().any(|ack| {
                    ack["node_id"] == member.node_id.as_str()
                        && ack["member_generation"] == json!(member.generation)
                        && ack["transition_digest"] == transition_digest.as_str()
                })
        });
        if missing {
            return Err(operator(
                "seed_operator_rotation_acknowledgements_incomplete",
            ));
        }
        loaded
            .coordinator
            .complete_authority_rotation(generation, new_digest)
            .map_err(|_| failed())?;
    } else if status != "COMPLETED" || loaded.seed_key_digest != new_digest {
        return Err(operator("seed_operator_rotation_state_invalid"));
    }
    if exists_or_link(&next_key) {
        fs::rename(&next_key, &active_key).map_err(|_| failed())?;
        fs::set_permissions(&active_key, fs::Permissions::from_mode(OWNER_FILE))
            .map_err(|_| failed())?;
        sync_directory(&identity_root).map_err(|_| failed())?;
    }
    let active = load_node_signer(&active_key)?;
    if active.verification_key_digest() != new_digest {
        return Err(operator("seed_operator_rotation_key_mismatch"));
    }
    Ok(json!({
        "protocol": SEED_OPERATOR_ROTATION_PROTOCOL,
        "event": "rotation_completed",
        "swarm_id": loaded.swarm_id,
        "authority_generation": generation,
        "old_seed_key_digest": old_digest,
        "new_seed_key_digest": new_digest,
        "completed_at_unix_ms": millis(instant),
        "route_ready": false,
    }))
}
