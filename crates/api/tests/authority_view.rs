// SPDX-License-Identifier: AGPL-3.0-only
//! Round-14 persisted owner authority against the EXECUTED certified executable
//! (`L10-evidence/r14-probe-authority.py` -> `fixtures/oracle_authority.json`): the `authority`
//! block of `GET /api/status` (key order included), the `/api/create` answer, the idle `owner_run`
//! block, and the refusals the oracle gives for a state it cannot open.
//!
//! Documented difference (no seed coordinator in this workspace, SE-L10-SEED-COORD): v0.1.4
//! RESUMES the member when it restarts on a state root, so it answers generation + 1 and a renewed
//! lease; this view reports the stored generation and a lease judged against the clock. The
//! comparison below therefore checks `membership_generation` / `lease_state` against the stored
//! rows, and everything else against the oracle.
use api::{
    authority_view::{Member, PersistedAuthority, idle_owner_run, no_swarm_status},
    wire::Json,
};
use rusqlite::Connection;
use serde_json::{Value, json};
use std::{os::unix::fs::PermissionsExt, path::PathBuf};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_authority.json")).expect("fixture")
}
fn scratch(tag: &str) -> PathBuf {
    scratch_at(
        tag,
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos()),
    )
}
fn scratch_at(tag: &str, tick: u128) -> PathBuf {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let unique = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!(
        "authority-view-{tag}-{}-{tick}-{unique}",
        std::process::id(),
    ));
    std::fs::create_dir(&dir).expect("exclusive fixture directory");
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).expect("mode");
    dir
}
#[test]
fn equal_clock_ticks_cannot_reuse_an_existing_fixture_database() {
    let first = scratch_at("clock-collision", 7);
    let second = scratch_at("clock-collision", 7);
    let schema = fixture()["seed_members_schema"]
        .as_str()
        .unwrap()
        .to_owned();
    let a = Connection::open(first.join("state.sqlite3")).unwrap();
    a.execute_batch(&schema).unwrap();
    let b = Connection::open(second.join("state.sqlite3")).unwrap();
    b.execute_batch(&schema)
        .expect("a fresh fixture cannot already contain seed_members");
    assert_ne!(first, second);
    drop((a, b));
    std::fs::remove_dir_all(first).unwrap();
    std::fs::remove_dir_all(second).unwrap();
}
/// A state root as the oracle left it: `authority/authority.json` + `authority/seed/state.sqlite3`.
fn build_root(fx: &Value, rows: &Value, authority_json: &Value, with_authority: bool) -> PathBuf {
    let root = scratch("root");
    if !with_authority {
        return root;
    }
    let seed = root.join("authority/seed");
    std::fs::create_dir_all(&seed).expect("seed dir");
    for dir in [root.join("authority"), seed.clone()] {
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).expect("mode");
    }
    let doc = root.join("authority/authority.json");
    // `_atomic_private_json`: sorted keys, compact
    let canonical = serde_json::to_string(authority_json).expect("json");
    std::fs::write(&doc, canonical).expect("write");
    std::fs::set_permissions(&doc, std::fs::Permissions::from_mode(0o600)).expect("mode");
    let db = Connection::open(seed.join("state.sqlite3")).expect("db");
    db.execute_batch(fx["seed_members_schema"].as_str().expect("schema"))
        .expect("schema");
    for row in rows.as_array().expect("rows") {
        db.execute(
            "insert into seed_members values (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15)",
            rusqlite::params![
                row["node_id"].as_str(),
                row["endpoint_id"].as_str(),
                row["endpoint_addrs_json"].as_str(),
                row["peer_class"].as_str(),
                row["runtime_capability_json"].as_str(),
                row["verification_key_digest"].as_str(),
                row["incarnation"].as_str(),
                row["generation"].as_i64(),
                row["lease_expires_at"].as_f64(),
                row["last_heartbeat_sequence"].as_i64(),
                row["last_liveness_at"].as_f64(),
                row["next_heartbeat_due_at"].as_f64(),
                row["last_activity_receipt_at"].as_f64(),
                row["active_requests"].as_i64(),
                row["lifecycle_state"].as_str(),
            ],
        )
        .expect("insert");
    }
    root
}
fn keys(json: &Json) -> Vec<String> {
    json.keys().into_iter().map(str::to_owned).collect()
}
fn key_list(value: &Value) -> Vec<String> {
    value
        .as_array()
        .expect("keys")
        .iter()
        .map(|k| k.as_str().expect("k").to_owned())
        .collect()
}

#[test]
fn idle_owner_run_equals_the_oracle() {
    let fx = fixture();
    assert_eq!(
        idle_owner_run().to_value(),
        fx["status_after_create"]["owner_run"]
    );
    assert_eq!(
        keys(&idle_owner_run()),
        vec![
            "state",
            "step",
            "error_code",
            "error_message",
            "failed_step",
            "steps",
            "participants",
            "registry_origin",
            "route_ready",
            "run_id"
        ]
    );
}
#[test]
fn an_owner_with_no_swarm_has_the_oracle_block() {
    let fx = fixture();
    let variant = fx["variants"]
        .as_array()
        .expect("v")
        .iter()
        .find(|v| v["label"] == "authority_dir_removed")
        .expect("variant");
    let got = no_swarm_status(true);
    assert_eq!(got.to_value(), variant["authority"]);
    assert_eq!(keys(&got), key_list(&variant["authority_keys"]));
    // and a root without `authority/` loads as no swarm
    let root = build_root(&fx, &json!([]), &Value::Null, false);
    assert_eq!(PersistedAuthority::load(&root).expect("load"), None);
}
#[test]
fn status_and_create_answer_equal_the_oracle_for_a_created_swarm() {
    let fx = fixture();
    let rows = &fx["seed_members_rows_after_create"];
    let root = build_root(&fx, rows, &fx["authority_json"], true);
    let view = PersistedAuthority::load(&root)
        .expect("load")
        .expect("swarm");
    assert_eq!(
        view.swarm_id,
        fx["authority_json"]["swarm_id"].as_str().expect("id")
    );
    // lease far in the future: fresh; the oracle's own status right after /api/create
    let got = view.status(true, 0.0);
    let want = &fx["status_after_create"]["authority"];
    assert_eq!(got.to_value(), *want, "authority block after create");
    assert_eq!(
        keys(&got),
        key_list(&fx["status_after_create"]["authority_keys"])
    );
    // `POST /api/create` on a swarm that is already current answers `authority.status()` (no `members`)
    let again = view.authority_status(true, 0.0);
    assert_eq!(again.to_value(), fx["create_again"]["body"]);
    assert_eq!(keys(&again), key_list(&fx["create_again"]["keys"]));
    let created = view.membership_result();
    assert_eq!(created.to_value(), fx["create_answer"]["body"]);
    assert_eq!(keys(&created), key_list(&fx["create_answer"]["keys"]));
}
#[test]
fn restart_variants_equal_the_oracle_except_for_the_resume() {
    let fx = fixture();
    let mut checked = 0;
    for v in fx["variants"].as_array().expect("variants") {
        let label = v["label"].as_str().unwrap_or("");
        if v.get("launch_failed").is_some() || label == "authority_dir_removed" {
            continue;
        }
        let root = build_root(&fx, &v["rows_in"], &fx["authority_json"], true);
        let view = PersistedAuthority::load(&root)
            .expect("load")
            .expect("swarm");
        // lease judged against a clock before every stored lease except `lease_in_the_past`
        let got = view.status(true, 1000.0);
        let mut want = v["authority"].clone();
        let stored = &v["rows_in"][0];
        want["membership_generation"] = stored["generation"].clone();
        want["members"][0]["generation"] = stored["generation"].clone();
        want["lease_state"] = json!(
            if stored["lease_expires_at"].as_f64().unwrap_or(0.0) > 1000.0 {
                "fresh"
            } else {
                "expired"
            }
        );
        assert_eq!(got.to_value(), want, "{label}");
        assert_eq!(
            keys(&got),
            key_list(&v["authority_keys"]),
            "{label} key order"
        );
        checked += 1;
    }
    assert_eq!(checked, 5);
}
#[test]
fn the_lease_is_fresh_only_while_strictly_in_the_future() {
    // SOURCE rule, not an executed vector (the oracle renews a restarted lease): `float(lease_expires_at) > self._clock()`
    // (authority.py:664-668) - a lease expiring exactly now is `expired`.
    let fx = fixture();
    let rows = &fx["seed_members_rows_after_create"];
    let root = build_root(&fx, rows, &fx["authority_json"], true);
    let view = PersistedAuthority::load(&root)
        .expect("load")
        .expect("swarm");
    let lease = view.members[0].lease_expires_at;
    let state = |now: f64| {
        view.status(true, now)
            .get("lease_state")
            .map(|s| s.to_value())
    };
    assert_eq!(state(lease), Some(json!("expired")));
    assert_eq!(
        state(f64::from_bits(lease.to_bits() - 1)),
        Some(json!("fresh"))
    );
    assert_eq!(
        state(f64::from_bits(lease.to_bits() + 1)),
        Some(json!("expired"))
    );
}
#[test]
fn states_the_oracle_cannot_open_are_refused() {
    let fx = fixture();
    let mut codes = Vec::new();
    for v in fx["variants"].as_array().expect("variants") {
        let label = v["label"].as_str().unwrap_or("");
        let Some(failed) = v["launch_failed"].as_str() else {
            continue;
        };
        let code = failed
            .split("\"code\": \"")
            .nth(1)
            .and_then(|s| s.split('"').next())
            .unwrap_or("")
            .to_owned();
        let doc = if label == "other_target_node" {
            let mut d = fx["authority_json"].clone();
            d["target_node_id"] = json!("judge-mac-0000000000000000");
            d
        } else {
            fx["authority_json"].clone()
        };
        let rows = if label == "no_member_rows" {
            json!([])
        } else {
            fx["seed_members_rows_after_create"].clone()
        };
        let root = build_root(&fx, &rows, &doc, true);
        let got = PersistedAuthority::load(&root)
            .err()
            .map(|e| e.code.to_string());
        // `generation_7` fails in the oracle's RESUME (`seed_resume_generation_stale`), which this host does not run
        if label == "generation_7" {
            assert_eq!(code, "seed_resume_generation_stale");
            continue;
        }
        assert_eq!(got.as_deref(), Some(code.as_str()), "{label}");
        codes.push(code);
    }
    codes.sort();
    assert_eq!(
        codes,
        vec!["authority_membership_state_invalid", "seed_member_unknown"]
    );
    let _ = Member {
        node_id: String::new(),
        generation: 0,
        lifecycle_state: String::new(),
        lease_expires_at: 0.0,
    };
}
