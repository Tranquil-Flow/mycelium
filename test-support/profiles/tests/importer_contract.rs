// SPDX-License-Identifier: AGPL-3.0-only
use latency_profiles::*;
use std::sync::atomic::{AtomicUsize, Ordering};
static ID: AtomicUsize = AtomicUsize::new(0);
struct Fixture(std::path::PathBuf);
impl Fixture {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l2-profile-{}-{}",
            std::process::id(),
            ID.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&p).unwrap();
        Self(p)
    }
    fn populate(&self, malformed: bool) {
        for a in [6, 7, 8] {
            let d = self.0.join(format!("attempt{a}"));
            std::fs::create_dir(&d).unwrap();
            let result = serde_json::json!({"exe_sha256":"a".repeat(64),"member_join":{"run_model":{"selected":"a9-route"}},"chats":[{"ttft_s":1.0}],"grant":"DO_NOT_COPY","join":"DO_NOT_COPY","cookie":"DO_NOT_COPY","tokens":["DO_NOT_COPY"],"native_control":{"key":"DO_NOT_COPY"}});
            std::fs::write(d.join("RESULT.json"), result.to_string()).unwrap();
            let records = [
                serde_json::json!({"event":"run_progress","elapsed_s":0.0,"owner_run":{"step":"model","state":"running"}}),
                serde_json::json!({"event":"run_progress","elapsed_s":1.0,"owner_run":{"step":"compile","state":"running"}}),
                serde_json::json!({"event":"run_progress","elapsed_s":3.0,"owner_run":{"step":"publish","state":"running"}}),
                serde_json::json!({"event":"run_progress","elapsed_s":6.0,"owner_run":{"step":"serving","state":"serving"}}),
                serde_json::json!({"event":"chat","ttft_s":1.0,"total_s":2.0,"grant":{"keys":["DO_NOT_COPY"]},"tokens":["DO_NOT_COPY"]}),
                serde_json::json!({"event":"chat","ttft_s":3.0,"total_s":4.0,"cookie":"DO_NOT_COPY"}),
            ];
            let text = if malformed {
                "{bad: DO_NOT_COPY".into()
            } else {
                records
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join("\n")
            };
            std::fs::write(d.join("events.jsonl"), text).unwrap();
            // These are deliberately malformed: opening/parsing either would fail.
            std::fs::write(d.join("window.json"), "not JSON DO_NOT_COPY").unwrap();
            std::fs::write(d.join("native-control.json"), "not JSON DO_NOT_COPY").unwrap();
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).expect("remove isolated profile fixture");
    }
}
#[test]
fn allowlist_omits_secret_fields_deduplicates_results_and_keeps_raw_phases() {
    let f = Fixture::new();
    f.populate(false);
    let b = import_saved(&f.0).unwrap();
    let json = serde_json::to_string(&b).unwrap();
    assert!(!json.contains("DO_NOT_COPY"));
    assert!(!json.contains("native_control"));
    let ttft = b
        .profiles
        .iter()
        .find(|p| p.id == "v014-attempt6-ttft-v1")
        .unwrap();
    assert_eq!(
        ttft.samples.iter().map(|s| s.value).collect::<Vec<_>>(),
        [1000.0, 3000.0]
    );
    assert_eq!(ttft.statistics.count, 2);
    assert_eq!(ttft.statistics.p50_ms, Some(1000.0));
    assert_eq!(ttft.statistics.p99_ms, None);
    let compile = b
        .profiles
        .iter()
        .find(|p| p.id == "v014-attempt6-compile-v1")
        .unwrap();
    assert_eq!(compile.samples[0].value, 2000.0);
    assert!(!compile.exact);
    assert!(compile.qualified_p99().is_none());
    let loaded = ttft.loaded(3).unwrap();
    assert_eq!(loaded.origin, "SYNTHETIC");
    assert_eq!(loaded.samples[1].value, 9000.0);
    assert!(loaded.qualified_p99().is_none());
    assert_eq!(loaded.samples[0].host_load, None);
    assert!(
        b.files
            .iter()
            .any(|f| f.source == "attempt6/SOAK-RESULT.json" && f.status == "MISSING")
    );
}
#[test]
fn nullable_unstarted_phase_is_not_a_parser_failure() {
    assert!(
        parse_evidence(r#"{"event":"owner_status","owner_run":{"step":null,"state":"idle"}}"#)
            .is_ok()
    );
}
#[test]
fn malformed_input_error_does_not_echo_secret_values() {
    let f = Fixture::new();
    f.populate(true);
    let error = import_saved(&f.0).unwrap_err().to_string();
    assert_eq!(error, "evidence_json_invalid");
    assert!(!error.contains("DO_NOT_COPY"));
}
