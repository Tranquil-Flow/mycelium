// SPDX-License-Identifier: AGPL-3.0-only
use planner::discovery::MemberIdentity;
use serde_json::Value;
fn ids(value: &Value) -> Vec<MemberIdentity> {
    value
        .as_array()
        .unwrap()
        .iter()
        .map(|m| MemberIdentity {
            node_id: m["node_id"].as_str().unwrap().into(),
            generation: m["generation"].as_u64().unwrap(),
            incarnation: m["incarnation"].as_str().unwrap().into(),
            endpoint_id: m["endpoint_id"].as_str().unwrap().into(),
            verification_key_digest: m["verification_key_digest"].as_str().unwrap().into(),
        })
        .collect()
}
#[test]
fn member_set_and_all_four_identity_fields_match_actual_python_gate() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/rollover-members.json")).unwrap();
    for c in cases.as_array().unwrap() {
        let result = planner::rollover::check_members(&ids(&c["previous"]), &ids(&c["current"]));
        let actual = result.err().map_or("ok", |e| e.code);
        assert_eq!(actual, c["expected"], "{}", c["name"]);
    }
}
#[test]
fn duplicate_current_nodes_do_not_hide_removed_participants() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/rollover-members.json")).unwrap();
    let previous = ids(&cases[0]["previous"]);
    let current = vec![previous[0].clone(), previous[0].clone()];
    assert_eq!(
        planner::rollover::check_members(&previous, &current)
            .unwrap_err()
            .code,
        "rollover_members_changed"
    );
}
