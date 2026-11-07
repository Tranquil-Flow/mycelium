// SPDX-License-Identifier: AGPL-3.0-only
//! The `OwnerRun` machine against traces recorded by EXECUTING v0.1.4's `owner_run.OwnerRun`
//! (`owner_run.py @ f4e366e9`) with a scripted fake chain: FX-SLICE1-owner-run
//! (probe `r11-probe-owner-run.py`, 39 scenarios, deterministic across repeated executions).
//! Every scenario's operations are replayed against the machine through [`support::Sim`] and every
//! recorded snapshot, op answer and chain entry is compared exactly.
mod support;
use serde_json::Value;
use support::{Blocked, Sim};

const FIXTURE: &str = include_str!("fixtures/oracle_owner_run.json");

fn fixture() -> Value {
    serde_json::from_str(FIXTURE).expect("fixture parses")
}

fn next_record<'a>(it: &mut std::slice::Iter<'a, Value>, seen: &mut usize) -> &'a Value {
    *seen += 1;
    it.next().expect("an expected record")
}

fn replay(name: &str) {
    let fx = fixture();
    let backoff = fx["config"][name]["backoff"].as_f64();
    let flat = fx["config"][name]["flat_counts"].as_bool().unwrap_or(false);
    let scenario = &fx["scenarios"][name];
    assert!(scenario.is_object(), "no scenario {name} in the fixture");
    let mut sim = Sim::new(backoff);
    let mut recorded = scenario["ops"].as_array().expect("ops").iter();
    let mut seen = 0usize;
    // The fixture holds the probe's ops that RECORD something (start/stop/rebuild answers, snapshots);
    // the script that produced them is replayed below from the same JSON (`script`).
    let script = &fx["scripts"][name];
    assert!(script.is_array(), "fixture has no script for {name}");
    for op in script.as_array().expect("script") {
        let kind = op["op"].as_str().expect("op kind");
        let mut record: Option<&Value> = None;
        match kind {
            "start" => {
                let r = next_record(&mut recorded, &mut seen);
                record = Some(r);
                let reason = op["reason"].as_str().unwrap_or("start").to_owned();
                let ceiling = op.get("ceiling").filter(|c| !c.is_null());
                match sim.run.start(&reason, ceiling, sim.clock()) {
                    Ok(cmds) => {
                        sim.exec(cmds);
                        let snap = sim.run.snapshot();
                        assert_eq!(r["ok"], true, "{name}: start should have been refused: {r}");
                        assert_eq!(r["state"], snap.state, "{name}: start state");
                        assert_eq!(
                            r["reason"],
                            snap.reason.unwrap_or_default(),
                            "{name}: start reason"
                        );
                        assert_eq!(
                            r["run_id"],
                            snap.run_id.clone().unwrap_or_default(),
                            "{name}: run id"
                        );
                        assert_eq!(r["error_code"], Value::Null, "{name}");
                    }
                    Err(e) => {
                        assert_eq!(r["ok"], false, "{name}: start was refused with {}", e.code);
                        assert_eq!(r["code"], e.code, "{name}: start refusal");
                    }
                }
            }
            "start_stop_atomic" => {
                let mut cmds = sim.run.start("start", None, sim.clock()).expect("start");
                cmds.extend(sim.run.stop());
                sim.exec(cmds);
            }
            "stop" | "rebuild_async" | "rebuild" => {
                let r = next_record(&mut recorded, &mut seen);
                record = Some(r);
                let outcome = match kind {
                    "stop" => Ok(sim.run.stop()),
                    "rebuild_async" => sim.run.rebuild_async(sim.clock()),
                    _ => sim.run.rebuild(sim.clock()),
                };
                let outcome = outcome.map_err(|e| e.code);
                let mut error = outcome.as_ref().err().cloned();
                if let Ok(cmds) = outcome {
                    sim.exec(cmds);
                    // the probe's `join_seconds` elapsed with the old run's teardown still blocked
                    if kind == "rebuild"
                        && let Err(e) = sim.run.rebuild_join_expired(false)
                    {
                        error = Some(e.code);
                    }
                }
                match error {
                    None => {
                        assert_eq!(
                            r["ok"], true,
                            "{name}: {kind} should have been refused: {r}"
                        );
                        assert_eq!(r["state"], sim.run.snapshot().state, "{name}: {kind} state");
                        assert_eq!(
                            r["rebuild_requested"].as_bool(),
                            (kind == "rebuild_async").then_some(true),
                            "{name}: rebuild_requested"
                        );
                    }
                    Some(code) => {
                        assert_eq!(r["ok"], false, "{name}: {kind} was refused with {code}");
                        assert_eq!(r["code"], code, "{name}: {kind} refusal");
                    }
                }
                sim.settle();
            }
            "status" => {
                let r = next_record(&mut recorded, &mut seen);
                record = Some(r);
                sim.live_status();
            }
            "await" => {
                let what = op["what"].as_str().expect("what");
                if let Some(key) = what.strip_prefix("blocked:") {
                    let have = match &sim.blocked {
                        Blocked::Step(s) => format!("step:{s}"),
                        Blocked::Teardown => "teardown".into(),
                        Blocked::No => "nothing".into(),
                    };
                    assert_eq!(have, key, "{name}: the chain should be blocked at {key}");
                } else if let Some(state) = what.strip_prefix("state:") {
                    assert_eq!(sim.run.snapshot().state, state, "{name}: await state");
                } else if what == "finished" {
                    assert!(
                        !sim.run.thread_alive(),
                        "{name}: the run thread should have ended"
                    );
                } else if let Some(state) = what.strip_prefix("rollover:") {
                    assert_eq!(
                        sim.run.snapshot().rollover.state,
                        state,
                        "{name}: await rollover"
                    );
                } else if what == "rebuild_idle" {
                    if sim.rebuild_join_patched {
                        sim.run.rebuild_join_expired(true).expect("async expiry");
                    }
                } else {
                    panic!("unknown await {what}");
                }
            }
            "give" => sim.give(
                op["key"].as_str().expect("key"),
                &op.get("outcome")
                    .cloned()
                    .unwrap_or(serde_json::json!({"ok": true})),
            ),
            "facts" => {
                for (k, v) in op["facts"].as_object().expect("facts") {
                    match k.as_str() {
                        "monitor" => sim.facts.monitor = v.clone(),
                        "ceiling" => sim.facts.ceiling = v.clone(),
                        "rollover_due" => sim.facts.rollover_due = v.as_bool().unwrap_or(false),
                        other => panic!("unknown fact {other}"),
                    }
                }
                sim.settle();
            }
            "clock" => {
                sim.mono += op["advance"].as_f64().expect("advance");
                sim.settle();
            }
            "patch_rebuild_join" => sim.rebuild_join_patched = true,
            "snap" => {}
            other => panic!("unknown op {other}"),
        }
        // snapshots attached to ops ("snap": label) and explicit snap ops
        let wants_snapshot = kind == "snap" || op.get("snap").is_some_and(|s| !s.is_null());
        if kind == "status" {
            let r = record.expect("status record");
            assert_eq!(
                r["snapshot"],
                sim.norm_snapshot(flat),
                "{name}: live status snapshot"
            );
        }
        if wants_snapshot {
            let r = recorded.next().expect("an expected snapshot");
            seen += 1;
            assert_eq!(r["op"], "snap");
            assert_eq!(
                r["snapshot"],
                sim.norm_snapshot(flat),
                "{name}: snapshot {:?} differs from the executed oracle",
                r["label"]
            );
        }
        let _ = record;
    }
    assert!(
        recorded.next().is_none(),
        "{name}: unreplayed expected records"
    );
    // the probe leaves nothing running: stop, then finish whatever the chain is blocked in
    let cmds = sim.run.stop();
    sim.exec(cmds);
    for _ in 0..40 {
        if sim.blocked != Blocked::No {
            let key = match &sim.blocked {
                Blocked::Step(s) => format!("step:{s}"),
                _ => "teardown".to_owned(),
            };
            sim.give(&key, &serde_json::json!({"ok": true}));
        }
        sim.settle();
        if !sim.run.thread_alive() {
            break;
        }
    }
    assert!(seen > 0, "{name}: nothing was compared");
    assert_eq!(
        Value::Array(sim.entered.clone()),
        scenario["entered"],
        "{name}: the chain was called differently from the oracle"
    );
}

const REPLAYED: &[&str] = &[
    "ceiling_check_raises_then_clears",
    "ceiling_reached_in_serving",
    "happy",
    "monitor_code_bad_closure",
    "monitor_code_via_status",
    "monitor_raises_via_status",
    "rebuild_async_stop_incomplete",
    "rebuild_of_failed_run_runs_fresh",
    "rebuild_requires_run",
    "rebuild_stop_incomplete",
    "rollover_backoff_capped_60",
    "rollover_backoff_then_fatal",
    "rollover_cleanup_unproven_fatal",
    "rollover_due_wins_over_ceiling",
    "rollover_failure_then_success",
    "rollover_members_changed_fatal",
    "rollover_model_changed_fatal",
    "rollover_run_stopped_by_user_midstep",
    "rollover_success",
    "serving_failure_not_rollover_is_final",
    "start_refusals",
    "start_with_ceiling_then_rebuild",
    "status_while_not_serving_skips_monitor",
    "step_error_code_80_and_81_chars",
    "step_error_code_not_closed",
    "step_error_mid_chain",
    "step_error_model_with_message_fix",
    "step_generic_exception",
    "step_progress_truncation",
    "stop_after_step_ok_before_next",
    "stop_before_first_step_runs",
    "stop_during_rollover_backoff",
    "stop_during_rollover_teardown",
    "stop_during_step",
    "stop_when_idle",
    "teardown_exception_after_failure_with_chain_unproven",
    "teardown_exception_after_stop",
    "teardown_ok_with_chain_unproven",
    "teardown_runs_with_failed_false_on_stop_and_true_on_failure",
];

/// Proves: B-RUN-10.
#[test]
fn ceiling_check_raises_then_clears() {
    replay("ceiling_check_raises_then_clears");
}

/// Proves: B-RUN-10.
#[test]
fn ceiling_reached_in_serving() {
    replay("ceiling_reached_in_serving");
}

/// Proves: B-RUN-01 B-RUN-04 B-RUN-17.
#[test]
fn happy() {
    replay("happy");
}

/// Proves: B-RUN-03.
#[test]
fn monitor_code_bad_closure() {
    replay("monitor_code_bad_closure");
}

/// Proves: B-RUN-17 B-RUN-03.
#[test]
fn monitor_code_via_status() {
    replay("monitor_code_via_status");
}

/// Proves: B-RUN-17 B-RUN-03.
#[test]
fn monitor_raises_via_status() {
    replay("monitor_raises_via_status");
}

/// Proves: B-RUN-15.
#[test]
fn rebuild_async_stop_incomplete() {
    replay("rebuild_async_stop_incomplete");
}

/// Proves: B-RUN-15 B-RUN-14.
#[test]
fn rebuild_of_failed_run_runs_fresh() {
    replay("rebuild_of_failed_run_runs_fresh");
}

/// Proves: B-RUN-02 B-RUN-15.
#[test]
fn rebuild_requires_run() {
    replay("rebuild_requires_run");
}

/// Proves: B-RUN-15.
#[test]
fn rebuild_stop_incomplete() {
    replay("rebuild_stop_incomplete");
}

/// Proves: B-ROLL-04.
#[test]
fn rollover_backoff_capped_60() {
    replay("rollover_backoff_capped_60");
}

/// Proves: B-ROLL-04 B-ROLL-02.
#[test]
fn rollover_backoff_then_fatal() {
    replay("rollover_backoff_then_fatal");
}

/// Proves: B-ROLL-04.
#[test]
fn rollover_cleanup_unproven_fatal() {
    replay("rollover_cleanup_unproven_fatal");
}

/// Proves: B-ROLL-02 B-RUN-10.
#[test]
fn rollover_due_wins_over_ceiling() {
    replay("rollover_due_wins_over_ceiling");
}

/// Proves: B-ROLL-04 B-CONSENT-15.
#[test]
fn rollover_members_changed_fatal() {
    replay("rollover_members_changed_fatal");
}

/// Proves: B-ROLL-04 B-CONSENT-15.
#[test]
fn rollover_model_changed_fatal() {
    replay("rollover_model_changed_fatal");
}

/// Proves: B-ROLL-04 B-ROLL-02.
#[test]
fn rollover_failure_then_success() {
    replay("rollover_failure_then_success");
}

/// Proves: B-ROLL-02 B-RUN-04.
#[test]
fn rollover_run_stopped_by_user_midstep() {
    replay("rollover_run_stopped_by_user_midstep");
}

/// Proves: B-ROLL-02 B-RUN-14.
#[test]
fn rollover_success() {
    replay("rollover_success");
}

/// Proves: B-RUN-03 B-RUN-10.
#[test]
fn serving_failure_not_rollover_is_final() {
    replay("serving_failure_not_rollover_is_final");
}

/// Proves: B-RUN-02 B-CONSENT-16.
#[test]
fn start_refusals() {
    replay("start_refusals");
}

/// Proves: B-RUN-15 B-RUN-02 B-CONSENT-16 B-RUN-14.
#[test]
fn start_with_ceiling_then_rebuild() {
    replay("start_with_ceiling_then_rebuild");
}

/// Proves: B-RUN-17.
#[test]
fn status_while_not_serving_skips_monitor() {
    replay("status_while_not_serving_skips_monitor");
}

/// Proves: B-RUN-03.
#[test]
fn step_error_code_80_and_81_chars() {
    replay("step_error_code_80_and_81_chars");
}

/// Proves: B-RUN-03.
#[test]
fn step_error_code_not_closed() {
    replay("step_error_code_not_closed");
}

/// Proves: B-RUN-03 B-RUN-04.
#[test]
fn step_error_mid_chain() {
    replay("step_error_mid_chain");
}

/// Proves: B-RUN-03.
#[test]
fn step_error_model_with_message_fix() {
    replay("step_error_model_with_message_fix");
}

/// Proves: B-RUN-03.
#[test]
fn step_generic_exception() {
    replay("step_generic_exception");
}

/// Proves: B-RUN-01.
#[test]
fn step_progress_truncation() {
    replay("step_progress_truncation");
}

/// Proves: B-RUN-04.
#[test]
fn stop_after_step_ok_before_next() {
    replay("stop_after_step_ok_before_next");
}

/// Proves: B-RUN-04.
#[test]
fn stop_before_first_step_runs() {
    replay("stop_before_first_step_runs");
}

/// Proves: B-ROLL-02 B-RUN-04.
#[test]
fn stop_during_rollover_backoff() {
    replay("stop_during_rollover_backoff");
}

/// Proves: B-ROLL-02 B-RUN-04.
#[test]
fn stop_during_rollover_teardown() {
    replay("stop_during_rollover_teardown");
}

/// Proves: B-RUN-04.
#[test]
fn stop_during_step() {
    replay("stop_during_step");
}

/// Proves: B-RUN-04 B-ONB-07.
#[test]
fn stop_when_idle() {
    replay("stop_when_idle");
}

/// Proves: B-RUN-03.
#[test]
fn teardown_exception_after_failure_with_chain_unproven() {
    replay("teardown_exception_after_failure_with_chain_unproven");
}

/// Proves: B-RUN-03 B-RUN-04.
#[test]
fn teardown_exception_after_stop() {
    replay("teardown_exception_after_stop");
}

/// Proves: B-RUN-03.
#[test]
fn teardown_ok_with_chain_unproven() {
    replay("teardown_ok_with_chain_unproven");
}

/// Proves: B-RUN-03 B-RUN-04.
#[test]
fn teardown_runs_with_failed_false_on_stop_and_true_on_failure() {
    replay("teardown_runs_with_failed_false_on_stop_and_true_on_failure");
}

/// Every scenario the oracle probe recorded is replayed (a new fixture scenario without a test is a failure).
#[test]
fn every_recorded_scenario_is_replayed() {
    let fx = fixture();
    let recorded: Vec<&str> = fx["scenarios"]
        .as_object()
        .expect("scenarios")
        .keys()
        .map(String::as_str)
        .collect();
    let mut want: Vec<&str> = REPLAYED.to_vec();
    want.sort_unstable();
    let mut have = recorded;
    have.sort_unstable();
    assert_eq!(have, want);
    assert_eq!(fx["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
}
