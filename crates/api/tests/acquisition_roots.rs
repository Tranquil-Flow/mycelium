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
/// `app_host::reopen_acquisition_if_needed` (`if acquisition is None or its state_root differs:
/// self._open_acquisition()`) on a host root below a legitimate symlinked ancestor: the open
/// acquisition keeps its physical root (above), so the same root spelled through the alias is
/// the same root and the open acquisition (and any attempt it runs) is kept, not closed and
/// reopened.
#[test]
fn app_host_reopen_keeps_the_open_acquisition_below_a_symlinked_ancestor() {
    use api::app_host::{self, AcquisitionSite, AcquisitionView};
    let f = Fixture::new("reopen");
    fs::create_dir(f.0.join("physical")).unwrap();
    symlink(f.0.join("physical"), f.0.join("alias")).unwrap();
    let root = f.0.join("alias/state");
    fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
    let transport =
        || -> Arc<dyn api::acquisition::Transport> { Arc::new(HttpTransport::hugging_face(true)) };
    let site = AcquisitionSite {
        root: &root,
        sources: &root,
        target: false,
        run_model_env: None,
        transport: &transport,
    };
    let (mut acquisition, mut active, mut policy_error) = (None, false, None);
    let mut view = AcquisitionView {
        acquisition: &mut acquisition,
        active: &mut active,
        policy_error: &mut policy_error,
    };
    app_host::open_acquisition(&mut view, &site).expect("the run model's acquisition opens");
    let opened = view.acquisition.clone().expect("opened");
    assert_ne!(
        opened.state_root(),
        app_host::acquisition_root(&site).unwrap()
    );
    app_host::reopen_acquisition_if_needed(&mut view, &site);
    assert_eq!(
        opened.status().state,
        "idle",
        "the same root must not be closed and reopened"
    );
    // A different root (this Mac becomes a target: the staging consumer's `serving` root) still
    // closes the open acquisition and opens the run model's there.
    let target = AcquisitionSite {
        target: true,
        ..site
    };
    app_host::reopen_acquisition_if_needed(&mut view, &target);
    assert_eq!(opened.status().state, "closed");
    let reopened = view.acquisition.take().expect("reopened");
    assert_eq!(
        reopened.state_root(),
        fs::canonicalize(app_host::acquisition_root(&target).unwrap()).unwrap()
    );
    reopened.close().unwrap();
}

/// `_policy_loop`'s decision (app.py:1007-1030 @ f4e366e9), shared by the headless launcher and
/// the packaged executable: an ended attempt only clears the flag and observes nothing; a lost
/// membership cancels with `membership_required` before this Mac is observed; otherwise the
/// resource guard's code cancels, and a satisfied guard keeps the attempt.
#[test]
fn the_policy_verdict_follows_the_attempt_the_membership_then_this_mac() {
    use api::app_host::{PolicyVerdict, acquisition_policy_verdict};
    let unreachable = || -> bool { panic!("an ended attempt reads no membership") };
    for ended in ["prepared", "cancelled", "failed"] {
        assert_eq!(
            acquisition_policy_verdict(ended, unreachable, || panic!("nor observes this Mac")),
            PolicyVerdict::Finished,
            "{ended}"
        );
    }
    for running in ["starting", "resolving_metadata", "downloading", "verifying"] {
        assert_eq!(
            acquisition_policy_verdict(running, || false, || panic!("membership first")),
            PolicyVerdict::Cancel("membership_required"),
            "{running}"
        );
        assert_eq!(
            acquisition_policy_verdict(running, || true, || Some("resource_power_policy_violation")),
            PolicyVerdict::Cancel("resource_power_policy_violation"),
            "{running}"
        );
        assert_eq!(
            acquisition_policy_verdict(running, || true, || None),
            PolicyVerdict::Keep,
            "{running}"
        );
    }
}
