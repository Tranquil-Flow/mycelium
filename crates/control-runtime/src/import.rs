// SPDX-License-Identifier: AGPL-3.0-only
//! Offline, read-only v0.1.4 identity and standing-grant import. The installer
//! supplies the selected accepted binding, policy and observed RAM; import does
//! not authenticate membership, refresh a lease, or authorize execution.
//! Sources: mycelium_node/identity.py:168-291, standing_consent.py:124-136 @ f4e366e9.
use crate::{Error, Result, Store};
use protocol::identity::Identity;
use rustix::fs::{Mode, OFlags, openat};
use serde_json::{Value, json};
use std::{
    fs::File,
    io::Read,
    os::unix::fs::MetadataExt,
    path::{Component, Path},
};
use zeroize::Zeroizing;

pub struct LegacyState {
    identity: Identity,
    grant: Value,
}
fn contract(e: protocol::ContractError) -> Error {
    Error::Refused(e.0)
}
fn io(e: rustix::io::Errno) -> Error {
    Error::Io(e.into())
}
/// Walk every ancestor by held descriptor: a symlinked ancestor is not accepted.
fn private_root(path: &Path) -> Result<File> {
    if !path.is_absolute() {
        return Err(Error::Refused("import_path_invalid"));
    }
    let mut fd = openat(
        rustix::fs::CWD,
        "/",
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(io)?;
    for part in path.components() {
        match part {
            Component::RootDir => {}
            Component::Normal(name) => {
                fd = openat(
                    &fd,
                    name,
                    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                    Mode::empty(),
                )
                .map_err(io)?;
            }
            _ => return Err(Error::Refused("import_path_invalid")),
        }
    }
    let file = File::from(fd);
    let m = file.metadata()?;
    if m.uid() != rustix::process::getuid().as_raw() || m.mode() & 0o077 != 0 {
        return Err(Error::Refused("import_permissions_invalid"));
    }
    Ok(file)
}
fn read(root: &File, name: &str, max: u64) -> Result<Zeroizing<Vec<u8>>> {
    let fd = openat(
        root,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(io)?;
    let file = File::from(fd);
    let m = file.metadata()?;
    if !m.is_file()
        || m.nlink() != 1
        || m.uid() != rustix::process::getuid().as_raw()
        || m.mode() & 0o077 != 0
        || m.len() > max
    {
        return Err(Error::Refused("import_file_invalid"));
    }
    let mut bytes = Zeroizing::new(Vec::new());
    (&file)
        .take(
            max.checked_add(1)
                .ok_or(Error::Refused("import_limit_invalid"))?,
        )
        .read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    if bytes.len() as u64 > max
        || (
            m.dev(),
            m.ino(),
            m.len(),
            m.mtime(),
            m.mtime_nsec(),
            m.ctime(),
            m.ctime_nsec(),
        ) != (
            after.dev(),
            after.ino(),
            after.len(),
            after.mtime(),
            after.mtime_nsec(),
            after.ctime(),
            after.ctime_nsec(),
        )
    {
        return Err(Error::Refused("import_file_changed"));
    }
    Ok(bytes)
}
fn identity(bytes: &[u8]) -> Result<Identity> {
    let secret: Zeroizing<[u8; 32]> = Zeroizing::new(
        bytes
            .try_into()
            .map_err(|_| Error::Refused("node_identity_invalid"))?,
    );
    let first = Identity::from_secret("derive", &secret)
        .map_err(|_| Error::Refused("node_identity_invalid"))?;
    let digest = first.verification_key_digest();
    Identity::from_secret(&format!("node-identity-{}", &digest[7..39]), &secret)
        .map_err(|_| Error::Refused("node_identity_invalid"))
}
fn validate_grant(grant: &Value) -> Result<()> {
    let fields = [
        "protocol",
        "disclosure_digest",
        "caps",
        "granted_at",
        "binding",
        "bound_at",
        "withdrawn_at",
        "state",
    ];
    if !grant
        .as_object()
        .is_some_and(|o| o.len() == fields.len() && fields.iter().all(|k| o.contains_key(*k)))
        || grant["protocol"] != "mycelium.a13_standing_consent.v1"
        || grant["disclosure_digest"] != crate::consent::disclosure_digest()
        || !matches!(
            grant["state"].as_str(),
            Some("granted" | "granted_unbound" | "withdrawn")
        )
    {
        return Err(Error::Refused("standing_consent_state_invalid"));
    }
    if !grant["binding"].is_null()
        && !grant["binding"].as_object().is_some_and(|o| {
            o.len() == 3
                && ["swarm_id", "node_id", "seed_key_digest"].iter().all(|k| {
                    o.get(*k)
                        .and_then(Value::as_str)
                        .is_some_and(|s| !s.is_empty())
                })
        })
    {
        return Err(Error::Refused("standing_consent_binding_invalid"));
    }
    Ok(())
}
impl LegacyState {
    /// `root` is the private directory containing node.key and standing-consent.json.
    /// It may be an installer snapshot assembled from the installation's two roots.
    pub fn read(
        root: &Path,
        binding: &Value,
        policy: &Value,
        memory: &Value,
        max_json: budgets::Bytes,
    ) -> Result<Self> {
        let root = private_root(root)?;
        let key = read(&root, "node.key", 32)?;
        let identity = identity(&key)?;
        let bytes = read(&root, "standing-consent.json", max_json.get())?;
        let grant = protocol::strict::object(
            &bytes,
            usize::try_from(max_json.get()).map_err(|_| Error::Refused("import_limit_invalid"))?,
        )
        .map_err(|_| Error::Refused("standing_consent_state_invalid"))?;
        validate_grant(&grant)?;
        protocol::consent::caps(&grant["caps"], policy, memory).map_err(contract)?;
        if !grant["binding"].is_null() && &grant["binding"] != binding {
            return Err(Error::Refused("standing_consent_owner_mismatch"));
        }
        Ok(Self { identity, grant })
    }
    pub fn identity(&self) -> &Identity {
        &self.identity
    }
    pub fn grant(&self) -> &Value {
        &self.grant
    }
    pub fn admission(
        &self,
        binding: &Value,
        offer: &Value,
        resource_violation: Option<&str>,
    ) -> Result<Option<String>> {
        protocol::consent::standing_admission(&self.grant, binding, offer, resource_violation)
            .map_err(contract)
    }
    /// One create-only SQLite transaction preserves exact grant JSON and key bytes.
    /// Existing authority is never silently replaced, even by an identical import.
    pub fn persist(&self, store: &mut Store) -> Result<()> {
        let document = json!({"secret":self.identity.secret_bytes().as_slice(),"grant":self.grant});
        let bytes = Zeroizing::new(
            serde_json::to_vec(&document)
                .map_err(|_| Error::Refused("import_serialization_failed"))?,
        );
        store.save_document("legacy_identity_grant", None, &bytes)?;
        Ok(())
    }
    pub fn restore(store: &Store) -> Result<Self> {
        let (_, bytes) = store
            .load_document("legacy_identity_grant")?
            .ok_or(Error::Refused("import_missing"))?;
        let bytes = Zeroizing::new(bytes);
        let mut document: Value =
            serde_json::from_slice(&bytes).map_err(|_| Error::Refused("import_state_invalid"))?;
        let key = Zeroizing::new(
            serde_json::from_value::<Vec<u8>>(document["secret"].take())
                .map_err(|_| Error::Refused("import_state_invalid"))?,
        );
        let grant = document["grant"].take();
        validate_grant(&grant)?;
        Ok(Self {
            identity: identity(&key)?,
            grant,
        })
    }
}
