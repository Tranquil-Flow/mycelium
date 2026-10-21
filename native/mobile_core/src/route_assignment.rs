// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-issued route assignments delivered by the seed
//! (`mycelium_seed/route_assignment.py`): the minimal A2 slice that lets a
//! release node load a stage and allow-list one route peer without any
//! debug-only path. The member signs a request with its enrolled key and current
//! membership generation; the node accepts only a statement signed by the seed
//! key its invitation pinned, addressed to this node id, endpoint id and
//! generation, answering this exact request. The stage pack travels beside the
//! statement and must recompute to the signed digest before it is loaded.
//!
//! Two pack versions are dispatched on their protocol: a v1 GPT-2 pack with
//! inline weights (`mycelium.pixel_stage_pack.v1`), or a seed-signed v2 Qwen2
//! manifest (`mycelium.mobile_stage_pack.v2`) that pins its weights by digest;
//! the node fetches those into its artifact store before it loads the stage.

use serde_json::{Value, json};

use crate::artifact_grant::{self, Grant};
use crate::identity::Identity;
use crate::membership::{self, MemberConfig};
use crate::runtime::Gpt2BlockStage;
use crate::stage_manifest::{self, StageManifest};

pub const REQUEST_PROTOCOL: &str = "mycelium.seed.route_assignment_request.v1";
pub const ASSIGNMENT_PROTOCOL: &str = "mycelium.seed.route_assignment.v1";
pub const DELIVERY_PROTOCOL: &str = "mycelium.seed.route_assignment_delivery.v1";
/// Lifetime of one member request (the seed refuses more than 120 s).
pub const REQUEST_TTL_SECONDS: f64 = 60.0;
const MAX_CLOCK_SKEW_SECONDS: f64 = 30.0;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct RouteAssignmentError(pub String);

fn fail(code: &str) -> RouteAssignmentError {
    RouteAssignmentError(code.to_owned())
}

/// One verified assignment: what the node may load and whom it may talk to.
#[derive(Debug, Clone, PartialEq)]
pub struct RouteAssignment {
    pub assignment_id: String,
    pub stage_pack_digest: String,
    pub peer_endpoint_id: String,
    pub route_generation: u64,
    pub forward_to: bool,
    pub expires_at: f64,
    /// The route controllers (iroh endpoint ids) this assignment authorizes on
    /// the control ALPN. The set comes from the seed-signed statement, never
    /// from the controller: a session may not widen its own allow-list. Empty
    /// is legal and means the control ALPN refuses everyone.
    pub controller_endpoint_ids: Vec<String>,
}

/// The verified stage that came with an assignment.
pub enum DeliveredStage {
    /// v1: weights inline, loadable at once.
    Gpt2(Box<Gpt2BlockStage>),
    /// v2: a seed-signed manifest; `envelope` is kept so the node can persist
    /// it and re-verify it at boot against the pinned seed key. `grant` is the
    /// seed's A2 grant for the manifest's objects, bound to this node.
    Qwen2 { manifest: Box<StageManifest>, envelope: Value, grant: Box<Grant> },
}

impl DeliveredStage {
    pub fn pack_digest(&self) -> &str {
        match self {
            Self::Gpt2(stage) => stage.pack_digest(),
            Self::Qwen2 { manifest, .. } => &manifest.pack_digest,
        }
    }
}

enum VerifiedPack {
    Gpt2(Box<Gpt2BlockStage>),
    Qwen2(Box<StageManifest>),
}

/// Verify the pack that travelled beside a seed-signed assignment.
fn verify_stage_pack(pack: &Value, pinned_seed_key: &Value) -> Result<VerifiedPack, RouteAssignmentError> {
    let signed_v2 = pack.get("protocol").and_then(Value::as_str) == Some(membership::SIGNED_MESSAGE_PROTOCOL)
        && pack["message"]["protocol"] == stage_manifest::MANIFEST_PROTOCOL;
    if signed_v2 {
        let manifest = StageManifest::verify_signed(pack, pinned_seed_key).map_err(|e| fail(&e.0))?;
        return Ok(VerifiedPack::Qwen2(Box::new(manifest)));
    }
    if pack.get("protocol").and_then(Value::as_str) == Some(stage_manifest::MANIFEST_PROTOCOL) {
        return Err(fail("route_assignment_stage_manifest_unsigned"));
    }
    Gpt2BlockStage::from_document(pack).map(|s| VerifiedPack::Gpt2(Box::new(s))).map_err(|e| fail(&e.0))
}

/// What this node expects the seed's answer to be bound to.
pub struct Expected<'a> {
    pub pinned_seed_key: &'a Value,
    pub swarm_id: &'a str,
    pub seed_node_id: &'a str,
    pub node_id: &'a str,
    pub endpoint_id: &'a str,
    /// This member's enrolled key digest (a v2 grant must name it).
    pub member_key_digest: &'a str,
    pub generation: u64,
    pub request_message_id: &'a str,
    pub now: f64,
}

/// The member-signed request; `applied` names the assignment the node runs now.
pub fn request(
    cfg: &MemberConfig,
    identity: &Identity,
    message_id: &str,
    generation: u64,
    applied: Option<&str>,
    now: f64,
) -> Result<Value, RouteAssignmentError> {
    if generation < 1 {
        return Err(fail("membership_generation_invalid"));
    }
    let message = json!({
        "protocol": REQUEST_PROTOCOL,
        "message_id": message_id,
        "swarm_id": cfg.swarm_id,
        "sender_node_id": cfg.node_id,
        "sender_endpoint_id": identity.endpoint_id(),
        "recipient_node_id": cfg.seed_node_id,
        "incarnation": cfg.incarnation,
        "generation": generation,
        "issued_at": now,
        "expires_at": now + REQUEST_TTL_SECONDS,
        "applied_assignment_id": applied,
    });
    membership::envelope(identity, message).map_err(|e| fail(e.0))
}

fn text<'v>(v: &'v Value, code: &str) -> Result<&'v str, RouteAssignmentError> {
    v.as_str().filter(|s| !s.is_empty() && s.len() <= 128).ok_or_else(|| fail(code))
}

fn finite(v: &Value, code: &str) -> Result<f64, RouteAssignmentError> {
    v.as_f64().filter(|x| x.is_finite()).ok_or_else(|| fail(code))
}

/// Verify one delivery. `Ok(None)` is the seed's signed word that this node has
/// no assignment (never assigned, withdrawn or expired).
pub fn verify_delivery(reply: &Value, expected: &Expected<'_>) -> Result<Option<(RouteAssignment, DeliveredStage)>, RouteAssignmentError> {
    let map = reply.as_object().ok_or_else(|| fail("route_assignment_delivery_invalid"))?;
    let with_grant = map.contains_key("artifact_grant");
    if map.len() != 3 + usize::from(with_grant)
        || map.get("protocol").and_then(Value::as_str) != Some(DELIVERY_PROTOCOL)
        || !map.contains_key("envelope")
        || !map.contains_key("stage_pack")
    {
        return Err(fail("route_assignment_delivery_invalid"));
    }
    let m = membership::verify_seed_envelope(&map["envelope"], expected.pinned_seed_key, ASSIGNMENT_PROTOCOL).map_err(|e| fail(e.0))?;
    if m["swarm_id"] != expected.swarm_id
        || m["sender_node_id"] != expected.seed_node_id
        || m["recipient_node_id"] != expected.node_id
        || m["recipient_endpoint_id"] != expected.endpoint_id
    {
        return Err(fail("route_assignment_recipient_mismatch"));
    }
    if m["generation"].as_u64() != Some(expected.generation) {
        return Err(fail("route_assignment_generation_mismatch"));
    }
    if m["request_message_id"] != expected.request_message_id {
        return Err(fail("route_assignment_request_mismatch"));
    }
    if m["route_ready"] != Value::Bool(false) {
        return Err(fail("route_assignment_route_ready_invalid"));
    }
    let issued = finite(&m["issued_at"], "route_assignment_time_invalid")?;
    let expires = finite(&m["expires_at"], "route_assignment_time_invalid")?;
    if expected.now > expires {
        return Err(fail("route_assignment_expired"));
    }
    if expected.now + MAX_CLOCK_SKEW_SECONDS < issued {
        return Err(fail("route_assignment_from_future"));
    }
    let a = &m["assignment"];
    if a.is_null() {
        return if map["stage_pack"].is_null() && !with_grant { Ok(None) } else { Err(fail("route_assignment_delivery_invalid")) };
    }
    let fields = a.as_object().ok_or_else(|| fail("route_assignment_invalid"))?;
    let required = ["assignment_id", "stage_pack_digest", "peer_endpoint_id", "route_generation", "forward_to", "expires_at"];
    if fields.len() != 6 + usize::from(fields.contains_key("controller_endpoint_ids"))
        || !required.iter().all(|key| fields.contains_key(*key))
    {
        return Err(fail("route_assignment_invalid"));
    }
    let peer = text(&a["peer_endpoint_id"], "route_assignment_peer_invalid")?;
    if peer.len() != 64 || !peer.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) || peer == expected.endpoint_id {
        return Err(fail("route_assignment_peer_invalid"));
    }
    // The controller set is authority, not a request: it is the seed's, and a
    // controller can never name or extend itself. At most 16 distinct ids, each
    // a 64-hex endpoint id that is neither this node nor its route peer.
    let mut controller_endpoint_ids = match fields.get("controller_endpoint_ids") {
        None => Vec::new(),
        Some(list) => {
            let list = list.as_array().ok_or_else(|| fail("route_assignment_controller_invalid"))?;
            if list.len() > 16 {
                return Err(fail("route_assignment_controller_invalid"));
            }
            let mut seen = std::collections::BTreeSet::new();
            let mut ids = Vec::with_capacity(list.len());
            for item in list {
                let id = text(item, "route_assignment_controller_invalid")?;
                if id.len() != 64 || !id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
                    return Err(fail("route_assignment_controller_invalid"));
                }
                if id == expected.endpoint_id || id == peer || !seen.insert(id) {
                    return Err(fail("route_assignment_controller_invalid"));
                }
                ids.push(id.to_owned());
            }
            ids
        }
    };
    controller_endpoint_ids.sort();
    let assignment = RouteAssignment {
        assignment_id: text(&a["assignment_id"], "route_assignment_invalid")?.to_owned(),
        stage_pack_digest: text(&a["stage_pack_digest"], "route_assignment_invalid")?.to_owned(),
        peer_endpoint_id: peer.to_owned(),
        route_generation: a["route_generation"].as_u64().filter(|g| *g >= 1).ok_or_else(|| fail("route_assignment_invalid"))?,
        forward_to: a["forward_to"].as_bool().ok_or_else(|| fail("route_assignment_invalid"))?,
        expires_at: finite(&a["expires_at"], "route_assignment_invalid")?,
        controller_endpoint_ids,
    };
    if expected.now >= assignment.expires_at {
        return Err(fail("route_assignment_expired"));
    }
    // The pack's own digest is recomputed over its content by the runtime, then
    // must equal the digest the seed signed.
    let stage = match verify_stage_pack(&map["stage_pack"], expected.pinned_seed_key)? {
        VerifiedPack::Gpt2(stage) if !with_grant => DeliveredStage::Gpt2(stage),
        VerifiedPack::Qwen2(manifest) if with_grant => {
            // The A2 grant: seed-signed, current, for this node and this manifest.
            let now_ms = (expected.now * 1000.0) as u64;
            let grant = artifact_grant::verify_grant(&map["artifact_grant"], expected.pinned_seed_key, now_ms).map_err(|e| fail(e.0))?;
            grant
                .check_binding(expected.node_id, expected.endpoint_id, expected.member_key_digest, &manifest.pack_digest)
                .map_err(|e| fail(e.0))?;
            if grant.assignment_id != assignment.assignment_id {
                return Err(fail("artifact_grant_assignment_mismatch"));
            }
            DeliveredStage::Qwen2 { envelope: map["stage_pack"].clone(), manifest, grant: Box::new(grant) }
        }
        VerifiedPack::Gpt2(_) => return Err(fail("route_assignment_delivery_invalid")),
        VerifiedPack::Qwen2(_) => return Err(fail("route_assignment_artifact_grant_missing")),
    };
    if stage.pack_digest() != assignment.stage_pack_digest {
        return Err(fail("route_assignment_stage_digest_mismatch"));
    }
    Ok(Some((assignment, stage)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> MemberConfig {
        MemberConfig {
            swarm_id: "swarm-a".into(),
            node_id: "node-a".into(),
            seed_node_id: "seed-node".into(),
            incarnation: "inc-1".into(),
            software_version: "v".into(),
            peer_class: "android_native_iroh".into(),
            runtime_capability: json!({}),
            ttl_seconds: 60.0,
        }
    }

    fn pack() -> Value {
        let path = format!("{}/tests/fixtures/gpt2_stage.json", env!("CARGO_MANIFEST_DIR"));
        let f: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        f["pack"].clone()
    }

    struct World {
        seed: Identity,
        member: Identity,
        peer: String,
    }

    fn world() -> World {
        World {
            seed: Identity::from_secret("seed-endpoint", &[7u8; 32]).unwrap(),
            member: Identity::from_secret(&"a".repeat(64), &[9u8; 32]).unwrap(),
            peer: "b".repeat(64),
        }
    }

    fn delivery(w: &World, assignment: Value, stage_pack: Value, edit: impl FnOnce(&mut Value)) -> Value {
        let mut statement = json!({
            "protocol": ASSIGNMENT_PROTOCOL,
            "message_id": "route-delivery-1",
            "swarm_id": "swarm-a",
            "sender_node_id": "seed-node",
            "sender_endpoint_id": "seed-endpoint",
            "recipient_node_id": "node-a",
            "recipient_endpoint_id": w.member.endpoint_id(),
            "incarnation": "seed-inc",
            "generation": 3,
            "request_message_id": "req-1",
            "issued_at": 1000.0,
            "expires_at": 1060.0,
            "assignment": assignment,
            "route_ready": false,
        });
        edit(&mut statement);
        json!({
            "protocol": DELIVERY_PROTOCOL,
            "envelope": {
                "protocol": membership::SIGNED_MESSAGE_PROTOCOL,
                "message": statement,
                "signature": w.seed.sign(&statement).unwrap(),
                "verification_key": w.seed.public_key_record(),
            },
            "stage_pack": stage_pack,
        })
    }

    fn assignment(w: &World) -> Value {
        json!({
            "assignment_id": "route-1",
            "stage_pack_digest": pack()["pack_digest"],
            "peer_endpoint_id": w.peer,
            "route_generation": 7,
            "forward_to": true,
            "expires_at": 5000.0,
        })
    }

    fn check(w: &World, reply: &Value) -> Result<Option<(RouteAssignment, DeliveredStage)>, RouteAssignmentError> {
        let pinned = w.seed.public_key_record();
        verify_delivery(
            reply,
            &Expected {
                pinned_seed_key: &pinned,
                swarm_id: "swarm-a",
                seed_node_id: "seed-node",
                node_id: "node-a",
                endpoint_id: w.member.endpoint_id(),
                member_key_digest: &w.member.verification_key_digest(),
                generation: 3,
                request_message_id: "req-1",
                now: 1010.0,
            },
        )
    }

    #[test]
    fn request_is_member_signed_and_names_the_applied_assignment() {
        let w = world();
        let env = request(&cfg(), &w.member, "req-1", 3, Some("route-1"), 10.0).unwrap();
        assert_eq!(env["message"]["protocol"], REQUEST_PROTOCOL);
        assert_eq!(env["message"]["generation"], 3);
        assert_eq!(env["message"]["applied_assignment_id"], "route-1");
        assert_eq!(env["message"]["expires_at"].as_f64(), Some(70.0));
        crate::identity::verify(&env["message"], &env["signature"], &env["verification_key"]).unwrap();
        let none = request(&cfg(), &w.member, "req-2", 3, None, 10.0).unwrap();
        assert!(none["message"]["applied_assignment_id"].is_null());
        assert!(request(&cfg(), &w.member, "req-3", 0, None, 10.0).is_err());
    }

    #[test]
    fn a_bound_seed_signed_assignment_yields_a_digest_verified_stage() {
        let w = world();
        let (a, stage) = check(&w, &delivery(&w, assignment(&w), pack(), |_| {})).unwrap().unwrap();
        let DeliveredStage::Gpt2(mut stage) = stage else { panic!("v1 pack") };
        assert_eq!(a.assignment_id, "route-1");
        assert_eq!(a.peer_endpoint_id, w.peer);
        assert_eq!(a.route_generation, 7);
        assert!(a.forward_to);
        assert_eq!(stage.pack_digest(), pack()["pack_digest"].as_str().unwrap());
        let rows = vec![vec![0.0; 8]];
        assert!(stage.execute("r", &rows).is_ok());
        assert!(check(&w, &delivery(&w, Value::Null, Value::Null, |_| {})).unwrap().is_none());
    }

    #[test]
    fn foreign_stale_replayed_or_tampered_deliveries_fail_closed() {
        let w = world();
        let code = |reply: Value| check(&w, &reply).err().expect("refused").0;
        // Signed by another key than the pinned seed.
        let impostor = World { seed: Identity::from_secret("seed-endpoint", &[8u8; 32]).unwrap(), ..world() };
        assert_eq!(code(delivery(&impostor, assignment(&w), pack(), |_| {})), "membership_seed_key_mismatch");
        // Tampered after signing.
        let mut tampered = delivery(&w, assignment(&w), pack(), |_| {});
        tampered["envelope"]["message"]["assignment"]["peer_endpoint_id"] = json!("c".repeat(64));
        assert_eq!(code(tampered), "membership_signature_invalid");
        // Addressed to another node, endpoint or generation, or another request (replay).
        assert_eq!(
            code(delivery(&w, assignment(&w), pack(), |m| m["recipient_node_id"] = json!("node-b"))),
            "route_assignment_recipient_mismatch"
        );
        assert_eq!(
            code(delivery(&w, assignment(&w), pack(), |m| m["recipient_endpoint_id"] = json!("d".repeat(64)))),
            "route_assignment_recipient_mismatch"
        );
        assert_eq!(code(delivery(&w, assignment(&w), pack(), |m| m["generation"] = json!(2))), "route_assignment_generation_mismatch");
        assert_eq!(
            code(delivery(&w, assignment(&w), pack(), |m| m["request_message_id"] = json!("req-0"))),
            "route_assignment_request_mismatch"
        );
        assert_eq!(code(delivery(&w, assignment(&w), pack(), |m| m["expires_at"] = json!(1005.0))), "route_assignment_expired");
        assert_eq!(code(delivery(&w, assignment(&w), pack(), |m| m["route_ready"] = json!(true))), "route_assignment_route_ready_invalid");
        // A pack that is not the signed one, or whose content was altered.
        let mut other = assignment(&w);
        other["stage_pack_digest"] = json!(format!("sha256:{}", "0".repeat(64)));
        assert_eq!(code(delivery(&w, other, pack(), |_| {})), "route_assignment_stage_digest_mismatch");
        let mut altered = pack();
        altered["epsilon"] = json!(0.5);
        assert_eq!(code(delivery(&w, assignment(&w), altered, |_| {})), "stage_pack_digest_mismatch");
        // The node never peers with itself, and an assignment past its expiry is refused.
        let mut own = assignment(&w);
        own["peer_endpoint_id"] = json!(w.member.endpoint_id());
        assert_eq!(code(delivery(&w, own, pack(), |_| {})), "route_assignment_peer_invalid");
        let mut old = assignment(&w);
        old["expires_at"] = json!(1001.0);
        assert_eq!(code(delivery(&w, old, pack(), |_| {})), "route_assignment_expired");
        // A "no assignment" answer cannot smuggle a pack.
        assert_eq!(code(delivery(&w, Value::Null, pack(), |_| {})), "route_assignment_delivery_invalid");
    }

    #[test]
    fn the_controller_set_is_the_seeds_and_only_the_seeds() {
        let w = world();
        let with = |ids: Value| {
            let mut a = assignment(&w);
            a["controller_endpoint_ids"] = ids;
            a
        };
        let ids = |a: &RouteAssignment| a.controller_endpoint_ids.clone();
        // A signed assignment installs exactly its set, order-independent.
        let two = json!(["c".repeat(64), "d".repeat(64)]);
        let (a, _) = check(&w, &delivery(&w, with(two.clone()), pack(), |_| {})).unwrap().unwrap();
        assert_eq!(ids(&a), vec!["c".repeat(64), "d".repeat(64)]);
        let (reordered, _) = check(&w, &delivery(&w, with(json!(["d".repeat(64), "c".repeat(64)])), pack(), |_| {})).unwrap().unwrap();
        assert_eq!(ids(&reordered), ids(&a), "the set is canonical, not ordered");
        // Absent means no controller may drive the phone at all.
        let (none, _) = check(&w, &delivery(&w, assignment(&w), pack(), |_| {})).unwrap().unwrap();
        assert!(ids(&none).is_empty());
        let (empty, _) = check(&w, &delivery(&w, with(json!([])), pack(), |_| {})).unwrap().unwrap();
        assert!(ids(&empty).is_empty());
        // A controller can never name itself, the phone, or its own route peer.
        for bad in [
            json!([w.member.endpoint_id()]),
            json!(["c".repeat(64), w.member.endpoint_id()]),
            json!([w.peer]),
            json!(["not-hex"]),
            json!(["c".repeat(63)]),
            json!([1]),
        ] {
            assert_eq!(
                check(&w, &delivery(&w, with(bad.clone()), pack(), |_| {})).err().expect("refused").0,
                "route_assignment_controller_invalid",
                "{bad}"
            );
        }
        // A duplicate, and a set past its bound.
        assert_eq!(
            check(&w, &delivery(&w, with(json!(["c".repeat(64), "c".repeat(64)])), pack(), |_| {})).err().expect("refused").0,
            "route_assignment_controller_invalid"
        );
        let many: Vec<Value> = (0..17).map(|i| json!(format!("{:0>64}", i))).collect();
        assert_eq!(
            check(&w, &delivery(&w, with(Value::Array(many)), pack(), |_| {})).err().expect("refused").0,
            "route_assignment_controller_invalid"
        );
        // The set is signed: a tampered controller list fails the signature,
        // so no controller can widen its own allow-list after the fact.
        let mut tampered = delivery(&w, with(json!(["c".repeat(64)])), pack(), |_| {});
        tampered["envelope"]["message"]["assignment"]["controller_endpoint_ids"] = json!(["e".repeat(64)]);
        assert_eq!(check(&w, &tampered).err().expect("refused").0, "membership_signature_invalid");
    }

    #[test]
    fn cancellation_regression_assignment_field_set_stays_closed() {
        let w = world();
        let mut a = assignment(&w);
        a["unknown_authority"] = json!([]);
        assert_eq!(check(&w, &delivery(&w, a, pack(), |_| {})).err().expect("unknown seventh field refused").0, "route_assignment_invalid");
    }

    #[test]
    fn the_python_seed_delivery_fixture_verifies() {
        let path = format!("{}/tests/fixtures/route_assignment.json", env!("CARGO_MANIFEST_DIR"));
        let f: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let e = &f["expected"];
        let got = verify_delivery(
            &f["delivery"],
            &Expected {
                pinned_seed_key: &f["seed_key"],
                swarm_id: e["swarm_id"].as_str().unwrap(),
                seed_node_id: e["seed_node_id"].as_str().unwrap(),
                node_id: e["node_id"].as_str().unwrap(),
                endpoint_id: e["endpoint_id"].as_str().unwrap(),
                member_key_digest: &Identity::from_secret(&"a".repeat(64), &[0x11; 32]).unwrap().verification_key_digest(),
                generation: e["generation"].as_u64().unwrap(),
                request_message_id: e["request_message_id"].as_str().unwrap(),
                now: e["now"].as_f64().unwrap(),
            },
        )
        .unwrap()
        .unwrap();
        assert_eq!(got.0.peer_endpoint_id, e["peer_endpoint_id"].as_str().unwrap());
        assert_eq!(got.0.route_generation, 7);
    }
}

#[cfg(test)]
mod v2_tests {
    use super::*;

    fn fixture(name: &str) -> Value {
        let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    fn expected(f: &Value) -> (Value, Value) {
        (f["seed_key"].clone(), f["expected"].clone())
    }

    fn verify(f: &Value, delivery: &Value) -> Result<Option<(RouteAssignment, DeliveredStage)>, RouteAssignmentError> {
        let (key, e) = expected(f);
        verify_delivery(
            delivery,
            &Expected {
                pinned_seed_key: &key,
                swarm_id: e["swarm_id"].as_str().unwrap(),
                seed_node_id: e["seed_node_id"].as_str().unwrap(),
                node_id: e["node_id"].as_str().unwrap(),
                endpoint_id: e["endpoint_id"].as_str().unwrap(),
                member_key_digest: &Identity::from_secret(&"a".repeat(64), &[0x11; 32]).unwrap().verification_key_digest(),
                generation: e["generation"].as_u64().unwrap(),
                request_message_id: e["request_message_id"].as_str().unwrap(),
                now: e["now"].as_f64().unwrap(),
            },
        )
    }

    #[test]
    fn the_python_seed_v2_delivery_yields_a_signed_manifest() {
        let f = fixture("route_assignment_v2.json");
        let (a, stage) = verify(&f, &f["delivery"]).unwrap().unwrap();
        let DeliveredStage::Qwen2 { manifest, envelope, grant } = stage else { panic!("v2 manifest") };
        assert_eq!(grant.manifest_pack_digest, manifest.pack_digest);
        assert_eq!(grant.assignment_id, a.assignment_id);
        assert_eq!(grant.grant_id, "grant-delivery-fixture-1");
        assert_eq!(a.stage_pack_digest, manifest.pack_digest);
        assert_eq!(manifest.start_layer, 0);
        assert_eq!(manifest.end_layer_exclusive, 2);
        assert_eq!(manifest.tensors.len(), 24);
        assert_eq!(envelope, f["delivery"]["stage_pack"]);
    }

    #[test]
    fn a_v2_pack_that_is_unsigned_resigned_or_swapped_is_refused() {
        let f = fixture("route_assignment_v2.json");
        let code = |d: &Value| verify(&f, d).err().expect("refused").0;
        // Manifest stripped of its seed signature.
        let mut unsigned = f["delivery"].clone();
        unsigned["stage_pack"] = f["delivery"]["stage_pack"]["message"].clone();
        assert_eq!(code(&unsigned), "route_assignment_stage_manifest_unsigned");
        // Manifest signed by a key other than the pinned seed.
        let other = Identity::from_secret("seed-endpoint-1", &[0x33; 32]).unwrap();
        let message = f["delivery"]["stage_pack"]["message"].clone();
        let mut foreign = f["delivery"].clone();
        foreign["stage_pack"] = json!({
            "protocol": membership::SIGNED_MESSAGE_PROTOCOL,
            "signature": other.sign(&message).unwrap(),
            "verification_key": other.public_key_record(),
            "message": message,
        });
        assert_eq!(code(&foreign), "stage_manifest_membership_seed_key_mismatch");
        // Tampered manifest content under the original signature.
        let mut tampered = f["delivery"].clone();
        tampered["stage_pack"]["message"]["golden"]["seed"] = json!(43);
        assert_eq!(code(&tampered), "stage_manifest_membership_signature_invalid");
    }

    #[test]
    fn a_v2_delivery_needs_its_seed_grant_bound_to_this_node() {
        let f = fixture("route_assignment_v2.json");
        let code = |d: &Value| verify(&f, d).err().expect("refused").0;
        let mut missing = f["delivery"].clone();
        missing.as_object_mut().unwrap().remove("artifact_grant");
        assert_eq!(code(&missing), "route_assignment_artifact_grant_missing");
        let mut tampered = f["delivery"].clone();
        tampered["artifact_grant"]["message"]["maximum_chunk_bytes"] = json!(1);
        assert_eq!(code(&tampered), "artifact_grant_signature_invalid");
        // A grant for another node, re-signed by the real seed key, is still refused.
        let seed = Identity::from_secret("seed-endpoint-1", &[0x22; 32]).unwrap();
        let mut message = f["delivery"]["artifact_grant"]["message"].clone();
        message["recipient_node_id"] = json!("node-other");
        let mut foreign = f["delivery"].clone();
        foreign["artifact_grant"] = json!({
            "protocol": membership::SIGNED_MESSAGE_PROTOCOL,
            "signature": seed.sign(&message).unwrap(),
            "verification_key": seed.public_key_record(),
            "message": message,
        });
        assert_eq!(foreign["artifact_grant"]["verification_key"], f["seed_key"]);
        assert_eq!(code(&foreign), "artifact_grant_recipient_mismatch");
        // A v1 pack never carries a grant.
        let v1 = fixture("route_assignment.json");
        let mut smuggled = v1["delivery"].clone();
        smuggled["artifact_grant"] = f["delivery"]["artifact_grant"].clone();
        assert_eq!(verify(&v1, &smuggled).err().unwrap().0, "route_assignment_delivery_invalid");
    }
}
