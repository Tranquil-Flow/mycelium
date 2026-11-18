// SPDX-License-Identifier: AGPL-3.0-only
//! Lane RW: a joined Mac's restart resume whose response was lost AFTER the seed committed it
//! (rv42 physical run, 18 Nov: B's first relaunch got a relay 502 on `/seed/resume`, every later
//! relaunch logged `member_resume_failed: membership_message_expired`).
//!
//! Parity pin, not a fix. v0.1.4 @ f4e366e9 does the same, executed (lane RW oracle probe on
//! `tests/a13_macos/test_peer.py`'s fixture, REPORT-laneRW-resume-wedge.md):
//! - the member replays its checkpointed request byte for byte (`peer.py:983-1003`);
//! - the seed answers a known request id with the acceptance it committed, before any other check
//!   (`mycelium_seed/coordinator.py:745-750`, `state.py:1788-1814`, no expiry, no prune);
//! - the member verifies that acceptance at its own `now` (`mycelium_node/membership.py:510-516`),
//!   so once the acceptance's 60 s `expires_at` has passed it refuses `membership_message_expired`,
//!   on every later start, forever.
//!
//! Inside the window the retry succeeds (the v0.1.4 test
//! `test_resume_response_loss_replays_exact_generation_then_next_restart_advances_once`).
//! Real seed (TLS, coordinator on an injected clock), real pairing and join; no sleeps.
use api::{app_host, handoff, joined_member, peer, seed::SeedOptions};
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
        let accepted = with_env(&time, &signer, &options, |env| {
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
        }
    }
    /// One launch of the joined Mac: `peer.start()` (the resume at app start), dialling `options`.
    fn start_over(&self, options: &SeedOptions) -> Result<Option<u64>, (String, bool)> {
        with_env(&self.time, &self.signer, options, |env| {
            peer::start(&self.member_root, &self.signer, true, env)
        })
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
    /// The first relaunch: the resume reaches the seed and commits there, its answer is lost on the
    /// way back (the relay's 502). The member is left with its checkpoint, the seed one generation
    /// ahead.
    fn lose_committed_resume(&mut self) -> Value {
        let upstream = self.server.as_ref().unwrap().address();
        let (proxy, forwarded) = lossy_proxy(upstream);
        let options = SeedOptions {
            connect_to: Some(proxy),
            ..self.options.clone()
        };
        let (code, started) = self.start_over(&options).unwrap_err();
        forwarded.join().unwrap();
        eprintln!("lost_resume_first_launch:code={code} started={started}");
        let pending = self.file("resume.pending.json").expect("resume checkpoint");
        assert_eq!((self.retained_generation(), self.seed_generation()), (1, 2));
        pending
    }
}
/// A TCP relay for exactly two seed connections (`/seed/identity`, then `/seed/resume`; the client
/// dials once per request, `Connection: close`). The first is relayed both ways. The second is
/// relayed until the member sends its first TLS application-data record (its TLS 1.3 Finished or,
/// in TLS 1.2, the request itself: the seed's whole handshake has reached the member by then);
/// everything the seed sends after that, the HTTP response included, is read and discarded, and
/// the member's socket is closed. The request reaches the seed whole: it commits, the answer is lost.
fn lossy_proxy(upstream: SocketAddr) -> (SocketAddr, JoinHandle<()>) {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let handle = std::thread::spawn(move || {
        for index in 0..2 {
            let (mut client, _) = listener.accept().unwrap();
            let mut server = TcpStream::connect(upstream).unwrap();
            let (mut from_client, mut to_server) =
                (client.try_clone().unwrap(), server.try_clone().unwrap());
            let application = Arc::new(AtomicBool::new(false));
            let seen = application.clone();
            let request = std::thread::spawn(move || {
                let mut header = [0u8; 5];
                while from_client.read_exact(&mut header).is_ok() {
                    let mut body =
                        vec![0u8; usize::from(u16::from_be_bytes([header[3], header[4]]))];
                    if from_client.read_exact(&mut body).is_err() {
                        break;
                    }
                    if header[0] == 23 {
                        seen.store(true, Ordering::SeqCst);
                    }
                    if to_server.write_all(&header).is_err() || to_server.write_all(&body).is_err()
                    {
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
                let lost = index == 1 && application.load(Ordering::SeqCst);
                if !lost && client.write_all(&chunk[..n]).is_err() {
                    break;
                }
            }
            client.shutdown(Shutdown::Both).unwrap_or(());
            request.join().unwrap();
        }
    });
    (address, handle)
}
fn with_env<R>(
    time: &Time,
    signer: &Identity,
    options: &SeedOptions,
    f: impl FnOnce(&peer::JoinEnv) -> R,
) -> R {
    let random = |n| app_host::hex(&app_host::random(n).unwrap());
    let clock = || time.now();
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

/// The rv42 wedge, as v0.1.4: retried after the committed acceptance's `expires_at`, the member
/// refuses the replayed acceptance itself (`membership_message_expired`, `member.rs` accept_resume ->
/// `protocol::membership::verify`), not started, checkpoint kept unchanged; waiting longer (past the
/// seed-side lease of generation 2 too) does not change the answer. The packaged launch only logs it
/// (`member_resume_failed`): it is not a recovery code, so the Mac is not running and cannot pair.
#[test]
fn lost_resume_response_retried_after_the_window_wedges_as_v014() {
    let mut rig = Rig::new();
    let pending = rig.lose_committed_resume();
    let ttl = seconds(B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37);
    for wait in [ttl + 1, 600, 3600] {
        rig.time.advance(wait);
        assert_eq!(
            rig.start().unwrap_err(),
            ("membership_message_expired".to_owned(), false)
        );
        assert_eq!(rig.file("resume.pending.json"), Some(pending.clone()));
        assert_eq!((rig.retained_generation(), rig.seed_generation()), (1, 2));
        assert!(rig.file("membership.disposition.json").is_none());
        // Which side refuses: the seed answers the replayed request (2xx) with the acceptance it
        // committed for it, whose own `expires_at` has passed; the member refuses that answer.
        let replayed = rig
            .coordinator
            .accept_resume(&pending["resume_envelope"])
            .unwrap();
        let expires_at = replayed["message"]["expires_at"].as_f64().unwrap();
        assert!(expires_at < rig.time.now(), "{expires_at}");
        assert_eq!(replayed["message"]["membership_generation"], json!(2));
    }
    assert_eq!(
        joined_member::recovery_code("membership_message_expired"),
        None
    );
}
