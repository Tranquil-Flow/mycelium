// SPDX-License-Identifier: AGPL-3.0-only
//! The member rows of `ProductProjector._build` (`mycelium_product_spine/projector.py:690-814`
//! @ f4e366e9). The lease is `int(float(member.get("lease_expires_at", 0)) * 1_000)` (`:696`) and a
//! member is fresh iff that is `> now_unix_ms` (`:697`). Every expected value below is derived by hand
//! from Python `float()`/`int()` semantics and the cited lines; the pseudonyms are
//! `"device-" + HMAC-SHA256(salt, node_id).hexdigest()[:20]` (`:300-306`) for the salt `bytes(range(32))`,
//! computed with `openssl dgst -sha256 -mac HMAC`.
use api::{
    product_projector::{MemberProjection, ProductProjector, ProjectionError},
    schema::Raised,
};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};

const NODE_A: &str = "device-79b90eb7f434ad4e3191";
const NODE_B: &str = "device-fb6720de2ed549f06291";
const NODE_C: &str = "device-e0d945791eb7c1e35584";

fn projector() -> ProductProjector {
    let salt: Vec<u8> = (0u8..32).collect();
    ProductProjector::new(&salt).expect("a 32-byte salt is valid")
}

fn rows(members: Value) -> Vec<Map<String, Value>> {
    match members {
        Value::Array(items) => items
            .into_iter()
            .map(|item| match item {
                Value::Object(map) => map,
                other => panic!("member row must be an object: {other}"),
            })
            .collect(),
        other => panic!("members must be a list: {other}"),
    }
}

fn project(
    members: Value,
    stages: Value,
    now_unix_ms: u64,
) -> Result<MemberProjection, ProjectionError> {
    let stages = match stages {
        Value::Array(items) => items,
        other => panic!("stages must be a list: {other}"),
    };
    projector().project_members(&rows(members), &stages, now_unix_ms)
}

/// `_binding()` (`:100-113`): every field `None`.
fn binding() -> Value {
    json!({
        "deployment_id": null,
        "deployment_epoch": null,
        "route_id": null,
        "route_generation": null,
        "topology_version": null,
    })
}

/// The device entity of `:722-759`.
fn device(entity_id: &str, label: &str, freshness: Value, attributes: Value) -> Value {
    json!({
        "entity_id": entity_id,
        "kind": "device",
        "label": label,
        "source_id": "membership-source",
        "binding": binding(),
        "freshness": freshness,
        "attributes": attributes,
    })
}

/// The membership readiness row of `:761-776`.
fn readiness(scope_id: &str, state: &str, reason_code: Value) -> Value {
    json!({
        "scope_id": scope_id,
        "dimension": "membership",
        "state": state,
        "reason_code": reason_code,
        "source_id": "membership-source",
    })
}

/// The `member_without_placement` notice of `:804-813`.
fn unplaced(scope_id: &str) -> Value {
    json!({
        "notice_id": format!("unplaced-{scope_id}"),
        "scope_id": scope_id,
        "severity": "info",
        "code": "member_without_placement",
        "source_id": "membership-source",
    })
}

fn raised(class: &'static str, message: &str) -> ProjectionError {
    ProjectionError {
        raised: Raised::Builtin(class),
        message: message.to_owned(),
    }
}

fn ids(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(raw, public)| ((*raw).to_owned(), (*public).to_owned()))
        .collect()
}

fn set(raw: &[&str]) -> BTreeSet<String> {
    raw.iter().map(|id| (*id).to_owned()).collect()
}

#[test]
fn product_projector_members_missing_lease_is_expired() {
    // No `lease_expires_at`: `.get(..., 0)` -> int(float(0) * 1000) = 0, and 0 > now is false for every
    // now >= 0 (equality at now 0 included). last_liveness_at 5.5 -> int(5500.0) = 5500.
    let members = json!([{
        "node_id": "node-a",
        "peer_class": "macos_arm64",
        "generation": 3,
        "authority_generation": 2,
        "incarnation": "inc-a",
        "lifecycle_state": "RUNNING",
        "activation_eligible": true,
        "last_liveness_at": 5.5,
        "runtime_capability": {
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.native_activation.v1",
        },
    }]);
    let expected = MemberProjection {
        entities: vec![device(
            NODE_A,
            "Native inference device",
            json!({"status": "stale", "observed_at_unix_ms": 5500, "valid_until_unix_ms": 5500}),
            json!({
                "peer_class": "macos_arm64",
                "membership_generation": 3,
                "authority_generation": 2,
                "incarnation": "inc-a",
                "lifecycle": "running",
                "lease_freshness": "expired",
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.native_activation.v1",
                "activation_eligible": false,
                "placement_id": null,
            }),
        )],
        readiness: vec![readiness(
            NODE_A,
            "not_ready",
            json!("membership_lease_expired"),
        )],
        notices: vec![unplaced(NODE_A)],
        public_member_ids: ids(&[("node-a", NODE_A)]),
        current_raw_members: set(&[]),
        eligible_raw_members: set(&[]),
    };
    assert_eq!(
        project(members.clone(), json!([]), 1_000),
        Ok(expected.clone())
    );
    assert_eq!(project(members, json!([]), 0), Ok(expected));
}

#[test]
fn product_projector_members_null_lease_fails() {
    // An explicit None is not the missing-key default: float(None) raises TypeError (no `or 0`).
    let error = raised(
        "TypeError",
        "float() argument must be a string or a real number, not 'NoneType'",
    );
    let null_lease = json!({
        "node_id": "node-a",
        "lease_expires_at": null,
        "lifecycle_state": "RUNNING",
        "activation_eligible": true,
    });
    assert_eq!(
        project(json!([null_lease]), json!([]), 1_000),
        Err(error.clone())
    );
    // A valid member before it does not save the projection.
    let fresh = json!({"node_id": "node-b", "lease_expires_at": 2.0});
    assert_eq!(
        project(json!([fresh, null_lease]), json!([]), 1_000),
        Err(error)
    );
    // The node_id filter (`:691-693`) runs first: a row without a usable node id never reads its lease.
    let unnamed = json!([
        {"lease_expires_at": null},
        {"node_id": "", "lease_expires_at": null},
        {"node_id": 7, "lease_expires_at": null},
    ]);
    assert_eq!(
        project(unnamed, json!([]), 1_000),
        Ok(MemberProjection::default())
    );
}

#[test]
fn product_projector_members_empty_string_lease_fails() {
    // float("") and float(" ") raise ValueError with the repr of the original text.
    for (text, message) in [
        ("", "could not convert string to float: ''"),
        (" ", "could not convert string to float: ' '"),
    ] {
        let members = json!([{"node_id": "node-a", "lease_expires_at": text}]);
        assert_eq!(
            project(members, json!([]), 1_000),
            Err(raised("ValueError", message)),
            "lease {text:?}"
        );
    }
}

#[test]
fn product_projector_members_numeric_text_lease_accepted() {
    // float(" 12.5 ") = 12.5 -> 12500 > 12000: fresh. float("12") = 12.0 -> observed 12000.
    let members = json!([{
        "node_id": "node-a",
        "lease_expires_at": " 12.5 ",
        "last_liveness_at": "12",
        "peer_class": "android_termux_iroh",
        "generation": 1,
        "authority_generation": 1,
        "incarnation": "inc-a",
        "lifecycle_state": "configured",
        "activation_eligible": true,
        "runtime_capability": {"runtime_backend": "termux", "transport": "iroh"},
    }]);
    let expected = MemberProjection {
        entities: vec![device(
            NODE_A,
            "Mobile conformance device",
            json!({"status": "current", "observed_at_unix_ms": 12000, "valid_until_unix_ms": 12500}),
            json!({
                "peer_class": "android_termux_iroh",
                "membership_generation": 1,
                "authority_generation": 1,
                "incarnation": "inc-a",
                "lifecycle": "configured",
                "lease_freshness": "fresh",
                "runtime_backend": "termux",
                "transport": "iroh",
                "activation_protocol": null,
                "activation_eligible": true,
                "placement_id": null,
            }),
        )],
        readiness: vec![readiness(NODE_A, "ready", Value::Null)],
        notices: vec![unplaced(NODE_A)],
        public_member_ids: ids(&[("node-a", NODE_A)]),
        current_raw_members: set(&["node-a"]),
        eligible_raw_members: set(&["node-a"]),
    };
    assert_eq!(project(members, json!([]), 12_000), Ok(expected));
    // Exponent and digit-group text as float() reads them: "1e1" = 10.0 -> 10000 ms and
    // "1_000" = 1000.0 -> 1000000 ms; fresh one millisecond before, expired at equality.
    for (text, lease_ms) in [("1e1", 10_000_u64), ("1_000", 1_000_000)] {
        for (now, freshness) in [(lease_ms - 1, "fresh"), (lease_ms, "expired")] {
            let members = json!([{"node_id": "node-a", "lease_expires_at": text}]);
            let projection = project(members, json!([]), now).expect("numeric text is accepted");
            let entity = &projection.entities[0];
            assert_eq!(
                (
                    &entity["attributes"]["lease_freshness"],
                    &entity["freshness"]["valid_until_unix_ms"],
                ),
                (&json!(freshness), &json!(lease_ms)),
                "lease {text:?} at now {now}"
            );
        }
    }
}

#[test]
fn product_projector_members_truncation_and_equality() {
    // 10.0009 s * 1000 = 10000.9 and int() truncates to 10000: not > 10000 (expired), > 9999 (fresh).
    // Defaults: observed int(float(0) * 1000) = 0, peer_class "linux_tbd", generations 1,
    // incarnation "unknown", runtime {} -> backend "tbd", transport "none", protocol None.
    let members = json!([{
        "node_id": "node-a",
        "lease_expires_at": 10.0009,
        "lifecycle_state": "running",
        "activation_eligible": true,
    }]);
    let attributes = |lease_freshness: &str, eligible: bool| {
        json!({
            "peer_class": "linux_tbd",
            "membership_generation": 1,
            "authority_generation": 1,
            "incarnation": "unknown",
            "lifecycle": "running",
            "lease_freshness": lease_freshness,
            "runtime_backend": "tbd",
            "transport": "none",
            "activation_protocol": null,
            "activation_eligible": eligible,
            "placement_id": null,
        })
    };
    let expired = MemberProjection {
        entities: vec![device(
            NODE_A,
            "Native inference device",
            json!({"status": "stale", "observed_at_unix_ms": 0, "valid_until_unix_ms": 10000}),
            attributes("expired", false),
        )],
        readiness: vec![readiness(
            NODE_A,
            "not_ready",
            json!("membership_lease_expired"),
        )],
        notices: vec![unplaced(NODE_A)],
        public_member_ids: ids(&[("node-a", NODE_A)]),
        current_raw_members: set(&[]),
        eligible_raw_members: set(&[]),
    };
    assert_eq!(project(members.clone(), json!([]), 10_000), Ok(expired));
    let fresh = MemberProjection {
        entities: vec![device(
            NODE_A,
            "Native inference device",
            json!({"status": "current", "observed_at_unix_ms": 0, "valid_until_unix_ms": 10000}),
            attributes("fresh", true),
        )],
        readiness: vec![readiness(NODE_A, "ready", Value::Null)],
        notices: vec![unplaced(NODE_A)],
        public_member_ids: ids(&[("node-a", NODE_A)]),
        current_raw_members: set(&["node-a"]),
        eligible_raw_members: set(&["node-a"]),
    };
    assert_eq!(project(members, json!([]), 9_999), Ok(fresh));
}

#[test]
fn product_projector_members_fresh_member_counts_eligible() {
    // Lease 2000.0 s -> 2000000 ms > 1999999: every named member is fresh (current). Only
    // `activation_eligible is True` with lifecycle configured/running is eligible (`:701-706`):
    // node-b's 1 is not `True`, node-c is stopping. The one stage on node-a gives its placement id
    // (`:709-715`); a non-mapping stage and a non-string node id place nothing (`:556-558`).
    let stages = json!([
        {"node_id": "node-a", "placement_id": "placement-1"},
        "not-a-mapping",
        {"node_id": 7, "placement_id": "placement-2"},
    ]);
    let members = json!([
        {
            "node_id": "node-a",
            "lease_expires_at": 2000.0,
            "last_liveness_at": 1999.5,
            "peer_class": "macos_arm64",
            "generation": 4,
            "authority_generation": 2,
            "incarnation": "inc-a",
            "lifecycle_state": "RUNNING",
            "activation_eligible": true,
            "runtime_capability": {
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.native_activation.v1",
            },
        },
        {
            "node_id": "node-b",
            "lease_expires_at": 2000.0,
            "lifecycle_state": "configured",
            "activation_eligible": 1,
        },
        {"node_id": "", "lease_expires_at": 2000.0, "activation_eligible": true},
        {
            "node_id": "node-c",
            "lease_expires_at": 2000.0,
            "lifecycle_state": "stopping",
            "activation_eligible": true,
        },
    ]);
    let projection = project(members, stages, 1_999_999).expect("valid members project");
    let entity_ids: Vec<&Value> = projection
        .entities
        .iter()
        .map(|e| &e["entity_id"])
        .collect();
    assert_eq!(entity_ids, [&json!(NODE_A), &json!(NODE_B), &json!(NODE_C)]);
    assert_eq!(
        projection.entities[0],
        device(
            NODE_A,
            "Native inference device",
            json!({"status": "current", "observed_at_unix_ms": 1999500, "valid_until_unix_ms": 2000000}),
            json!({
                "peer_class": "macos_arm64",
                "membership_generation": 4,
                "authority_generation": 2,
                "incarnation": "inc-a",
                "lifecycle": "running",
                "lease_freshness": "fresh",
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.native_activation.v1",
                "activation_eligible": true,
                "placement_id": "placement-1",
            }),
        )
    );
    for (index, lifecycle) in [(1, "configured"), (2, "stopping")] {
        let attributes = &projection.entities[index]["attributes"];
        assert_eq!(
            (
                &attributes["lifecycle"],
                &attributes["lease_freshness"],
                &attributes["activation_eligible"],
            ),
            (&json!(lifecycle), &json!("fresh"), &json!(false)),
            "entity {index}"
        );
    }
    assert_eq!(
        projection.readiness,
        [
            readiness(NODE_A, "ready", Value::Null),
            readiness(NODE_B, "ready", Value::Null),
            readiness(NODE_C, "not_ready", json!("membership_stopping")),
        ]
    );
    assert_eq!(projection.notices, [unplaced(NODE_B), unplaced(NODE_C)]);
    assert_eq!(
        projection.public_member_ids,
        ids(&[("node-a", NODE_A), ("node-b", NODE_B), ("node-c", NODE_C)])
    );
    assert_eq!(
        projection.current_raw_members,
        set(&["node-a", "node-b", "node-c"])
    );
    assert_eq!(projection.eligible_raw_members, set(&["node-a"]));
}

/// Paired vectors recorded by executing `ProductProjector._build` at f4e366e9 (CPython 3.13.7, salt
/// `bytes(range(32))`, no route) on the same member row and `now_unix_ms`: the oracle's exception
/// `[class, message]`, or the first device entity's lease freshness, eligibility, observed and
/// valid-until instants, generations and peer class.
const ORACLE_VECTORS: &[(&str, &str, u64, &str)] = &[
    (
        "lease:missing@0",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true}"#,
        0,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":0,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:null@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":null}"#,
        1000,
        r#"["TypeError", "float() argument must be a string or a real number, not 'NoneType'"]"#,
    ),
    (
        "lease:empty@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":""}"#,
        1000,
        r#"["ValueError", "could not convert string to float: ''"]"#,
    ),
    (
        "lease:abc@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"abc"}"#,
        1000,
        r#"["ValueError", "could not convert string to float: 'abc'"]"#,
    ),
    (
        "lease:nan@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"nan"}"#,
        1000,
        r#"["ValueError", "cannot convert float NaN to integer"]"#,
    ),
    (
        "lease:inf@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"inf"}"#,
        1000,
        r#"["OverflowError", "cannot convert float infinity to integer"]"#,
    ),
    (
        "lease:-inf@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"-inf"}"#,
        1000,
        r#"["OverflowError", "cannot convert float infinity to integer"]"#,
    ),
    (
        "lease:Infinity@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"Infinity"}"#,
        1000,
        r#"["OverflowError", "cannot convert float infinity to integer"]"#,
    ),
    (
        "lease:1e3@999999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"1e3"}"#,
        999999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":1000000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:1e3@1000000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"1e3"}"#,
        1000000,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":1000000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:ws12@11999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":" 12 "}"#,
        11999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":12000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:tabnl12@11999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"\t12\n"}"#,
        11999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":12000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:0x10@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"0x10"}"#,
        1000,
        r#"["ValueError", "could not convert string to float: '0x10'"]"#,
    ),
    (
        "lease:1_000@999999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"1_000"}"#,
        999999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":1000000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:1__000@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"1__000"}"#,
        1000,
        r#"["ValueError", "could not convert string to float: '1__000'"]"#,
    ),
    (
        "lease:_1@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"_1"}"#,
        1000,
        r#"["ValueError", "could not convert string to float: '_1'"]"#,
    ),
    (
        "lease:1_@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"1_"}"#,
        1000,
        r#"["ValueError", "could not convert string to float: '1_'"]"#,
    ),
    (
        "lease:1e1_0@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"1e1_0"}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":10000000000000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:arabic12@11999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"\u0661\u0662"}"#,
        11999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":12000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:fullwidth3@2999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"\uff13"}"#,
        2999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":3000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:nbsp5ideo@4999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"\u00a05\u3000"}"#,
        4999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":5000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:vt5@4999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"\u000b5"}"#,
        4999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":5000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:fs5@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"\u001c5"}"#,
        1000,
        r#"["ValueError", "could not convert string to float: '\\x1c5'"]"#,
    ),
    (
        "lease:true@999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":true}"#,
        999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":1000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:true@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":true}"#,
        1000,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":1000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:false@0",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":false}"#,
        0,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":0,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:-0.5@0",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":-0.5}"#,
        0,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":0,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:-0.0009@0",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":-0.0009}"#,
        0,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":0,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:10.0009@10000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":10.0009}"#,
        10000,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":10000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:10.0009@9999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":10.0009}"#,
        9999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":10000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:1e300@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":1e+300}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":1000000000000000000161765076786456438212668646231659438295495017101117499225738747865260243034213915253779773568180337416027445820567779199643391541606026068611150746122284976177256650044200527276807327067690462112661427500197051226489898260678763391449376088547292320814127957486330655468919122263277568,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:maxfloat@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":1.7976931348623157e+308}"#,
        1000,
        r#"["OverflowError", "cannot convert float infinity to integer"]"#,
    ),
    (
        "lease:int5@5000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":5}"#,
        5000,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":5000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:float5@4999",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":5.0}"#,
        4999,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":5000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:list@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":[]}"#,
        1000,
        r#"["TypeError", "float() argument must be a string or a real number, not 'list'"]"#,
    ),
    (
        "lease:dict@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":{}}"#,
        1000,
        r#"["TypeError", "float() argument must be a string or a real number, not 'dict'"]"#,
    ),
    (
        "lease:str1e400@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"1e400"}"#,
        1000,
        r#"["OverflowError", "cannot convert float infinity to integer"]"#,
    ),
    (
        "lease:literal1e400@1000",
        r#"{"node_id": "node-a", "lease_expires_at": 1e400}"#,
        1000,
        r#"["OverflowError", "cannot convert float infinity to integer"]"#,
    ),
    (
        "lease:neg0@0",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"-0"}"#,
        0,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":0,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:+.5e-3@0",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"+.5e-3"}"#,
        0,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":0,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:1e17s-u64max@18446744073709551615",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":1e+17}"#,
        18446744073709551615,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":100000000000000000000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:1.8e16s-u64max@18446744073709551615",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":1.8e+16}"#,
        18446744073709551615,
        r#"{"lease":"expired","eligible":false,"observed":0,"valid_until":18000000000000000000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:2p53plus1@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":9007199254740993}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":9007199254740992000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "lease:e5@1000",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":"e5"}"#,
        1000,
        r#"["ValueError", "could not convert string to float: 'e5'"]"#,
    ),
    (
        "liveness:null",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"last_liveness_at":null}"#,
        1000,
        r#"["TypeError", "float() argument must be a string or a real number, not 'NoneType'"]"#,
    ),
    (
        "liveness:x",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"last_liveness_at":"x"}"#,
        1000,
        r#"["ValueError", "could not convert string to float: 'x'"]"#,
    ),
    (
        "liveness:-5",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"last_liveness_at":-5}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "liveness:ws7",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"last_liveness_at":" 7 "}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":7000,"valid_until":7000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "liveness:1e300",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"last_liveness_at":1e+300}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":1000000000000000000161765076786456438212668646231659438295495017101117499225738747865260243034213915253779773568180337416027445820567779199643391541606026068611150746122284976177256650044200527276807327067690462112661427500197051226489898260678763391449376088547292320814127957486330655468919122263277568,"valid_until":1000000000000000000161765076786456438212668646231659438295495017101117499225738747865260243034213915253779773568180337416027445820567779199643391541606026068611150746122284976177256650044200527276807327067690462112661427500197051226489898260678763391449376088547292320814127957486330655468919122263277568,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "liveness:true",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"last_liveness_at":true}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":1000,"valid_until":2000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "generation:str7",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":"7"}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":7,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "generation:ws007",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":" 007 "}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":7,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "generation:1.5str",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":"1.5"}"#,
        1000,
        r#"["ValueError", "invalid literal for int() with base 10: '1.5'"]"#,
    ),
    (
        "generation:float-1.9",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":-1.9}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":-1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "generation:true",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":true}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "generation:null",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":null}"#,
        1000,
        r#"["TypeError", "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"]"#,
    ),
    (
        "generation:1_0",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":"1_0"}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":10,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "generation:int10e30",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":1000000000000000000000000000000}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1000000000000000000000000000000,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "generation:arabic5",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":"\u0665"}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":5,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "generation:x300",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}"#,
        1000,
        r#"["ValueError", "invalid literal for int() with base 10: 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"]"#,
    ),
    (
        "generation:neg-0",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":"-0"}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":0,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "generation:- 1",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":"- 1"}"#,
        1000,
        r#"["ValueError", "invalid literal for int() with base 10: '- 1'"]"#,
    ),
    (
        "generation:float1e300",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":1e+300}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1000000000000000052504760255204420248704468581108159154915854115511802457988908195786371375080447864043704443832883878176942523235360430575644792184786706982848387200926575803737830233794788090059368953234970799945081119038967640880074652742780142494579258788820056842838115669472196386865459400540160,"authority":1,"peer_class":"linux_tbd"}"#,
    ),
    (
        "order:lease-before-liveness",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":null,"last_liveness_at":null}"#,
        1000,
        r#"["TypeError", "float() argument must be a string or a real number, not 'NoneType'"]"#,
    ),
    (
        "order:liveness-before-generation",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"last_liveness_at":"x","generation":null}"#,
        1000,
        r#"["ValueError", "could not convert string to float: 'x'"]"#,
    ),
    (
        "order:generation-before-authority",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"generation":null,"authority_generation":"x"}"#,
        1000,
        r#"["TypeError", "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"]"#,
    ),
    (
        "peer_class:null",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"peer_class":null}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"None"}"#,
    ),
    (
        "peer_class:1.5",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"peer_class":1.5}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"1.5"}"#,
    ),
    (
        "peer_class:1e16",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"peer_class":1e+16}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"1e+16"}"#,
    ),
    (
        "peer_class:1e-05",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"peer_class":1e-05}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"1e-05"}"#,
    ),
    (
        "peer_class:true",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"peer_class":true}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"True"}"#,
    ),
    (
        "peer_class:list",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"peer_class":[1,"a"]}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"[1, 'a']"}"#,
    ),
    (
        "peer_class:dict",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"peer_class":{"a":1}}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"{'a': 1}"}"#,
    ),
    (
        "peer_class:int10e30",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"peer_class":1000000000000000000000000000000}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"1000000000000000000000000000000"}"#,
    ),
    (
        "peer_class:neg0f",
        r#"{"node_id":"node-a","lifecycle_state":"RUNNING","activation_eligible":true,"lease_expires_at":2.0,"peer_class":-0.0}"#,
        1000,
        r#"{"lease":"fresh","eligible":true,"observed":0,"valid_until":2000,"generation":1,"authority":1,"peer_class":"-0.0"}"#,
    ),
];

fn summary(result: Result<MemberProjection, ProjectionError>) -> Value {
    match result {
        Ok(projection) => match projection.entities.first() {
            Some(entity) => {
                let attributes = &entity["attributes"];
                json!({
                    "lease": attributes["lease_freshness"],
                    "eligible": attributes["activation_eligible"],
                    "observed": entity["freshness"]["observed_at_unix_ms"],
                    "valid_until": entity["freshness"]["valid_until_unix_ms"],
                    "generation": attributes["membership_generation"],
                    "authority": attributes["authority_generation"],
                    "peer_class": attributes["peer_class"],
                })
            }
            None => json!("no entity"),
        },
        Err(error) => json!([error.raised.identity().0, error.message]),
    }
}

#[test]
fn product_projector_members_lease_oracle_vectors() {
    for (name, member, now, expected) in ORACLE_VECTORS {
        let member: Value = serde_json::from_str(member).expect(name);
        let expected: Value = serde_json::from_str(expected).expect(name);
        assert_eq!(
            summary(project(json!([member]), json!([]), *now)),
            expected,
            "{name}"
        );
    }
    // Two oracle vectors too long for the table. `int(10**400)` is beyond the float range.
    let huge = format!(
        r#"{{"node_id":"node-a","lease_expires_at":1{}}}"#,
        "0".repeat(400)
    );
    let huge: Value = serde_json::from_str(&huge).expect("a JSON integer");
    assert_eq!(
        summary(project(json!([huge]), json!([]), 1_000)),
        json!(["OverflowError", "int too large to convert to float"])
    );
    // `int()` refuses text over CPython's default 4300 digits.
    let members =
        json!([{"node_id": "node-a", "lease_expires_at": 2.0, "generation": "1".repeat(4_301)}]);
    assert_eq!(
        summary(project(members, json!([]), 1_000)),
        json!([
            "ValueError",
            "Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; \
             use sys.set_int_max_str_digits() to increase the limit"
        ])
    );
}

#[test]
fn product_projector_members_revoked_member_oracle() {
    // Recorded from the executed oracle (as above): a STOPPED member adds the revoked notice and
    // incident (`:777-803`), whose instants are max(0, observed) = int(0.25 * 1000) = 250.
    let members = json!([{
        "node_id": "node-a",
        "lifecycle_state": "STOPPED",
        "lease_expires_at": 0.5,
        "last_liveness_at": 0.25,
    }]);
    let expected = MemberProjection {
        entities: vec![
            device(
                NODE_A,
                "Native inference device",
                json!({"status": "stale", "observed_at_unix_ms": 250, "valid_until_unix_ms": 500}),
                json!({
                    "peer_class": "linux_tbd",
                    "membership_generation": 1,
                    "authority_generation": 1,
                    "incarnation": "unknown",
                    "lifecycle": "stopped",
                    "lease_freshness": "expired",
                    "runtime_backend": "tbd",
                    "transport": "none",
                    "activation_protocol": null,
                    "activation_eligible": false,
                    "placement_id": null,
                }),
            ),
            json!({
                "entity_id": format!("incident-revoked-{NODE_A}"),
                "kind": "incident",
                "label": "Member revoked",
                "source_id": "membership-source",
                "binding": binding(),
                "freshness": {"status": "current", "observed_at_unix_ms": 250, "valid_until_unix_ms": 250},
                "attributes": {
                    "state": "member_revoked",
                    "reason_code": "member_revoked",
                    "observed_at_unix_ms": 250,
                },
            }),
        ],
        readiness: vec![readiness(NODE_A, "not_ready", json!("member_revoked"))],
        notices: vec![
            json!({
                "notice_id": format!("revoked-{NODE_A}"),
                "scope_id": NODE_A,
                "severity": "warning",
                "code": "member_revoked",
                "source_id": "membership-source",
            }),
            unplaced(NODE_A),
        ],
        public_member_ids: ids(&[("node-a", NODE_A)]),
        current_raw_members: set(&[]),
        eligible_raw_members: set(&[]),
    };
    assert_eq!(project(members, json!([]), 1_000), Ok(expected));
}

/// The member fields `_build` passes through `str()`: `peer_class` (`:738`), `incarnation` (`:743`),
/// and `runtime_capability`'s `runtime_backend` and `transport` (`:746-747`).
const STRINGIFIED_FIELDS: [&str; 4] = ["incarnation", "peer_class", "runtime_backend", "transport"];

/// Project one fresh member carrying `value` (JSON text) at `field`; the device entity's attribute.
fn stringified(field: &str, value: &Value) -> Result<Value, ProjectionError> {
    let mut member = Map::new();
    member.insert("node_id".to_owned(), json!("node-a"));
    member.insert("lease_expires_at".to_owned(), json!(20));
    if field == "runtime_backend" || field == "transport" {
        let mut runtime = Map::new();
        runtime.insert(field.to_owned(), value.clone());
        member.insert("runtime_capability".to_owned(), Value::Object(runtime));
    } else {
        member.insert(field.to_owned(), value.clone());
    }
    project(Value::Array(vec![Value::Object(member)]), json!([]), 10_000)
        .map(|projection| projection.entities[0]["attributes"][field].clone())
}

/// Every field of [`STRINGIFIED_FIELDS`] against one expected result; the mismatches, labelled.
fn stringified_mismatches(
    label: &str,
    value: &Value,
    expected: &Result<Value, ProjectionError>,
) -> Vec<String> {
    STRINGIFIED_FIELDS
        .iter()
        .filter_map(|field| {
            let actual = stringified(field, value);
            (actual != *expected)
                .then(|| format!("{label} @ {field}: got {actual:?}, expected {expected:?}"))
        })
        .collect()
}

/// `str(float)` is `float.__repr__`: the shortest digits that read back, the closest to the value
/// among equally short ones, fixed notation for decimal exponents in -4..16, else `d.ddde+XX`; an
/// infinity (a JSON number beyond the float range) is `inf`. Every expected text was recorded by
/// executing the oracle `_build` member loop (f4e366e9; CPython 3.13.7 and 3.14.2 agree) with the
/// JSON value at each of the four fields, and equals `str(json.loads(text))`
/// (`target/rc-logs-pm-a2/record.py`, `recorded-python3.13.json`). The first 22 are the reviewer's
/// tie cases (`rv-logs-pm/float-summary.json`).
const FLOAT_TEXT_CASES: &[(&str, &str)] = &[
    ("1000000000000000.2", "1000000000000000.2"),
    ("100000000000000.12", "100000000000000.12"),
    ("10000000000000.062", "10000000000000.062"),
    ("1000000000000.0312", "1000000000000.0312"),
    ("100000000000.01562", "100000000000.01562"),
    ("759764761995381.2", "759764761995381.2"),
    ("9463507963503.062", "9463507963503.062"),
    ("946125565527587.2", "946125565527587.2"),
    ("195473576505796.62", "195473576505796.62"),
    ("642330777722732.2", "642330777722732.2"),
    ("908585811843394.2", "908585811843394.2"),
    ("1705805336890119.2", "1705805336890119.2"),
    ("2107339645600423.2", "2107339645600423.2"),
    ("86321998976681.62", "86321998976681.62"),
    ("964507912068740.2", "964507912068740.2"),
    ("1003692702460896.2", "1003692702460896.2"),
    ("1216902883714341.2", "1216902883714341.2"),
    ("2010713243104000.2", "2010713243104000.2"),
    ("1568658460894677.2", "1568658460894677.2"),
    ("146701572242589.62", "146701572242589.62"),
    ("569330862207251.2", "569330862207251.2"),
    ("33421561441485.312", "33421561441485.312"),
    ("-1000000000000000.2", "-1000000000000000.2"),
    ("1e16", "1e+16"),
    ("1E+16", "1e+16"),
    ("9999999999999998.0", "9999999999999998.0"),
    ("1e22", "1e+22"),
    ("12345678901234567890.0", "1.2345678901234567e+19"),
    ("1.2345678901234567e+300", "1.2345678901234567e+300"),
    ("1.7976931348623157e308", "1.7976931348623157e+308"),
    ("0.0001", "0.0001"),
    ("1e-05", "1e-05"),
    ("0.00012345", "0.00012345"),
    ("5e-324", "5e-324"),
    ("-1e-5", "-1e-05"),
    ("2.2250738585072014e-308", "2.2250738585072014e-308"),
    ("-0.0", "-0.0"),
    ("0.0", "0.0"),
    ("2.0", "2.0"),
    ("1e15", "1000000000000000.0"),
    ("1E2", "100.0"),
    ("0.30000000000000004", "0.30000000000000004"),
    ("0.1", "0.1"),
    ("0.1000000000000000055511151231257827", "0.1"),
    ("0.3333333333333333", "0.3333333333333333"),
    ("1e400", "inf"),
    ("-1e400", "-inf"),
    // Integer literals stay `int` (near misses of the float cases above).
    ("100", "100"),
    ("-0", "0"),
];

#[test]
fn product_projector_members_float_text_parity() {
    let mut mismatches = Vec::new();
    for (text, expected) in FLOAT_TEXT_CASES {
        let value: Value = serde_json::from_str(text).expect(text);
        mismatches.extend(stringified_mismatches(text, &value, &Ok(json!(expected))));
    }
    assert!(
        mismatches.is_empty(),
        "{} of {} float texts differ from Python str():\n{}",
        mismatches.len(),
        FLOAT_TEXT_CASES.len() * STRINGIFIED_FIELDS.len(),
        mismatches.join("\n")
    );
}

/// `str()` of a list or dict is its `repr()`: each item is `repr()`ed recursively (an `int` keeps
/// every digit, a `float` is `float.__repr__`, `True`/`False`/`None`, a string is quoted and its
/// non-printable characters escaped), keys in the dict's order. A top-level string is itself.
/// Recorded like [`FLOAT_TEXT_CASES`] (`recorded-python3.13.json`, `containers`); the first three
/// are the reviewer's F2 cases (`rv-logs-pm/fresh-summary.json`).
const CONTAINER_REPR_CASES: &[(&str, &str, &str)] = &[
    (
        "big-int-in-list",
        "[100000000000000000000]",
        "[100000000000000000000]",
    ),
    ("zwsp-in-list", r#"["\u200b"]"#, r"['\u200b']"),
    (
        "escaped-key",
        r#"{"\u200b": "\u00e9"}"#,
        "{'\\u200b': '\u{e9}'}",
    ),
    (
        "nested",
        r#"{"a": [1, {"b": null, "c": [true, false]}], "d": {}}"#,
        "{'a': [1, {'b': None, 'c': [True, False]}], 'd': {}}",
    ),
    (
        "floats-in-list",
        "[1.0, 1, 0.1, 1e16, 1e-05, -0.0, 1000000000000000.2, 1e400, -1e400]",
        "[1.0, 1, 0.1, 1e+16, 1e-05, -0.0, 1000000000000000.2, inf, -inf]",
    ),
    ("int-float-kind", "[1.0, 1]", "[1.0, 1]"),
    (
        "bool-none-in-list",
        "[true, null, false]",
        "[True, None, False]",
    ),
    (
        "bool-none-in-dict",
        r#"{"a": [true, null]}"#,
        "{'a': [True, None]}",
    ),
    (
        "quotes-backslash",
        r#"["'", "\"", "'\"", "a\\b"]"#,
        r#"["'", '"', '\'"', 'a\\b']"#,
    ),
    (
        "controls",
        r#"["\u0000\n\t\r\u007f"]"#,
        r"['\x00\n\t\r\x7f']",
    ),
    (
        "nbsp-linesep",
        r#"["\u00a0", "\u2028"]"#,
        r"['\xa0', '\u2028']",
    ),
    (
        "printable-non-ascii",
        r#"["\u00e9", "\ud83d\ude00"]"#,
        "['\u{e9}', '\u{1f600}']",
    ),
    (
        "astral-format-char",
        r#"["\udb40\udc01"]"#,
        r"['\U000e0001']",
    ),
    ("empties", r#"[[], {}, ""]"#, "[[], {}, '']"),
    (
        "int-signs",
        "[-0, -5, -100000000000000000000]",
        "[0, -5, -100000000000000000000]",
    ),
    ("duplicate-key", r#"{"a": 1, "a": 2}"#, "{'a': 2}"),
    ("top-level-str-not-escaped", r#""\u200b""#, "\u{200b}"),
    ("top-level-bool", "true", "True"),
    ("top-level-null", "null", "None"),
];

/// CPython's `int.__str__` refuses more than `sys.get_int_max_str_digits()` (4300) digits.
const INT_STR_LIMIT: &str = "Exceeds the limit (4300 digits) for integer string conversion; \
                             use sys.set_int_max_str_digits() to increase the limit";

#[test]
fn product_projector_members_container_repr_parity() {
    let mut mismatches = Vec::new();
    for (name, text, expected) in CONTAINER_REPR_CASES {
        let value: Value = serde_json::from_str(text).expect(name);
        mismatches.extend(stringified_mismatches(name, &value, &Ok(json!(expected))));
    }
    // `int` digits around the 4300-digit limit (`json.loads` refuses longer literals, so these were
    // recorded by calling `_build` with `[10**4299]`, `[10**4300]`, `10**4300`, `[-(10**4300-1)]`).
    let int_cases = [
        (
            "[10**4299]",
            format!("[1{}]", "0".repeat(4_299)),
            Ok(json!(format!("[1{}]", "0".repeat(4_299)))),
        ),
        (
            "[-(10**4300-1)]",
            format!("[-{}]", "9".repeat(4_300)),
            Ok(json!(format!("[-{}]", "9".repeat(4_300)))),
        ),
        (
            "[10**4300]",
            format!("[1{}]", "0".repeat(4_300)),
            Err(raised("ValueError", INT_STR_LIMIT)),
        ),
        (
            "10**4300",
            format!("1{}", "0".repeat(4_300)),
            Err(raised("ValueError", INT_STR_LIMIT)),
        ),
    ];
    for (name, text, expected) in &int_cases {
        let value: Value = serde_json::from_str(text).expect(name);
        mismatches.extend(stringified_mismatches(name, &value, expected));
    }
    assert!(
        mismatches.is_empty(),
        "{} container/int str() results differ from Python:\n{}",
        mismatches.len(),
        mismatches.join("\n")
    );

    // A failing `str()` raises in the dict literal's source order (`:738-747`), after the lease and
    // liveness (`:696`, `:716`). Recorded from the oracle (`order-python3.13.json`).
    let big = format!("[1{}]", "0".repeat(4_300));
    let ordered = [
        (
            format!(
                r#"{{"node_id":"node-a","lease_expires_at":20,"peer_class":{big},"generation":"x"}}"#
            ),
            raised("ValueError", INT_STR_LIMIT),
        ),
        (
            format!(
                r#"{{"node_id":"node-a","lease_expires_at":20,"incarnation":{big},"generation":"x"}}"#
            ),
            raised("ValueError", "invalid literal for int() with base 10: 'x'"),
        ),
        (
            format!(
                r#"{{"node_id":"node-a","lease_expires_at":20,"authority_generation":"y","runtime_capability":{{"runtime_backend":{big}}}}}"#
            ),
            raised("ValueError", "invalid literal for int() with base 10: 'y'"),
        ),
        (
            format!(
                r#"{{"node_id":"node-a","lease_expires_at":20,"last_liveness_at":null,"peer_class":{big}}}"#
            ),
            raised(
                "TypeError",
                "float() argument must be a string or a real number, not 'NoneType'",
            ),
        ),
    ];
    for (member, expected) in ordered {
        let member: Value = serde_json::from_str(&member).expect("a member row");
        assert_eq!(
            project(json!([member]), json!([]), 10_000).map(|_| ()),
            Err(expected)
        );
    }

    // Declared limit: dict insertion order. The oracle keeps it (`{"z": 1, "a": 2}` is recorded as
    // `{'z': 1, 'a': 2}`), but a member row reaches this seam as a `serde_json::Map`, which without
    // serde_json's `preserve_order` feature (not enabled in this workspace) is a `BTreeMap`
    // (serde_json 1.0.154 src/map.rs:38-39): the order is gone when the row is parsed (here by
    // `serde_json::from_str`), before `project_members` sees it. On the oracle's own
    // product path every mapping value is key-sorted already: `peer_class`/`incarnation` are
    // non-empty text columns and `runtime_capability_json` must equal its sort_keys canonical JSON
    // (mycelium_seed/state.py:746-758, :778). The seam's actual behaviour is pinned here.
    let value: Value = serde_json::from_str(r#"{"z": 1, "a": 2}"#).expect("a JSON object");
    assert_eq!(
        stringified_mismatches(
            "dict-order (declared limit)",
            &value,
            &Ok(json!("{'a': 2, 'z': 1}"))
        ),
        Vec::<String>::new()
    );
}
