// SPDX-License-Identifier: AGPL-3.0-only
// BACKLOG 8 (live v0.1.4 incident 2026-10-27 ~21:41), node-service half: a hung operation
// inside a request (here a receipt seal/write that never returns) must not stop the status
// publisher, health, admission, cancel, subscribe or shutdown paths. L6 proved the same for
// the Core lock (crates/node/tests/liveness.rs); this pins the host/request locks.
// Real threads, real signed receipt (as tests/cancel_ack.rs); only the seal callback hangs.
use api::{actions::Backend, requests::Service, schema::PublicError};
use budgets::BudgetId as B;
use node_service::{
    receipts,
    service::{Driver, Job, NodeService, duration},
};
use serde_json::{Value, json};
use std::{
    sync::{
        Arc, Mutex,
        mpsc::{self, Receiver, Sender},
    },
    time::Instant,
};

/// A named probe run concurrently with the hung seal.
type Operation = (&'static str, Box<dyn FnOnce() + Send>);

struct HungSeal {
    entered: Mutex<Option<Sender<()>>>,
    release: Mutex<Option<Receiver<()>>>,
    finished: Mutex<Option<Sender<()>>>,
}
impl Driver for HungSeal {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        let result = (|| -> node_service::loopback::Result<()> {
            let owner = receipts::identity("slice-owner")?;
            let member = receipts::identity("slice-member")?;
            let (mut source, session) = receipts::Receipts::open(
                &owner,
                (&member.public_key_record(), member.endpoint_id()),
                &job.id,
                job.generation,
                &job.document,
                std::time::Duration::ZERO,
                job.deadline
                    .saturating_duration_since(std::time::Instant::now()),
            )?;
            job.install(session)?;
            let envelope = source.sign(
                &owner,
                &[17],
                false,
                &protocol::canonical::digest(&json!("controlled-test"))?,
            )?;
            let entered = self.entered.lock().unwrap().take().unwrap();
            let release = self.release.lock().unwrap().take().unwrap();
            // The seal is caller I/O (production: a file write). It hangs until released,
            // bounded by the cleanup budget so a failing test cannot leak this thread.
            job.receipt(envelope, 0., move |_| {
                entered
                    .send(())
                    .map_err(|_| protocol::ContractError("seal_observer_gone"))?;
                // Released (sender dropped) and timed out both end the hang.
                match release.recv_timeout(duration(B::StageCleanup)) {
                    Ok(()) | Err(_) => Ok(()),
                }
            })?;
            Ok(())
        })();
        if let Some(done) = self.finished.lock().unwrap().take() {
            done.send(()).expect("test waits for the driver");
        }
        result.map_err(|_| PublicError::new(503, "hung_seal_driver_failed"))
    }
}
fn qualification() -> Value {
    let digest = protocol::canonical::digest(&json!("controlled-test")).unwrap();
    json!({"qualification_id":"controlled","qualification_digest":digest,"deployment_id":"slice-development","deployment_epoch":1,"topology_version":1,"model_id":"controlled","resolved_commit":"controlled","manifest_digest":digest,"path_manifest_digest":digest,"stage_load_proof_digests":[]})
}
fn document(q: &Value) -> Value {
    json!({"protocol":"mycelium.request_gateway.v1","prompt":"controlled","max_new_tokens":2,"qualification":q})
}
struct Rig {
    service: NodeService,
    request_id: String,
    token: String,
    release: Option<Sender<()>>,
    finished: Receiver<()>,
}
fn hung_rig() -> Rig {
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let (finished_tx, finished_rx) = mpsc::channel();
    let driver = HungSeal {
        entered: Mutex::new(Some(entered_tx)),
        release: Mutex::new(Some(release_rx)),
        finished: Mutex::new(Some(finished_tx)),
    };
    let q = qualification();
    let service = NodeService::new(Arc::new(driver), "bearer".into(), q.clone());
    let accepted = service.submit(document(&q)).unwrap();
    entered_rx
        .recv_timeout(duration(B::WorkerStartup))
        .expect("driver must reach the hung seal");
    Rig {
        service,
        request_id: accepted.request_id,
        token: accepted.session_token,
        release: Some(release_tx),
        finished: finished_rx,
    }
}
impl Rig {
    /// Release the hung seal and require the driver to finish: it was hung, not wedged.
    fn release(&mut self) {
        drop(self.release.take());
        self.finished
            .recv_timeout(duration(B::WorkerStartup))
            .expect("driver returns once its seal is released");
    }
}
impl Drop for Rig {
    fn drop(&mut self) {
        drop(self.release.take());
    }
}
/// Run every named operation concurrently; each must finish within one registry poll
/// while the seal is still hung. Returns the names that did not.
type Handles = Vec<(&'static str, std::thread::JoinHandle<()>)>;
fn starved(operations: Vec<Operation>) -> (Vec<&'static str>, Handles) {
    let (done_tx, done_rx) = mpsc::channel();
    let names: Vec<_> = operations.iter().map(|(n, _)| *n).collect();
    let mut handles = Vec::new();
    for (name, operation) in operations {
        let done = done_tx.clone();
        let handle = std::thread::Builder::new()
            .name(format!("liveness-{name}"))
            .spawn(move || {
                operation();
                // A starved operation completes after the probe window closed.
                if let Err(late) = done.send(name) {
                    eprintln!("late completion of {}", late.0);
                }
            })
            .unwrap();
        handles.push((name, handle));
    }
    drop(done_tx);
    let end = Instant::now() + duration(B::RegistryPoll);
    let mut finished = Vec::new();
    while let Some(left) = end.checked_duration_since(Instant::now()) {
        match done_rx.recv_timeout(left) {
            Ok(name) => finished.push(name),
            Err(_) => break,
        }
    }
    let stuck = names
        .into_iter()
        .filter(|n| !finished.contains(n))
        .collect();
    (stuck, handles)
}
/// After the seal is released every operation must have completed without panicking.
fn reap(handles: Handles) {
    for (name, handle) in handles {
        handle
            .join()
            .unwrap_or_else(|_| panic!("operation {name} panicked"));
    }
}
#[test]
fn hung_receipt_seal_never_starves_status_health_admission_cancel_or_subscribe() {
    let mut rig = hung_rig();
    let s = rig.service.clone();
    let (id, token) = (rig.request_id.clone(), rig.token.clone());
    let operations: Vec<Operation> = vec![
        ("health", {
            let s = s.clone();
            Box::new(move || {
                s.health().unwrap();
            })
        }),
        ("generation", {
            let s = s.clone();
            Box::new(move || {
                s.generation().unwrap();
            })
        }),
        ("status_publish_then_read", {
            let mut s = s.clone();
            Box::new(move || {
                s.publish().unwrap();
                s.published_status().unwrap();
            })
        }),
        ("second_submit_refused", {
            let s = s.clone();
            Box::new(move || {
                let q = qualification();
                let refused = s.submit(document(&q)).err().expect("one admitted request");
                assert_eq!(refused.code, "gateway_capacity_exhausted");
            })
        }),
        ("subscribe", {
            let (s, id, token) = (s.clone(), id.clone(), token.clone());
            Box::new(move || {
                s.subscribe(&id, None, Some(&token)).unwrap();
            })
        }),
        ("cancel", {
            let (s, id, token) = (s.clone(), id.clone(), token.clone());
            Box::new(move || {
                s.cancel(&id, Some(&token)).unwrap();
            })
        }),
    ];
    let (stuck, handles) = starved(operations);
    // Releasing before asserting lets a RED run end cleanly instead of leaking blocked threads.
    rig.release();
    reap(handles);
    assert!(
        stuck.is_empty(),
        "operations starved by a hung seal (request lock held across caller I/O): {stuck:?}"
    );
}
#[test]
fn hung_receipt_seal_never_blocks_shutdown_fencing() {
    let mut rig = hung_rig();
    let s = rig.service.clone();
    let (stuck, handles) = starved(vec![("request_shutdown", {
        let s = s.clone();
        Box::new(move || {
            s.request_shutdown().unwrap();
        })
    })]);
    rig.release();
    reap(handles);
    assert!(
        stuck.is_empty(),
        "shutdown fencing starved by a hung seal: {stuck:?}"
    );
}
