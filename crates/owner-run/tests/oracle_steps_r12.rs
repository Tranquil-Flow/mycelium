// SPDX-License-Identifier: AGPL-3.0-only
//! The `descriptors` and `observe` steps against EXECUTED v0.1.4 (FX-SLICE1-steps-r12: probe
//! `r12-probe-steps.py` runs the real `AppDemoChain._step_descriptors` / `_step_observe` /
//! `_progress_wait` on a stub app under a virtual clock; two identical executions). Compared per case:
//! the outcome (closed code, message, fix), the progress lines in order, the order and number of every
//! collaborator call (run-length encoded), `ctx.state` (participants, descriptors, rollover binding),
//! `ctx.result['participants']`, the number of publishes and the virtual time at the end (= the number
//! of polls of the wait).
mod support;
use owner_run::chain::{StepEnv, StepError};
use owner_run::machine::Reason;
use owner_run::state::RunState;
use owner_run::steps::descriptors::run_descriptors_step;
use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
use owner_run::steps::observe::run_observe_step;
use serde_json::{Value, json};
use support::vworld::{MONO0, VEnv, VPorts, World};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_steps_r12.json")).expect("fixture parses")
}

fn model_output() -> ModelStepOutput {
    let model = PreparedModel {
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c".repeat(40),
        architecture: "qwen3".into(),
        label: String::new(),
        choice: None,
        shape_registered: true,
        owner_storage_need: 0,
    };
    let digest = format!("sha256:{}", "ab".repeat(32));
    ModelStepOutput {
        prepared: PreparedSource {
            model: model.clone(),
            manifest_digest: digest.clone(),
            source_bytes: json!(1),
        },
        slice_mode: false,
        manifest_digest: digest,
        choice: None,
        model_id: model.model_id,
        resolved_commit: model.resolved_commit,
    }
}

fn run_state(spec: &Value) -> RunState {
    let reason = match spec["reason"].as_str() {
        Some("rollover") => Reason::Rollover,
        Some("rebuild") => Reason::Rebuild,
        _ => Reason::Start,
    };
    let mut st = RunState::new("run-1", "dep-1", reason);
    st.local_node = spec["local"].as_str().map(str::to_owned);
    st.model = Some(model_output());
    if !spec["binding"].is_null() {
        st.rollover_binding = Some(spec["binding"].clone());
    }
    st
}

fn outcome(result: Result<(), StepError>) -> Value {
    match result {
        Ok(()) => json!({"ok": true}),
        Err(StepError::Refused(e)) => {
            json!({"refused": {"code": e.code, "message": e.message, "fix": e.fix}})
        }
        Err(StepError::Stopped) => json!({"stopped": true}),
        Err(StepError::Exception { class }) => json!({"exception": class}),
    }
}

fn progress_rle(progress: &[String]) -> Value {
    support::vworld::rle(progress)
}

fn interval() -> f64 {
    use budgets::{BudgetId, Entries};
    BudgetId::OwnerPoll
        .span(Entries::ZERO)
        .map_or(f64::NAN, |s| s.millis() as f64 / 1000.0)
}

/// One case, compared in full. `Err` names the first difference.
fn replay(which: &str, name: &str, case: &Value) -> Result<(), String> {
    let spec = &case["script"];
    let rec = &case["record"];
    let world = World::new(spec);
    let mut env = VEnv::new(&world);
    let mut ports = VPorts {
        world: world.clone(),
        collected: false,
    };
    let mut state = run_state(spec);
    let result = if which == "descriptors" {
        run_descriptors_step(&mut env, &mut ports, &mut state, interval())
    } else {
        state.descriptors = spec["descriptors"].as_array().unwrap().clone();
        state.participants = spec["participants"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| n.as_str().unwrap().to_owned())
            .collect();
        let mut observe = VPorts {
            world: world.clone(),
            collected: false,
        };
        run_observe_step(&mut env, &mut ports, &mut observe, &mut state, interval())
    };
    let got = json!({
        "outcome": outcome(result),
        "progress": progress_rle(&env.progress),
        "calls": world.calls(),
        "state": {
            "participants": if state.participants.is_empty() && rec["state"]["participants"].is_null() { Value::Null } else { json!(state.participants) },
            "descriptors": if state.descriptors.is_empty() && rec["state"]["descriptors"].is_null() { Value::Null } else { json!(state.descriptors) },
            "rollover_binding": state.rollover_binding.clone().unwrap_or(Value::Null),
        },
        "participants": env.results.get("participants").cloned().unwrap_or(Value::Null),
        "publishes": env.publishes,
        "t_end": world.t.get(),
    });
    let want = json!({
        "outcome": rec["outcome"], "progress": rec["progress"], "calls": rec["calls"], "state": rec["state"],
        "participants": rec["participants"], "publishes": rec["publishes"], "t_end": rec["t_end"],
    });
    if got == want {
        return Ok(());
    }
    for key in [
        "outcome",
        "progress",
        "calls",
        "state",
        "participants",
        "publishes",
        "t_end",
    ] {
        if got[key] != want[key] {
            let clip = |v: &Value| v.to_string().chars().take(900).collect::<String>();
            return Err(format!(
                "{which}/{name}: {key} differs\n rust   {}\n oracle {}",
                clip(&got[key]),
                clip(&want[key])
            ));
        }
    }
    Err(format!("{which}/{name}: differs"))
}

fn group(which: &str, pick: impl Fn(&str) -> bool, at_least: usize) {
    let doc = fixture();
    let mut n = 0;
    let mut bad = Vec::new();
    for (name, case) in doc[which].as_object().unwrap() {
        if !pick(name) {
            continue;
        }
        n += 1;
        if let Err(e) = replay(which, name, case) {
            bad.push(e);
        }
    }
    assert!(n >= at_least, "{which}: only {n} cases (need {at_least})");
    assert!(
        bad.is_empty(),
        "{which}: {} of {n} cases differ from the oracle; first 3:\n{}",
        bad.len(),
        bad.iter().take(3).cloned().collect::<Vec<_>>().join("\n")
    );
}

/// Proves: B-RUN-07 (descriptors budget 180 s, two_macs_required after it, a member's own code from its
/// report wins), B-RUN-08 (participants listed with labels, short ids and progress) for every scenario.
#[test]
fn every_recorded_descriptors_scenario_equals_the_oracle() {
    group("descriptors", |_| true, 45);
}

/// Proves: B-RUN-07 (a joined Mac that keeps downloading is waited for as long as its bytes rise; a flat
/// download, a verify/compile after the last byte and a silent link each end with their own code and text).
#[test]
fn download_verify_and_silence_budgets_equal_the_oracle() {
    group(
        "descriptors",
        |n| {
            n.starts_with("download")
                || n.starts_with("verify")
                || n.starts_with("compile_phase")
                || n.starts_with("silent")
        },
        10,
    );
}

/// Proves: B-RUN-07 (a descriptor must outlive its use by 60 s; an aging one is re-signed while the run
/// waits, and one that never is named `member_descriptor_stale`).
#[test]
fn descriptor_margin_and_resigning_equal_the_oracle() {
    group(
        "descriptors",
        |n| n.starts_with("stale") || n.starts_with("margin") || n.starts_with("expiry"),
        6,
    );
}

/// Proves: B-RUN-04 (a Stop ends the descriptors wait as Stopped, never a failure). The peer-clock refusal
/// (`peer_clock_skew_exceeded`: Mac tail, rounded offset, direction) has no B-id: proposal to add it to
/// B-RUN-07's key cases.
#[test]
fn clock_skew_and_stop_equal_the_oracle() {
    group(
        "descriptors",
        |n| n.starts_with("skew") || n.starts_with("stop"),
        7,
    );
}

/// Proves: B-CONSENT-15 (a rollover run keeps the first run's members and model: the binding is checked
/// against the live members and their identities; a Start records the binding for later rollovers).
#[test]
fn rollover_binding_equals_the_oracle() {
    group(
        "descriptors",
        |n| n.starts_with("rollover") || n.starts_with("start_records"),
        6,
    );
}

/// Proves: B-RUN-25, B-RUN-07 (the observe step's waits: bootstrap, collect and cleanup budgets renew on visible
/// progress; a silent Mac is waited for while its lease is live; the same-host / same-boot codes are final).
#[test]
fn every_recorded_observe_scenario_equals_the_oracle() {
    group("observe", |_| true, 30);
}

/// Proves: B-RUN-25 (a member refusal reported during bootstrap ends the step with the member's own code,
/// whichever participant reports it first).
#[test]
fn member_refusals_during_bootstrap_equal_the_oracle() {
    group(
        "observe",
        |n| {
            n.starts_with("bootstrap_member")
                || n.starts_with("bootstrap_error")
                || n.starts_with("bootstrap_earlier")
        },
        4,
    );
}

#[test]
fn the_virtual_clock_starts_where_the_probe_started() {
    let world = World::new(&json!({}));
    let mut env = VEnv::new(&world);
    assert_eq!(env.now(), MONO0);
}
