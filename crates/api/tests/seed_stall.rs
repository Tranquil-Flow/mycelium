// SPDX-License-Identifier: AGPL-3.0-only
//! RV42 S1 (`analysis/RV42-S1-RELAY-502-RCA.md`): a joined Mac got bursts of `502
//! relay_backend_unavailable` whenever the owner's seed stalled past the relay's 5 s hop. These
//! pin the v0.2-only aggravators the RCA names, with v0.1.4's bounds unchanged:
//! - the page's status reads the authority's fields first and the seed's member rows
//!   (`coordinator.members()`) after, so a host can release its authority lock in between
//!   (`app_status::shared_snapshot`); what it answers is what `app_status::shared` answers.
use api::{
    app_host::{self, Authority, Unmanaged},
    app_status,
    windows::PendingWindows,
};
use budgets::{BudgetId, Entries};
use serde_json::json;
use std::{
    fs,
    path::PathBuf,
    sync::{Arc, Mutex, mpsc},
    thread,
    time::Duration,
};

struct Root(PathBuf);
impl Root {
    fn new(tag: &str) -> Self {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join(format!("seed-stall-{tag}-{}-{stamp}", std::process::id()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap_or_default();
    }
}
/// The relay's hop bound (5 s, `public_relay.py:115`): what a stalled seed costs a joined Mac.
fn hop() -> Duration {
    Duration::from_millis(
        BudgetId::PyMyceliumA13MacosPublicRelayRelayhandlerForwardTimeoutL115
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
/// An owner with a running seed (`/api/create`'s owner collapse).
fn owner(root: &Root) -> Arc<Mutex<Authority<Unmanaged>>> {
    let host = Arc::new(Mutex::new(
        Authority::<Unmanaged>::load(root.0.clone()).unwrap(),
    ));
    let created = app_host::create_owner_json(
        &*host,
        json!({"storage_bytes": 4_000_000_000u64})
            .as_object()
            .unwrap(),
    );
    assert!(created.is_ok(), "create: {created:?}");
    host
}

/// The snapshot touches no coordinator: it is taken while another caller holds the seed's
/// coordinator, and the fields it renders once the coordinator is free equal `shared`'s.
#[test]
fn the_status_snapshot_reads_no_coordinator_and_renders_what_shared_answers() {
    let root = Root::new("snapshot");
    let host = owner(&root);
    let pending = PendingWindows::default();
    let facts = app_status::Facts::default();
    let expected: Vec<(&str, serde_json::Value)> = {
        let a = host.lock().unwrap();
        app_status::shared(&a, &pending, &facts)
            .into_iter()
            .map(|(k, v)| (k, v.to_value()))
            .collect()
    };
    let (seed, node) = {
        let a = host.lock().unwrap();
        let node = a.swarm.as_ref().unwrap()["node_id"].as_str().unwrap();
        (a.seed.clone().unwrap(), node.to_owned())
    };
    let (held_tx, held_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let holder = thread::spawn(move || {
        seed.coordinator.with_current_member(&node, |_, _| {
            held_tx.send(()).unwrap();
            release_rx.recv().unwrap_or_default();
            Ok(())
        })
    });
    held_rx
        .recv_timeout(hop())
        .expect("the coordinator is held");

    let (taken_tx, taken_rx) = mpsc::channel();
    let status = thread::spawn({
        let host = host.clone();
        move || {
            let (pending, facts) = (PendingWindows::default(), app_status::Facts::default());
            let snapshot = {
                let a = host.lock().unwrap();
                app_status::shared_snapshot(&a, &pending, &facts)
            };
            taken_tx.send(()).unwrap();
            snapshot
                .fields()
                .into_iter()
                .map(|(k, v)| (k, v.to_value()))
                .collect::<Vec<_>>()
        }
    });
    let taken = taken_rx.recv_timeout(hop());
    let free = host.try_lock().is_ok();
    release_tx.send(()).unwrap();
    assert!(holder.join().unwrap().is_ok());
    let fields = status.join().unwrap();
    assert_eq!(taken, Ok(()), "the snapshot waited on the held coordinator");
    assert!(free, "the authority is free once the snapshot is taken");
    assert_eq!(
        fields.iter().map(|(k, _)| *k).collect::<Vec<_>>(),
        expected.iter().map(|(k, _)| *k).collect::<Vec<_>>(),
        "v0.1.4's key order"
    );
    assert_eq!(fields, expected);
    app_host::stop_authority(&*host);
}

/// The relay's verified-TLS hop into the seed builds its client configuration once per seed
/// runtime, as v0.1.4's `bind_seed` builds its SSL context once (public_relay.py:219-226), instead
/// of a CA store and a configuration per relayed request; what the hop answers is unchanged.
#[test]
fn the_relay_hop_builds_its_tls_configuration_once_and_still_reaches_the_seed() {
    use api::{joined_member::SeedHop, relay::SeedBackend};
    let root = Root::new("hop-tls");
    let host = owner(&root);
    let seed = host.lock().unwrap().seed.clone().unwrap();
    let first = seed.hop_client_config().expect("the seed's local CA loads");
    let hop = SeedHop(seed.clone());
    for _ in 0..2 {
        let reply = hop
            .request("GET", "/seed/identity", "application/json", b"")
            .unwrap_or_else(|_| panic!("the hop reaches the seed"));
        assert_eq!(
            reply.status,
            200,
            "{}",
            String::from_utf8_lossy(&reply.body)
        );
        assert!(reply.content_type.starts_with("application/json"));
    }
    let again = seed.hop_client_config().expect("still built");
    assert!(
        Arc::ptr_eq(&first, &again),
        "one configuration per seed runtime, not one per request"
    );
    app_host::stop_authority(&*host);
}
