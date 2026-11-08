// SPDX-License-Identifier: AGPL-3.0-only
//! Trusted root spellings must not weaken below-root no-follow admission.
use api::{
    acquisition::{Acquisition, HttpTransport, Options, PrepareError, Preparer},
    model_catalog,
};
use std::{
    fs,
    os::unix::fs::{DirBuilderExt, symlink},
    path::{Path, PathBuf},
    sync::Arc,
};

struct Fixture(PathBuf);
impl Fixture {
    fn new(name: &str) -> Self {
        let root =
            std::env::temp_dir().join(format!("acquisition-root-{name}-{}", std::process::id()));
        fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
        Self(root)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("fixture cleanup");
    }
}
struct NeverPrepare;
impl Preparer for NeverPrepare {
    fn prepare(&self, _: &Path, _: &model_catalog::Model) -> Result<(), PrepareError> {
        panic!("root admission never prepares or downloads a model")
    }
}
fn open(root: &Path) -> Result<Acquisition, api::acquisition::AcqError> {
    Acquisition::open(
        root,
        &model_catalog::registered()[0],
        Options {
            transport: Arc::new(HttpTransport::hugging_face(true)),
            preparer: Arc::new(NeverPrepare),
            disk_free: Box::new(api::acquisition::disk_free_bytes),
            clock_unix_ms: Box::new(api::acquisition::system_clock_ms),
            observer: None,
        },
    )
}
#[test]
fn acquisition_accepts_a_trusted_root_below_a_symlinked_ancestor() {
    let f = Fixture::new("ancestor");
    fs::create_dir(f.0.join("physical")).unwrap();
    symlink(f.0.join("physical"), f.0.join("alias")).unwrap();
    let candidate = f.0.join("alias/state");
    let acquisition = open(&candidate).expect("legitimate symlinked ancestor");
    assert_eq!(
        acquisition.state_root(),
        fs::canonicalize(&candidate).unwrap()
    );
    assert_ne!(acquisition.state_root(), candidate);
    assert_eq!(acquisition.status().state, "idle");
    acquisition.close().unwrap();
    let reopened = open(&candidate).expect("same root reopens");
    reopened.close().unwrap();
}
#[test]
fn acquisition_refuses_root_and_below_root_symlinks_without_touching_foreign_bytes() {
    let f = Fixture::new("refusals");
    let physical = fs::canonicalize(&f.0).unwrap();
    let root = physical.join("state");
    open(&root).unwrap().close().unwrap();
    let foreign = physical.join("foreign");
    fs::write(&foreign, b"foreign sentinel").unwrap();
    fs::remove_file(root.join("acquisition.lock")).unwrap();
    symlink(&foreign, root.join("acquisition.lock")).unwrap();
    assert_eq!(
        open(&root).err().unwrap().code(),
        "acquisition_state_root_unsafe"
    );
    symlink(&root, physical.join("root-link")).unwrap();
    assert_eq!(
        open(&physical.join("root-link")).err().unwrap().code(),
        "acquisition_state_root_unsafe"
    );
    assert_eq!(fs::read(&foreign).unwrap(), b"foreign sentinel");
}
