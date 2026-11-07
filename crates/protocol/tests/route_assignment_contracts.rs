// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `mycelium_seed.route_assignment.RouteAssignmentService.answer` (the seed's answer to a member's signed
//! route-assignment request) replayed against scenarios recorded by executing the oracle over a real
//! coordinator database with a settable clock (`round9-route-assignment-probe.py`, scratch keys): the exact
//! refusal code or the exact delivery statement, the delivery / applied bookkeeping of the assignment row, and
//! the ids the delivery id source was asked for. v1-style packs only: the v2 artifact grant is not covered.
use protocol::identity::Identity;
use protocol::route_assignment::{RouteRow, RouteService, RouteTable, answer};
use protocol::seed_coordinator::{SeedCoordinatorError, SeedFrame, evaluate_join};
use protocol::seed_members::{SeedTable, Timing, advance_member_generation};
use protocol::{TimeLimits, membership};
use serde_json::{Value, json};

const REQUEST_PROTOCOL: &str = "mycelium.seed.route_assignment_request.v1";

fn generated_request(spec: &Value, v: &Value, index: usize) -> Value {
    let key = spec["key"].as_str().unwrap();
    let byte = u8::try_from(v["keys"][key]["byte"].as_u64().unwrap()).unwrap();
    let endpoint = v["keys"][key]["endpoint"].as_str().unwrap();
    let identity = Identity::from_secret(endpoint, &[byte; 32]).unwrap();
    let message = json!({
        "protocol": REQUEST_PROTOCOL,
        "message_id": spec["id_format"].as_str().unwrap().replace("{i}", &index.to_string()),
        "swarm_id": v["swarm_id"], "sender_node_id": spec["node"], "sender_endpoint_id": endpoint,
        "recipient_node_id": v["seed_node_id"], "incarnation": "boot-1", "generation": 1,
        "issued_at": spec["issued_at"], "expires_at": spec["expires_at"], "applied_assignment_id": null,
    });
    json!({
        "protocol": membership::SIGNED_MESSAGE_PROTOCOL,
        "message": message,
        "signature": identity.sign(&message).unwrap(),
        "verification_key": identity.public_key_record(),
    })
}

fn row_of(spec: &Value) -> RouteRow {
    RouteRow {
        node_id: spec["node_id"].as_str().unwrap().to_owned(),
        assignment_id: spec["assignment_id"].as_str().unwrap().to_owned(),
        endpoint_id: spec["endpoint_id"].as_str().unwrap().to_owned(),
        verification_key_digest: spec["verification_key_digest"].as_str().unwrap().to_owned(),
        stage_pack: serde_json::from_str(spec["stage_pack_json"].as_str().unwrap()).unwrap(),
        stage_pack_digest: spec["stage_pack_digest"].as_str().unwrap().to_owned(),
        peer_endpoint_id: spec["peer_endpoint_id"].as_str().unwrap().to_owned(),
        route_generation: spec["route_generation"].as_u64().unwrap(),
        forward_to: spec["forward_to"] == 1,
        issued_at: spec["issued_at"].as_f64().unwrap(),
        expires_at: spec["expires_at"].as_f64().unwrap(),
        controller_endpoint_ids_json: spec["controller_endpoint_ids_json"]
            .as_str()
            .unwrap()
            .to_owned(),
        withdrawn: false,
        delivered_at: None,
        delivered_generation: None,
        applied_at: None,
        applied_generation: None,
        artifact_sources: None,
    }
}

fn code_of<T>(got: Result<T, SeedCoordinatorError>) -> Result<(), &'static str> {
    got.map(|_| ()).map_err(|e| e.0)
}

#[test]
fn route_assignment_answers_match_the_executed_service() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round9-route-assignment-vectors.json"
    ))
    .unwrap();
    let limits = TimeLimits {
        max_ttl_seconds: 3600.0,
        clock_skew_seconds: 30.0,
    };
    let timing = Timing {
        keepalive_interval_seconds: 30.0,
        evidence_freshness_seconds: 90.0,
    };
    let (mut steps, mut ok, mut refused) = (0usize, 0usize, 0usize);
    for scenario in v["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        let frame = SeedFrame {
            swarm_id: v["swarm_id"].as_str().unwrap(),
            seed_node_id: v["seed_node_id"].as_str().unwrap(),
            seed_url: v["seed_url"].as_str(),
            seed_key_record: &v["seed_key_record"],
            limits,
            lease_seconds: 300.0,
            message_ttl_seconds: 60.0,
        };
        let service = RouteService {
            swarm_id: v["swarm_id"].as_str().unwrap(),
            seed_node_id: v["seed_node_id"].as_str().unwrap(),
            seed_endpoint_id: v["seed_endpoint_id"].as_str().unwrap(),
            incarnation: v["seed_incarnation"].as_str().unwrap(),
        };
        let mut coordinator_ids = 0usize;
        let mut table = SeedTable::new(Box::new(move || {
            coordinator_ids += 1;
            format!("seed-msg-{}", coordinator_ids - 1)
        }));
        let mut routes = RouteTable::default();
        let mut delivery_ids = 0usize;
        for (index, step) in scenario["steps"].as_array().unwrap().iter().enumerate() {
            let at = format!("{name}#{index} {} {}", step["op"], step["label"]);
            let now = step["now"].as_f64().unwrap();
            let want = &step["result"];
            let node = step["node"].as_str();
            let mut drawn: Vec<String> = Vec::new();
            if step["op"] == "generate" {
                let spec = &step["spec"];
                for i in 0..spec["count"].as_u64().unwrap() as usize {
                    let request = generated_request(spec, &v, i);
                    answer(
                        &mut table,
                        &mut routes,
                        &service,
                        &request,
                        now,
                        &mut || String::from("unused"),
                    )
                    .unwrap_or_else(|e| panic!("{at}: generated request {i} refused with {e}"));
                }
                assert_eq!(
                    routes.row_state(spec["node"].as_str().unwrap()),
                    Some(want["row"].clone()),
                    "{at}"
                );
                continue;
            }
            steps += 1;
            let mut statement: Option<Value> = None;
            let got: Result<(), &'static str> = match step["op"].as_str().unwrap() {
                "join" => {
                    let (token, envelope) = (step["token"].as_str().unwrap(), &step["envelope"]);
                    match table.replays_join(token, envelope) {
                        Err(e) => Err(e.0),
                        Ok(true) => Ok(()),
                        Ok(false) => code_of(
                            evaluate_join(&frame, &table.rows(), token, envelope, now)
                                .and_then(|a| table.admit_join(token, envelope, &a, now, &timing)),
                        ),
                    }
                }
                "assign" => {
                    routes.insert_replace(row_of(&step["row"]));
                    Ok(())
                }
                "withdraw" => code_of(routes.withdraw(node.unwrap())),
                "advance" => code_of(advance_member_generation(
                    &mut table,
                    node.unwrap(),
                    step["expected_generation"].as_u64().unwrap(),
                    step["lifecycle_state"].as_str().unwrap(),
                    now,
                    &timing,
                )),
                "request" => {
                    let mut next = delivery_ids;
                    let got = answer(
                        &mut table,
                        &mut routes,
                        &service,
                        &step["envelope"],
                        now,
                        &mut || {
                            let id = format!("route-delivery-{next}");
                            drawn.push(id.clone());
                            next += 1;
                            id
                        },
                    );
                    delivery_ids = next;
                    if let Ok(delivery) = &got {
                        statement = Some(delivery.clone());
                    }
                    code_of(got)
                }
                other => panic!("{at}: unknown op {other}"),
            };
            if want["ok"] == true {
                assert_eq!(got, Ok(()), "{at}");
                ok += 1;
            } else if let Some(code) = want["code"].as_str() {
                assert_eq!(got, Err(code), "{at}");
                refused += 1;
            } else {
                assert!(got.is_err(), "{at}: oracle crashed; v0.2 must refuse");
                refused += 1;
            }
            if let Some(delivery) = &statement {
                assert_eq!(delivery["statement"], want["statement"], "{at}: statement");
                assert_eq!(
                    delivery["stage_pack"], want["stage_pack"],
                    "{at}: stage_pack"
                );
            }
            if step["op"] == "request" {
                assert_eq!(json!(drawn), want["ids"], "{at}: delivery ids");
            }
            if let Some(node) = node {
                assert_eq!(
                    routes.row_state(node),
                    if want["row"].is_null() {
                        None
                    } else {
                        Some(want["row"].clone())
                    },
                    "{at}: assignment row"
                );
            }
        }
    }
    assert_eq!((steps, ok, refused), (281, 61, 220));
}
