// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (partial: the durable artifact-admission journal equals the oracle's ledger on 26 executed states).
//!
//! The durable artifact-admission journal against the EXECUTED v0.1.4 sidecar.
//!
//! `fixtures/sidecar-artifact-oracle.json` is the recorded result of the real oracle
//! (`mycelium-iroh-sidecar @ f4e366e9`, release profile, probe `L9-evidence/r14-artifact-probe.rb`)
//! started with `--local-only` and the four `--artifact-*` flags over 27 journal states. A state the
//! oracle starts on (`ready_line`) must open here and leave the same bytes in the ledger; a state
//! it ends `fatal` on must be refused here and leave the ledger file as it was (never recreated,
//! never repaired).
#![cfg(all(feature = "iroh-adapter", unix))]
use runtime::artifact_journal::{DurableConfig, DurableObjects, LEDGER_FILE};
use serde_json::Value;
use std::{
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/sidecar-artifact-oracle.json")).unwrap()
}
fn now_ms() -> u64 {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
}
fn skew() -> u64 {
    budgets::BudgetId::PyMyceliumMobileArtifactGrantClockSkewMsL40
        .span(budgets::Entries::ZERO)
        .unwrap()
        .millis()
}
fn scratch(label: &str) -> PathBuf {
    let base = std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, PathBuf::from);
    let dir = base.join(format!("l9art-{label}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    dir
}
fn config(dir: &Path, state: &Path) -> DurableConfig {
    DurableConfig {
        objects: dir.join("objects"),
        state_dir: state.to_path_buf(),
        fence_capacity: budgets::BudgetId::ArtifactMaxConnections
            .count()
            .map(|entries| usize::try_from(entries.get()).unwrap())
            .unwrap(),
        clock_skew_ms: skew(),
        now: Arc::new(now_ms),
    }
}

/// Prepares the state directory the probe prepared for `case`.
fn prepare(case: &Value, dir: &Path) -> PathBuf {
    std::fs::create_dir_all(dir.join("objects")).unwrap();
    let state = dir.join("st");
    let ledger = state.join(LEDGER_FILE);
    if case["state_is_file"].as_bool().unwrap() {
        std::fs::write(&state, "x").unwrap();
        return state;
    }
    let before = case["ledger_before"].as_str();
    let kind = case["kind"].as_str().unwrap();
    if before.is_none() && kind == "file" {
        return state; // no state directory at all
    }
    std::fs::create_dir(&state).unwrap();
    match kind {
        "directory" => std::fs::create_dir(&ledger).unwrap(),
        "symlink" => {
            std::fs::write(dir.join("real-ledger"), before.unwrap()).unwrap();
            std::os::unix::fs::symlink(dir.join("real-ledger"), &ledger).unwrap();
        }
        _ => {
            std::fs::write(&ledger, before.unwrap()).unwrap();
            std::fs::set_permissions(
                &ledger,
                std::fs::Permissions::from_mode(
                    u32::try_from(case["mode"].as_u64().unwrap()).unwrap(),
                ),
            )
            .unwrap();
        }
    }
    if case["tmp_leftover"].as_bool().unwrap() {
        std::fs::write(state.join("source-ledger.jsonl.tmp"), "stale").unwrap();
    }
    state
}
fn ledger_after(state: &Path) -> Option<String> {
    let ledger = state.join(LEDGER_FILE);
    let meta = std::fs::symlink_metadata(&ledger).ok()?;
    if meta.file_type().is_symlink() {
        return Some("<symlink>".to_owned());
    }
    if meta.file_type().is_dir() {
        return Some("<directory>".to_owned());
    }
    std::fs::set_permissions(&ledger, std::fs::Permissions::from_mode(0o600)).unwrap();
    Some(std::fs::read_to_string(&ledger).unwrap())
}

#[test]
fn the_fixture_is_the_executed_oracle_table() {
    let f = fixture();
    assert!(
        f["provenance"]["oracle"]
            .as_str()
            .unwrap()
            .contains("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    assert!(
        f["provenance"]["oracle"]
            .as_str()
            .unwrap()
            .contains("RELEASE")
    );
    assert_eq!(f["provenance"]["cases"], 27);
    assert_eq!(f["cases"].as_array().unwrap().len(), 27);
}

#[test]
fn opening_equals_the_oracle_start_or_fatal_and_leaves_the_same_ledger_bytes() {
    let f = fixture();
    let mut checked = 0;
    for case in f["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        if case["closed_fd"].as_bool().unwrap() {
            continue; // a bootstrap case, not a journal state
        }
        let dir = scratch(name);
        let state = prepare(case, &dir);
        let started = now_ms();
        let opened = DurableObjects::open(config(&dir, &state));
        let finished = now_ms();
        let oracle_started = case["ready_line"].as_bool().unwrap();
        assert_eq!(
            opened.is_ok(),
            oracle_started,
            "{name}: the oracle {} (exit {}), got {:?}",
            if oracle_started {
                "starts"
            } else {
                "ends fatal"
            },
            case["exit"],
            opened.as_ref().err()
        );
        let expected = case["ledger_after"].as_str().map(str::to_owned);
        let got = ledger_after(&state);
        if name == "fresh_state_dir" {
            // The header carries "now" at creation: the oracle's own genesis is a time, ours is ours.
            let line = got.expect("a ledger was created");
            let header: Value = serde_json::from_str(line.trim_end()).unwrap();
            let genesis = header["genesis_unix_ms"].as_u64().unwrap();
            assert!(
                (started..=finished).contains(&genesis),
                "{name}: genesis {genesis}"
            );
            assert_eq!(
                line,
                format!(
                    "{{\"genesis_unix_ms\":{genesis},\"protocol\":\"mycelium.artifact_source_ledger.v1\"}}\n"
                ),
                "{name}: header bytes (key order, newline)"
            );
            assert!(expected.is_some(), "{name}: the oracle left a ledger");
        } else {
            assert_eq!(got, expected, "{name}: ledger bytes after");
        }
        assert!(
            !state.join("source-ledger.jsonl.tmp").exists() || !state.is_dir(),
            "{name}: no tmp file is left"
        );
        drop(opened);
        // A mode-000 ledger would block the removal: loosen it first.
        if let Ok(meta) = std::fs::symlink_metadata(state.join(LEDGER_FILE))
            && meta.is_file()
        {
            std::fs::set_permissions(
                state.join(LEDGER_FILE),
                std::fs::Permissions::from_mode(0o600),
            )
            .unwrap();
        }
        std::fs::remove_dir_all(&dir).unwrap();
        checked += 1;
    }
    assert_eq!(checked, 26);
}
