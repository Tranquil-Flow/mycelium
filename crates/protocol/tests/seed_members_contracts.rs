// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `SeedCoordinator` member paths replayed against scenarios recorded by executing the oracle's real
//! coordinator over a scratch database with a settable clock (`round9-seed-members-probe.py`, scratch keys):
//! `receive_member_message` (heartbeat and the five other member protocols), `lease_renewal`,
//! `assignment_offer`, `advance_member_generation`, `member_authority_guard`, the liveness projection, the
//! resume retry shortcut, the revoked-resume branch and join envelopes whose key record carries a
//! malformed digest. Every step compares the exact refusal code or the exact resulting member projection
//! (and the ids the id source was asked for); a refusal must leave the projection unchanged.
use protocol::identity::Identity;
use protocol::seed_coordinator::{SeedCoordinatorError, SeedFrame, evaluate_join, evaluate_resume};
use protocol::seed_members::{
    Coordinator, Offer, SeedTable, Timing, advance_member_generation, assignment_offer,
    member_authority_guard, receive_member_message,
};
use protocol::{TimeLimits, membership};
use serde_json::{Value, json};

const DIG: &str = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

fn generated_envelope(spec: &Value, v: &Value, index: usize) -> Value {
    let key = spec["key"].as_str().unwrap();
    let byte = u8::try_from(v["member_keys"][key]["byte"].as_u64().unwrap()).unwrap();
    let endpoint = v["member_keys"][key]["endpoint"].as_str().unwrap();
    let identity = Identity::from_secret(endpoint, &[byte; 32]).unwrap();
    let message = json!({
        "protocol": "mycelium.membership.capability_report.v1",
        "message_id": spec["id_format"].as_str().unwrap().replace("{i}", &index.to_string()),
        "swarm_id": v["swarm_id"], "sender_node_id": spec["node"], "sender_endpoint_id": endpoint,
        "recipient_node_id": v["seed_node_id"], "incarnation": "boot-1", "generation": 1,
        "issued_at": spec["issued_at"], "expires_at": spec["expires_at"],
        "platform": "macOS", "architecture": "arm64", "memory_bytes": 1u64 << 30,
        "available_storage_bytes": 1u64 << 31, "backends": ["mlx"], "precisions": ["fp16"],
    });
    json!({
        "protocol": membership::SIGNED_MESSAGE_PROTOCOL,
        "message": message,
        "signature": identity.sign(&message).unwrap(),
        "verification_key": identity.public_key_record(),
    })
}

fn code_of<T>(got: Result<T, SeedCoordinatorError>) -> Result<(), &'static str> {
    got.map(|_| ()).map_err(|e| e.0)
}

fn run(select: &dyn Fn(&str) -> bool) -> (usize, usize, usize, usize) {
    let v: Value =
        serde_json::from_str(include_str!("../fixtures/round9-seed-members-vectors.json")).unwrap();
    let limits = TimeLimits {
        max_ttl_seconds: v["max_message_ttl_seconds"].as_f64().unwrap(),
        clock_skew_seconds: 30.0,
    };
    let (mut steps, mut ok, mut refused, mut accidents) = (0usize, 0usize, 0usize, 0usize);
    for scenario in v["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        if !select(name) {
            continue;
        }
        let cfg = &scenario["config"];
        let timing = Timing {
            keepalive_interval_seconds: cfg["keepalive_interval_seconds"].as_f64().unwrap(),
            evidence_freshness_seconds: cfg["evidence_freshness_seconds"].as_f64().unwrap(),
        };
        let frame = SeedFrame {
            swarm_id: v["swarm_id"].as_str().unwrap(),
            seed_node_id: v["seed_node_id"].as_str().unwrap(),
            seed_url: v["seed_url"].as_str(),
            seed_key_record: &v["seed_key_record"],
            limits,
            lease_seconds: cfg["lease_seconds"].as_f64().unwrap(),
            message_ttl_seconds: cfg["message_ttl_seconds"].as_f64().unwrap(),
        };
        let coord = Coordinator {
            frame,
            timing,
            seed_endpoint_id: v["seed_endpoint_id"].as_str().unwrap(),
            seed_incarnation: v["seed_incarnation"].as_str().unwrap(),
        };
        let frame = &coord.frame;
        let seed_digest = frame.seed_key_record["verification_key_digest"]
            .as_str()
            .unwrap()
            .to_owned();
        let mut next_id = 0usize;
        let mut table = SeedTable::new(Box::new(move || {
            next_id += 1;
            format!("seed-msg-{}", next_id - 1)
        }));
        for (index, step) in scenario["steps"].as_array().unwrap().iter().enumerate() {
            let at = format!("{name}#{index} {}", step["op"]);
            let now = step["now"].as_f64().unwrap();
            let want = &step["result"];
            if step["op"] == "generate" {
                let spec = &step["spec"];
                for i in 0..spec["count"].as_u64().unwrap() as usize {
                    let envelope = generated_envelope(spec, &v, i);
                    receive_member_message(
                        &mut table,
                        &coord,
                        &envelope,
                        "mycelium.membership.capability_report.v1",
                        now,
                    )
                    .unwrap_or_else(|e| panic!("{at}: generated message {i} refused with {e}"));
                }
                assert_eq!(
                    table.projection(spec["node"].as_str().unwrap(), now, &timing),
                    Some(want["member"].clone()),
                    "{at}"
                );
                continue;
            }
            steps += 1;
            let node = step["node"].as_str();
            let mut returned: Option<Value> = None;
            let got: Result<(), &'static str> = match step["op"].as_str().unwrap() {
                "join" => {
                    let token = step["token"].as_str().unwrap();
                    let envelope = &step["envelope"];
                    match table.replays_join(token, envelope) {
                        Err(e) => Err(e.0),
                        Ok(true) => Ok(()),
                        Ok(false) => code_of(
                            evaluate_join(frame, &table.rows(), token, envelope, now).and_then(
                                |admission| {
                                    table.admit_join(token, envelope, &admission, now, &timing)
                                },
                            ),
                        ),
                    }
                }
                "resume" => {
                    let envelope = &step["envelope"];
                    match table.replays_resume(envelope) {
                        Err(e) => Err(e.0),
                        Ok(true) => Ok(()),
                        Ok(false) => code_of(
                            evaluate_resume(frame, &table.rows(), envelope, now).and_then(
                                |admission| table.admit_resume(envelope, &admission, now, &timing),
                            ),
                        ),
                    }
                }
                "message" => {
                    let got = receive_member_message(
                        &mut table,
                        &coord,
                        &step["envelope"],
                        step["expected_protocol"].as_str().unwrap(),
                        now,
                    );
                    if let Ok(message) = &got {
                        assert_eq!(message["message_id"], want["message_id"], "{at}");
                    }
                    code_of(got)
                }
                "renewal" => {
                    let got = table.renewal(
                        node.unwrap(),
                        step["heartbeat_message_id"].as_str().unwrap(),
                    );
                    if let Ok(renewal) = &got {
                        for (key, expected) in want["renewal"].as_object().unwrap() {
                            assert_eq!(&renewal[key], expected, "{at}: renewal.{key}");
                        }
                    }
                    code_of(got)
                }
                "advance" => match step["expected_generation"].as_u64() {
                    None => Err("accident_not_an_integer"),
                    Some(generation) => {
                        let got = advance_member_generation(
                            &mut table,
                            node.unwrap(),
                            generation,
                            step["lifecycle_state"].as_str().unwrap(),
                            now,
                            &timing,
                        );
                        returned = got.as_ref().ok().cloned();
                        code_of(got)
                    }
                },
                "guard" => {
                    let states: Vec<&str> = step["eligible_lifecycle_states"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|s| s.as_str().unwrap())
                        .collect();
                    let got = member_authority_guard(
                        &table,
                        node.unwrap(),
                        step["expected_generation"].as_u64().unwrap(),
                        step["expected_peer_class"].as_str().unwrap(),
                        &states,
                        now,
                        &timing,
                    );
                    returned = got.as_ref().ok().cloned();
                    code_of(got)
                }
                "offer" => match step["peers"].as_array() {
                    None => Err("accident_peers_not_a_sequence"),
                    Some(peers) => {
                        let peers: Vec<String> = peers
                            .iter()
                            .map(|p| p.as_str().unwrap().to_owned())
                            .collect();
                        let got = assignment_offer(
                            &mut table,
                            &coord,
                            &Offer {
                                node_id: node.unwrap(),
                                deployment_id: step["deployment_id"].as_str().unwrap(),
                                deployment_epoch: step["deployment_epoch"].as_u64().unwrap(),
                                assignment_id: step["assignment_id"].as_str().unwrap(),
                                assignment_digest: DIG,
                                stage_pack_digest: DIG,
                                graph_digest: DIG,
                                load_generation: 1,
                                peer_node_ids: &peers,
                                placement_provenance: step["placement_provenance"]
                                    .as_str()
                                    .unwrap(),
                            },
                            now,
                        );
                        if let Ok(message) = &got {
                            assert_eq!(message, &want["offer"], "{at}: offer message");
                        }
                        code_of(got)
                    }
                },
                "rotation" => {
                    let got = table.begin_authority_rotation(&step["transition"], &seed_digest);
                    if let Ok(digest) = &got {
                        assert_eq!(digest, step["transition_digest"].as_str().unwrap(), "{at}");
                    }
                    code_of(got)
                }
                "member" => Ok(()),
                other => panic!("{at}: unknown op {other}"),
            };
            if want["ok"] == true {
                assert_eq!(got, Ok(()), "{at}: oracle admitted");
                ok += 1;
            } else if let Some(code) = want["code"].as_str() {
                assert_eq!(got, Err(code), "{at}");
                refused += 1;
            } else {
                assert!(
                    got.is_err(),
                    "{at}: oracle crashed ({}); v0.2 must refuse",
                    want["python_exception"]
                );
                accidents += 1;
            }
            if matches!(step["op"].as_str(), Some("advance" | "guard")) && want["ok"] == true {
                assert_eq!(
                    returned.as_ref(),
                    Some(&want["projection"]),
                    "{at}: projection"
                );
            }
            let drawn = table.take_drawn();
            assert_eq!(
                json!(drawn),
                want["ids"],
                "{at}: ids asked of the id source"
            );
            if let Some(node) = node {
                assert_eq!(
                    table.projection(node, now, &timing),
                    if want["member"].is_null() {
                        None
                    } else {
                        Some(want["member"].clone())
                    },
                    "{at}: member projection"
                );
            }
        }
    }
    (steps, ok, refused, accidents)
}

#[test]
fn member_paths_match_the_executed_coordinator() {
    // 216 steps (plus the 4,096-message replay-window fill): 110 admitted, 95 exact-code refusals,
    // 11 Python accidents refused.
    assert_eq!(
        run(&|name| name != "join_malformed_key_digest"),
        (216, 110, 95, 11)
    );
}

#[test]
fn join_envelopes_with_a_malformed_key_digest_match_the_executed_coordinator() {
    // 18 refusals (key-pin-invalid vs verifier-invalid ordering) and one valid control.
    assert_eq!(
        run(&|name| name == "join_malformed_key_digest"),
        (19, 1, 18, 0)
    );
}
