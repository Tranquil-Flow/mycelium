// SPDX-License-Identifier: AGPL-3.0-only
//! The real node-service authority composition (`enrollment::{join, accept, signed}`,
//! `LocalMembership::verify_acceptance`, the L5 `Identity` signer and
//! `protocol::membership::verify`) judged against the pinned v0.1.4 verifier.
//!
//! `join_requests_get_the_v014_coordinator_outcome` additionally judges every join request by
//! what v0.1.4's real join authority (`SeedCoordinator.accept_join`) did, executed.
//!
//! Exemplars are signed by the composition under scratch TEST keys (fixed seeds, no
//! host key material). `authority-exemplars.json` is the set the oracle probe
//! (`round6-authority-oracle-probe.py`) executed `verify_membership_message` over;
//! `authority-oracle-outcomes.json` holds what v0.1.4 did with each. Both the committed
//! set and a freshly composed set must be accepted/refused with exactly those outcomes
//! and, where v0.1.4 refuses, the exact code. Set `L5_WRITE_AUTHORITY_EXEMPLARS=<path>`
//! to re-emit the freshly composed set for the probe.
use budgets::WallExpiry;
use node_service::{enrollment::*, service};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;

const NOW_MS: u64 = 1_700_000_000_000;
const SWARM: &str = "swarm-authority";
const OWNER_NODE: &str = "owner-node";
const MEMBER_NODE: &str = "member-node";

fn now() -> WallExpiry {
    WallExpiry::from_unix_millis(NOW_MS)
}
fn key(endpoint: &str, seed: u8) -> Identity {
    Identity::from_secret(endpoint, &[seed; 32]).unwrap()
}
fn root() -> std::path::PathBuf {
    let p = std::env::temp_dir()
        .canonicalize()
        .unwrap()
        .join(format!("l5-authority-{}", service::random_id().unwrap()));
    std::fs::create_dir(&p).unwrap();
    std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700)).unwrap();
    p
}
struct World {
    owner: Identity,
    member: Identity,
    foreign: Identity,
    invitation: Invitation,
}
fn world() -> World {
    let owner = key("owner-endpoint", 0x51);
    let member = key("member-endpoint", 0x52);
    let foreign = key("foreign-endpoint", 0x53);
    let invitation = Invitation {
        swarm: SWARM.into(),
        owner_node: OWNER_NODE.into(),
        owner_endpoint: owner.endpoint_id().into(),
        owner_key: owner.public_key_record(),
        member_node: MEMBER_NODE.into(),
        member_key: member.public_key_record(),
        nonce: "authority-nonce".into(),
    };
    World {
        owner,
        member,
        foreign,
        invitation,
    }
}
struct Exemplar {
    name: &'static str,
    kind: &'static str,
    envelope: Value,
}
type Edit = fn(&mut Value);
/// (name, edit to the signed message, signer: 0 = rightful, 1 = foreign key
/// re-signing a rightful verification_key, 2 = foreign key + foreign record,
/// 3 = rightful signature kept after the edit)
const VARIANTS: &[(&str, Edit, u8)] = &[
    ("base", |_| {}, 0),
    (
        "recipient_other",
        |m| m["recipient_node_id"] = json!("other-node"),
        0,
    ),
    ("swarm_other", |m| m["swarm_id"] = json!("other-swarm"), 0),
    (
        "sender_node_other",
        |m| m["sender_node_id"] = json!("other-sender"),
        0,
    ),
    (
        "protocol_other",
        |m| m["protocol"] = json!("mycelium.membership.heartbeat.v1"),
        0,
    ),
    (
        "expired",
        |m| {
            m["issued_at"] = json!(1_699_990_000.0);
            m["expires_at"] = json!(1_699_990_500.0);
            if m.get("lease_expires_at").is_some() {
                m["lease_expires_at"] = json!(1_699_990_500.0);
            }
        },
        0,
    ),
    (
        "from_future",
        |m| {
            m["issued_at"] = json!(1_700_001_000.0);
            m["expires_at"] = json!(1_700_001_300.0);
            if m.get("lease_expires_at").is_some() {
                m["lease_expires_at"] = json!(1_700_001_300.0);
            }
        },
        0,
    ),
    (
        "tampered_after_signing",
        |m| m["incarnation"] = json!("tampered"),
        3,
    ),
    ("signature_by_foreign_key", |_| {}, 1),
    ("foreign_signer_and_record", |_| {}, 2),
];
fn variants(w: &World, kind: &'static str, base: &Value, signer: &Identity) -> Vec<Exemplar> {
    VARIANTS
        .iter()
        .map(|(name, edit, mode)| {
            let mut message = base["message"].clone();
            edit(&mut message);
            let envelope = match mode {
                0 => signed(signer, message).unwrap(),
                1 => {
                    let mut env = signed(&w.foreign, message).unwrap();
                    env["verification_key"] = signer.public_key_record();
                    env
                }
                2 => signed(&w.foreign, message).unwrap(),
                _ => {
                    let mut env = base.clone();
                    env["message"] = message;
                    env
                }
            };
            Exemplar {
                name: Box::leak(format!("{kind}:{name}").into_boxed_str()),
                kind,
                envelope,
            }
        })
        .collect()
}
fn compose(w: &World) -> (Value, Vec<Exemplar>) {
    let request = join(&w.member, &w.invitation, "boot-1", "127.0.0.1:12345", now()).unwrap();
    let acceptance = accept(&w.owner, &w.invitation, &request, 1, now()).unwrap();
    let mut out = variants(w, "join_request", &request, &w.member);
    out.extend(variants(w, "join_acceptance", &acceptance, &w.owner));
    // Envelope-shape refusals the verifier sees before any pin.
    let mut extra = acceptance.clone();
    extra["extra"] = json!(1);
    out.push(Exemplar {
        name: "join_acceptance:envelope_extra_field",
        kind: "join_acceptance",
        envelope: extra,
    });
    let mut extra = request.clone();
    extra["extra"] = json!(1);
    out.push(Exemplar {
        name: "join_request:envelope_extra_field",
        kind: "join_request",
        envelope: extra,
    });
    (request, out)
}
/// The exemplars whose `verify_membership_message` outcome (pinned to the invited member key)
/// is not what v0.1.4 does with a join: v0.1.4's join authority is `SeedCoordinator.accept_join`,
/// which verifies a join against the key the request carries (`verify_join_request`, self-attested)
/// and binds that key. Its executed outcome is in `round7-join-pin-oracle-outcomes.json` and is
/// judged by `join_requests_get_the_v014_coordinator_outcome`.
const JUDGED_BY_COORDINATOR: &[&str] = &[
    "join_request:foreign_signer_and_record",
    "join_request:sender_node_other",
];
fn fixture(raw: &str) -> Value {
    serde_json::from_str(raw).unwrap()
}
fn judge(w: &World, request: &Value, e: &Exemplar) -> Result<(), String> {
    let root = root();
    let local = LocalMembership::approve(
        &root,
        w.invitation.clone(),
        node::Caps {
            storage_bytes: 1,
            memory_bytes: 1,
            wall_ms: 1,
            commands: 1,
        },
        true,
        now(),
    )
    .unwrap();
    let result = match e.kind {
        "join_request" => accept(&w.owner, &w.invitation, &e.envelope, 1, now())
            .map(|_| ())
            .map_err(|e| e.to_string()),
        _ => local
            .verify_acceptance(request, &e.envelope, now())
            .map_err(|e| e.to_string()),
    };
    std::fs::remove_dir_all(root).unwrap();
    result
}

#[test]
fn composed_and_committed_exemplars_get_the_oracle_outcome() {
    let w = world();
    let (request, composed) = compose(&w);
    if let Ok(path) = std::env::var("L5_WRITE_AUTHORITY_EXEMPLARS") {
        let items: Vec<Value> = composed
            .iter()
            .map(|e| json!({"name": e.name, "kind": e.kind, "envelope": e.envelope}))
            .collect();
        let doc = json!({"now_ms": NOW_MS, "swarm": SWARM, "owner_node": OWNER_NODE, "member_node": MEMBER_NODE,
            "owner_key": w.owner.public_key_record(), "member_key": w.member.public_key_record(),
            "request": request, "items": items});
        std::fs::write(path, serde_json::to_vec_pretty(&doc).unwrap()).unwrap();
    }
    let outcomes = fixture(include_str!("fixtures/authority-oracle-outcomes.json"));
    let committed = fixture(include_str!("fixtures/authority-exemplars.json"));
    assert_eq!(committed["owner_key"], w.owner.public_key_record());
    assert_eq!(committed["member_key"], w.member.public_key_record());
    let from_file: Vec<Exemplar> = committed["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| Exemplar {
            name: Box::leak(i["name"].as_str().unwrap().to_owned().into_boxed_str()),
            kind: if i["kind"] == "join_request" {
                "join_request"
            } else {
                "join_acceptance"
            },
            envelope: i["envelope"].clone(),
        })
        .collect();
    assert_eq!(from_file.len(), composed.len());
    let mut seen = BTreeMap::new();
    for (set, label, req) in [
        (&composed, "composed", &request),
        (&from_file, "committed", &committed["request"]),
    ] {
        for e in set.iter() {
            let want = &outcomes[e.name];
            assert!(want.is_object(), "no oracle outcome for {}", e.name);
            let got = judge(&w, req, e);
            // `verify_membership_message` never sees the join request an acceptance answers;
            // node-service binds acceptances to it afterwards (checked below).
            if JUDGED_BY_COORDINATOR.contains(&e.name) {
                continue;
            }
            match want["ok"].as_bool().unwrap() {
                true => assert_eq!(got, Ok(()), "{label} {}", e.name),
                false => assert_eq!(
                    got,
                    Err(want["error"].as_str().unwrap().to_owned()),
                    "{label} {}",
                    e.name
                ),
            }
            *seen.entry(label).or_insert(0usize) += 1;
        }
    }
    assert_eq!(seen["composed"], seen["committed"]);
    assert!(seen["composed"] >= 20, "{seen:?}");
    // Replay of a rightful acceptance against a different request is refused by the
    // service binding (node-service-owned; v0.1.4's contract verifier cannot see it).
    let other = join(&w.member, &w.invitation, "boot-2", "127.0.0.1:12346", now()).unwrap();
    let acceptance = composed
        .iter()
        .find(|e| e.name == "join_acceptance:base")
        .unwrap();
    assert_eq!(
        judge(&w, &other, acceptance),
        Err("join_acceptance_binding".to_owned())
    );
}

/// v0.1.4 joins are trust-on-first-use of the key AND the node id the request carries; ruling (c)
/// of round 7 (parent-confirmed): whatever v0.1.4 accepts, v0.2 must accept. A stricter v0.2 pin is a
/// regression, not an improvement (it goes on the v0.3 list). No input is exempt.
#[test]
fn join_requests_get_the_v014_coordinator_outcome() {
    let w = world();
    let outcomes = fixture(include_str!(
        "fixtures/round7-join-pin-oracle-outcomes.json"
    ));
    let committed = fixture(include_str!("fixtures/authority-exemplars.json"));
    let mut judged = 0;
    for i in committed["items"].as_array().unwrap() {
        if i["kind"] != "join_request" {
            continue;
        }
        let name = i["name"].as_str().unwrap();
        let want = &outcomes["results"][format!("exemplar:{name}")];
        assert!(want.is_object(), "no coordinator outcome for {name}");
        let e = Exemplar {
            name: Box::leak(name.to_owned().into_boxed_str()),
            kind: "join_request",
            envelope: i["envelope"].clone(),
        };
        let got = judge(&w, &committed["request"], &e);
        if want["accepted"] == true {
            assert_eq!(got, Ok(()), "v0.1.4 accepted {name}; v0.2 must accept");
        } else {
            assert!(
                got.is_err(),
                "v0.1.4 refused {name} ({}); v0.2 accepted",
                want["code"]
            );
        }
        judged += 1;
    }
    assert_eq!(judged, 11);
    // The freshly composed foreign-key join is accepted as well (not only the committed bytes).
    let (_, composed) = compose(&w);
    let fresh = composed
        .iter()
        .find(|e| e.name == "join_request:sender_node_other")
        .unwrap();
    assert_eq!(judge(&w, &committed["request"], fresh), Ok(()));
}
