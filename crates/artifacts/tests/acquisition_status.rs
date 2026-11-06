// SPDX-License-Identifier: AGPL-3.0-only
//! CL-L1-07: acquisition status validation and the acquisition store's current/history subset,
//! as v0.1.4 (f4e366e9) `validate_acquisition_status` (mycelium_swarm_artifacts.py:1119-1247,
//! `_REASON` :44) and `ArtifactAcquisitionStore` (mycelium_live/artifact_provisioner.py:212-228
//! init, :369-419 write_current/_read_history/finish, :441-466 ledger, :476-500
//! `_recover_interrupted`). Every expected outcome below is what the executed oracle (CPython
//! 3.14.2) returns or raises on the same input.
#[path = "support/temp.rs"]
mod test_temp;
use artifacts::acquisition::{
    AcquisitionState, AcquisitionStatus, ArtifactAcquisitionStore, DEFAULT_HISTORY_LIMIT, PyNumber,
    PyValue, loads, validate_acquisition_status,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{fs, path::Path};

const INVALID: &str = "artifact_acquisition_status_invalid";
const FUTURE: u64 = 9_000_000_000_000;

fn digest(c: char) -> String {
    format!("sha256:{}", c.to_string().repeat(64))
}

/// A well-formed nonterminal status: every cross-field sum holds (10 + 30 + 60 = 100, one active
/// source of 20 bytes plus 10 origin bytes = 30 transferred).
fn base() -> Value {
    json!({
        "protocol": "mycelium.swarm_artifact_acquisition.v1",
        "generation": 1,
        "acquisition_id": "acq-1",
        "state": "transferring",
        "phase": "transferring",
        "model_id": "example/model",
        "model_revision": "0123456789abcdef0123456789abcdef01234567",
        "representation": "bf16",
        "assignment_id": "assignment-1",
        "placement_id": "placement-1",
        "stage_id": "stage-0",
        "layer_start": 0,
        "layer_end_exclusive": 4,
        "total_bytes": 100,
        "cached_verified_bytes": 10,
        "transferred_verified_bytes": 30,
        "missing_bytes": 60,
        "quarantined_bytes": 0,
        "duplicate_bytes_prevented": 0,
        "eligible_source_count": 2,
        "active_source_count": 1,
        "sources": [{"source_ref": "source-0123456789ab", "state": "active", "verified_bytes": 20}],
        "origin_bytes": 10,
        "aggregate_bytes_per_second": 1.5,
        "eta_seconds": 40,
        "chunk_count": 4,
        "verified_chunk_count": 2,
        "resumed_chunk_count": 1,
        "source_rotation_count": 0,
        "manifest_digest": digest('1'),
        "assignment_digest": digest('2'),
        "representation_digest": digest('3'),
        "feasibility_digest": digest('4'),
        "evidence_generation": 0,
        "promotion_digest": null,
        "reason_code": null,
        "retryable": false,
        "started_at_unix_ms": 1000,
        "updated_at_unix_ms": 1000,
        "terminal_at_unix_ms": null
    })
}

/// A well-formed terminal status in `state`: no phase, a terminal timestamp, a promotion digest
/// for `ready` (:1242) and a reason code for `failed` (:1243).
fn terminal(state: &str) -> Value {
    let mut doc = with(
        base(),
        &[
            ("state", json!(state)),
            ("phase", Value::Null),
            ("updated_at_unix_ms", json!(2000)),
            ("terminal_at_unix_ms", json!(2000)),
        ],
    );
    if state == "ready" {
        doc["promotion_digest"] = json!(digest('5'));
    }
    if state == "failed" {
        doc["reason_code"] = json!("transfer_failed");
    }
    doc
}

fn with(mut doc: Value, changes: &[(&str, Value)]) -> Value {
    for (key, value) in changes {
        doc[*key] = value.clone();
    }
    doc
}

fn check(doc: &Value) -> artifacts::Result<AcquisitionStatus> {
    validate_acquisition_status(&PyValue::from_json(doc))
}

/// `"accepted"` or the refusal code.
fn outcome(doc: &Value) -> &'static str {
    match check(doc) {
        Ok(_) => "accepted",
        Err(e) => e.code,
    }
}

fn accepted(doc: &Value) -> AcquisitionStatus {
    let result = check(doc);
    assert!(result.is_ok(), "refused: {:?}", result.as_ref().err());
    result.unwrap()
}

fn now_ms() -> u64 {
    let since = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap();
    u64::try_from(since.as_millis()).unwrap()
}

fn store_root(label: &str, current: Option<&[u8]>, history: Option<&[u8]>) -> std::path::PathBuf {
    let root = test_temp::directory(label);
    fs::create_dir(root.join("state")).unwrap();
    if let Some(bytes) = current {
        fs::write(root.join("state/current.json"), bytes).unwrap();
    }
    if let Some(bytes) = history {
        fs::write(root.join("state/history.json"), bytes).unwrap();
    }
    root
}

fn open(root: &Path) -> artifacts::Result<ArtifactAcquisitionStore> {
    ArtifactAcquisitionStore::open(root, DEFAULT_HISTORY_LIMIT)
}

fn open_outcome(root: &Path) -> &'static str {
    match open(root) {
        Ok(_) => "opened",
        Err(e) => e.code,
    }
}

#[test]
fn acquisition_status_updated_equal_started_ok() {
    let status = accepted(&base());
    assert_eq!(status.started_at_unix_ms, 1000);
    assert_eq!(status.updated_at_unix_ms, 1000);
    assert_eq!(status.state, AcquisitionState::Transferring);
    assert_eq!(status.phase, Some(AcquisitionState::Transferring));
    assert_eq!(status.generation, 1);
    assert_eq!(status.acquisition_id, "acq-1");
    assert_eq!(status.sources.len(), 1);
    assert_eq!(status.sources[0].verified_bytes, 20);
    assert_eq!(status.terminal_at_unix_ms, None);
    // The validated status round-trips: validating its own value returns it unchanged.
    assert_eq!(accepted_value(&status), status);
    // Equality at the top of the range (`_integer` maximum 2**63-1, :311-316).
    let max = i64::MAX as u64;
    let top = accepted(&with(
        base(),
        &[
            ("started_at_unix_ms", json!(max)),
            ("updated_at_unix_ms", json!(max)),
        ],
    ));
    assert_eq!((top.started_at_unix_ms, top.updated_at_unix_ms), (max, max));
    let later = accepted(&with(base(), &[("updated_at_unix_ms", json!(1001))]));
    assert_eq!(later.updated_at_unix_ms, 1001);
}

fn accepted_value(status: &AcquisitionStatus) -> AcquisitionStatus {
    let result = validate_acquisition_status(&status.to_value());
    assert!(result.is_ok(), "refused: {:?}", result.as_ref().err());
    result.unwrap()
}

#[test]
fn acquisition_status_updated_before_started_refused() {
    assert_eq!(outcome(&base()), "accepted");
    for (started, updated) in [(1000, 999), (2, 1), (i64::MAX, i64::MAX - 1)] {
        let doc = with(
            base(),
            &[
                ("started_at_unix_ms", json!(started)),
                ("updated_at_unix_ms", json!(updated)),
            ],
        );
        assert_eq!(
            outcome(&doc),
            INVALID,
            "started={started} updated={updated}"
        );
    }
    // Also refused when the status is terminal and the terminal time would still be in order.
    let failed = with(
        terminal("failed"),
        &[
            ("started_at_unix_ms", json!(2001)),
            ("terminal_at_unix_ms", json!(3000)),
        ],
    );
    assert_eq!(outcome(&failed), INVALID);
}

#[test]
fn acquisition_status_terminal_null_ok() {
    for state in [
        "pending",
        "reserving",
        "discovering_sources",
        "transferring",
        "verifying_chunks",
        "verifying_pack",
        "promoting",
        "cancelling",
    ] {
        let status = accepted(&with(base(), &[("state", json!(state))]));
        assert_eq!(status.terminal_at_unix_ms, None, "{state}");
        assert_eq!(status.updated_at_unix_ms, 1000);
        assert_eq!(status.promotion_digest, None);
        assert_eq!(status.reason_code, None);
    }
    // A nonterminal state without a phase is also fine.
    let status = accepted(&with(base(), &[("phase", Value::Null)]));
    assert_eq!((status.phase, status.terminal_at_unix_ms), (None, None));
}

#[test]
fn acquisition_status_terminal_equal_updated_ok() {
    for (state, expected) in [
        ("ready", AcquisitionState::Ready),
        ("cancelled", AcquisitionState::Cancelled),
        ("failed", AcquisitionState::Failed),
    ] {
        let status = accepted(&terminal(state));
        assert_eq!(status.state, expected);
        assert_eq!(status.updated_at_unix_ms, 2000);
        assert_eq!(status.terminal_at_unix_ms, Some(2000), "{state}");
        assert_eq!(status.phase, None);
    }
    let ready = accepted(&terminal("ready"));
    assert_eq!(ready.promotion_digest, Some(digest('5')));
    let failed = accepted(&terminal("failed"));
    assert_eq!(failed.reason_code.as_deref(), Some("transfer_failed"));
    let later = accepted(&with(
        terminal("cancelled"),
        &[("terminal_at_unix_ms", json!(2001))],
    ));
    assert_eq!(later.terminal_at_unix_ms, Some(2001));
}

#[test]
fn acquisition_status_terminal_before_updated_refused() {
    assert_eq!(outcome(&terminal("cancelled")), "accepted");
    for state in ["ready", "cancelled", "failed"] {
        for (updated, terminal_at) in [(2000, 1999), (2000, 1000), (i64::MAX, i64::MAX - 1)] {
            let doc = with(
                terminal(state),
                &[
                    ("updated_at_unix_ms", json!(updated)),
                    ("terminal_at_unix_ms", json!(terminal_at)),
                ],
            );
            assert_eq!(outcome(&doc), INVALID, "{state} {updated} {terminal_at}");
        }
    }
}

#[test]
fn acquisition_status_started_zero_refused() {
    let one = accepted(&with(
        base(),
        &[
            ("started_at_unix_ms", json!(1)),
            ("updated_at_unix_ms", json!(1)),
        ],
    ));
    assert_eq!((one.started_at_unix_ms, one.updated_at_unix_ms), (1, 1));
    // started >= 1 (:1191-1195), the int range ends at 2**63-1 (:311-316).
    for started in [
        json!(0),
        json!(-1),
        json!(i64::MIN),
        json!(9_223_372_036_854_775_808u64),
    ] {
        let doc = with(
            base(),
            &[
                ("started_at_unix_ms", started.clone()),
                ("updated_at_unix_ms", json!(1000)),
            ],
        );
        assert_eq!(outcome(&doc), INVALID, "started={started}");
    }
    let zeros = with(
        base(),
        &[
            ("started_at_unix_ms", json!(0)),
            ("updated_at_unix_ms", json!(0)),
        ],
    );
    assert_eq!(outcome(&zeros), INVALID);
}

#[test]
fn acquisition_status_bool_timestamp_refused() {
    assert_eq!(outcome(&base()), "accepted");
    assert_eq!(outcome(&terminal("cancelled")), "accepted");
    // `type(value) is not int` (:314): a bool is never an int timestamp, nor is a float.
    for field in [
        "started_at_unix_ms",
        "updated_at_unix_ms",
        "generation",
        "total_bytes",
        "chunk_count",
    ] {
        for bad in [json!(true), json!(false), json!(1000.0), json!("1000")] {
            let doc = with(base(), &[(field, bad.clone())]);
            assert_eq!(outcome(&doc), INVALID, "{field}={bad}");
        }
    }
    for bad in [json!(true), json!(false), json!(2000.0)] {
        let doc = with(
            terminal("cancelled"),
            &[("terminal_at_unix_ms", bad.clone())],
        );
        assert_eq!(outcome(&doc), INVALID, "terminal={bad}");
    }
    // `retryable` must be exactly a bool (:1188); a number is refused.
    assert_eq!(outcome(&with(base(), &[("retryable", json!(0))])), INVALID);
    // Rates are int or float (:319-328), not bool.
    assert_eq!(
        outcome(&with(
            base(),
            &[("aggregate_bytes_per_second", json!(true))]
        )),
        INVALID
    );
    // An int rate stays an int (the oracle returns the decoded value, :1247).
    let int_rate = accepted(&with(base(), &[("aggregate_bytes_per_second", json!(7))]));
    assert_eq!(int_rate.aggregate_bytes_per_second, PyNumber::Int(7));
    assert_eq!(
        accepted(&base()).aggregate_bytes_per_second,
        PyNumber::Float(1.5)
    );
    // Only the type refuses these: with started_at 1, `true` (1) would pass updated >= started,
    // and with updated_at 1 it would pass terminal >= updated (:314 via :1191-1203); the int 1 in
    // the same place is accepted.
    let ones = with(
        base(),
        &[
            ("started_at_unix_ms", json!(1)),
            ("updated_at_unix_ms", json!(1)),
        ],
    );
    assert_eq!(outcome(&ones), "accepted");
    let mut refused = vec![with(ones.clone(), &[("updated_at_unix_ms", json!(true))])];
    for state in ["ready", "cancelled", "failed"] {
        let ones = with(
            terminal(state),
            &[
                ("started_at_unix_ms", json!(1)),
                ("updated_at_unix_ms", json!(1)),
                ("terminal_at_unix_ms", json!(1)),
            ],
        );
        assert_eq!(outcome(&ones), "accepted", "{state}");
        refused.push(with(ones, &[("terminal_at_unix_ms", json!(true))]));
    }
    for doc in &refused {
        assert_eq!(outcome(doc), INVALID, "{doc}");
    }
    // The store refuses the same documents in current.json and leaves the file as it was.
    let root = store_root("acq-bool-open-ok", Some(ones.to_string().as_bytes()), None);
    assert_eq!(open_outcome(&root), "opened");
    fs::remove_dir_all(&root).unwrap();
    for doc in &refused {
        let bytes = doc.to_string().into_bytes();
        let root = store_root("acq-bool-open", Some(&bytes), None);
        assert_eq!(open_outcome(&root), "artifact_state_corrupt", "{doc}");
        assert_eq!(fs::read(root.join("state/current.json")).unwrap(), bytes);
        assert!(!root.join("state/history.json").exists());
        fs::remove_dir_all(&root).unwrap();
    }
}

#[test]
fn acquisition_status_interrupted_current_recovered_failed() {
    let prior = with(
        terminal("cancelled"),
        &[("acquisition_id", json!("acq-0")), ("generation", json!(1))],
    );
    // 1. An interrupted transfer whose last update lies ahead of the clock keeps that time.
    let current = with(
        base(),
        &[
            ("generation", json!(2)),
            ("updated_at_unix_ms", json!(FUTURE)),
        ],
    );
    let root = store_root(
        "acq-recover-future",
        Some(current.to_string().as_bytes()),
        Some(json!([prior]).to_string().as_bytes()),
    );
    let store = open(&root);
    assert!(store.is_ok(), "open refused: {:?}", store.as_ref().err());
    let store = store.unwrap();
    let ledger = store.ledger().unwrap();
    assert_eq!(ledger.current, None);
    assert_eq!(ledger.generation, 2);
    assert_eq!(ledger.history.len(), 2);
    assert_eq!(ledger.history[0], accepted(&prior));
    let recovered = with(
        current.clone(),
        &[
            ("state", json!("failed")),
            ("phase", Value::Null),
            ("reason_code", json!("interrupted_transfer")),
            ("retryable", json!(true)),
            ("updated_at_unix_ms", json!(FUTURE)),
            ("terminal_at_unix_ms", json!(FUTURE)),
        ],
    );
    assert_eq!(ledger.history[1], accepted(&recovered));
    assert!(!root.join("state/current.json").exists());
    // The executed oracle's history.json for exactly these two files, byte for byte.
    assert_eq!(
        String::from_utf8(fs::read(root.join("state/history.json")).unwrap()).unwrap(),
        ORACLE_HISTORY
    );
    for dir in ["objects", "partials", "quarantine", "promoted", "state"] {
        assert!(root.join(dir).is_dir(), "{dir}");
    }
    // Reopening finds nothing left to recover and the same ledger.
    assert_eq!(open(&root).unwrap().ledger().unwrap(), ledger);
    fs::remove_dir_all(&root).unwrap();

    // 2. A past update moves to the recovery time: nondecreasing, terminal == updated.
    let root = store_root(
        "acq-recover-past",
        Some(base().to_string().as_bytes()),
        None,
    );
    let before = now_ms();
    let store = open(&root).unwrap();
    let after = now_ms();
    let ledger = store.ledger().unwrap();
    assert_eq!(ledger.current, None);
    assert_eq!(ledger.history.len(), 1);
    let failed = &ledger.history[0];
    assert_eq!(failed.state, AcquisitionState::Failed);
    assert_eq!(failed.phase, None);
    assert_eq!(failed.reason_code.as_deref(), Some("interrupted_transfer"));
    assert!(failed.retryable);
    assert_eq!(failed.started_at_unix_ms, 1000);
    assert!(
        (before..=after).contains(&failed.updated_at_unix_ms),
        "{before} <= {} <= {after}",
        failed.updated_at_unix_ms
    );
    assert_eq!(failed.terminal_at_unix_ms, Some(failed.updated_at_unix_ms));
    assert!(!root.join("state/current.json").exists());
    fs::remove_dir_all(&root).unwrap();

    // 3. A current status that is already terminal is moved to history unchanged.
    let done = terminal("ready");
    let root = store_root(
        "acq-recover-terminal",
        Some(done.to_string().as_bytes()),
        None,
    );
    let ledger = open(&root).unwrap().ledger().unwrap();
    assert_eq!(
        (ledger.current, ledger.history),
        (None, vec![accepted(&done)])
    );
    assert!(!root.join("state/current.json").exists());
    fs::remove_dir_all(&root).unwrap();

    // 4. A live write_current is what the next open recovers; finish appends and clears it.
    let root = store_root("acq-recover-write", None, None);
    let mut store = open(&root).unwrap();
    store.write_current(&PyValue::from_json(&base())).unwrap();
    assert_eq!(store.ledger().unwrap().current, Some(accepted(&base())));
    let ledger = open(&root).unwrap().ledger().unwrap();
    assert_eq!(ledger.current, None);
    assert_eq!(
        ledger.history[0].reason_code.as_deref(),
        Some("interrupted_transfer")
    );
    let mut store = open(&root).unwrap();
    let next = with(
        terminal("cancelled"),
        &[("acquisition_id", json!("acq-2")), ("generation", json!(3))],
    );
    let next_status = PyValue::from_json(&next);
    store
        .write_current(&PyValue::from_json(&with(
            base(),
            &[("acquisition_id", json!("acq-2")), ("generation", json!(3))],
        )))
        .unwrap();
    store.finish(&next_status).unwrap();
    let ledger = store.ledger().unwrap();
    assert_eq!(ledger.current, None);
    assert_eq!(ledger.generation, 3);
    assert_eq!(ledger.history.len(), 2);
    assert_eq!(ledger.history[1], accepted(&next));
    // finish refuses a status without a terminal time (:400-401).
    assert_eq!(
        store.finish(&PyValue::from_json(&base())).unwrap_err().code,
        "artifact_terminal_status_invalid"
    );
    fs::remove_dir_all(&root).unwrap();

    // 5. finish keeps the last history_limit statuses (:405): 65 terminal entries plus the
    // recovered one, opened with history_limit 64, leave generations 3..=65 and the recovery.
    let entries: Vec<Value> = (1..=65)
        .map(|g| {
            with(
                terminal("cancelled"),
                &[
                    ("acquisition_id", json!(format!("acq-h{g}"))),
                    ("generation", json!(g)),
                ],
            )
        })
        .collect();
    let interrupted = with(
        base(),
        &[
            ("generation", json!(66)),
            ("updated_at_unix_ms", json!(FUTURE)),
        ],
    );
    let root = store_root(
        "acq-recover-limit",
        Some(interrupted.to_string().as_bytes()),
        Some(Value::Array(entries.clone()).to_string().as_bytes()),
    );
    let ledger = ArtifactAcquisitionStore::open(&root, 64)
        .unwrap()
        .ledger()
        .unwrap();
    let generations: Vec<u64> = ledger.history.iter().map(|s| s.generation).collect();
    assert_eq!(generations, (3..=66).collect::<Vec<u64>>());
    let kept: Vec<AcquisitionStatus> = entries[2..].iter().map(accepted).collect();
    assert_eq!(ledger.history[..63], kept[..]);
    assert_eq!(
        ledger.history[63].reason_code.as_deref(),
        Some("interrupted_transfer")
    );
    // The executed oracle's history.json for exactly these two files (86290 bytes).
    let written = fs::read(root.join("state/history.json")).unwrap();
    assert_eq!(
        format!("{:x}", Sha256::digest(&written)),
        "c1dfc01d72f6e5ab4ddb38e20617099c736a66e295a3cd26051d8c429b94f43d"
    );
    fs::remove_dir_all(&root).unwrap();
}

#[test]
fn acquisition_status_corrupt_current_refused() {
    let valid = base().to_string();
    let control = store_root("acq-corrupt-control", Some(valid.as_bytes()), None);
    assert_eq!(open_outcome(&control), "opened");
    fs::remove_dir_all(&control).unwrap();
    let bool_time = with(base(), &[("updated_at_unix_ms", json!(true))]).to_string();
    let truncated = &valid[..valid.len() / 2];
    let mut bom = b"\xef\xbb\xbf".to_vec();
    bom.extend_from_slice(valid.as_bytes());
    let mut trailing = valid.clone().into_bytes();
    trailing.extend_from_slice(b" x");
    let duplicate_bad = format!(
        "{}, \"updated_at_unix_ms\": true}}",
        &valid[..valid.len() - 1]
    );
    let corrupt: Vec<(&str, Vec<u8>)> = vec![
        ("empty", Vec::new()),
        ("truncated", truncated.as_bytes().to_vec()),
        ("not json", b"current".to_vec()),
        ("list", b"[]".to_vec()),
        ("null", b"null".to_vec()),
        ("number", b"7".to_vec()),
        ("invalid utf-8", b"\xff\xfe".to_vec()),
        ("utf-8 bom", bom),
        ("trailing data", trailing),
        ("bool timestamp", bool_time.into_bytes()),
        ("duplicate key, last is bad", duplicate_bad.into_bytes()),
        ("missing key", {
            let mut doc = base();
            doc.as_object_mut().unwrap().remove("stage_id");
            doc.to_string().into_bytes()
        }),
        (
            "extra key",
            with(base(), &[("extra", json!(1))])
                .to_string()
                .into_bytes(),
        ),
        ("int over 4300 digits", {
            let digits = "1".repeat(4301);
            valid
                .replacen(
                    "\"total_bytes\":100",
                    &format!("\"total_bytes\":{digits}"),
                    1,
                )
                .into_bytes()
        }),
    ];
    for (name, bytes) in &corrupt {
        let root = store_root("acq-corrupt", Some(bytes), None);
        assert_eq!(open_outcome(&root), "artifact_state_corrupt", "{name}");
        // Nothing is recovered or rewritten: current.json stays, history.json is not created.
        assert_eq!(
            &fs::read(root.join("state/current.json")).unwrap(),
            bytes,
            "{name}"
        );
        assert!(!root.join("state/history.json").exists(), "{name}");
        fs::remove_dir_all(&root).unwrap();
    }
    // A directory where current.json should be is unreadable state, not absent state.
    let root = store_root("acq-corrupt-dir", None, None);
    fs::create_dir(root.join("state/current.json")).unwrap();
    assert_eq!(open_outcome(&root), "artifact_state_corrupt");
    fs::remove_dir_all(&root).unwrap();
    // json.loads keeps the last duplicate: a bad value followed by a good one is accepted.
    let duplicate_good = valid.replacen('{', r#"{"updated_at_unix_ms": true, "#, 1);
    let root = store_root(
        "acq-corrupt-dup-good",
        Some(duplicate_good.as_bytes()),
        None,
    );
    assert_eq!(open_outcome(&root), "opened");
    fs::remove_dir_all(&root).unwrap();
    // An unhashable state escapes as the oracle's TypeError (frozenset membership, :1122).
    let list_state = with(base(), &[("state", json!(["transferring"]))]).to_string();
    let root = store_root("acq-corrupt-type", Some(list_state.as_bytes()), None);
    assert_eq!(open_outcome(&root), "TypeError");
    fs::remove_dir_all(&root).unwrap();
    // ledger() rereads current.json (:446-452): corruption after open is refused there too.
    let root = store_root("acq-corrupt-later", None, None);
    let mut store = open(&root).unwrap();
    store.write_current(&PyValue::from_json(&base())).unwrap();
    fs::write(root.join("state/current.json"), b"{\"protocol\":").unwrap();
    assert_eq!(store.ledger().unwrap_err().code, "artifact_state_corrupt");
    fs::remove_dir_all(&root).unwrap();
    // A corrupt history is its own code (:393-397), checked after current.json.
    for history in [&b"{}"[..], b"[1]", b"[", b"[{}]"] {
        let root = store_root("acq-corrupt-history", None, Some(history));
        assert_eq!(open_outcome(&root), "artifact_history_corrupt");
        fs::remove_dir_all(&root).unwrap();
    }
    let root = store_root("acq-corrupt-both", Some(b"{"), Some(b"{"));
    assert_eq!(open_outcome(&root), "artifact_state_corrupt");
    fs::remove_dir_all(&root).unwrap();
}

#[test]
fn acquisition_status_terminal_state_requires_terminal_timestamp() {
    for state in ["ready", "cancelled", "failed"] {
        assert_eq!(outcome(&terminal(state)), "accepted", "{state}");
        // (state in _TERMINAL_STATES) != (terminal is not None) (:1241).
        let no_time = with(terminal(state), &[("terminal_at_unix_ms", Value::Null)]);
        assert_eq!(outcome(&no_time), INVALID, "{state} without terminal time");
        // state in _TERMINAL_STATES and phase is not None (:1244).
        for phase in ["promoting", "transferring", "ready"] {
            let phased = with(terminal(state), &[("phase", json!(phase))]);
            assert_eq!(outcome(&phased), INVALID, "{state} with phase {phase}");
        }
    }
    // A nonterminal state with a terminal time is refused.
    for state in ["transferring", "cancelling", "promoting"] {
        let doc = with(
            base(),
            &[
                ("state", json!(state)),
                ("terminal_at_unix_ms", json!(1000)),
            ],
        );
        assert_eq!(outcome(&doc), INVALID, "{state} with terminal time");
    }
    // ready carries promotion_digest and only ready does (:1242).
    let ready_without = with(terminal("ready"), &[("promotion_digest", Value::Null)]);
    assert_eq!(outcome(&ready_without), INVALID);
    let cancelled_with = with(
        terminal("cancelled"),
        &[("promotion_digest", json!(digest('5')))],
    );
    assert_eq!(outcome(&cancelled_with), INVALID);
}

#[test]
fn acquisition_status_reason_code_grammar_not_enum() {
    // `_REASON = [a-z][a-z0-9_]{0,127}\Z` (:44): a grammar, not a closed reason set.
    let at_edge = format!("a{}", "_".repeat(127));
    for reason in [
        "zz_unlisted_reason_9",
        "x",
        "a0",
        "interrupted_transfer",
        at_edge.as_str(),
    ] {
        let doc = with(terminal("failed"), &[("reason_code", json!(reason))]);
        let status = accepted(&doc);
        assert_eq!(status.reason_code.as_deref(), Some(reason));
    }
    let too_long = format!("a{}", "b".repeat(128));
    for reason in [
        json!("Bad-Code"),
        json!("bad-code"),
        json!("Upper"),
        json!("9starts_with_digit"),
        json!("_leading_underscore"),
        json!(""),
        json!("trailing\n"),
        json!("caf\u{e9}"),
        json!(too_long),
        json!(7),
        json!(["x"]),
    ] {
        let doc = with(terminal("failed"), &[("reason_code", reason.clone())]);
        assert_eq!(outcome(&doc), INVALID, "reason={reason}");
    }
    // A reason code on a non-failed state, and a failed state without one, are refused (:1243).
    for doc in [
        with(base(), &[("reason_code", json!("zz_unlisted_reason_9"))]),
        with(
            terminal("ready"),
            &[("reason_code", json!("zz_unlisted_reason_9"))],
        ),
        with(
            terminal("cancelled"),
            &[("reason_code", json!("zz_unlisted_reason_9"))],
        ),
        with(terminal("failed"), &[("reason_code", Value::Null)]),
    ] {
        assert_eq!(outcome(&doc), INVALID, "{doc}");
    }
    // The same grammar holds for a reason decoded from text with an escape.
    let escaped = loads(br#""zz_\u0075nlisted""#).unwrap();
    let mut doc = PyValue::from_json(&terminal("failed"));
    if let PyValue::Dict(entries) = &mut doc {
        for (key, value) in entries.iter_mut() {
            if key.to_text().as_deref() == Some("reason_code") {
                *value = escaped.clone();
            }
        }
    }
    let status = validate_acquisition_status(&doc).unwrap();
    assert_eq!(status.reason_code.as_deref(), Some("zz_unlisted"));
}

/// `history.json` written by the executed oracle (CPython 3.14.2, f4e366e9) when it opens a store
/// whose history holds the cancelled `acq-0` and whose current.json is the interrupted `acq-1`
/// updated at [`FUTURE`] (case 1 of `acquisition_status_interrupted_current_recovered_failed`).
const ORACLE_HISTORY: &str = concat!(
    r#"[{"acquisition_id":"acq-0","active_source_count":1,"aggregate_bytes_per_second":1.5,"#,
    r#""assignment_digest":"sha256:2222222222222222222222222222222222222222222222222222222222222222","#,
    r#""assignment_id":"assignment-1","cached_verified_bytes":10,"chunk_count":4,"#,
    r#""duplicate_bytes_prevented":0,"eligible_source_count":2,"eta_seconds":40,"evidence_generation":0,"#,
    r#""feasibility_digest":"sha256:4444444444444444444444444444444444444444444444444444444444444444","#,
    r#""generation":1,"layer_end_exclusive":4,"layer_start":0,"#,
    r#""manifest_digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111","#,
    r#""missing_bytes":60,"model_id":"example/model","#,
    r#""model_revision":"0123456789abcdef0123456789abcdef01234567","origin_bytes":10,"phase":null,"#,
    r#""placement_id":"placement-1","promotion_digest":null,"#,
    r#""protocol":"mycelium.swarm_artifact_acquisition.v1","quarantined_bytes":0,"reason_code":null,"#,
    r#""representation":"bf16","#,
    r#""representation_digest":"sha256:3333333333333333333333333333333333333333333333333333333333333333","#,
    r#""resumed_chunk_count":1,"retryable":false,"source_rotation_count":0,"#,
    r#""sources":[{"source_ref":"source-0123456789ab","state":"active","verified_bytes":20}],"#,
    r#""stage_id":"stage-0","started_at_unix_ms":1000,"state":"cancelled","terminal_at_unix_ms":2000,"#,
    r#""total_bytes":100,"transferred_verified_bytes":30,"updated_at_unix_ms":2000,"verified_chunk_count":2},"#,
    r#"{"acquisition_id":"acq-1","active_source_count":1,"aggregate_bytes_per_second":1.5,"#,
    r#""assignment_digest":"sha256:2222222222222222222222222222222222222222222222222222222222222222","#,
    r#""assignment_id":"assignment-1","cached_verified_bytes":10,"chunk_count":4,"#,
    r#""duplicate_bytes_prevented":0,"eligible_source_count":2,"eta_seconds":40,"evidence_generation":0,"#,
    r#""feasibility_digest":"sha256:4444444444444444444444444444444444444444444444444444444444444444","#,
    r#""generation":2,"layer_end_exclusive":4,"layer_start":0,"#,
    r#""manifest_digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111","#,
    r#""missing_bytes":60,"model_id":"example/model","#,
    r#""model_revision":"0123456789abcdef0123456789abcdef01234567","origin_bytes":10,"phase":null,"#,
    r#""placement_id":"placement-1","promotion_digest":null,"#,
    r#""protocol":"mycelium.swarm_artifact_acquisition.v1","quarantined_bytes":0,"#,
    r#""reason_code":"interrupted_transfer","representation":"bf16","#,
    r#""representation_digest":"sha256:3333333333333333333333333333333333333333333333333333333333333333","#,
    r#""resumed_chunk_count":1,"retryable":true,"source_rotation_count":0,"#,
    r#""sources":[{"source_ref":"source-0123456789ab","state":"active","verified_bytes":20}],"#,
    r#""stage_id":"stage-0","started_at_unix_ms":1000,"state":"failed","terminal_at_unix_ms":9000000000000,"#,
    r#""total_bytes":100,"transferred_verified_bytes":30,"updated_at_unix_ms":9000000000000,"verified_chunk_count":2}]"#,
    "\n"
);
