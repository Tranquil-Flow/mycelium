// SPDX-License-Identifier: AGPL-3.0-only
//! Listing custody under faults: directory I/O never holds the owner's actions; crash residue
//! of key/state creation is recovered when nothing was ever listed and kept when something was;
//! each listing is signed by its own key. Real loopback directories and sockets throughout.
//! Crash residue is injected as the exact files an interrupted creation leaves behind.
#[path = "support/swarm_publisher.rs"]
mod support;
use api::swarm::publisher::{
    DirectoryClient, DirectoryPort, Listing, OwnerState, Phase, SwarmPublisher,
    app::{AppPublisher, DB_FILE, KEY_FILE, OwnerFacts},
    heartbeat_seconds,
    host::Stop,
};
use budgets::{BudgetId, Entries};
use ed25519_dalek::SigningKey;
use serde_json::{Map, Value, json};
use std::{
    fs,
    io::Write,
    net::{TcpListener, TcpStream},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use support::{Scratch, Server, set_clock};

const NOW: i64 = 1_800_000_000;
const MODEL: &str = "Qwen/Qwen3-0.6B";
const ENTRY: &str = "https://entry.example.org";

fn span(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// An owner action answers within two owner-poll periods.
fn prompt() -> Duration {
    span(BudgetId::OwnerPoll) * 2
}
fn patience() -> Duration {
    span(BudgetId::ModelSelection) * 4
}
fn body(directory: &str) -> Map<String, Value> {
    json!({"directory": directory, "name": "Custody swarm", "access_mode": "request",
           "description": "Public text"})
    .as_object()
    .unwrap()
    .clone()
}
fn facts(state: &str, route_ready: bool) -> OwnerFacts {
    let run = json!({"protocol": "mycelium.owner_run.v1", "state": state, "route_ready": route_ready,
                     "registry_origin": if route_ready { json!("http://127.0.0.1:9") } else { Value::Null },
                     "participants": [{"node_id": "a"}]});
    OwnerFacts::from_owner_run(true, Some(&run), Some(MODEL), Some(ENTRY))
}
fn serving() -> OwnerFacts {
    facts("serving", true)
}
fn stopped() -> OwnerFacts {
    facts("stopped", false)
}
fn rows(origin: &str) -> Vec<Value> {
    DirectoryClient::new(origin)
        .unwrap()
        .call("GET", "/swarms", b"")
        .unwrap()["swarms"]
        .as_array()
        .unwrap()
        .clone()
}
fn publisher_files(root: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(root)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with("swarm-publisher"))
        .collect();
    names.sort();
    names
}
fn state_dir(root: &Scratch) -> PathBuf {
    let state = root.0.join("state");
    fs::create_dir(&state).unwrap();
    state
}

/// A loopback directory that accepts and never answers: the client's own registered deadline
/// ends each exchange. Every accepted connection is reported, so a test acts while one waits.
struct Silent {
    origin: String,
    accepted: mpsc::Receiver<()>,
    stop: Arc<AtomicBool>,
    join: Option<JoinHandle<()>>,
}
impl Silent {
    fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let (send, accepted) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        let join = thread::spawn(move || {
            let mut held = Vec::new();
            for socket in listener.incoming() {
                let socket = socket.unwrap();
                if stopping.load(Ordering::SeqCst) {
                    break;
                }
                held.push(socket);
                if send.send(()).is_err() {
                    eprintln!("silent_directory_observer_gone");
                }
            }
        });
        Self {
            origin,
            accepted,
            stop,
            join: Some(join),
        }
    }
}
impl Drop for Silent {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        TcpStream::connect(self.origin.strip_prefix("http://").unwrap()).unwrap();
        self.join.take().unwrap().join().unwrap();
    }
}

#[test]
fn owner_actions_answer_while_a_tick_waits_on_a_silent_directory() {
    let root = Scratch::new();
    let state = state_dir(&root);
    let silent = Silent::start();
    let p = Arc::new(AppPublisher::new(&state));
    p.publish(true, &body(&silent.origin)).unwrap();
    let ticking = {
        let p = p.clone();
        thread::spawn(move || p.tick(NOW, &serving()))
    };
    silent
        .accepted
        .recv_timeout(patience())
        .expect("the tick is inside a directory exchange");
    let started = Instant::now();
    let out = p.unpublish(true, &Map::new()).unwrap();
    let unpublish = started.elapsed();
    let started = Instant::now();
    let back = p.publish(true, &body(&silent.origin)).unwrap();
    let publish = started.elapsed();
    let started = Instant::now();
    let status = p.status_json();
    let read = started.elapsed();
    assert!(
        unpublish < prompt() && publish < prompt() && read < prompt(),
        "owner actions waited for the directory: unpublish {unpublish:?}, publish {publish:?}, status {read:?}"
    );
    assert_eq!(out["enabled"], false);
    assert_eq!(back["enabled"], true);
    assert_eq!(status["enabled"], true);
    // The record was durable before it was sent, so the revoked listing owes a withdrawal; the
    // failed exchange does not defer it, because the owner changed the preference meanwhile.
    ticking.join().unwrap().unwrap();
    let after = p.status_json();
    assert_eq!(after["phase"], "withdraw_pending", "{after}");
    assert_eq!(after["next_attempt_at"], 0, "{after}");
}

#[derive(Clone, Copy, Debug)]
enum Residue {
    /// A B12 key file created (create_new) and never written.
    EmptyKey,
    /// A written key, then a state file created and never given its schema.
    KeyAndEmptyState,
    /// Only the state file, created and never given its schema.
    EmptyState,
    /// A torn atomic creation: the temporary state file (and its journal) never renamed.
    TornTemporary,
}
fn private_file(path: &Path, bytes: &[u8]) {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .unwrap();
    file.write_all(bytes).unwrap();
    file.sync_all().unwrap();
}
fn inject(state: &Path, residue: Residue) {
    match residue {
        Residue::EmptyKey => private_file(&state.join(KEY_FILE), b""),
        Residue::KeyAndEmptyState => {
            private_file(&state.join(KEY_FILE), &[5; 32]);
            private_file(&state.join(DB_FILE), b"");
        }
        Residue::EmptyState => private_file(&state.join(DB_FILE), b""),
        Residue::TornTemporary => {
            private_file(
                &state.join(format!("{DB_FILE}.tmp")),
                b"SQLite format 3\0torn",
            );
            private_file(&state.join(format!("{DB_FILE}.tmp-journal")), b"torn");
        }
    }
}

#[test]
fn crash_residue_from_before_any_listing_is_recovered_by_the_next_publish() {
    let mut refused = Vec::new();
    for residue in [
        Residue::EmptyKey,
        Residue::KeyAndEmptyState,
        Residue::EmptyState,
        Residue::TornTemporary,
    ] {
        let root = Scratch::new();
        let state = state_dir(&root);
        set_clock(&root.0, NOW);
        let server = Server::start(&root.0, false);
        inject(&state, residue);
        let p = AppPublisher::new(&state);
        // Residue owes nothing: startup has nothing to resume and the opt-in is not refused.
        match p.resume() {
            Ok(resumed) => assert!(!resumed, "{residue:?}"),
            Err(e) => refused.push(format!("{residue:?}: resume refused {}", e.code)),
        }
        let pending = match p.publish(true, &body(&server.origin)) {
            Ok(pending) => pending,
            Err(e) => {
                refused.push(format!("{residue:?}: publish refused {}", e.code));
                continue;
            }
        };
        assert_eq!(pending["phase"], "pending", "{residue:?}");
        // The recovered custody lists and withdraws like a fresh one.
        let listed = p.tick(NOW, &serving()).unwrap().unwrap();
        assert_eq!(listed.phase, Phase::Listed, "{residue:?}");
        assert_eq!(rows(&server.origin).len(), 1, "{residue:?}");
        p.tick(NOW, &stopped()).unwrap();
        assert!(rows(&server.origin).is_empty(), "{residue:?}");
        let left = publisher_files(&state);
        if left != [DB_FILE] {
            refused.push(format!("{residue:?}: residue left behind {left:?}"));
        }
    }
    assert!(
        refused.is_empty(),
        "crash residue was not recovered: {refused:#?}"
    );
}

/// A B12-layout listing: the installation key file plus a state file bound to it, listed.
fn b12_listing(state: &Path, origin: &str) {
    let seed = [6; 32];
    private_file(&state.join(KEY_FILE), &seed);
    let port = Arc::new(DirectoryClient::new(origin).unwrap());
    let mut legacy = SwarmPublisher::open(
        &state.join(DB_FILE),
        SigningKey::from_bytes(&seed),
        port,
        origin,
    )
    .unwrap();
    legacy
        .set_listing(Some(Listing {
            name: "Custody swarm".into(),
            description: "Public text".into(),
            access_mode: "request".into(),
            open_quota: None,
        }))
        .unwrap();
    let listed = legacy
        .reconcile(
            NOW,
            &OwnerState::Serving {
                model: MODEL.into(),
                members: 1,
                entry_origin: ENTRY.into(),
            },
        )
        .unwrap();
    assert_eq!(listed.phase, Phase::Listed);
}

#[test]
fn a_listing_whose_key_is_unreadable_is_kept_and_later_withdrawn_with_that_key() {
    let root = Scratch::new();
    let state = state_dir(&root);
    set_clock(&root.0, NOW);
    let server = Server::start(&root.0, false);
    b12_listing(&state, &server.origin);
    assert_eq!(rows(&server.origin).len(), 1);
    // The key file is lost (truncated) while the listing is live: never discarded or replaced.
    let key = state.join(KEY_FILE);
    let seed = fs::read(&key).unwrap();
    fs::write(&key, b"").unwrap();
    let saved = fs::read(state.join(DB_FILE)).unwrap();
    let p = AppPublisher::new(&state);
    assert_eq!(p.resume().unwrap_err().code, "swarm_publisher_key_invalid");
    assert_eq!(
        p.publish(true, &body(&server.origin)).unwrap_err().code,
        "swarm_publisher_key_invalid"
    );
    assert_eq!(fs::read(state.join(DB_FILE)).unwrap(), saved);
    assert!(key.exists());
    assert_eq!(rows(&server.origin).len(), 1);
    drop(p);
    // With its key back, the retained listing is withdrawn by the key that listed it.
    fs::write(&key, &seed).unwrap();
    let p = AppPublisher::new(&state);
    assert!(p.resume().unwrap());
    assert!(!key.exists(), "the B12 key moved into the state file");
    p.tick(NOW, &stopped()).unwrap();
    assert!(rows(&server.origin).is_empty());
    assert_eq!(publisher_files(&state), [DB_FILE]);
}

fn owner_key(row: &Value) -> String {
    row["record"]["owner_key"].as_str().unwrap().to_owned()
}
fn each_listing_has_its_own_key(python: bool) {
    let root = Scratch::new();
    let state = state_dir(&root);
    set_clock(&root.0, NOW);
    let mut server = Server::start(&root.0, python);
    let p = AppPublisher::new(&state);
    p.publish(true, &body(&server.origin)).unwrap();
    p.tick(NOW, &serving()).unwrap();
    let first = rows(&server.origin).remove(0);
    assert_eq!(first["record"]["entry"]["key"], owner_key(&first));
    p.tick(NOW, &stopped()).unwrap();
    assert!(rows(&server.origin).is_empty());
    p.tick(NOW, &serving()).unwrap();
    let second = rows(&server.origin).remove(0);
    assert_ne!(
        owner_key(&second),
        owner_key(&first),
        "listings are linkable"
    );
    // The withdrawn listing's key is gone from local state.
    for name in publisher_files(&state) {
        let bytes = fs::read(state.join(&name)).unwrap();
        let text = String::from_utf8_lossy(&bytes);
        assert!(
            !text.contains(&owner_key(&first)),
            "{name} kept the old key"
        );
    }
    assert_ne!(second["record"]["swarm_id"], first["record"]["swarm_id"]);
    assert_eq!(second["record"]["entry"]["key"], owner_key(&second));
    // The key lives only in the private state file (0600); there is no other key file.
    assert_eq!(publisher_files(&state), [DB_FILE]);
    let mode = fs::metadata(state.join(DB_FILE))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);
    // Its debt keeps its own key across a restart: quit offline, then retry after the restart.
    server.stop();
    let quit = p.stop(NOW, Stop::Quit).unwrap().unwrap();
    assert_eq!(quit.phase, Phase::WithdrawPending);
    drop(p);
    let later = NOW + heartbeat_seconds().unwrap();
    set_clock(&root.0, later);
    server.restart();
    assert_eq!(owner_key(&rows(&server.origin)[0]), owner_key(&second));
    let p = AppPublisher::new(&state);
    assert!(p.resume().unwrap());
    p.tick(later, &stopped()).unwrap();
    assert!(rows(&server.origin).is_empty());
    assert_eq!(p.status_json()["phase"], "pending");
}

#[test]
fn rust_authority_each_listing_has_its_own_key() {
    each_listing_has_its_own_key(false);
}
#[test]
#[ignore = "requires the deployed Python directory copy + cryptography interpreter; explicit loopback qualification"]
fn deployed_python_directory_each_listing_has_its_own_key() {
    each_listing_has_its_own_key(true);
}
