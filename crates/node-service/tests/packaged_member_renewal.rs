// SPDX-License-Identifier: AGPL-3.0-only
//! A joined packaged Mac's lease renewal behaves as the SHIPPED v0.1.4 app's `_renew_loop`
//! (app.py:911-927 with `RemoteMacMember.renew`, peer.py:1037-1060 @ f4e366e9; REVIEW-laneGR
//! 'Member renewal'): a pass that fails retries at the next pass, never stops the loop and never
//! fences a member whose own lease is still valid; only a definitive refusal records a terminal
//! disposition (`_translate`, peer.py:299-323). Not the CLI node's `reconnect.py` transient rule.
//!
//! The failing seed answers are injected on the member's own seed path: a loopback TCP forwarder
//! between the member and the owner's seed (TLS stays end to end) that refuses connections while
//! it is told to, then forwards again (`support/packaged_macs.rs` for the two Macs).
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use budgets::BudgetId as B;
use desktop::lifecycle::Poll;
use macs::{Mac, approve_and_join, join_body, member_lease, millis, owner, state_root};
use node_service::packaged_network::NetworkConfig;
use std::{
    net::{SocketAddr, TcpListener, TcpStream},
    os::unix::fs::DirBuilderExt,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    thread::JoinHandle,
    time::Instant,
};

const RENEWAL: B = B::OwnerPoll;

/// A loopback TCP forwarder to `target` that drops every connection while `refuse` is set.
struct Forwarder {
    address: SocketAddr,
    refuse: Arc<AtomicBool>,
    refused: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    accept: Option<JoinHandle<Vec<JoinHandle<()>>>>,
}
impl Forwarder {
    fn start(target: SocketAddr) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = listener.local_addr().unwrap();
        let (refuse, refused, stop) = (
            Arc::new(AtomicBool::new(false)),
            Arc::new(AtomicUsize::new(0)),
            Arc::new(AtomicBool::new(false)),
        );
        let (r, n, s) = (refuse.clone(), refused.clone(), stop.clone());
        let accept = std::thread::spawn(move || {
            let mut pumps = Vec::new();
            while !s.load(Ordering::SeqCst) {
                let Ok((inbound, _)) = listener.accept() else {
                    std::thread::sleep(millis(RENEWAL) / 10);
                    continue;
                };
                if r.load(Ordering::SeqCst) {
                    n.fetch_add(1, Ordering::SeqCst);
                    drop(inbound);
                    continue;
                }
                inbound.set_nonblocking(false).unwrap();
                let Ok(outbound) = TcpStream::connect(target) else {
                    continue;
                };
                for (mut from, mut to) in [
                    (inbound.try_clone().unwrap(), outbound.try_clone().unwrap()),
                    (outbound, inbound),
                ] {
                    pumps.push(std::thread::spawn(move || {
                        std::io::copy(&mut from, &mut to).unwrap_or_default();
                        to.shutdown(std::net::Shutdown::Write).unwrap_or_default();
                    }));
                }
            }
            pumps
        });
        Self {
            address,
            refuse,
            refused,
            stop,
            accept: Some(accept),
        }
    }
}
impl Drop for Forwarder {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(accept) = self.accept.take() {
            // The pumps end with their connections (the Macs quit first).
            drop(accept.join().unwrap_or_default());
        }
    }
}

/// (1) Renewals that fail while the member's own lease is still valid are retried at the next pass:
/// the member stays `current` (no disposition, no fence), and once its seed answers again its lease
/// moves on, with no relaunch or re-join.
#[test]
fn packaged_member_renewal_survives_failed_passes_while_its_lease_is_valid() {
    let base = state_root("renew-retry");
    let owner = owner(&base);
    let forwarder = Forwarder::start(owner.seams.member_seed.connect_to.unwrap());
    let mut seams = owner.seams.clone();
    seams.member_seed.connect_to = Some(forwarder.address);
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
            ..NetworkConfig::default()
        },
    );
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    let joined = approve_and_join(&owner, &member);
    assert_eq!(joined["membership_state"], "current", "{joined}");
    // The member's seed path fails: several renewal passes in a row cannot reach it.
    forwarder.refuse.store(true, Ordering::SeqCst);
    let deadline = Instant::now() + millis(B::WorkerStartup);
    let refused = |passes: usize| {
        while forwarder.refused.load(Ordering::SeqCst) < passes {
            assert!(
                Instant::now() < deadline,
                "no renewal attempt reached the seed path"
            );
            std::thread::sleep(millis(RENEWAL));
        }
    };
    // A pass whose connection the forwarder accepted just before it began refusing still lands.
    // The passes are sequential, so that one is over when the first refused pass is counted:
    // the lease read then is the one the three further failing passes must leave alone.
    refused(1);
    let failing = member_lease(&owner.mac).unwrap();
    refused(4);
    assert_eq!(
        member_lease(&owner.mac),
        Some(failing),
        "a renewal got through"
    );
    let during = member.status();
    assert_eq!(
        during["authority"]["membership_state"], "current",
        "{during}"
    );
    assert_eq!(during["authority"]["lease_state"], "fresh", "{during}");
    // The seed answers again: the very same loop renews the lease.
    forwarder.refuse.store(false, Ordering::SeqCst);
    let deadline = Instant::now() + millis(B::WorkerStartup);
    loop {
        let now = member_lease(&owner.mac).unwrap();
        if now > failing {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "the loop did not renew once the seed answered again: {failing} -> {now}"
        );
        std::thread::sleep(millis(RENEWAL));
    }
    assert_eq!(member.status()["authority"]["membership_state"], "current");
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(forwarder);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
