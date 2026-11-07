// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-private replay checkpoint record (`mycelium_a6_recovery.py:406-560` at f4e366e9).
//! Every expected code is the oracle's exact `ValueError` text at the cited line; the digest
//! literals come from one read-only run of the oracle (command in the worker RESULT.json).
use protocol::{
    ContractError,
    replay_checkpoint::{CHECKPOINT_PROTOCOL, ReplayCheckpoint, ReplayCheckpointArgs},
};
use serde_json::{Value, json};

/// `_digest((11, 12))` (`:123-125`): sha256 of `[11,12]`.
const PROMPT_DIGEST: &str =
    "sha256:2e96afc0e9b31d97254a398e034dd89ae673b8958ef903ee31144c9742fd63be";
/// `_prefix_digest((101, 102))` (`:399-400`): sha256 of `[101,102]`.
const PREFIX_DIGEST: &str =
    "sha256:ec535b6ad533b0b50b5c99762a71f494c89d0122e475bdfa3f00f9cdbf40b6c7";
/// `_digest({"greedy": True})`: sha256 of `{"greedy":true}`.
const SETTINGS_DIGEST: &str =
    "sha256:3e169704eb0fda11206eed6a88353979333bc676cfb1b7c2ab2572e632f63bfc";

/// A valid record with every floored integer above its floor and nothing committed yet.
fn base() -> ReplayCheckpointArgs {
    ReplayCheckpointArgs {
        protocol: json!(CHECKPOINT_PROTOCOL),
        store_identity: json!("store-1"),
        request_id: json!("req-1"),
        session_id: json!("sess-1"),
        source_attempt: json!(2),
        path_id: json!("path-a"),
        path_digest: json!(format!("sha256:{}", "a".repeat(64))),
        path_attempt: json!(1),
        prompt_digest: json!(PROMPT_DIGEST),
        generation_settings_digest: json!(SETTINGS_DIGEST),
        committed_token_count: json!(0),
        committed_prefix_digest: Value::Null,
        delivery_cursor: json!(1),
        watermark: json!(0),
        publisher_generation: json!(3),
        deployment_id: json!("deployment-a4"),
        deployment_epoch: json!(4),
        topology_generation: json!(5),
        source_qualification_digest: json!(format!("sha256:{}", "0".repeat(64))),
        model_id: json!("model-7b"),
        representation_digest: json!(format!("sha256:{}", "1".repeat(64))),
        tokenizer_digest: json!(format!("sha256:{}", "4".repeat(64))),
        recovery_attempt: json!(1),
        terminal_authority: Value::Null,
        created_at_unix_ms: json!(1_000),
        expiry_unix_ms: json!(2_000_000),
        reconciliation_state: json!("active"),
        encoded_prompt: json!([11, 12]),
        committed_prefix_tokens: json!([]),
    }
}

fn accepted(args: &ReplayCheckpointArgs, case: &str) -> ReplayCheckpoint {
    let result = ReplayCheckpoint::new(args);
    assert_eq!(result.as_ref().err(), None, "{case}: must be accepted");
    result.unwrap()
}

fn refused(args: &ReplayCheckpointArgs, code: &'static str, case: &str) {
    assert_eq!(
        ReplayCheckpoint::new(args).err(),
        Some(ContractError(code)),
        "{case}"
    );
}

type Set = fn(&mut ReplayCheckpointArgs, Value);

/// The integer fields in `__post_init__` order (`:463-475`), with the oracle's floor and
/// its exact refusal (`_integer`, `:143-146`).
fn integer_fields() -> [(&'static str, i64, &'static str, Set); 11] {
    [
        (
            "source_attempt",
            1,
            "source_attempt must be an integer >= 1",
            |a, v| a.source_attempt = v,
        ),
        (
            "path_attempt",
            0,
            "path_attempt must be an integer >= 0",
            |a, v| a.path_attempt = v,
        ),
        (
            "recovery_attempt",
            0,
            "recovery_attempt must be an integer >= 0",
            |a, v| a.recovery_attempt = v,
        ),
        (
            "deployment_epoch",
            1,
            "deployment_epoch must be an integer >= 1",
            |a, v| a.deployment_epoch = v,
        ),
        (
            "topology_generation",
            1,
            "topology_generation must be an integer >= 1",
            |a, v| a.topology_generation = v,
        ),
        (
            "committed_token_count",
            0,
            "committed_token_count must be an integer >= 0",
            |a, v| a.committed_token_count = v,
        ),
        (
            "watermark",
            0,
            "watermark must be an integer >= 0",
            |a, v| a.watermark = v,
        ),
        (
            "delivery_cursor",
            -1,
            "delivery_cursor must be an integer >= -1",
            |a, v| a.delivery_cursor = v,
        ),
        (
            "publisher_generation",
            0,
            "publisher_generation must be an integer >= 0",
            |a, v| a.publisher_generation = v,
        ),
        (
            "created_at_unix_ms",
            1,
            "created_at_unix_ms must be an integer >= 1",
            |a, v| a.created_at_unix_ms = v,
        ),
        (
            "expiry_unix_ms",
            1,
            "expiry_unix_ms must be an integer >= 1",
            |a, v| a.expiry_unix_ms = v,
        ),
    ]
}

#[test]
fn private_replay_checkpoint_floor_boundaries() {
    accepted(&base(), "base");
    for (name, minimum, code, set) in integer_fields() {
        if name == "delivery_cursor" {
            continue; // its own test below
        }
        let mut at_minimum = base();
        set(&mut at_minimum, json!(minimum));
        let mut below = base();
        set(&mut below, json!(minimum - 1));
        refused(&below, code, &format!("{name} = {}", minimum - 1));
        if name == "expiry_unix_ms" {
            // :475 passes at 1, but created_at_unix_ms >= 1 makes 1 already expired (:476-477);
            // the lowest accepted expiry is created_at_unix_ms + 1.
            refused(
                &at_minimum,
                "checkpoint_already_expired",
                "expiry_unix_ms = 1",
            );
            let mut lowest = base();
            lowest.created_at_unix_ms = json!(1);
            lowest.expiry_unix_ms = json!(2);
            accepted(&lowest, "created_at_unix_ms = 1, expiry_unix_ms = 2");
        } else {
            accepted(&at_minimum, &format!("{name} = {minimum}"));
        }
    }

    // Source order: with every floor broken, each refusal names the next field in :463-475.
    let mut args = base();
    for (_, minimum, _, set) in integer_fields() {
        set(&mut args, json!(minimum - 1));
    }
    for (name, minimum, code, set) in integer_fields() {
        refused(&args, code, &format!("source order at {name}"));
        let repaired = if name == "expiry_unix_ms" { 2 } else { minimum };
        set(&mut args, json!(repaired));
    }
    accepted(&args, "every floor repaired");
}

#[test]
fn private_replay_checkpoint_delivery_cursor_minus_one_ok_minus_two_refused() {
    // :471-472: -1 means "nothing committed yet" (gateway acknowledged_through).
    let mut args = base();
    args.delivery_cursor = json!(-1);
    let checkpoint = accepted(&args, "delivery_cursor = -1");
    assert_eq!(checkpoint.delivery_cursor.to_string(), "-1");
    args.delivery_cursor = json!(-2);
    refused(
        &args,
        "delivery_cursor must be an integer >= -1",
        "delivery_cursor = -2",
    );
}

#[test]
fn private_replay_checkpoint_bool_refused() {
    // `type(value) is not int` (:144) refuses bool even where True == 1 meets the floor.
    for (name, _, code, set) in integer_fields() {
        for flag in [true, false] {
            let mut args = base();
            set(&mut args, json!(flag));
            refused(&args, code, &format!("{name} = {flag}"));
        }
    }
    // Token IDs use the same strict test (:487-497).
    let mut args = base();
    args.encoded_prompt = json!([11, true]);
    refused(
        &args,
        "checkpoint_encoded_prompt_invalid",
        "encoded_prompt bool",
    );
    let mut args = base();
    args.committed_prefix_tokens = json!([false]);
    refused(
        &args,
        "checkpoint_prefix_tokens_invalid",
        "committed_prefix_tokens bool",
    );
}

#[test]
fn private_replay_checkpoint_digest_matches_canonical() {
    // `json.dumps(_canonical_document(), sort_keys=True, separators=(",", ":"), allow_nan=False)`
    // (:123-125, :512-543; ensure_ascii stays True), printed by the oracle.
    const CANONICAL: &str = r#"{"committed_prefix_digest":"sha256:ec535b6ad533b0b50b5c99762a71f494c89d0122e475bdfa3f00f9cdbf40b6c7","committed_prefix_tokens":[101,102],"committed_token_count":2,"created_at_unix_ms":1000,"delivery_cursor":1,"deployment_epoch":4,"deployment_id":"deployment-a4","encoded_prompt":[11,12],"expiry_unix_ms":18446744073709551616,"generation_settings_digest":"sha256:3e169704eb0fda11206eed6a88353979333bc676cfb1b7c2ab2572e632f63bfc","model_id":"model-7b","path_attempt":1,"path_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","path_id":"path-a","prompt_digest":"sha256:2e96afc0e9b31d97254a398e034dd89ae673b8958ef903ee31144c9742fd63be","protocol":"mycelium.private_request_replay_checkpoint.v1","publisher_generation":3,"reconciliation_state":"frozen","recovery_attempt":1,"representation_digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111","request_id":"req-1","session_id":"sess-1","source_attempt":2,"source_qualification_digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","store_identity":"st\u00f6re-1","terminal_authority":"owner-a4","tokenizer_digest":"sha256:4444444444444444444444444444444444444444444444444444444444444444","topology_generation":5,"watermark":2}"#;
    /// `checkpoint.checkpoint_digest()` printed by the same oracle run.
    const DIGEST: &str = "sha256:e478000bdc10a3342274c3ed659d4f26b39ddf9d9b2c7a7bb1aae4b3f85b3a3b";
    {
        use sha2::{Digest, Sha256};
        assert_eq!(
            format!(
                "sha256:{}",
                hex::encode(Sha256::digest(CANONICAL.as_bytes()))
            ),
            DIGEST,
            "the pasted digest is the sha256 of the pasted canonical document"
        );
    }

    let mut args = base();
    args.store_identity = json!("st\u{f6}re-1");
    args.committed_token_count = json!(2);
    args.committed_prefix_digest = json!(PREFIX_DIGEST);
    args.watermark = json!(2);
    args.terminal_authority = json!("owner-a4");
    args.expiry_unix_ms = serde_json::from_str("18446744073709551616").unwrap();
    args.reconciliation_state = json!("frozen");
    args.committed_prefix_tokens = json!([101, 102]);
    let checkpoint = accepted(&args, "digest fixture");
    let canonical = protocol::canonical::to_ascii_bytes(&checkpoint.canonical_document()).unwrap();
    assert_eq!(String::from_utf8(canonical).unwrap(), CANONICAL);
    assert_eq!(checkpoint.checkpoint_digest(), DIGEST);

    // The digests bind the exact private token sequences (:499-507).
    let mut tampered = args.clone();
    tampered.encoded_prompt = json!([11, 13]);
    refused(
        &tampered,
        "checkpoint_prompt_digest_mismatch",
        "prompt tampered",
    );
    let mut tampered = args.clone();
    tampered.committed_prefix_tokens = json!([101, 103]);
    refused(
        &tampered,
        "checkpoint_prefix_digest_mismatch",
        "prefix tampered",
    );
}

#[test]
fn private_replay_checkpoint_expired_refused() {
    // :476-477: expiry_unix_ms <= created_at_unix_ms is already expired.
    let mut args = base();
    for (created, expiry, code) in [
        ("1000", "1000", Some("checkpoint_already_expired")),
        ("1000", "999", Some("checkpoint_already_expired")),
        ("1000", "1001", None),
        // Python ints are unbounded: no truncation or saturation at 2**64.
        (
            "18446744073709551616",
            "18446744073709551616",
            Some("checkpoint_already_expired"),
        ),
        (
            "18446744073709551616",
            "18446744073709551615",
            Some("checkpoint_already_expired"),
        ),
        ("18446744073709551616", "18446744073709551617", None),
    ] {
        args.created_at_unix_ms = serde_json::from_str(created).unwrap();
        args.expiry_unix_ms = serde_json::from_str(expiry).unwrap();
        let case = format!("created {created}, expiry {expiry}");
        match code {
            Some(code) => refused(&args, code, &case),
            None => {
                accepted(&args, &case);
            }
        }
    }

    // The expiry check precedes the reconciliation state check (:476-480).
    let mut args = base();
    args.reconciliation_state = json!("terminal");
    args.expiry_unix_ms = json!(1_000);
    refused(
        &args,
        "checkpoint_already_expired",
        "expired and state invalid",
    );
    args.expiry_unix_ms = json!(1_001);
    refused(
        &args,
        "checkpoint_reconciliation_state_invalid",
        "state invalid",
    );
    for state in ["active", "frozen", "reconciled"] {
        args.reconciliation_state = json!(state);
        accepted(&args, state);
    }
}
