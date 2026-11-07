// SPDX-License-Identifier: AGPL-3.0-only
//! Descriptor-relative private enrollment storage, matching node/identity.py
//! no-follow/single-link/raw-key rules at f4e366e9. Never touches installed state.
use crate::loopback::Result;
use budgets::{BudgetId as B, Entries};
use protocol::identity::Identity;
use rustix::fs::{Mode, OFlags, mkdirat, openat};
use std::{
    fs::File,
    io::{Read, Write},
    os::unix::fs::MetadataExt,
    path::{Component, Path},
};
use zeroize::Zeroizing;

pub struct PrivateRoot(File);
impl PrivateRoot {
    pub fn open(path: &Path) -> Result<Self> {
        if !path.is_absolute() {
            return Err("node_identity_path_invalid".into());
        }
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let mut fd = openat(rustix::fs::CWD, "/", flags, Mode::empty())?;
        for part in path.components() {
            match part {
                Component::RootDir => {}
                Component::Normal(name) => {
                    let next = match openat(&fd, name, flags, Mode::empty()) {
                        Ok(next) => next,
                        Err(e) if e == rustix::io::Errno::NOENT => {
                            match mkdirat(&fd, name, Mode::RUSR | Mode::WUSR | Mode::XUSR) {
                                Ok(()) => openat(&fd, name, flags, Mode::empty())?,
                                Err(e) if e == rustix::io::Errno::EXIST => {
                                    // A concurrent creator is not trusted: acquire
                                    // and validate the same no-follow descriptor.
                                    openat(&fd, name, flags, Mode::empty())?
                                }
                                Err(e) => return Err(e.into()),
                            }
                        }
                        Err(e) => return Err(e.into()),
                    };
                    fd = next;
                }
                _ => return Err("node_identity_path_invalid".into()),
            }
        }
        let root = Self(File::from(fd));
        let m = root.0.metadata()?;
        if m.uid() != rustix::process::getuid().as_raw() || m.mode() & 0o777 != 0o700 {
            return Err("node_identity_permissions_invalid".into());
        }
        Ok(root)
    }
    pub fn read(&self, name: &str, max: u64) -> Result<Option<Zeroizing<Vec<u8>>>> {
        if name.contains('/') || name == "." || name == ".." {
            return Err("private_name_invalid".into());
        }
        let fd = match openat(
            &self.0,
            name,
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::empty(),
        ) {
            Ok(fd) => fd,
            Err(e) if e == rustix::io::Errno::NOENT => return Ok(None),
            Err(e) => return Err(e.into()),
        };
        let file = File::from(fd);
        let before = file.metadata()?;
        if !before.is_file()
            || before.nlink() != 1
            || before.uid() != rustix::process::getuid().as_raw()
            || before.mode() & 0o077 != 0
            || before.len() > max
        {
            return Err("node_identity_file_invalid".into());
        }
        let mut bytes = Zeroizing::new(Vec::new());
        (&file)
            .take(max.checked_add(1).ok_or("private_limit_overflow")?)
            .read_to_end(&mut bytes)?;
        let after = file.metadata()?;
        if bytes.len() as u64 > max
            || (
                before.dev(),
                before.ino(),
                before.len(),
                before.mtime(),
                before.mtime_nsec(),
                before.ctime(),
                before.ctime_nsec(),
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
            return Err("node_identity_file_changed".into());
        }
        Ok(Some(bytes))
    }
    /// Create-only: collisions never replace an existing identity or choice.
    pub fn create(&self, name: &str, bytes: &[u8]) -> Result<bool> {
        if name.contains('/') || name == "." || name == ".." {
            return Err("private_name_invalid".into());
        }
        let fd = match openat(
            &self.0,
            name,
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::RUSR | Mode::WUSR,
        ) {
            Ok(fd) => fd,
            Err(e) if e == rustix::io::Errno::EXIST => return Ok(false),
            Err(e) => return Err(e.into()),
        };
        let mut file = File::from(fd);
        file.write_all(bytes)?;
        file.sync_all()?;
        self.0.sync_all()?;
        Ok(true)
    }
}
pub fn identity(path: &Path) -> Result<Identity> {
    let root = PrivateRoot::open(path)?;
    let count = B::PyMyceliumNodeIdentityCreateRetriesL25
        .count()
        .map_err(|e| format!("identity count: {e:?}"))?
        .get();
    for _ in 0..count {
        match root.read("node.key", 32)? {
            Some(bytes) if bytes.len() == 32 => {
                let secret = Zeroizing::new(<[u8; 32]>::try_from(bytes.as_slice())?);
                let first = Identity::from_secret("derive", &secret)?;
                return Ok(Identity::from_secret(
                    &format!("node-identity-{}", &first.verification_key_digest()[7..39]),
                    &secret,
                )?);
            }
            Some(_) => {}
            None => {
                let mut secret = Zeroizing::new([0u8; 32]);
                File::open("/dev/urandom")?.read_exact(secret.as_mut())?;
                root.create("node.key", secret.as_slice())?;
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(
            B::PyMyceliumNodeIdentityCreateRetrySecondsL26
                .span(Entries::ZERO)
                .map_err(|e| format!("identity retry: {e:?}"))?
                .millis(),
        ));
    }
    Err("node_identity_invalid".into())
}
