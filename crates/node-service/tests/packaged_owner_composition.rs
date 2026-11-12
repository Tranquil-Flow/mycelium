// SPDX-License-Identifier: AGPL-3.0-only
//! S4c-b1: owner composition, without models or application launches.
#![cfg(feature = "desktop-shell")]
use api::{
    chat_service::{RouteGateway, RunRoute},
    schema::PublicError,
};
use desktop::{install::Roots, lifecycle::Poll, services::Services};
use node_service::{
    desktop_host::DesktopServices,
    packaged,
    service::{Driver, Job, NodeService},
};
use serde_json::Value;
use std::{
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

struct NoNative;
impl Driver for NoNative {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        panic!("model-free owner composition must not execute native inference")
    }
}
/// Named constructor-only double: no serving run or gateway exists in this root-admission test.
struct IdleRunDouble;
impl RunRoute for IdleRunDouble {
    fn status(&self) -> Option<Value> {
        None
    }
    fn gateway(&self, _: &str) -> Option<Arc<dyn RouteGateway>> {
        None
    }
    fn model_id(&self) -> Option<String> {
        None
    }
}
struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        use std::os::unix::fs::DirBuilderExt;
        // Deliberately retain macOS's /var spelling. Admission must canonicalize the ancestor,
        // not demand lexical equality with /private/var.
        let path = std::env::temp_dir().join(format!(
            "s4cb1-{}",
            node_service::service::random_id().unwrap()
        ));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .unwrap();
        Self(path)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        if let Err(error) = std::fs::remove_dir_all(&self.0) {
            eprintln!("s4cb1_scratch_cleanup_failed: {error}");
        }
    }
}
fn services(state: &std::path::Path) -> DesktopServices {
    let dirs = packaged::prepare_dirs(state).unwrap();
    DesktopServices::new(
        NodeService::new(
            Arc::new(NoNative),
            "model-free-owner".into(),
            node_service::native::development_qualification().unwrap(),
        ),
        packaged::CALLBACK_CAPACITY,
        &dirs.private,
    )
    .with_chat_run(
        Arc::new(IdleRunDouble),
        Some(state.join("v02-node/evidence/chat-timing.jsonl")),
    )
    .expect("W1: the packaged owner's timing file must be admitted under its own state root")
}
fn shutdown(host: &DesktopServices) {
    let allowance = budgets::BudgetId::StageCleanup
        .span(budgets::Entries::ZERO)
        .unwrap();
    let end = Instant::now() + Duration::from_millis(allowance.millis());
    host.begin_shutdown(end).unwrap();
    loop {
        match host.poll_shutdown().unwrap() {
            Poll::Verified => break,
            Poll::Pending => {
                assert!(Instant::now() < end, "model-free services did not close");
                std::thread::yield_now();
            }
        }
    }
}
#[test]
fn timing_admission_accepts_the_real_default_folder_shape_and_macos_temp_alias() {
    let scratch = Scratch::new();
    let roots = Roots::new(&scratch.0, &scratch.0.join("Applications")).unwrap();
    for state in [roots.state(), scratch.0.join("temporary-owner")] {
        let host = services(&state);
        assert!(host.run_chats().is_some());
        shutdown(&host);
    }
}
