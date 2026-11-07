// SPDX-License-Identifier: AGPL-3.0-only
//! Round 5: a stalled desktop/native qualification must name the phase it stalled in.
//! The owner and the desktop probe print ordered milestones into the evidence log; the
//! harness timestamps their first appearance and, on the (unchanged) deadline, reports
//! the first milestone that never arrived and the phase that was in progress.
use budgets::{BudgetId as B, Entries};
use node_service::phase::{self, Timeline};

/// The harness deadline is the registered native request wall, never a literal.
fn deadline_ms() -> u64 {
    B::MemberChosenWall.span(Entries::ZERO).unwrap().millis()
}

fn line(name: &str, since: u64) -> String {
    format!("SLICE_PHASE {name} detail=x wall_ms=1000 since_start_ms={since}\n")
}

#[test]
fn an_empty_log_stalls_in_desktop_boot() {
    let stall = Timeline::default().stall();
    assert_eq!(stall.phase, "desktop_boot");
    assert_eq!(stall.awaiting, "L11_NATIVE_READY");
    assert_eq!(stall.last_seen, None);
}

#[test]
fn each_missing_milestone_names_its_own_phase() {
    let cases = [
        (vec![], "desktop_boot"),
        (vec!["L11_NATIVE_READY"], "page_send"),
        (vec!["L11_NATIVE_READY", "job_begin"], "worker_startup"),
        (
            vec!["L11_NATIVE_READY", "job_begin", "member_registered"],
            "staging",
        ),
        (
            vec![
                "L11_NATIVE_READY",
                "job_begin",
                "member_registered",
                "staging_done",
                "member_ready",
                "owner_leaf_ready",
            ],
            "grant",
        ),
        (
            vec![
                "L11_NATIVE_READY",
                "job_begin",
                "member_registered",
                "staging_done",
                "member_ready",
                "owner_leaf_ready",
                "token_grant",
                "token_signed",
            ],
            "page_ack",
        ),
        (
            vec![
                "L11_NATIVE_READY",
                "job_begin",
                "member_registered",
                "staging_done",
                "member_ready",
                "owner_leaf_ready",
                "token_grant",
                "token_signed",
                "page_ack",
            ],
            "dom_token",
        ),
    ];
    for (seen, expected) in cases {
        let mut log = String::new();
        for (i, name) in seen.iter().enumerate() {
            if name.starts_with("L11_") {
                log.push_str(&format!("{name}\n"));
            } else {
                log.push_str(&line(name, i as u64 * 10));
            }
        }
        let mut timeline = Timeline::default();
        timeline.observe(&log, 500);
        assert_eq!(timeline.stall().phase, expected, "seen {seen:?}");
    }
}

#[test]
fn a_complete_run_has_no_stall_and_records_first_seen_times() {
    let mut log = String::new();
    let mut timeline = Timeline::default();
    for (index, milestone) in phase::MILESTONES.iter().enumerate() {
        if milestone.marker.starts_with("L11_") {
            log.push_str(&format!("{}\n", milestone.marker));
        } else {
            log.push_str(&line(milestone.marker, 0));
        }
        timeline.observe(&log, 100 * (index as u64 + 1));
        // Observing again later must not move a first-seen time.
        timeline.observe(&log, 100 * (index as u64 + 1) + 7);
    }
    assert!(timeline.complete());
    let report = timeline.report("done", deadline_ms());
    let seen = report["seen"].as_array().unwrap();
    assert_eq!(seen.len(), phase::MILESTONES.len());
    assert_eq!(seen[0]["marker"], "L11_NATIVE_READY");
    assert_eq!(seen[0]["elapsed_ms"], 100);
    assert_eq!(report["outcome"], "done");
}

#[test]
fn the_stall_report_names_phase_awaited_marker_and_last_seen_time() {
    let mut log = String::new();
    log.push_str("L11_NATIVE_READY\n");
    log.push_str(&line("job_begin", 1));
    log.push_str(&line("member_registered", 2));
    let mut timeline = Timeline::default();
    timeline.observe("L11_NATIVE_READY\n", 1500);
    timeline.observe(&log, 4200);
    let report = timeline.report("deadline", deadline_ms());
    assert_eq!(report["outcome"], "deadline");
    assert_eq!(report["deadline_ms"], deadline_ms());
    assert_eq!(report["stalled_phase"], "staging");
    assert_eq!(report["awaiting"], "staging_done");
    assert_eq!(report["last_seen"]["marker"], "member_registered");
    assert_eq!(report["last_seen"]["elapsed_ms"], 4200);
    assert_eq!(
        report["seen"][0]["elapsed_ms"], 1500,
        "first-seen time is kept"
    );
}

#[test]
fn markers_must_be_whole_lines_not_substrings() {
    // A log line that merely mentions a milestone (an error text) is not a milestone.
    let mut timeline = Timeline::default();
    timeline.observe("error: waiting for L11_NATIVE_READY failed\n", 10);
    timeline.observe("note SLICE_PHASE job_begin pasted mid-line\n", 20);
    assert_eq!(timeline.stall().phase, "desktop_boot");
}

#[test]
fn mark_formats_the_documented_line() {
    let text = phase::format("staging_done", "segments=236", 1_000_000, 4321);
    assert_eq!(
        text,
        "SLICE_PHASE staging_done detail=segments=236 wall_ms=1000000 since_start_ms=4321"
    );
}

#[test]
fn a_driver_failure_line_is_recorded_with_its_code_and_first_seen_time() {
    let mut timeline = Timeline::default();
    timeline.observe(&line("job_begin", 1), 100);
    assert_eq!(timeline.failure(), None);
    let log = format!(
        "{}slice driver failed: token_path_grant_expired\n",
        line("token_grant", 2)
    );
    timeline.observe(&log, 700);
    timeline.observe(&log, 900);
    assert_eq!(
        timeline.failure(),
        Some(("token_path_grant_expired".to_owned(), 700))
    );
    let report = timeline.report("driver_failed", deadline_ms());
    assert_eq!(report["failure"]["code"], "token_path_grant_expired");
    assert_eq!(report["failure"]["elapsed_ms"], 700);
}

#[test]
fn a_probe_failure_receipt_is_a_failure_too() {
    let mut timeline = Timeline::default();
    timeline.observe("L11_SLICE_FAILED_native_send\n", 50);
    assert_eq!(timeline.failure(), Some(("native_send".to_owned(), 50)));
}

#[test]
fn page_fetch_receipts_are_kept_in_order_with_first_seen_times() {
    let mut timeline = Timeline::default();
    timeline.observe("L11_SLICE_PAGE_FETCH_START_events_1\n", 100);
    let log = "L11_SLICE_PAGE_FETCH_START_events_1\nL11_SLICE_PAGE_FETCH_END_events_1_200\nL11_NATIVE_READY\n";
    timeline.observe(log, 900);
    timeline.observe(log, 1200);
    let report = timeline.report("deadline", deadline_ms());
    let page = report["page_fetch"].as_array().unwrap();
    assert_eq!(page.len(), 2, "no duplicates, no unrelated lines");
    assert_eq!(page[0]["line"], "L11_SLICE_PAGE_FETCH_START_events_1");
    assert_eq!(page[0]["elapsed_ms"], 100);
    assert_eq!(page[1]["line"], "L11_SLICE_PAGE_FETCH_END_events_1_200");
    assert_eq!(page[1]["elapsed_ms"], 900);
}

/// Round 6: the shell reports the web view's inactive-scheduling policy and every change
/// of the window's occlusion; the timeline keeps them so a stalled page can be told apart
/// from an occluded, suspended one.
#[test]
fn webview_policy_and_window_occlusion_receipts_are_kept_with_first_seen_times() {
    let mut timeline = Timeline::default();
    timeline.observe("L11_WEBVIEW_INACTIVE_POLICY:Throttle\n", 40);
    let log =
        "L11_WEBVIEW_INACTIVE_POLICY:Throttle\nL11_WINDOW_OCCLUSION:occluded\nL11_NATIVE_READY\n";
    timeline.observe(log, 900);
    let log = format!("{log}L11_WINDOW_OCCLUSION:visible\n");
    timeline.observe(&log, 5000);
    timeline.observe(&log, 6000);
    let report = timeline.report("exited", deadline_ms());
    let state = report["webview_state"].as_array().unwrap();
    let lines: Vec<_> = state.iter().map(|s| s["line"].as_str().unwrap()).collect();
    assert_eq!(
        lines,
        [
            "L11_WEBVIEW_INACTIVE_POLICY:Throttle",
            "L11_WINDOW_OCCLUSION:occluded",
            "L11_WINDOW_OCCLUSION:visible"
        ]
    );
    assert_eq!(state[0]["elapsed_ms"], 40);
    assert_eq!(state[2]["elapsed_ms"], 5000);
}
