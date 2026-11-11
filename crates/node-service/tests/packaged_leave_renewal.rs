// SPDX-License-Identifier: AGPL-3.0-only
//! A member's leave and a lease renewal pass on the wire (B3F item 8; the flaky
//! `packaged_two_host` "a member that left still renews its lease"). v0.1.4 @ f4e366e9:
//! `RemoteMacMember.renew` holds the peer's lock across its seed round trip (peer.py:1037-1060)
//! and the leave's `disconnect` sets `_left`, then calls `peer.set_contribution_enabled` under
//! that same lock (member_leave.py:237-252), so the leave returns only after a pass that was on
//! the wire committed, and `_renew_loop` starts no pass once `_left` is set (app.py:918). Once
//! `/api/leave` answered, the member's seed lease therefore never moves again.
//!
//! The member reaches the owner's seed through a loopback TCP forwarder of this test that can
//! hold the next connection (a renewal pass) before it reaches the seed: the pass is on the wire,
//! the leave is asked for, then the pass is let through.
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use budgets::BudgetId as B;
use desktop::lifecycle::Poll;
use macs::{
    Mac, approve_and_join, join_body, member_lease, millis, owner, state_root, test_network,
};
use node_service::packaged_network::NetworkConfig;
use serde_json::json;
use std::{
    io::{Read, Write},
    net::{Shutdown, SocketAddr, TcpListener, TcpStream},
    os::unix::fs::DirBuilderExt,
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread::JoinHandle,
    time::Instant,
};

/// The member's renewal runs on this budget instead of v0.1.4's 20 s.
const RENEWAL: B = B::OwnerPoll;

#[derive(Default)]
struct Hold {
    armed: bool,
    taken: bool,
    released: bool,
}
#[derive(Debug, PartialEq)]
enum Event {
    /// The held connection arrived (a renewal pass is on the wire).
    Held,
    /// The held connection ended both ways (the seed answered it).
    Closed,
}
/// A loopback TCP forwarder to the owner's seed that can hold one connection.
struct SeedForwarder {
    addr: SocketAddr,
    hold: Arc<(Mutex<Hold>, Condvar)>,
    stop: Arc<AtomicBool>,
    accept: Option<JoinHandle<()>>,
}
fn pipe(mut from: TcpStream, mut to: TcpStream) {
    let mut buf = [0u8; 16 * 1024];
    while let Ok(n) = from.read(&mut buf) {
        if n == 0 || to.write_all(&buf[..n]).is_err() {
            break;
        }
    }
    to.shutdown(Shutdown::Write).unwrap_or(());
}
impl SeedForwarder {
    fn start(upstream: SocketAddr, events: mpsc::Sender<Event>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let hold: Arc<(Mutex<Hold>, Condvar)> = Arc::default();
        let stop = Arc::new(AtomicBool::new(false));
        let (accept_hold, accept_stop) = (hold.clone(), stop.clone());
        let accept = std::thread::spawn(move || {
            let mut connections: Vec<JoinHandle<()>> = Vec::new();
            for client in listener.incoming() {
                if accept_stop.load(Ordering::SeqCst) {
                    break;
                }
                let Ok(client) = client else { continue };
                let (hold, events) = (accept_hold.clone(), events.clone());
                connections.push(std::thread::spawn(move || {
                    let held = {
                        let mut h = hold.0.lock().unwrap();
                        let held = h.armed && !h.taken;
                        h.taken |= held;
                        held
                    };
                    if held {
                        events.send(Event::Held).unwrap_or(());
                        let h = hold.0.lock().unwrap();
                        let (h, _) = hold
                            .1
                            .wait_timeout_while(h, millis(B::WorkerStartup), |h| !h.released)
                            .unwrap();
                        drop(h);
                    }
                    let Ok(server) = TcpStream::connect(upstream) else {
                        return;
                    };
                    let (client_in, server_out) =
                        (client.try_clone().unwrap(), server.try_clone().unwrap());
                    let up = std::thread::spawn(move || pipe(client_in, server_out));
                    pipe(server, client);
                    up.join().unwrap();
                    if held {
                        events.send(Event::Closed).unwrap_or(());
                    }
                }));
            }
            for connection in connections {
                connection.join().unwrap();
            }
        });
        Self {
            addr,
            hold,
            stop,
            accept: Some(accept),
        }
    }
    fn arm(&self) {
        self.hold.0.lock().unwrap().armed = true;
    }
    fn release(&self) {
        self.hold.0.lock().unwrap().released = true;
        self.hold.1.notify_all();
    }
}
impl Drop for SeedForwarder {
    fn drop(&mut self) {
        self.release();
        self.stop.store(true, Ordering::SeqCst);
        TcpStream::connect(self.addr).map(drop).unwrap_or(());
        if let Some(accept) = self.accept.take() {
            accept.join().unwrap();
        }
    }
}

/// A renewal pass is on the wire when the member leaves: `/api/leave` answers only once that
/// pass is over, and the member's seed lease never moves after the leave answered.
#[test]
fn a_renewal_pass_on_the_wire_never_extends_the_lease_after_leave() {
    let base = state_root("leave-renewal");
    let owner = owner(&base);
    let (events, seen) = mpsc::channel();
    let forwarder = SeedForwarder::start(owner.seams.member_seed.connect_to.unwrap(), events);
    let mut seams = owner.seams.clone();
    seams.member_seed.connect_to = Some(forwarder.addr);
    let state = base.join("member");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    let member = Mac::launch(
        &state,
        NetworkConfig {
            seams,
            renewal: RENEWAL,
            ..test_network()
        },
    );
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    approve_and_join(&owner, &member);
    // The member renews through the forwarder.
    let first = member_lease(&owner.mac).unwrap();
    let deadline = Instant::now() + millis(B::WorkerStartup);
    while member_lease(&owner.mac).unwrap() <= first {
        assert!(
            Instant::now() < deadline,
            "the member's lease was not renewed"
        );
        std::thread::sleep(millis(RENEWAL));
    }
    // The next pass is held on the wire.
    forwarder.arm();
    assert_eq!(
        seen.recv_timeout(millis(B::WorkerStartup)).unwrap(),
        Event::Held
    );
    let (answer, answered) = mpsc::channel();
    let (left, early_lease) = std::thread::scope(|scope| {
        let leave = scope.spawn(|| {
            let left = member.post("/api/leave", json!({"mode": "instant", "confirm": true}));
            answer.send(left).unwrap_or(());
        });
        // A leave that answers while the pass is still held: the lease it leaves behind.
        let early = answered.recv_timeout(millis(RENEWAL) * 4).ok();
        let early_lease = early.as_ref().map(|_| member_lease(&owner.mac).unwrap());
        forwarder.release();
        assert_eq!(
            seen.recv_timeout(millis(B::WorkerStartup)).unwrap(),
            Event::Closed
        );
        let left = early
            .or_else(|| answered.recv_timeout(millis(B::WorkerStartup)).ok())
            .expect("the leave answered");
        leave.join().unwrap();
        (left, early_lease)
    });
    let stopped = early_lease.unwrap_or_else(|| member_lease(&owner.mac).unwrap());
    // The held pass is over and `_left` is set: nothing renews any more.
    let now = member_lease(&owner.mac).unwrap();
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(forwarder);
    std::fs::remove_dir_all(&base).unwrap_or_default();
    assert_eq!(left.0, 200, "leave: {}", left.1);
    assert_eq!(left.1["state"], "left", "{}", left.1);
    assert_eq!(
        now,
        stopped,
        "the member's lease moved after its leave answered (a pass on the wire committed later; \
         the leave answered while it was held: {})",
        early_lease.is_some()
    );
}
