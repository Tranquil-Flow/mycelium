// SPDX-License-Identifier: AGPL-3.0-only
use latency_profiles::device::import_device;
use serde_json::{Value, json};
use std::{fs, path::PathBuf};
struct Fixture(PathBuf);
impl Fixture {
    fn new(name: &str) -> Self {
        let root = std::env::temp_dir().join(format!("fx-profile-{}-{name}", std::process::id()));
        fs::create_dir(&root).unwrap();
        fs::create_dir(root.join("attempt21")).unwrap();
        let s = Self(root);
        s.put("RESULT.json", &json!({"source_sha":"a".repeat(40),"exe_sha256":"b".repeat(64),"member_join":{"run_model":{"selected":"a9-route"}},"chats":[{"ttft_s":9.0}]}).to_string());
        s
    }
    fn put(&self, name: &str, text: &str) {
        fs::write(self.0.join("attempt21").join(name), text).unwrap();
    }
    fn import(&self) -> Value {
        serde_json::to_value(import_device(&self.0).unwrap()).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("owned fixture cleanup");
    }
}
fn phase<'a>(b: &'a Value, name: &str, machine: &str) -> &'a Value {
    b["profiles"]
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p["phase"] == name && p["machine"] == machine && p["origin"] == "MEASURED")
        .unwrap()
}
#[test]
fn imports_new_attempts_without_duplicate_logs_or_result_samples() {
    let f = Fixture::new("dedup");
    let events = "{\"event\":\"chat\",\"ttft_s\":2.0,\"total_s\":6.0,\"decode_tok_s\":10.0,\"done\":true}\n{\"event\":\"chat\",\"ttft_s\":4.0,\"total_s\":8.0,\"decode_tok_s\":12.0,\"done\":true}\n";
    f.put("events.jsonl", events);
    f.put("stdout.log", events);
    let b = f.import();
    assert_eq!(b["schema_version"], 2);
    let p = phase(&b, "ttft", "route");
    assert_eq!(p["statistics"]["count"], 2);
    assert_eq!(p["statistics"]["p50"], 2000.0);
    assert!(p["statistics"]["p99"].is_null());
    assert_eq!(p["statistics"]["status"], "BLOCKED");
    assert_eq!(phase(&b, "decode", "route")["unit"], "tokens/s");
    assert_eq!(p["source_sha"], "a".repeat(40));
    assert_eq!(b["files"][0]["sha256"].as_str().unwrap().len(), 64);
}
#[test]
fn preserves_censored_stop_failure_quit_and_rollover_without_success_claims() {
    let f = Fixture::new("phases");
    f.put("probe-stop-inflight.log", "{\"event\":\"stop_sent\",\"at\":10.0}\n{\"event\":\"owner_state\",\"state\":\"stopped\",\"seconds_after_stop\":75.11,\"cleanup_unproven\":{\"secret\":\"NEVER_COPY\"}}\n{\"event\":\"done\",\"owner_end\":75.11}\n");
    f.put(
        "quit-owner.json",
        "{\"seconds_to_exit\":7.33,\"quit_clean\":true,\"processes_left\":0}",
    );
    f.put(
        "SOAK-RESULT.json",
        "{\"rollovers\":[{\"start_s\":20.0,\"end_s\":40.0,\"gap_s\":20.0,\"gap_upper_s\":30.0}]}",
    );
    let b = f.import();
    let p = phase(&b, "stop", "owner");
    assert_eq!(p["samples"][0]["value"], 75110.0);
    assert_eq!(p["samples"][0]["outcome"], "cleanup_unproven");
    assert!(p["statistics"]["p50"].is_null());
    assert_eq!(phase(&b, "quit", "owner")["samples"][0]["value"], 7330.0);
    assert_eq!(
        phase(&b, "rollover", "route")["samples"][0]["upper_value"],
        30000.0
    );
    assert!(
        phase(&b, "close", "owner")["samples"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(!b.to_string().contains("NEVER_COPY"));
}
#[test]
fn keeps_recorded_host_load_os_and_unknown_hardware_without_inference() {
    let f = Fixture::new("metadata");
    f.put("events.jsonl", "{\"event\":\"installed\",\"host\":\"owner\",\"summary\":{\"os_version\":\"26.5.1\",\"load\":[8.0,7.0,6.0]}}\n{\"event\":\"owner_status\",\"latency_s\":0.01,\"hosts\":{\"owner\":{\"load\":[9.0,8.0,7.0]},\"laptop\":{\"load\":[2.0,1.0,1.0]}}}\n");
    let b = f.import();
    let p = phase(&b, "owner_status", "owner");
    assert_eq!(p["macos"], "26.5.1");
    assert!(p["hardware"].is_null());
    assert_eq!(p["samples"][0]["load"]["owner"][0], 9.0);
    assert!(p["statistics"]["p99"].is_null());
}
#[test]
fn rejects_negative_timing_symlinks_and_redacts_parse_errors() {
    let f = Fixture::new("invalid");
    f.put("events.jsonl", "{\"event\":\"chat\",\"ttft_s\":-1.0}\n");
    assert!(import_device(&f.0).is_err());
    f.put(
        "events.jsonl",
        "{\"event\":\"chat\",\"ttft_s\":\"SECRET_CANARY\"}\n",
    );
    let err = import_device(&f.0).unwrap_err().to_string();
    assert!(!err.contains("SECRET_CANARY"));
    fs::remove_file(f.0.join("attempt21/events.jsonl")).unwrap();
    std::os::unix::fs::symlink("RESULT.json", f.0.join("attempt21/events.jsonl")).unwrap();
    assert!(import_device(&f.0).is_err());
}
#[test]
fn older_stop_probe_is_imported_once_and_retains_cleanup_failure() {
    let f = Fixture::new("old_stop");
    f.put("probe-stop-duration-1.log", "{\"event\":\"stop_sent\"}\n{\"event\":\"state\",\"seconds_since_stop\":301.47,\"status\":{\"state\":\"stopped\",\"cleanup_unproven\":{\"code\":\"owner_route_stop_unproven\"}}}\n{\"event\":\"done\",\"seconds_since_stop\":301.47,\"status\":{\"state\":\"stopped\"}}\n");
    let b = f.import();
    let p = phase(&b, "stop", "owner");
    assert_eq!(p["samples"].as_array().unwrap().len(), 1);
    assert_eq!(p["samples"][0]["value"], 301470.0);
    assert_eq!(p["samples"][0]["outcome"], "cleanup_unproven");
}
#[test]
fn negative_compiler_duration_and_conflicting_rollover_summary_refuse() {
    let f = Fixture::new("bad_compile");
    f.put("events.jsonl","{\"event\":\"owner_status\",\"owner_run\":{\"run_id\":\"opaque\",\"build_seconds\":-1.0}}\n");
    assert!(import_device(&f.0).is_err());
}
#[test]
fn conflicting_rollover_summary_refuses() {
    let f = Fixture::new("rollover_conflict");
    f.put("events.jsonl", "");
    f.put("soak-events.jsonl","{\"event\":\"rollover_observation\",\"rollovers\":[{\"start_s\":1.0,\"end_s\":3.0,\"gap_s\":2.0}]}\n");
    f.put(
        "SOAK-RESULT.json",
        "{\"rollovers\":[{\"start_s\":1.0,\"end_s\":3.0,\"gap_s\":9.0}]}",
    );
    assert!(import_device(&f.0).is_err());
}
#[test]
fn conflicting_recorded_build_identity_refuses() {
    let f = Fixture::new("identity_conflict");
    f.put("events.jsonl",&json!({"event":"pre_ready_device_bytes","source_sha":"c".repeat(40),"exe_sha256":"b".repeat(64)}).to_string());
    assert!(import_device(&f.0).is_err());
}
#[test]
fn failed_quit_receipt_is_visible_blocker_not_a_measurement() {
    let f = Fixture::new("quit_failed");
    f.put("quit-owner.json", "command failed: SECRET_CANARY");
    let b = f.import();
    assert!(
        phase(&b, "quit", "owner")["samples"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(
        b["files"]
            .as_array()
            .unwrap()
            .iter()
            .any(|f| f["disposition"] == "BLOCKED_INVALID_QUIT_RECEIPT")
    );
    assert!(!b.to_string().contains("SECRET_CANARY"));
}
#[test]
fn repeated_status_or_summary_cannot_manufacture_independent_p99() {
    let f = Fixture::new("p99");
    f.put("events.jsonl", &"{\"event\":\"owner_status\",\"latency_s\":0.01,\"hosts\":{\"owner\":{\"load\":[1.0,1.0,1.0]}}}\n".repeat(120));
    let b = f.import();
    let p = phase(&b, "owner_status", "owner");
    assert_eq!(p["statistics"]["count"], 120);
    assert!(p["statistics"]["p99"].is_null());
    assert!(
        p["statistics"]["blocked_reasons"]
            .as_array()
            .unwrap()
            .iter()
            .any(|s| s == "independence_not_established")
    );
    let synthetic = b["profiles"]
        .as_array()
        .unwrap()
        .iter()
        .find(|p| {
            p["origin"] == "SYNTHETIC" && p["phase"] == "owner_status" && p["multiplier"] == 3
        })
        .unwrap();
    assert_eq!(synthetic["samples"][0]["value"], 30.0);
    assert_eq!(synthetic["statistics"]["status"], "SYNTHETIC_NOT_MEASURED");
}
