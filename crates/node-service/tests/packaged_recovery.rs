// SPDX-License-Identifier: AGPL-3.0-only
//! B10 (lane B): v0.1.4's endpoint recovery on the packaged Macs (`endpoint_recovery.py` and the
//! routes `/api/network/restart`, `/api/network/recovery-card`, `/api/network/reconnect`, app.py:
//! 1427-1475 @ f4e366e9). An owner whose relay comes back on a new origin relocates its seed and
//! signs a recovery card; the joined Mac's reconnect with that card verifies the same seed at the
//! new address, re-pins its membership and resumes it, so it follows the relay without anyone
//! editing its state (`repin-b.sh`). Also lane C's review C-01: the user's Start clears
//! `recovery_error` only when the membership really resumed (app.py:1632-1636).
//!
//! The member reaches the owner's seed through a loopback forwarder to whichever port the seed
//! listens on now, as the relay's public origin always reaches the current seed in the product.
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use desktop::lifecycle::Poll;
use macs::{
    Mac, Owner, PUBLIC, approve_and_join, free_port, join_body, listening, member_lease, owner,
    test_network,
};
use node_service::packaged_network::NetworkConfig;
use serde_json::{Value, json};
use std::{
    net::{Shutdown, TcpListener, TcpStream},
    os::unix::fs::DirBuilderExt,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicU16, Ordering},
    },
};

const MOVED: &str = "https://v02-packaged-pair-moved.example.org";

/// A loopback TCP forwarder to a port that can change (the owner's current seed).
struct Forward {
    port: u16,
    to: Arc<AtomicU16>,
}
impl Forward {
    fn start(to: u16) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let to = Arc::new(AtomicU16::new(to));
        let target = to.clone();
        std::thread::spawn(move || {
            for client in listener.incoming().flatten() {
                let port = target.load(Ordering::SeqCst);
                std::thread::spawn(move || {
                    let Ok(server) = TcpStream::connect(("127.0.0.1", port)) else {
                        return;
                    };
                    let (Ok(mut back), Ok(mut from)) = (client.try_clone(), server.try_clone())
                    else {
                        return;
                    };
                    let up = std::thread::spawn(move || {
                        let (mut client, mut server) = (client, server);
                        std::io::copy(&mut client, &mut server).unwrap_or_default();
                        server.shutdown(Shutdown::Write).unwrap_or_default();
                    });
                    std::io::copy(&mut from, &mut back).unwrap_or_default();
                    back.shutdown(Shutdown::Write).unwrap_or_default();
                    up.join().unwrap_or_default();
                });
            }
        });
        Self { port, to }
    }
    fn retarget(&self, port: u16) {
        self.to.store(port, Ordering::SeqCst);
    }
}

fn seed_port(owner: &Owner) -> u16 {
    owner.seams.member_seed.connect_to.unwrap().port()
}

/// The joined Mac (its renewal on the owner-poll budget) over `forward`.
fn member_via(base: &Path, owner: &Owner, forward: &Forward) -> Mac {
    let state = base.join("member");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .recursive(true)
        .create(&state)
        .unwrap();
    let mut seams = owner.seams.clone();
    seams.member_seed.connect_to = Some(format!("127.0.0.1:{}", forward.port).parse().unwrap());
    Mac::launch(
        &state,
        NetworkConfig {
            seams,
            renewal: budgets::BudgetId::OwnerPoll,
            ..test_network()
        },
    )
}

fn joined(base: &Path, owner: &Owner, forward: &Forward) -> Mac {
    let member = member_via(base, owner, forward);
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    let joined = approve_and_join(owner, &member);
    assert_eq!(joined["membership_state"], "current", "{joined}");
    member
}

fn code(answer: &(u16, Value)) -> (bool, Value) {
    (answer.0 == 200, answer.1["error"].clone())
}

/// The owner restarts its relay on a new origin: the answer carries the signed card (v0.1.4
/// `relocate_authority`, endpoint_recovery.py:39-57) and `/api/network/recovery-card` reissues it
/// (:117-142). The joined Mac's reconnect with that card (:89-114, app.py:1427-1435) re-pins and
/// resumes its membership: it is current and running again with no `recovery_error`, the owner's
/// seed sees its renewals on the new origin, and the Mac relaunched later resumes there by itself.
#[test]
fn a_joined_mac_follows_the_owners_relay_to_a_new_origin_with_the_signed_card() {
    let base = macs::state_root("recovery");
    let owner = owner(&base);
    let forward = Forward::start(seed_port(&owner));
    let member = joined(&base, &owner, &forward);
    let before = listening();
    let proxy = free_port();
    let restarted = owner.mac.post(
        "/api/network/restart",
        json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
               "public_origin": MOVED, "proxy_port": proxy}),
    );
    assert_eq!(restarted.0, 200, "restart: {}", restarted.1);
    assert_eq!(
        restarted.1["network"]["public_origin"], MOVED,
        "{}",
        restarted.1
    );
    let card = restarted.1["recovery_card"].clone();
    let statement = &card["statement"];
    assert_eq!(
        (
            &statement["protocol"],
            &statement["previous_origin"],
            &statement["public_origin"]
        ),
        (
            &json!("mycelium.a13.endpoint_recovery.v1"),
            &json!(PUBLIC),
            &json!(MOVED)
        ),
        "{card}"
    );
    let moved: Vec<u16> = listening()
        .into_iter()
        .filter(|p| *p != proxy && !before.contains(p))
        .collect();
    assert_eq!(moved.len(), 1, "one relocated seed listener: {moved:?}");
    forward.retarget(moved[0]);
    let reissued = owner.mac.post(
        "/api/network/recovery-card",
        json!({"relay_acknowledged": true}),
    );
    assert_eq!(reissued.0, 200, "recovery card: {}", reissued.1);
    let again = &reissued.1["recovery_card"]["statement"];
    assert_eq!(
        (&again["previous_origin"], &again["public_origin"]),
        (&json!(PUBLIC), &json!(MOVED)),
        "{}",
        reissued.1
    );
    // Refusals: the role (app.py:1428-1430, 1437-1439) and a card that is not the owner's
    // (`endpoint_recovery_invalid`, a ValueError: v0.1.4 answers its generic public code).
    let body = |card: &Value| json!({"card": card, "relay_acknowledged": true});
    assert_eq!(
        code(&owner.mac.post("/api/network/reconnect", body(&card))),
        (false, json!("network_consent_required"))
    );
    assert_eq!(
        code(&member.post(
            "/api/network/recovery-card",
            json!({"relay_acknowledged": true})
        )),
        (false, json!("network_consent_required"))
    );
    let mut forged = card.clone();
    forged["statement"]["public_origin"] = json!("https://elsewhere.example.org");
    assert_eq!(
        code(&member.post("/api/network/reconnect", body(&forged))),
        (false, json!("action_not_completed"))
    );
    let reconnected = member.post("/api/network/reconnect", body(&card));
    assert_eq!(reconnected.0, 200, "reconnect: {}", reconnected.1);
    let status = member.status();
    assert_eq!(
        (
            &status["authority"]["membership_state"],
            &status["running"],
            &status["recovery_error"]
        ),
        (&json!("current"), &json!(true), &Value::Null),
        "{status}"
    );
    let lease = member_lease(&owner.mac).unwrap();
    owner.mac.until(
        "the owner's seed sees the member renew on the new origin",
        |o| json!(member_lease(o)),
        |v| v.as_f64().is_some_and(|at| at > lease),
    );
    assert_eq!(member.quit(), Ok(Poll::Verified));
    let member = member_via(&base, &owner, &forward);
    let relaunched = member.status();
    assert_eq!(
        (
            &relaunched["authority"]["membership_state"],
            &relaunched["running"],
            &relaunched["recovery_error"]
        ),
        (&json!("current"), &json!(true), &Value::Null),
        "{relaunched}"
    );
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// Lane C review C-01: a joined Mac whose launch-time resume failed (`recovery_error`, not
/// running) is started again by the user's Start only when its membership really resumes: a Start
/// while the seed is still unreachable is refused by name and keeps the error; one after it is
/// reachable resumes the membership and clears it (app.py:1632-1636, `authority.start()` then
/// `self.recovery_error = None`).
#[test]
fn the_users_start_clears_recovery_error_only_when_the_membership_resumed() {
    let base = macs::state_root("recovery-start");
    let owner = owner(&base);
    let forward = Forward::start(seed_port(&owner));
    let member = joined(&base, &owner, &forward);
    assert_eq!(member.quit(), Ok(Poll::Verified));
    forward.retarget(free_port());
    let member = member_via(&base, &owner, &forward);
    let failed = member.status();
    assert_eq!(
        (&failed["running"], &failed["recovery_error"]),
        (&json!(false), &json!("peer_transport_unreachable")),
        "{failed}"
    );
    let refused = member.post("/api/start", json!({}));
    assert_eq!(
        code(&refused),
        (false, json!("peer_transport_unreachable")),
        "{}",
        refused.1
    );
    let still = member.status();
    assert_eq!(
        (&still["running"], &still["recovery_error"]),
        (&json!(false), &json!("peer_transport_unreachable")),
        "{still}"
    );
    forward.retarget(seed_port(&owner));
    let started = member.post("/api/start", json!({}));
    assert_eq!(started.0, 200, "start: {}", started.1);
    let status = member.status();
    assert_eq!(
        (
            &status["authority"]["membership_state"],
            &status["running"],
            &status["recovery_error"]
        ),
        (&json!("current"), &json!(true), &Value::Null),
        "{status}"
    );
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
