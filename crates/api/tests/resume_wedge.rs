// SPDX-License-Identifier: AGPL-3.0-only
//! Lane RW: a joined Mac's restart resume whose response was lost AFTER the seed committed it
//! (rv42 physical run, 18 Nov: B's first relaunch got a relay 502 on `/seed/resume`, every later
//! relaunch logged `member_resume_failed: membership_message_expired`).
//!
//! v0.1.4 @ f4e366e9 wedges here, executed (lane RW oracle probe on `tests/a13_macos/test_peer.py`'s
//! fixture, REPORT-laneRW-resume-wedge.md): the member replays its checkpointed request byte for
//! byte (`peer.py:896-930`, `:983-1003`), the seed answers a known request id with the acceptance it
//! committed, before any other check (`mycelium_seed/coordinator.py:745-750`, never pruned), and
//! the member refuses that acceptance once its 60 s `expires_at` has passed
//! (`mycelium_node/membership.py:510-516`), on every later start, forever.
//!
//! Deliberate departure (owner ruling, 18 Nov 2026; member side only, no wire or seed change): a
//! checkpointed request that has expired, or whose replayed acceptance comes back expired, is
//! replaced by a FRESH request (new message id, current time) under the same incarnation, restart
//! count and previous generation, checkpointed before it is sent, at most once per start. The
//! seed's `already_advanced` branch re-issues the generation it committed (no double advance).
//! A still-valid checkpoint is still replayed unchanged, and any other refusal still fails.
//!
//! Real seed (TLS, coordinator on an injected clock), real pairing and join; no sleeps.
use api::{app_host, handoff, peer, seed::SeedOptions};
use budgets::{BudgetId as B, Entries};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::{Ipv4Addr, Shutdown, SocketAddr, TcpListener, TcpStream},
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread::JoinHandle,
};

static NEXT: AtomicU64 = AtomicU64::new(0);
const SWARM: &str = "rw-resume-wedge";

/// The one clock every party reads (milliseconds since the epoch).
#[derive(Clone)]
struct Time(Arc<AtomicU64>);
impl Time {
    fn start() -> Self {
        Self(Arc::new(AtomicU64::new(
            (app_host::now() * 1000.0).floor() as u64
        )))
    }
    fn now(&self) -> f64 {
        self.0.load(Ordering::SeqCst) as f64 / 1000.0
    }
    fn advance(&self, seconds: u64) {
        self.0.fetch_add(seconds * 1000, Ordering::SeqCst);
    }
}
fn seconds(id: B) -> u64 {
    id.span(Entries::ZERO).unwrap().millis() / 1000
}

/// Owner seed + one joined member, all on `time`.
struct Rig {
    root: PathBuf,
    member_root: PathBuf,
    time: Time,
    coordinator: Arc<seed::Coordinator>,
    server: Option<seed::server::Server>,
    options: SeedOptions,
    signer: Identity,
    node_id: String,
    /// How far the joined Mac's clock runs ahead of the seed's (milliseconds).
    member_ahead: u64,
}
impl Rig {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "rw-wedge-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&root).unwrap();
        let root = fs::canonicalize(root).unwrap();
        let owner = root.join("owner");
        let member_root = root.join("member");
        for dir in [&owner, &member_root] {
            fs::create_dir(dir).unwrap();
            fs::set_permissions(dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        }
        let time = Time::start();
        let material = seed::tls::ensure_local_tls_material(&owner.join("tls")).unwrap();
        let seed_key = [41u8; 32];
        let seed_signer = Identity::from_secret("endpoint-rw-seed", &seed_key).unwrap();
        let options = seed::Options::new(SWARM, "seed-rw", "boot-rw").unwrap();
        let ids = AtomicU64::new(1);
        let clock = time.clone();
        let coordinator = Arc::new(
            seed::Coordinator::open(
                &owner.join("seed.sqlite"),
                seed_signer,
                options,
                Box::new(move || clock.now()),
                Box::new(move || {
                    format!(
                        "{:08x}-0000-4000-8000-{:012x}",
                        ids.fetch_add(1, Ordering::SeqCst),
                        41
                    )
                }),
            )
            .unwrap(),
        );
        let server = seed::server::Server::bind_tls(coordinator.clone(), &material, None).unwrap();
        let options = SeedOptions {
            ca_pem: Some(fs::read(&material.ca_certificate).unwrap()),
            connect_to: Some(server.address()),
            server_name: Some("127.0.0.1".into()),
        };
        let handoff_coordinator = handoff::Coordinator {
            signer: Identity::from_secret("endpoint-rw-seed", &seed_key).unwrap(),
            swarm_id: SWARM.into(),
            seed_url: server.base_url().to_owned(),
        };
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let ttl = seconds(B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37);
        let quota = json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false});
        let handoff = handoff::approve_pairing(
            &owner,
            true,
            Some(&handoff_coordinator),
            &request.to_value(),
            &json!(ttl),
            &quota,
            time.now(),
            &handoff::Entropy::os,
        )
        .unwrap();
        let accepted = with_env(&time, 0, &signer, &options, |env| {
            peer::accept_handoff(&member_root, true, &handoff.to_value(), env).unwrap()
        });
        let node_id = accepted.session.unwrap().node_id().to_owned();
        Self {
            root,
            member_root,
            time,
            coordinator,
            server: Some(server),
            options,
            signer,
            node_id,
            member_ahead: 0,
        }
    }
    /// One launch of the joined Mac: `peer.start()` (the resume at app start), dialling `options`.
    fn start_over(&self, options: &SeedOptions) -> Result<Option<u64>, (String, bool)> {
        with_env(
            &self.time,
            self.member_ahead,
            &self.signer,
            options,
            |env| peer::start(&self.member_root, &self.signer, true, env),
        )
        .map(|s| s.session.and_then(|s| s.generation()))
        .map_err(|f| (f.error.code.to_owned(), f.started))
    }
    fn start(&self) -> Result<Option<u64>, (String, bool)> {
        self.start_over(&self.options)
    }
    fn file(&self, name: &str) -> Option<Value> {
        fs::read(self.member_root.join(name))
            .ok()
            .map(|b| serde_json::from_slice(&b).unwrap())
    }
    fn retained_generation(&self) -> u64 {
        self.file("membership.state.json").unwrap()["membership_generation"]
            .as_u64()
            .unwrap()
    }
    fn seed_generation(&self) -> u64 {
        self.coordinator.member(&self.node_id).unwrap()["generation"]
            .as_u64()
            .unwrap()
    }
    fn seed_incarnation(&self) -> Value {
        self.coordinator.member(&self.node_id).unwrap()["incarnation"].clone()
    }
    /// One launch through a counting relay; `lose` drops the seed's answer to `/seed/resume`.
    /// Returns the outcome and how many seed requests the launch made.
    fn start_relayed(&self, lose: bool) -> (Result<Option<u64>, (String, bool)>, usize) {
        let relay = Relay::new(self.server.as_ref().unwrap().address(), lose);
        let options = SeedOptions {
            connect_to: Some(relay.address),
            ..self.options.clone()
        };
        let outcome = self.start_over(&options);
        (outcome, relay.finish())
    }
    /// The first relaunch: the resume reaches the seed and commits there, its answer is lost on the
    /// way back (the relay's 502). The member is left with its checkpoint, the seed one generation
    /// ahead.
    fn lose_committed_resume(&mut self) -> Value {
        let (outcome, requests) = self.start_relayed(true);
        let (code, started) = outcome.unwrap_err();
        eprintln!("lost_resume_first_launch:code={code} started={started}");
        assert_eq!(requests, 2);
        let pending = self.file("resume.pending.json").expect("resume checkpoint");
        assert_eq!((self.retained_generation(), self.seed_generation()), (1, 2));
        pending
    }
}
/// A TCP relay to the seed, one connection at a time (the client dials once per request,
/// `Connection: close`), counting the connections it relayed. With `lose`, every `/seed/resume`
/// connection (any after the first, `/seed/identity`) is relayed until the member sends its first
/// TLS application-data record (its TLS 1.3 Finished or, in TLS 1.2, the request itself: the seed's
/// whole handshake has reached the member by then); everything the seed sends after that, the HTTP
/// response included, is read and discarded, and the member's socket is closed. The request
/// reaches the seed whole: it commits, the answer is lost.
struct Relay {
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    handle: JoinHandle<usize>,
}
impl Relay {
    fn new(upstream: SocketAddr, lose: bool) -> Self {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let handle = std::thread::spawn(move || {
            let mut index = 0;
            loop {
                let (client, _) = listener.accept().unwrap();
                if stopped.load(Ordering::SeqCst) {
                    return index;
                }
                relay_one(client, upstream, lose && index > 0);
                index += 1;
            }
        });
        Self {
            address,
            stop,
            handle,
        }
    }
    /// Stop accepting and return how many connections were relayed.
    fn finish(self) -> usize {
        self.stop.store(true, Ordering::SeqCst);
        drop(TcpStream::connect(self.address).unwrap());
        self.handle.join().unwrap()
    }
}
fn relay_one(mut client: TcpStream, upstream: SocketAddr, lose: bool) {
    let mut server = TcpStream::connect(upstream).unwrap();
    let (mut from_client, mut to_server) =
        (client.try_clone().unwrap(), server.try_clone().unwrap());
    let application = Arc::new(AtomicBool::new(false));
    let seen = application.clone();
    let request = std::thread::spawn(move || {
        let mut header = [0u8; 5];
        while from_client.read_exact(&mut header).is_ok() {
            let mut body = vec![0u8; usize::from(u16::from_be_bytes([header[3], header[4]]))];
            if from_client.read_exact(&mut body).is_err() {
                break;
            }
            if header[0] == 23 {
                seen.store(true, Ordering::SeqCst);
            }
            if to_server.write_all(&header).is_err() || to_server.write_all(&body).is_err() {
                break;
            }
        }
        to_server.shutdown(Shutdown::Write).unwrap_or(());
    });
    let mut chunk = [0u8; 16 * 1024];
    loop {
        let n = server.read(&mut chunk).unwrap_or(0);
        if n == 0 {
            break;
        }
        let lost = lose && application.load(Ordering::SeqCst);
        if !lost && client.write_all(&chunk[..n]).is_err() {
            break;
        }
    }
    client.shutdown(Shutdown::Both).unwrap_or(());
    request.join().unwrap();
}
fn with_env<R>(
    time: &Time,
    ahead_ms: u64,
    signer: &Identity,
    options: &SeedOptions,
    f: impl FnOnce(&peer::JoinEnv) -> R,
) -> R {
    let random = |n| app_host::hex(&app_host::random(n).unwrap());
    let clock = || time.now() + ahead_ms as f64 / 1000.0;
    let env = peer::JoinEnv {
        signer,
        clock: &clock,
        random_hex: &random,
        seed: options,
    };
    f(&env)
}
impl Drop for Rig {
    fn drop(&mut self) {
        drop(self.server.take());
        fs::remove_dir_all(&self.root).unwrap_or(());
    }
}

fn message(pending: &Value, key: &str) -> Value {
    pending["resume_envelope"]["message"][key].clone()
}
/// The fields a regenerated request must keep from the lost one.
fn kept(pending: &Value) -> Value {
    json!([
        pending["incarnation"],
        pending["restart_count"],
        pending["previous_generation"],
        pending["previous_incarnation"],
        message(pending, "incarnation"),
        message(pending, "generation"),
        message(pending, "previous_incarnation"),
    ])
}

/// Control (v0.1.4 `test_resume_response_loss_...`): a retry inside the acceptance's window
/// replays the committed acceptance and advances exactly once; the next restart advances again.
#[test]
fn lost_resume_response_retried_inside_the_window_resumes_once() {
    let mut rig = Rig::new();
    rig.lose_committed_resume();
    rig.time.advance(30);
    assert_eq!(rig.start().unwrap(), Some(2));
    assert_eq!((rig.retained_generation(), rig.seed_generation()), (2, 2));
    assert!(rig.file("resume.pending.json").is_none());
    assert_eq!(rig.start().unwrap(), Some(3));
}

/// Idempotency kept: a checkpoint still inside its window is replayed byte for byte, not
/// re-signed. A second lost answer leaves the checkpoint unchanged (it is written before sending)
/// and the seed does not advance again; the next retry, still inside, resumes at generation 2.
#[test]
fn still_valid_checkpoint_is_replayed_unchanged() {
    let mut rig = Rig::new();
    let pending = rig.lose_committed_resume();
    rig.time.advance(30);
    let (outcome, requests) = rig.start_relayed(true);
    assert!(!outcome.unwrap_err().1);
    assert_eq!(requests, 2);
    assert_eq!(rig.file("resume.pending.json"), Some(pending.clone()));
    assert_eq!((rig.retained_generation(), rig.seed_generation()), (1, 2));
    rig.time.advance(20);
    let (outcome, requests) = rig.start_relayed(false);
    assert_eq!((outcome.unwrap(), requests), (Some(2), 2));
    assert_eq!((rig.retained_generation(), rig.seed_generation()), (2, 2));
    assert_eq!(rig.seed_incarnation(), pending["incarnation"]);
    assert_eq!(rig.start().unwrap(), Some(3));
}

/// The rv42 wedge, fixed (owner ruling, departs from v0.1.4): retried after the committed
/// acceptance's `expires_at` (+61 s, +600 s, past the seed-side lease of generation 2 at +3600 s),
/// the member re-signs ONE fresh request under the same incarnation and restart count, the seed
/// re-issues generation 2 without advancing again, and the next normal start advances once more.
#[test]
fn lost_resume_response_retried_after_the_window_resumes_with_a_fresh_request() {
    let ttl = seconds(B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37);
    for wait in [ttl + 1, 600, 3600] {
        let mut rig = Rig::new();
        let pending = rig.lose_committed_resume();
        rig.time.advance(wait);
        let (outcome, requests) = rig.start_relayed(false);
        assert_eq!(outcome, Ok(Some(2)), "+{wait} s");
        assert_eq!(requests, 2, "identity + one resume, +{wait} s");
        assert_eq!((rig.retained_generation(), rig.seed_generation()), (2, 2));
        assert!(rig.file("resume.pending.json").is_none());
        assert!(rig.file("membership.disposition.json").is_none());
        let retained = rig.file("membership.state.json").unwrap();
        assert_eq!(retained["incarnation"], pending["incarnation"]);
        assert_eq!(retained["restart_count"], pending["restart_count"]);
        assert_eq!(rig.seed_incarnation(), pending["incarnation"]);
        assert_eq!(rig.start().unwrap(), Some(3));
        assert_eq!((rig.retained_generation(), rig.seed_generation()), (3, 3));
    }
}

/// The fresh request is checkpointed before it is sent: when its answer is lost too, the
/// checkpoint holds it (new message id, current time, same incarnation / restart count / previous
/// generation), the seed has not advanced twice, and a retry inside ITS window replays it.
#[test]
fn regenerated_request_is_checkpointed_before_it_is_sent() {
    let mut rig = Rig::new();
    let pending = rig.lose_committed_resume();
    rig.time.advance(61);
    let (outcome, requests) = rig.start_relayed(true);
    assert!(!outcome.unwrap_err().1);
    assert_eq!(requests, 2);
    let fresh = rig.file("resume.pending.json").expect("fresh checkpoint");
    assert_ne!(fresh["resume_message_id"], pending["resume_message_id"]);
    assert_eq!(message(&fresh, "message_id"), fresh["resume_message_id"]);
    assert_eq!(message(&fresh, "issued_at"), json!(rig.time.now()));
    assert_eq!(kept(&fresh), kept(&pending));
    assert_eq!((rig.retained_generation(), rig.seed_generation()), (1, 2));
    assert_eq!(rig.seed_incarnation(), pending["incarnation"]);
    rig.time.advance(30);
    let (outcome, requests) = rig.start_relayed(false);
    assert_eq!((outcome.unwrap(), requests), (Some(2), 2));
    assert_eq!((rig.retained_generation(), rig.seed_generation()), (2, 2));
    assert_eq!(rig.start().unwrap(), Some(3));
}

/// The other trigger: the checkpoint is still inside its own window by the member's clock, but
/// the seed's stored acceptance (stamped by the seed's clock, 20 s behind, inside the 30 s skew)
/// comes back expired. The member re-signs once and resumes at generation 2.
#[test]
fn replayed_acceptance_that_comes_back_expired_is_re_requested_once() {
    let mut rig = Rig::new();
    rig.member_ahead = 20_000;
    let pending = rig.lose_committed_resume();
    rig.time.advance(50);
    let (outcome, requests) = rig.start_relayed(false);
    assert_eq!(outcome, Ok(Some(2)));
    assert_eq!(requests, 3, "identity, the replay, one fresh resume");
    assert_eq!((rig.retained_generation(), rig.seed_generation()), (2, 2));
    assert_eq!(rig.seed_incarnation(), pending["incarnation"]);
    assert!(rig.file("resume.pending.json").is_none());
    assert_eq!(rig.start().unwrap(), Some(3));
}

/// A genuinely different refusal is not retried: the owner revoked the member while its answer was
/// lost. The expired checkpoint is re-signed once, the seed refuses it (`peer_member_revoked`, a
/// terminal disposition), one resume request per start, never a loop, and the next start fails too.
#[test]
fn revoked_member_with_an_expired_checkpoint_fails_without_looping() {
    let mut rig = Rig::new();
    let pending = rig.lose_committed_resume();
    rig.coordinator
        .advance_member_generation(&rig.node_id, 2, "STOPPED")
        .unwrap();
    let revoked = rig.seed_generation();
    rig.time.advance(61);
    let (outcome, requests) = rig.start_relayed(false);
    assert_eq!(
        outcome.unwrap_err(),
        ("peer_member_revoked".to_owned(), false)
    );
    assert_eq!(requests, 2, "identity + one resume");
    assert_eq!(rig.seed_generation(), revoked);
    assert_eq!(rig.retained_generation(), 1);
    let disposition = rig.file("membership.disposition.json").unwrap();
    assert_eq!(disposition["membership_state"], "revoked");
    if let Some(fresh) = rig.file("resume.pending.json") {
        assert_eq!(kept(&fresh), kept(&pending));
    }
    rig.time.advance(1);
    let (outcome, requests) = rig.start_relayed(false);
    assert!(outcome.is_err(), "{outcome:?}");
    assert!(requests <= 2, "{requests}");
    assert_eq!(rig.seed_generation(), revoked);
}
