// SPDX-License-Identifier: AGPL-3.0-only
//! Real L5/L9/L10 settlement through the SLICE-1 host and L11 cleanup adapter.
//! Controlled signed peer + inert residue callback, NOT native worker/GPU proof.
#![cfg(feature = "service-composition")]
use api::schema::PublicError;
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant, Span};
use desktop::{
    channel::{Channel, ORIGIN, Request},
    core::Core,
    lifecycle::{Cleanup, QuitSource, Shutdown, State},
    services::adapt,
};
use node_service::{
    desktop_host::DesktopServices,
    receipts,
    service::{Driver, Job, NodeService},
};
use serde_json::json;
use std::{
    num::NonZeroUsize,
    sync::{Arc, Mutex, mpsc},
    time::{Duration, Instant},
};
/// A fresh private state root for the packaged action host (these chat paths never write it).
fn private() -> std::path::PathBuf {
    use std::os::unix::fs::DirBuilderExt;
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "node-settlement-private-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
fn budget() -> Span {
    BudgetId::StageCleanup.span(Entries::ZERO).unwrap()
}
fn wait() -> Duration {
    Duration::from_millis(
        BudgetId::WorkerStartup
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
struct SignedSession {
    entered: mpsc::SyncSender<()>,
    release: Mutex<mpsc::Receiver<()>>,
    settled: mpsc::SyncSender<()>,
}
impl Driver for SignedSession {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        // The grant window lives on this run's own clock, as in native.rs:839-847: the time since
        // the run began at admission, then the time left to `job.deadline`.
        let begun = Instant::now();
        let result = (|| -> node_service::loopback::Result<()> {
            let owner = receipts::identity("slice-owner")?;
            let member = receipts::identity("slice-member")?;
            let (source, session) = receipts::Receipts::open(
                &owner,
                (&member.public_key_record(), member.endpoint_id()),
                &job.id,
                job.generation,
                &job.document,
                begun.elapsed(),
                job.deadline.saturating_duration_since(Instant::now()),
            )?;
            job.install(session)?;
            self.entered.send(())?;
            self.release.lock().unwrap().recv_timeout(wait())?;
            assert!(
                job.cancelled(),
                "desktop shutdown did not cancel the real session"
            );
            let start = Instant::now();
            let now = || {
                MonoInstant::new(
                    ClockDomain(1),
                    start.elapsed().as_millis().try_into().unwrap(),
                )
            };
            let deadline = now().checked_add(budget()).unwrap();
            let mut round = router::cleanup::CleanupRound::new(
                source.subject.clone(),
                vec![router::cleanup::CleanupPeer {
                    node: "b-member".into(),
                    host: "loopback-mac".into(),
                    process: json!(std::process::id()),
                    run: format!("slice-{}", job.generation),
                    deployment: "slice-development".into(),
                    endpoint: member.endpoint_id().into(),
                    key: member.public_key_record(),
                    generation: json!(job.generation),
                }],
                deadline,
            )?;
            round.accept(
                "b-member",
                &receipts::cleaned(&member, &source.subject, job.generation, "b-member")?,
                || (now(), true),
            )?;
            // This fixture owns no native resources. It proves typed composition,
            // not absence of resources in a native driver or physical peer.
            let proof = round.finish(|| Ok(()), || (now(), true))?;
            job.settle(proof, router::cleanup::Terminal::Cancelled, now())?;
            self.settled.send(())?;
            Ok(())
        })();
        result.map_err(|error| {
            eprintln!("controlled_settlement_failed:{error}");
            PublicError::new(503, "controlled_settlement_failed")
        })
    }
}
struct Release(Option<mpsc::SyncSender<()>>);
impl Release {
    fn now(&mut self) {
        self.0.take().unwrap().send(()).unwrap();
    }
}
impl Drop for Release {
    fn drop(&mut self) {
        if let Some(tx) = self.0.take()
            && tx.send(()).is_err()
        {
            eprintln!("signed_driver_already_exited");
        }
    }
}
#[test]
fn real_node_shutdown_verifies_only_after_typed_signed_session_settlement() {
    let (entered_tx, entered) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let (settled_tx, settled) = mpsc::sync_channel(1);
    let mut release = Release(Some(release_tx));
    let node = NodeService::new(
        Arc::new(SignedSession {
            entered: entered_tx,
            release: Mutex::new(release_rx),
            settled: settled_tx,
        }),
        "isolated-test-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    let host = Arc::new(DesktopServices::new(
        node.clone(),
        NonZeroUsize::new(2).unwrap(),
        &private(),
    ));
    let (core, mut cleanup) = adapt(host, NonZeroUsize::new(2).unwrap());
    cleanup.bind_clock(Instant::now()).unwrap();
    let request = Request {
        window: "main".into(),
        method: "POST".into(),
        uri: format!("{ORIGIN}/chat/api/send"),
        headers: vec![
            ("Content-Type".into(), "application/json".into()),
            ("X-Mycelium-Chat".into(), "1".into()),
        ],
        body: serde_json::to_vec(&json!({"prompt":"controlled session","max_new_tokens":1}))
            .unwrap(),
    };
    Channel::default()
        .authorize_native_document(&request, "mycelium://localhost/chat")
        .unwrap();
    let (reply_tx, reply) = mpsc::sync_channel(1);
    core.submit(
        request,
        Box::new(move |value| reply_tx.send(value).unwrap()),
    );
    assert_eq!(reply.recv_timeout(wait()).unwrap().status, 200);
    entered.recv_timeout(wait()).unwrap();
    let mut shutdown = Shutdown::new(cleanup, budget()).unwrap();
    shutdown.request(QuitSource::WebUi, Span::ZERO);
    assert_eq!(shutdown.tick(Span::ZERO), State::Stopping);
    assert!(!node.poll_shutdown().unwrap());
    assert!(!shutdown.can_exit());
    release.now();
    settled.recv_timeout(wait()).unwrap();
    let deadline = Instant::now() + wait();
    loop {
        match shutdown.tick(Span::ZERO) {
            State::Stopping => {
                assert!(Instant::now() < deadline);
                std::thread::yield_now();
            }
            state => {
                assert_eq!(state, State::Verified);
                break;
            }
        }
    }
    assert!(node.poll_shutdown().unwrap());
    assert!(shutdown.can_exit());
}
