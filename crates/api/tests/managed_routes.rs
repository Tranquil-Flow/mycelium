// SPDX-License-Identifier: AGPL-3.0-only
//! The member-facing managed-control routes on the owner's real seed HTTPS server.
//!
//! v0.1.4 `LocalTLSSeedServer` (`tls.py:433-444 @ f4e366e9`) admits at most 16 concurrent
//! `/managed-control/*` requests (`BoundedSemaphore(16)`, non-blocking acquire) and refuses the
//! next one with `managed_control_capacity_exhausted` before it reaches the mailbox. A LAN or
//! relay flood therefore cannot pile up behind the mailbox's authentication guard, which the
//! seed's own member liveness shares.
use api::{
    app_host::{Unmanaged, start_seed},
    managed_routes::TARGET_ROUTES,
    seed::SeedClient,
};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::{Arc, Condvar, Mutex, mpsc},
    thread,
    time::Duration,
};

fn secs(n: u64) -> Duration {
    Duration::from_secs(n)
}

struct Root(PathBuf);
impl Drop for Root {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap_or_default();
    }
}

/// A mailbox dispatcher that HOLDS the first `hold` requests until released and answers every
/// later one at once; it counts every request it was handed.
#[derive(Default)]
struct Gate {
    state: Mutex<(usize, bool)>,
    changed: Condvar,
}
impl Gate {
    fn enter(&self, hold: usize) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.0 += 1;
        self.changed.notify_all();
        if state.0 <= hold {
            // A test that fails early releases the gate on drop; the bound only ends a stray hold.
            drop(self.changed.wait_timeout_while(state, secs(60), |s| !s.1));
        }
    }
    fn wait_entered(&self, n: usize) {
        let state = self.state.lock().unwrap();
        let (state, waited) = self
            .changed
            .wait_timeout_while(state, secs(30), |s| s.0 < n)
            .unwrap();
        assert!(
            !waited.timed_out(),
            "only {} of {n} requests arrived",
            state.0
        );
    }
    fn entered(&self) -> usize {
        self.state.lock().unwrap().0
    }
    fn release(&self) {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .1 = true;
        self.changed.notify_all();
    }
}

/// Releases every held request when the test ends, passed or failed.
struct Release(Arc<Gate>);
impl Drop for Release {
    fn drop(&mut self) {
        self.0.release();
    }
}

fn client(seed_url: &str, options: &api::seed::SeedOptions) -> SeedClient {
    // `post` leaves the answer unverified (the caller checks the owner's signature), so any
    // well-formed pin will do here.
    SeedClient::new(
        seed_url,
        "swarm-capacity",
        "pin",
        &[json!({"verification_key_digest": "pin"})],
        options,
    )
    .unwrap()
}

fn poll_body() -> (&'static str, Value) {
    let (path, protocol) = TARGET_ROUTES[0];
    (path, json!({"statement": {"protocol": protocol}}))
}

#[test]
fn a_seventeenth_concurrent_managed_control_request_is_refused_before_the_mailbox() {
    const HELD: usize = 16;
    let root = Root(std::env::temp_dir().join(format!(
        "b21-managed-capacity-{}-{}",
        std::process::id(),
        line!()
    )));
    std::fs::create_dir_all(&root.0).unwrap();
    // The seed's invite registry refuses a path below a symlink (`/var` -> `/private/var`).
    let root = Root(std::fs::canonicalize(&root.0).unwrap());
    let seed = start_seed::<Unmanaged>(&root.0, "swarm-capacity", None).unwrap();
    let gate = Arc::new(Gate::default());
    let _release = Release(gate.clone());
    {
        let gate = gate.clone();
        seed.mailbox_routes.bind(Arc::new(move |_raw: &[u8]| {
            gate.enter(HELD);
            Ok(br#"{"answered":true}"#.to_vec())
        }));
    }
    let options = seed.options();
    let (done_tx, done_rx) = mpsc::channel();
    let mut held = Vec::new();
    for _ in 0..HELD {
        let (client, done) = (client(&seed.seed_url, &options), done_tx.clone());
        held.push(thread::spawn(move || {
            let (path, body) = poll_body();
            // The receiver is gone only when the test already failed.
            drop(done.send(client.post(path, &body).map_err(|e| e.code)));
        }));
    }
    gate.wait_entered(HELD);

    // Every slot is held: the next request is refused by the route, never handed to the mailbox.
    let (path, body) = poll_body();
    let refused = client(&seed.seed_url, &options).post(path, &body);
    assert_eq!(
        refused.as_ref().map_err(|e| e.code.as_str()),
        Err("managed_control_capacity_exhausted"),
        "a 17th concurrent managed-control request: {refused:?}"
    );
    // Ruling 16: the capacity refusal answers 503 as v0.1.4 (tls.py:441).
    assert_eq!(
        refused.as_ref().err().and_then(|e| e.status),
        Some(503),
        "{refused:?}"
    );
    assert_eq!(
        gate.entered(),
        HELD,
        "the refused request reached the mailbox"
    );

    // Released slots are reusable: the held requests answer and the route admits again.
    gate.release();
    for _ in 0..HELD {
        let answer = done_rx.recv_timeout(secs(30)).unwrap();
        assert_eq!(answer, Ok(json!({"answered": true})));
    }
    for worker in held {
        worker.join().unwrap();
    }
    for _ in 0..HELD + 1 {
        assert_eq!(
            client(&seed.seed_url, &options)
                .post(path, &body)
                .map_err(|e| e.code),
            Ok(json!({"answered": true}))
        );
    }
    assert_eq!(gate.entered(), 2 * HELD + 1);
    seed.close();
}
