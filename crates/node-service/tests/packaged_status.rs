// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged `/api/status` feeds the unchanged v0.1.4 page what its JavaScript reads, as the
//! SHIPPED app's `DemoApp.status` (app.py:1244-1283 @ f4e366e9) publishes it (lane PH3b S1):
//! - `run_model` with `resources.memory_cap_options` (run_model.py:116-131 ->
//!   model_resources.py:605-631, :430-438), read by the model picker (run.js:53-66) and the member's
//!   `#standing-memory` choices (app.js:481-484); the join's caps check reads the same RAM
//!   (standing_consent.py:96-111 via `_device_memory_bytes`, app.py:1123-1126);
//! - `authority.members` rows of node_id / generation / lifecycle_state / clock only (:1253-1258),
//!   read by the member list (app.js:252-260);
//! - `renewal_error` (`_renew_loop`, :917-927) for a joined member's lease;
//! - `join_flow` of a joined Mac with `standing_error`, `standing_consent.last` and `last_report`
//!   (:1229-1236), read by `#standing-state` (app.js:527);
//! - `swarm` with every key `swarm.js` reads (swarm_publish.py:456-472; REVIEW-lanePH2 F7);
//! - and the overlay never panics on a document that is not an object (REVIEW-lanePH2 F8).
//!
//! Two (or three) packaged hosts in this one process (`support/packaged_macs.rs`); the device RAM is
//! v0.1.4's own test seam, given to the composition.
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use budgets::BudgetId as B;
use desktop::lifecycle::Poll;
use macs::{Mac, STORAGE, approve_and_join, consent, owner, private, state_root};
use node_service::packaged_network::{Network, NetworkConfig};
use serde_json::{Value, json};
use std::{
    net::{SocketAddr, TcpListener, TcpStream},
    os::unix::fs::DirBuilderExt,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::JoinHandle,
};

const GIB: u64 = 1 << 30;

fn launch(base: &Path, name: &str, config: NetworkConfig) -> Mac {
    let state = base.join(name);
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    Mac::launch(&state, config)
}
/// The page's join body (app.js:577-583) with the memory and wall caps the member picked.
fn join_with(member: &Mac, link: &str, memory: u64, wall: u64) -> (u16, Value) {
    let digest = member.status()["join_flow"]["standing_disclosure"]["digest"].clone();
    member.post(
        "/api/join/window",
        json!({
            "consent": consent(STORAGE),
            "standing_consent": {"acknowledged": true, "disclosure_digest": digest,
                "caps": {"storage_bytes": STORAGE, "max_memory_bytes": memory,
                         "max_wall_seconds": wall, "max_commands": 5000}},
            "join": link,
        }),
    )
}
fn u64s(value: &Value) -> Vec<u64> {
    value
        .as_array()
        .unwrap_or_else(|| panic!("not a list: {value}"))
        .iter()
        .map(|v| v.as_u64().unwrap())
        .collect()
}

/// A Mac's status offers v0.1.4's per-run memory choices for ITS RAM (1/2, 5/8 and 3/4 of it in
/// 256 MiB steps, never above 3/4): 8 / 10 / 12 GiB on a 16 GiB Mac, 2 / 2.5 / 3 GiB on a 4 GiB Mac.
/// The member picks the smallest offered (2 GiB) and the page's 10 minutes: the join saves those
/// caps and binds them to the owner's swarm; the page's own HTML default (4 GB, index.html:24) is
/// above this Mac's 3/4 and refused by name before anything changes. After the join the member's
/// status shows its join report and no standing or renewal error; the owner's member rows carry
/// exactly the four keys v0.1.4 keeps.
#[test]
fn the_packaged_status_feeds_the_consent_form_and_the_join_binds_the_caps_chosen_from_it() {
    let base = state_root("status-consent");
    let big = launch(
        &base,
        "sixteen",
        NetworkConfig {
            device_memory_bytes: Some(16 * GIB),
            ..NetworkConfig::default()
        },
    );
    let shown = big.status();
    let resources = &shown["run_model"]["resources"];
    assert_eq!(resources["device_memory_bytes"], 16 * GIB, "{shown}");
    assert_eq!(
        u64s(&resources["memory_cap_options"]),
        [8 * GIB, 10 * GIB, 12 * GIB],
        "{resources}"
    );
    assert_eq!(
        resources["memory_cap_ceiling_bytes"],
        12 * GIB,
        "{resources}"
    );
    assert!(shown["run_model"]["choices"].is_array(), "{shown}");
    assert_eq!(big.quit(), Ok(Poll::Verified));

    let owner = owner(&base);
    let member = launch(
        &base,
        "member",
        NetworkConfig {
            seams: owner.seams.clone(),
            device_memory_bytes: Some(4 * GIB),
            ..NetworkConfig::default()
        },
    );
    let before = member.status();
    let options = u64s(&before["run_model"]["resources"]["memory_cap_options"]);
    assert_eq!(options, [2 * GIB, 2 * GIB + GIB / 2, 3 * GIB], "{before}");
    let refused = join_with(&member, &owner.link, 4 * GIB, 600);
    assert_eq!(
        (refused.0, refused.1["error"].clone()),
        (400, json!("standing_consent_memory_exceeds_device")),
        "{}",
        refused.1
    );
    assert_eq!(member.status()["installation_role"], "owner");
    let waiting = join_with(&member, &owner.link, options[0], 600);
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    let joined = approve_and_join(&owner, &member);
    assert_eq!(joined["membership_state"], "current", "{joined}");

    let after = member.status();
    let flow = &after["join_flow"];
    let standing = &flow["standing_consent"];
    assert_eq!(standing["state"], "granted", "{flow}");
    assert_eq!(standing["caps"]["max_memory_bytes"], 2 * GIB, "{flow}");
    assert_eq!(standing["caps"]["max_wall_seconds"], 600, "{flow}");
    let grant = api::preferences::standing_consent::load(&private(&member.state))
        .unwrap()
        .unwrap();
    assert_eq!(grant["caps"], standing["caps"], "saved: {grant:?}");
    assert!(grant["binding"].is_object(), "bound: {grant:?}");
    assert_eq!(grant["state"], "granted", "{grant:?}");
    // app.py:1229-1236: what `#standing-state` reads (app.js:527).
    assert_eq!(
        (
            &flow["last_report"]["purpose"],
            &flow["last_report"]["state"]
        ),
        (&json!("join"), &json!("delivered")),
        "{flow}"
    );
    assert_eq!(flow["standing_error"], Value::Null, "{flow}");
    assert_eq!(flow["standing_error_http"], Value::Null, "{flow}");
    assert_eq!(standing["last"], Value::Null, "no offer yet: {flow}");
    assert_eq!(after["renewal_error"], Value::Null, "{after}");

    let seen = owner.mac.until(
        "owner sees the member",
        |o| o.status(),
        |v| v["authority"]["member_count"] == 2,
    );
    let rows = seen["authority"]["members"].as_array().unwrap();
    assert_eq!(rows.len(), 2, "{seen}");
    for row in rows {
        let mut keys: Vec<&str> = row
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        assert_eq!(
            keys,
            ["clock", "generation", "lifecycle_state", "node_id"],
            "{row}"
        );
    }
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// A loopback TCP forwarder to `target` that drops every connection while `refuse` is set.
struct Forwarder {
    address: SocketAddr,
    refuse: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    accept: Option<JoinHandle<Vec<JoinHandle<()>>>>,
}
impl Forwarder {
    fn start(target: SocketAddr) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = listener.local_addr().unwrap();
        let (refuse, stop) = (
            Arc::new(AtomicBool::new(false)),
            Arc::new(AtomicBool::new(false)),
        );
        let (r, s) = (refuse.clone(), stop.clone());
        let accept = std::thread::spawn(move || {
            let mut pumps = Vec::new();
            while !s.load(Ordering::SeqCst) {
                let Ok((inbound, _)) = listener.accept() else {
                    std::thread::sleep(macs::millis(B::OwnerPoll) / 10);
                    continue;
                };
                if r.load(Ordering::SeqCst) {
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
            stop,
            accept: Some(accept),
        }
    }
}
impl Drop for Forwarder {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(accept) = self.accept.take() {
            drop(accept.join().unwrap_or_default());
        }
    }
}

/// `renewal_error` of a joined member (`_renew_loop`, app.py:917-927 with `RemoteMacMember.renew`):
/// passes that cannot reach the owner's seed show `renewal_unavailable`; the next pass that renews
/// clears it to null. The failures are refused connections on the member's own seed path.
#[test]
fn a_joined_members_failed_renewal_shows_renewal_unavailable_until_it_renews_again() {
    let base = state_root("status-renewal");
    let owner = owner(&base);
    let forwarder = Forwarder::start(owner.seams.member_seed.connect_to.unwrap());
    let mut seams = owner.seams.clone();
    seams.member_seed.connect_to = Some(forwarder.address);
    let member = launch(
        &base,
        "member",
        NetworkConfig {
            seams,
            renewal: B::OwnerPoll,
            ..NetworkConfig::default()
        },
    );
    let waiting = join_with(&member, &owner.link, 4 * GIB, 600);
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    approve_and_join(&owner, &member);
    assert_eq!(member.status()["renewal_error"], Value::Null);
    forwarder.refuse.store(true, Ordering::SeqCst);
    member.until(
        "a failed renewal pass is shown",
        |m| m.status(),
        |v| v["renewal_error"] == "renewal_unavailable",
    );
    forwarder.refuse.store(false, Ordering::SeqCst);
    member.until(
        "the next renewal clears it",
        |m| m.status(),
        |v| v["renewal_error"].is_null(),
    );
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(forwarder);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// Once the owner opted in to a listing, the card's text has a name and an access mode
/// (swarm.js:33-34 `Published "${swarm.name}" · ... · access ${swarm.access_mode}`), and the key line
/// its fingerprint (:29), never `undefined`. The directory is a loopback listener that never answers.
#[test]
fn the_packaged_swarm_status_has_what_the_swarm_card_reads() {
    let base = state_root("status-swarm");
    let directory = TcpListener::bind("127.0.0.1:0").unwrap();
    let origin = format!(
        "http://127.0.0.1:{}",
        directory.local_addr().unwrap().port()
    );
    let mac = launch(&base, "owner", NetworkConfig::default());
    let created = mac.post("/api/create", json!({"consent": consent(4_000_000_000)}));
    assert_eq!(created.0, 200, "create: {}", created.1);
    let published = mac.post(
        "/api/swarm/publish",
        json!({"directory": origin, "name": "Packaged swarm", "access_mode": "request",
               "description": "A packaged owner"}),
    );
    assert_eq!(published.0, 200, "publish: {}", published.1);
    let swarm = mac.status()["swarm"].clone();
    assert_eq!(swarm["enabled"], true, "{swarm}");
    assert_eq!(swarm["name"], "Packaged swarm", "{swarm}");
    assert_eq!(swarm["access_mode"], "request", "{swarm}");
    assert!(swarm["entry_fingerprint"].is_string(), "{swarm}");
    assert_eq!(
        swarm["owner_fingerprint"], swarm["entry_fingerprint"],
        "{swarm}"
    );
    for key in ["attached", "killed"] {
        assert_eq!(swarm[key], false, "{key}: {swarm}");
    }
    for key in ["entry_origin", "heartbeat", "link"] {
        assert_eq!(swarm.get(key), Some(&Value::Null), "{key}: {swarm}");
    }
    assert_eq!(mac.quit(), Ok(Poll::Verified));
    drop(directory);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// The packaged status overlay takes the published document as it is: one that is not a JSON
/// object (none is published today) is left alone, never indexed into a panic that would stick as
/// `desktop_callback_panicked` (REVIEW-lanePH2 F8).
#[test]
fn the_status_overlay_never_panics_on_a_document_that_is_not_an_object() {
    let base = state_root("status-overlay");
    let root = private(&base);
    std::fs::create_dir_all(&root).unwrap();
    let authority = Mutex::new(api::app_host::Authority::load(root.clone()).unwrap());
    let network = Network::new(&root, NetworkConfig::default());
    for shape in [json!([]), json!("status"), json!(1), json!(true)] {
        let mut status = shape.clone();
        let overlaid = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            node_service::packaged_network::status_overlay(&mut status, Some(&authority), &network)
        }));
        assert!(overlaid.is_ok(), "the overlay panicked on {shape}");
        assert_eq!(status, shape);
    }
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
