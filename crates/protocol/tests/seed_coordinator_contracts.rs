// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `SeedCoordinator.accept_join` / `accept_resume` relation decisions (identity reuse, key and
//! endpoint conflict, join mismatch, resume identity / generation) and `_bounded_deadline`, replayed
//! against scenario sequences recorded by executing the oracle's real coordinator over a scratch
//! database (`round8-seed-coordinator-probe.py`, scratch keys).
use protocol::TimeLimits;
use protocol::seed_coordinator::{
    Admission, Member, SeedCoordinatorError, SeedFrame, bounded_deadline, evaluate_join,
    evaluate_resume,
};
use serde_json::{Value, json};

fn member_of(view: &Value) -> Member {
    Member {
        node_id: view["node_id"].as_str().unwrap().to_owned(),
        endpoint_id: view["endpoint_id"].as_str().unwrap().to_owned(),
        endpoint_addrs: view["endpoint_addrs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a.as_str().unwrap().to_owned())
            .collect(),
        peer_class: view["peer_class"].as_str().unwrap().to_owned(),
        runtime_capability: view["runtime_capability"].clone(),
        verification_key_digest: view["verification_key_digest"].as_str().unwrap().to_owned(),
        incarnation: view["incarnation"].as_str().unwrap().to_owned(),
        generation: view["generation"].as_u64().unwrap(),
        lease_expires_at: view["lease_expires_at"].as_f64().unwrap(),
        lifecycle_state: view["lifecycle_state"].as_str().unwrap().to_owned(),
    }
}

fn view_of(member: &Member) -> Value {
    json!({
        "node_id": member.node_id,
        "endpoint_id": member.endpoint_id,
        "endpoint_addrs": member.endpoint_addrs,
        "peer_class": member.peer_class,
        "runtime_capability": member.runtime_capability,
        "verification_key_digest": member.verification_key_digest,
        "incarnation": member.incarnation,
        "generation": member.generation,
        "lease_expires_at": member.lease_expires_at,
        "lifecycle_state": member.lifecycle_state,
    })
}

#[test]
fn join_and_resume_decisions_match_the_executed_coordinator() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round8-seed-coordinator-vectors.json"
    ))
    .unwrap();
    let now = v["now"].as_f64().unwrap();
    let limits = TimeLimits {
        max_ttl_seconds: v["max_message_ttl_seconds"].as_f64().unwrap(),
        clock_skew_seconds: 0.0,
    };
    let (mut steps, mut admitted, mut refused) = (0, 0, 0);
    for scenario in v["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        let seed_url = scenario["config"]["seed_url"].as_str();
        let frame = SeedFrame {
            swarm_id: v["swarm_id"].as_str().unwrap(),
            seed_node_id: v["seed_node_id"].as_str().unwrap(),
            seed_url,
            seed_key_record: &v["seed_key_record"],
            limits,
            lease_seconds: scenario["config"]["lease_seconds"].as_f64().unwrap(),
            message_ttl_seconds: scenario["config"]["message_ttl_seconds"].as_f64().unwrap(),
        };
        let mut members: Vec<Member> = Vec::new();
        for (index, step) in scenario["steps"].as_array().unwrap().iter().enumerate() {
            let at = format!("{name}#{index}");
            let got: Result<Admission, SeedCoordinatorError> = match step["op"].as_str().unwrap() {
                "join" => evaluate_join(
                    &frame,
                    &members,
                    step["token"].as_str().unwrap(),
                    &step["envelope"],
                    now,
                ),
                _ => evaluate_resume(&frame, &members, &step["envelope"], now),
            };
            let want = &step["result"];
            steps += 1;
            if want["ok"] == true {
                let admission = got.unwrap_or_else(|e| panic!("{at}: refused with {e}"));
                for (key, expected) in want["acceptance"].as_object().unwrap() {
                    assert_eq!(
                        &admission.acceptance[key], expected,
                        "{at}: acceptance.{key}"
                    );
                }
                assert_eq!(view_of(&admission.member), want["member"], "{at}");
                assert_eq!(member_of(&want["member"]), admission.member, "{at}");
                members.retain(|m| m.node_id != admission.member.node_id);
                members.push(admission.member);
                admitted += 1;
            } else if let Some(code) = want["code"].as_str() {
                assert_eq!(got.map(|_| ()).map_err(|e| e.0), Err(code), "{at}");
                refused += 1;
            } else {
                assert!(got.is_err(), "{at}: oracle crashed; v0.2 must refuse");
                refused += 1;
            }
        }
    }
    assert_eq!((steps, admitted, refused), (75, 41, 34));
}

#[test]
fn bounded_deadlines_match_the_executed_oracle() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round8-seed-coordinator-vectors.json"
    ))
    .unwrap();
    let cases = v["deadlines"].as_array().unwrap();
    for case in cases {
        let got = bounded_deadline(
            case["issued_at"].as_f64().unwrap(),
            case["ttl_seconds"].as_f64().unwrap(),
            case["previous_deadline"].as_f64(),
            case["reserve_headroom"].as_bool().unwrap(),
            v["max_message_ttl_seconds"].as_f64().unwrap(),
        );
        if case["ok"] == true {
            assert_eq!(got, Ok(case["deadline"].as_f64().unwrap()), "{case}");
        } else {
            assert_eq!(
                got.map_err(|e| e.0),
                Err(case["code"].as_str().unwrap()),
                "{case}"
            );
        }
    }
    assert_eq!(cases.len(), 23);
}
