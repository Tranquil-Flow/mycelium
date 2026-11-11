// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's `swarm` status carries every field the unchanged v0.1.4 page (`swarm.js`
//! `render`, crates/desktop/assets/swarm.js:24-43) reads, with v0.1.4's values
//! (`SwarmPublisher.status`, swarm_publish.py:456-472 @ f4e366e9; reached from `DemoApp.status`,
//! app.py:1281 `'swarm': _swarm_status(self)` -> :430-436 -> swarm_publish.py:524-530): the listing's
//! `name` and `access_mode`, the owner and entry key fingerprints, `attached`, the entry origin, the
//! last heartbeat's status, the swarm link and `killed`. Without them a published card reads
//! `Published "undefined" · starting · access undefined` (REVIEW-lanePH2 F7). Loopback directory only.
#[path = "support/swarm_publisher.rs"]
#[allow(dead_code)] // the shared support's `restart` is not used here
mod support;
use api::swarm::{
    docs,
    json::J,
    publisher::{
        DirectoryClient, DirectoryPort,
        app::{AppPublisher, OwnerFacts},
    },
    wire,
};
use serde_json::{Map, Value, json};
use std::sync::Arc;
use support::{Scratch, Server, set_clock};

const NOW: i64 = 1_800_000_000;
const MODEL: &str = "Qwen/Qwen3-0.6B";
const ENTRY: &str = "https://entry.example.org";

fn body(directory: &str) -> Map<String, Value> {
    json!({"directory": directory, "name": "Café swarm", "access_mode": "request",
           "description": "Public text"})
    .as_object()
    .unwrap()
    .clone()
}
fn serving() -> OwnerFacts {
    OwnerFacts::from_owner_run(
        true,
        Some(
            &json!({"protocol": "mycelium.owner_run.v1", "state": "serving", "route_ready": true,
                     "registry_origin": "http://127.0.0.1:9",
                     "participants": [{"node_id": "a"}, {"node_id": "b"}]}),
        ),
        Some(MODEL),
        Some(ENTRY),
    )
}

/// Every key `swarm.js` reads is present once the owner opted in (pending: nothing listed yet,
/// the owner's own listing choice is shown), and once listed they carry the record's values:
/// fingerprints of the listing key, the record's entry origin, the heartbeat's status and the
/// `mycelium-swarm:` link of (directory, swarm id, owner key).
#[test]
fn the_swarm_status_has_every_field_the_page_reads() {
    let root = Scratch::new();
    let state = root.0.join("state");
    std::fs::create_dir(&state).unwrap();
    set_clock(&root.0, NOW);
    let server = Server::start(&root.0, false);
    let client: Arc<dyn DirectoryPort> = Arc::new(DirectoryClient::new(&server.origin).unwrap());
    let port = client.clone();
    let p = AppPublisher::with_connect(&state, Box::new(move |_origin: &str| Ok(port.clone())));
    p.publish(true, &body(&server.origin)).unwrap();
    let pending = p.status_json();
    for key in [
        "enabled",
        "mode",
        "directory",
        "swarm_id",
        "name",
        "access_mode",
        "owner_fingerprint",
        "entry_fingerprint",
        "attached",
        "entry_origin",
        "heartbeat",
        "error_code",
        "link",
        "killed",
    ] {
        assert!(pending.get(key).is_some(), "{key} missing: {pending}");
    }
    assert_eq!(pending["name"], "Café swarm", "{pending}");
    assert_eq!(pending["access_mode"], "request", "{pending}");
    assert_eq!(pending["attached"], false, "{pending}");
    assert_eq!(pending["killed"], false, "{pending}");
    assert_eq!(
        (
            &pending["entry_origin"],
            &pending["heartbeat"],
            &pending["link"]
        ),
        (&Value::Null, &Value::Null, &Value::Null),
        "{pending}"
    );
    assert!(pending["entry_fingerprint"].is_string(), "{pending}");
    assert_eq!(
        pending["owner_fingerprint"], pending["entry_fingerprint"],
        "{pending}"
    );

    p.tick(NOW, &serving()).unwrap();
    let listed = p.status_json();
    let rows = client.call("GET", "/swarms", b"").unwrap()["swarms"].clone();
    let record = &rows[0]["record"];
    let owner_key = record["owner_key"].as_str().unwrap();
    let fingerprint = wire::fingerprint(&J::str(owner_key)).unwrap();
    assert_eq!(
        listed["owner_fingerprint"],
        fingerprint.as_str(),
        "{listed}"
    );
    assert_eq!(
        listed["entry_fingerprint"],
        fingerprint.as_str(),
        "{listed}"
    );
    assert_eq!(listed["swarm_id"], record["swarm_id"], "{listed}");
    assert_eq!(listed["name"], record["name"], "{listed}");
    assert_eq!(listed["access_mode"], record["access_mode"], "{listed}");
    assert_eq!(listed["entry_origin"], ENTRY, "{listed}");
    assert_eq!(listed["heartbeat"], "serving", "{listed}");
    assert_eq!(listed["attached"], true, "{listed}");
    assert_eq!(
        listed["link"],
        docs::swarm_link(
            &server.origin,
            record["swarm_id"].as_str().unwrap(),
            owner_key
        )
        .unwrap()
        .as_str(),
        "{listed}"
    );
}
