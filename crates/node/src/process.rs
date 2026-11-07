// SPDX-License-Identifier: AGPL-3.0-only
//! Exact Child-handle ownership; no process-name/PID scanning or global kills.
//! This adapter owns ONE non-forking native worker plus its exclusive directory.
//! Descendant/process-tree and model-device residue require the L8/L9 adapter.
pub use crate::fds::confine_descriptors;
use crate::{Residue, WorkerAdapter, WorkerError};
use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::{
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
};
pub struct NativeChild {
    child: Child,
    root: PathBuf,
    identity: (u64, u64),
    exited: bool,
    removed: bool,
}
impl NativeChild {
    pub fn spawn(command: &mut Command, root: &Path) -> std::result::Result<Self, WorkerError> {
        Self::spawn_passing(command, root, &[])
    }
    /// Spawn with the descriptors in `pass` (and only those above stderr) inherited; see
    /// [`crate::fds::confine_descriptors_except`] for the contract and refusals. A refusal
    /// happens before any directory or journal exists.
    pub fn spawn_passing(
        command: &mut Command,
        root: &Path,
        pass: &[std::os::fd::RawFd],
    ) -> std::result::Result<Self, WorkerError> {
        // Before any owned resource exists, so a refusal leaves nothing to clean up.
        crate::fds::confine_descriptors_except(command, pass)?;
        std::fs::DirBuilder::new().mode(0o700).create(root)?;
        let metadata = std::fs::symlink_metadata(root)?;
        let identity = (metadata.dev(), metadata.ino());
        let child = match command
            .current_dir(root)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
        {
            Ok(child) => child,
            Err(spawn) => {
                if let Err(cleanup) = std::fs::remove_dir(root) {
                    return Err(WorkerError::SpawnCleanup { spawn, cleanup });
                }
                return Err(WorkerError::Io(spawn));
            }
        };
        Ok(Self {
            child,
            root: root.into(),
            identity,
            exited: false,
            removed: false,
        })
    }
    pub fn pid(&self) -> u32 {
        self.child.id()
    }
    pub fn root(&self) -> &Path {
        &self.root
    }
}
impl WorkerAdapter for NativeChild {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        if !self.exited {
            if self.child.try_wait()?.is_some() {
                self.exited = true;
            } else {
                self.child.kill()?;
            }
        }
        Ok(())
    }
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError> {
        if !self.exited {
            self.exited = self.child.try_wait()?.is_some();
        }
        if self.exited && !self.removed {
            let metadata = std::fs::symlink_metadata(&self.root)?;
            if !metadata.is_dir() || (metadata.dev(), metadata.ino()) != self.identity {
                return Err(WorkerError::Refused("worker_resource_identity_changed"));
            }
            std::fs::remove_dir(&self.root)?;
            self.removed = true;
        }
        Ok(Residue {
            process_gone: self.exited,
            resources_gone: self.removed,
        })
    }
}
