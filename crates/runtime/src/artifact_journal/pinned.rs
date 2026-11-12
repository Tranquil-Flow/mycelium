// SPDX-License-Identifier: AGPL-3.0-only
//! Run-local source custody. Namespace validation never authorizes an unguarded pathname read.
use super::{ArtifactError, error};
use rustix::fs::{Mode, OFlags, open, openat};
use std::{
    collections::BTreeMap,
    fs::{File, Metadata},
    os::unix::fs::MetadataExt,
    path::{Component, Path},
    sync::Arc,
};

fn changed() -> ArtifactError {
    error("artifact_source_identity_changed")
}
#[derive(PartialEq, Eq)]
struct Identity {
    dev: u64,
    ino: u64,
    uid: u32,
    mode: u32,
}
impl Identity {
    fn of(m: &Metadata) -> Self {
        Self {
            dev: m.dev(),
            ino: m.ino(),
            uid: m.uid(),
            mode: m.mode(),
        }
    }
}
struct Directory {
    parent: Arc<File>,
    name: std::ffi::OsString,
    file: Arc<File>,
    identity: Identity,
}
struct Object {
    parents: Vec<Directory>,
    parent: Arc<File>,
    name: std::ffi::OsString,
    // Keeps the admitted inode allocated, including across unlink/replacement.
    file: File,
    metadata: Metadata,
}
impl Object {
    fn admit(path: &Path, length: Option<u64>) -> Result<Self, ArtifactError> {
        if !path.is_absolute() {
            return Err(changed());
        }
        let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let mut parent = Arc::new(File::from(
            open("/", flags | OFlags::DIRECTORY, Mode::empty()).map_err(|_| changed())?,
        ));
        let mut parents = Vec::new();
        let mut components = path.components().peekable();
        while let Some(component) = components.next() {
            let Component::Normal(name) = component else {
                if component == Component::RootDir {
                    continue;
                }
                return Err(changed());
            };
            if components.peek().is_none() {
                let file = File::from(
                    openat(&*parent, name, flags | OFlags::NONBLOCK, Mode::empty())
                        .map_err(|_| changed())?,
                );
                let metadata = file.metadata().map_err(|_| changed())?;
                if !metadata.is_file()
                    || metadata.nlink() != 1
                    || metadata.uid() != rustix::process::getuid().as_raw()
                    || length.is_some_and(|n| n != metadata.len())
                {
                    return Err(changed());
                }
                return Ok(Self {
                    parents,
                    parent,
                    name: name.into(),
                    file,
                    metadata,
                });
            }
            let file = Arc::new(File::from(
                openat(&*parent, name, flags | OFlags::DIRECTORY, Mode::empty())
                    .map_err(|_| changed())?,
            ));
            let identity = Identity::of(&file.metadata().map_err(|_| changed())?);
            parents.push(Directory {
                parent,
                name: name.into(),
                file: file.clone(),
                identity,
            });
            parent = file;
        }
        Err(changed())
    }
    fn same(&self, m: &Metadata) -> bool {
        let old = &self.metadata;
        Identity::of(m) == Identity::of(old)
            && m.is_file()
            && m.nlink() == 1
            && m.len() == old.len()
            && m.mtime() == old.mtime()
            && m.mtime_nsec() == old.mtime_nsec()
            && m.ctime() == old.ctime()
            && m.ctime_nsec() == old.ctime_nsec()
    }
    fn open(&self) -> Result<File, ArtifactError> {
        let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        for dir in &self.parents {
            let now = File::from(
                openat(
                    &*dir.parent,
                    &dir.name,
                    flags | OFlags::DIRECTORY,
                    Mode::empty(),
                )
                .map_err(|_| changed())?,
            );
            if Identity::of(&now.metadata().map_err(|_| changed())?) != dir.identity
                || Identity::of(&dir.file.metadata().map_err(|_| changed())?) != dir.identity
            {
                return Err(changed());
            }
        }
        if !self.same(&self.file.metadata().map_err(|_| changed())?) {
            return Err(changed());
        }
        // Open through the RETAINED parent, not the pathname that was just checked.
        let file = File::from(
            openat(
                &*self.parent,
                &self.name,
                flags | OFlags::NONBLOCK,
                Mode::empty(),
            )
            .map_err(|_| changed())?,
        );
        if !self.same(&file.metadata().map_err(|_| changed())?) {
            return Err(changed());
        }
        Ok(file)
    }
}
/// Original source objects captured at admission, without copying bytes or creating hardlinks.
/// Substitution refuses. A concurrent rename after a check can only read the original inode;
/// a post-read check prevents publishing that chunk once the namespace change is observable.
#[derive(Default)]
pub struct PinnedObjects {
    objects: BTreeMap<String, Object>,
}
impl PinnedObjects {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn admit(
        &mut self,
        hex: &str,
        path: &Path,
        length: Option<u64>,
    ) -> Result<(), ArtifactError> {
        if hex.len() != 64
            || !hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(changed());
        }
        let object = Object::admit(path, length)?;
        if let Some(old) = self.objects.get(hex) {
            if !old.same(&object.metadata) {
                return Err(changed());
            }
        } else {
            self.objects.insert(hex.into(), object);
        }
        Ok(())
    }
    pub(super) fn open(&self, hex: &str) -> Result<File, ArtifactError> {
        self.objects.get(hex).ok_or_else(changed)?.open()
    }
}
