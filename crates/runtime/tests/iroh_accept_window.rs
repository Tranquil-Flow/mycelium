// SPDX-License-Identifier: AGPL-3.0-only
//! `LoopbackHost::accept` windows (REVIEW-laneLAN-r2 R2-1). Callers accept in short windows
//! (`OwnerPoll` slices: the member's control loop, the route setup, the shard source) and call
//! again when one ends. A handshake in flight when a window ends, or a connection that completes
//! as it ends, belongs to the next window: dropping it made the dialer's one call fail fast
//! (`control_connect_failed` / `control_open_failed` / `control_read_failed`, then the two-host
//! tests' intermittent `owner_target_cleanup_unproven`). Both modes: LocalOnly loopback, and
//! direct LAN on an explicitly allowed 127.0.0.1 (as the runtime LAN suites), so no host
//! interface is needed. No sleeps: the handshake is parked by a relay that holds the server's
//! answers, and the late completion is a clock read after the window.
#![cfg(all(feature = "iroh-adapter", unix))]
#[path = "support/udp_hold.rs"]
mod udp_hold;

use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{EndpointAddr, SecretKey};
use runtime::iroh_host::{LanPolicy, LoopbackHost};
use std::{
    net::{IpAddr, SocketAddr},
    sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use transport::{Channel, Clock, Error};
use udp_hold::Hold;

/// A monotonic clock that jumps by `jump` milliseconds from its `jump_on`-th read on.
struct Stepping {
    base: Instant,
    reads: AtomicUsize,
    jump_on: usize,
    jump: u64,
}
impl Stepping {
    fn steady() -> Self {
        Self::jumping(usize::MAX, 0)
    }
    fn jumping(jump_on: usize, jump: u64) -> Self {
        Self {
            base: Instant::now(),
            reads: AtomicUsize::new(0),
            jump_on,
            jump,
        }
    }
    fn reads(&self) -> usize {
        self.reads.load(Ordering::SeqCst)
    }
}
impl Clock for Stepping {
    fn monotonic(&self) -> MonoInstant {
        let read = self.reads.fetch_add(1, Ordering::SeqCst);
        let elapsed = u64::try_from(self.base.elapsed().as_millis()).unwrap_or(u64::MAX);
        let extra = if read >= self.jump_on { self.jump } else { 0 };
        MonoInstant::new(ClockDomain(2), elapsed.saturating_add(extra))
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the accept window must not consult wall time")
    }
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap()
}
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn deadline(clock: &impl Clock, id: B) -> MonoInstant {
    clock
        .monotonic()
        .checked_add(id.span(Entries::ZERO).unwrap())
        .unwrap()
}
fn loopback() -> IpAddr {
    IpAddr::from([127, 0, 0, 1])
}
#[derive(Clone, Copy, Debug)]
enum Mode {
    LocalOnly,
    DirectLan,
}
fn server<'a>(rt: &'a tokio::runtime::Runtime, clock: &impl Clock, mode: Mode) -> LoopbackHost<'a> {
    let until = deadline(clock, B::RouterClose);
    match mode {
        Mode::LocalOnly => LoopbackHost::bind_protocols(
            rt,
            clock,
            until,
            SecretKey::generate(),
            &[Channel::Router],
        ),
        Mode::DirectLan => LoopbackHost::bind_lan(
            rt,
            clock,
            until,
            SecretKey::generate(),
            &[Channel::Router],
            loopback(),
            LanPolicy::with_explicit(vec![loopback()]).unwrap(),
        ),
    }
    .unwrap()
}
fn socket_of(host: &LoopbackHost<'_>) -> (iroh::EndpointId, SocketAddr) {
    let address = host.address().unwrap();
    (address.id, *address.ip_addrs().next().unwrap())
}
/// A dial in flight: the client's key, its outcome, `done` (the connection stays open until it
/// is dropped) and the client thread.
type Dial = (
    iroh::EndpointId,
    mpsc::Receiver<Result<(), Error>>,
    mpsc::Sender<()>,
    std::thread::JoinHandle<()>,
);
/// Dial `id` at `at` from a fresh LocalOnly client on its own thread.
fn dial(id: iroh::EndpointId, at: SocketAddr) -> Dial {
    let secret = SecretKey::generate();
    let key = secret.public();
    let (outcome_tx, outcome) = mpsc::channel();
    let (done, finished) = mpsc::channel::<()>();
    let client = std::thread::spawn(move || {
        let rt = runtime();
        let clock = Stepping::steady();
        let mut host =
            LoopbackHost::bind_with_secret(&rt, &clock, deadline(&clock, B::RouterClose), secret)
                .unwrap();
        let connected = host.connect(
            &EndpointAddr::new(id).with_ip_addr(at),
            &clock,
            deadline(&clock, B::RouterClose),
        );
        let ended = connected.as_ref().map(|c| c.close_reason()).ok().flatten();
        outcome_tx
            .send(match (&connected, ended) {
                (Ok(_), None) => Ok(()),
                (Ok(_), Some(_)) => Err(Error::Cancelled),
                (Err(e), _) => Err(*e),
            })
            .unwrap();
        // Hold the connection until the test has looked at the server side.
        assert!(
            finished.recv().is_err(),
            "no message is ever sent on `done`"
        );
        drop(connected);
        host.close(&clock, deadline(&clock, B::RouterClose))
            .unwrap();
    });
    (key, outcome, done, client)
}

/// A handshake parked across the end of an accept window is returned by the next window.
fn a_parked_handshake_survives_the_window(mode: Mode) {
    let rt = runtime();
    let clock = Stepping::steady();
    let mut host = server(&rt, &clock, mode);
    let (id, at) = socket_of(&host);
    let hold = Hold::start(at, span(B::SidecarReceivePollWait));
    let (client, outcome, done, dialer) = dial(id, hold.address());
    // Windows end while the server's answers are held, until one of them pulled the client's
    // Initial (the server answered). Only a window that pulled nothing is retried; nothing can
    // complete while held.
    let mut windows = 0;
    loop {
        windows += 1;
        assert!(
            windows <= 8,
            "{mode:?}: the server never pulled the client's Initial"
        );
        let window = host.accept(&clock, deadline(&clock, B::OwnerPoll));
        assert_eq!(
            window.err(),
            Some(Error::Expired),
            "{mode:?}: nothing can complete while the server's answers are held"
        );
        if hold.answered(span(B::OwnerPoll)) {
            break;
        }
    }
    hold.release();
    let next = host.accept(&clock, deadline(&clock, B::SidecarLocalHandshake));
    let dialed = outcome.recv_timeout(span(B::RouterClose));
    drop(done);
    dialer.join().unwrap();
    host.close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    assert_eq!(
        dialed,
        Ok(Ok(())),
        "{mode:?}: the dial whose handshake straddled a window failed"
    );
    let accepted = next.unwrap_or_else(|e| {
        panic!(
            "{mode:?}: the next window lost the handshake in flight at the end of the last: {e:?}"
        )
    });
    assert_eq!(accepted.remote_id(), client);
}

/// Proves: R2-1 (LocalOnly: a handshake in flight when an accept window ends is carried over
/// and returned by the next accept, not dropped; the dialer's call succeeds).
#[test]
fn on_loopback_a_handshake_in_flight_at_the_end_of_a_window_is_accepted_by_the_next() {
    a_parked_handshake_survives_the_window(Mode::LocalOnly);
}

/// Proves: R2-1 (direct LAN: the same carry-over, which `accept_lan` already had).
#[test]
fn on_the_lan_a_handshake_in_flight_at_the_end_of_a_window_is_accepted_by_the_next() {
    a_parked_handshake_survives_the_window(Mode::DirectLan);
}

/// A connection that completes inside the wait, read back after the window's deadline (the
/// thread was descheduled past the window edge), is handed to the next window, not closed.
fn a_connection_completed_at_the_edge_survives(mode: Mode) {
    let rt = runtime();
    let mut host = server(&rt, &Stepping::steady(), mode);
    // The window's clock. Read 0: its deadline; 1: the wait; 2: the check after the handshake,
    // which sees the window already over (the jump), as a descheduled thread would.
    let late = u64::try_from(span(B::SidecarLocalHandshake).as_millis()).unwrap() * 2;
    let clock = Stepping::jumping(2, late);
    let (id, at) = socket_of(&host);
    let (client, outcome, done, dialer) = dial(id, at);
    let edge = host.accept(&clock, deadline(&clock, B::SidecarLocalHandshake));
    let reads = clock.reads();
    let next = host.accept(&clock, deadline(&clock, B::SidecarLocalHandshake));
    let dialed = outcome.recv_timeout(span(B::RouterClose));
    let still_open = next
        .as_ref()
        .map(|c| c.close_reason().is_none())
        .map_err(|e| *e);
    drop(done);
    dialer.join().unwrap();
    let steady = Stepping::steady();
    host.close(&steady, deadline(&steady, B::RouterClose))
        .unwrap();
    assert_eq!(
        reads, 3,
        "{mode:?}: the handshake did not complete inside the window, so the late path was not taken"
    );
    assert_eq!(
        edge.err(),
        Some(Error::Expired),
        "{mode:?}: the caller still sees its window end"
    );
    assert_eq!(dialed, Ok(Ok(())), "{mode:?}");
    let accepted = next.unwrap_or_else(|e| {
        panic!(
            "{mode:?}: a connection completed at the window's edge was closed, not handed on: {e:?}"
        )
    });
    assert_eq!(accepted.remote_id(), client);
    assert_eq!(still_open, Ok(true), "{mode:?}: handed on, but closed");
}

/// Proves: R2-1 second path (LocalOnly: a connection that completed as its window ended is
/// returned by the next accept instead of being closed as late).
#[test]
fn on_loopback_a_connection_completed_at_the_window_edge_is_handed_to_the_next_window() {
    a_connection_completed_at_the_edge_survives(Mode::LocalOnly);
}

/// Proves: R2-1 second path (direct LAN: the same; this path was shared by both modes).
#[test]
fn on_the_lan_a_connection_completed_at_the_window_edge_is_handed_to_the_next_window() {
    a_connection_completed_at_the_edge_survives(Mode::DirectLan);
}
