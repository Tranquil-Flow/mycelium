// SPDX-License-Identifier: AGPL-3.0-only
//! The `demo_app.action` refusal ladder and dispatch against EXECUTED v0.1.4 (`demo_app.py:96-133`)
//! over the real `OwnerRun`: FX-SLICE1-demo-action (probe `r11-probe-demo-action.py`, 285 cases:
//! roles x paths x 18 bodies, authority/relay variants, five prior run states).
mod support;
use owner_run::action::{DemoFacts, DemoPath, Plan, admit, apply};
use serde_json::{Value, json};
use support::{Blocked, Sim};

const STEP_NAMES: [&str; 10] = [
    "model",
    "contributor",
    "descriptors",
    "observe",
    "compile",
    "slices",
    "publish",
    "targets",
    "activate",
    "serving",
];

struct Host {
    owner: bool,
    exchange: bool,
    relay: Option<u64>,
}

impl DemoFacts for Host {
    fn owner(&self) -> bool {
        self.owner
    }
    fn ensure_exchange(&mut self) -> bool {
        self.exchange
    }
    fn members_joined_without_relay(&self) -> Option<u64> {
        self.relay
    }
}

fn prior(sim: &mut Sim, kind: &str) {
    if kind == "fresh" {
        return;
    }
    let cmds = sim
        .run
        .start("start", None, sim.clock())
        .expect("prior start");
    sim.exec(cmds);
    match kind {
        "running" => sim.give("step:model", &json!({"wait": true})),
        "failed" => {
            sim.give(
                "step:model",
                &json!({"error": {"code": "owner_model_not_prepared"}}),
            );
            sim.give("teardown", &json!({"ok": true}));
        }
        "serving" | "stopped" => {
            for n in STEP_NAMES {
                sim.give(&format!("step:{n}"), &json!({"ok": true}));
            }
            if kind == "stopped" {
                let cmds = sim.run.stop();
                sim.exec(cmds);
                sim.give("teardown", &json!({"ok": true}));
            }
        }
        other => panic!("unknown prior {other}"),
    }
}

/// Proves: B-RUN-02 (owner_role_required, invalid_request, owner_swarm_required, owner_run_active,
/// owner_run_rebuild_requires_run, execution_ceiling_invalid before any run is touched), B-RUN-06 and
/// B-SWARM-18 (relay_required with the singular/plural message and the start_relay fix on start and
/// rebuild only), B-CONSENT-16 (a bad ceiling body is refused in the ladder), B-RUN-15 (rebuild answer
/// carries rebuild_requested), B-RUN-04 (stop on every prior state).
#[test]
fn the_ladder_and_dispatch_match_the_executed_oracle_on_every_case() {
    let fx: Value =
        serde_json::from_str(include_str!("fixtures/oracle_demo_action.json")).expect("fixture");
    let cases = fx["cases"].as_array().expect("cases");
    assert_eq!(cases.len(), 285);
    assert_eq!(fx["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let mut ok = 0;
    for (n, case) in cases.iter().enumerate() {
        let label = format!(
            "case {n}: role {} authority {} relay {} prior {} {} body {}",
            case["role"],
            case["authority"],
            case["relay"],
            case["prior"],
            case["path"],
            case["body"]
        );
        let path = DemoPath::from_path(case["path"].as_str().expect("path")).expect("owner route");
        let mut host = Host {
            owner: case["role"] == "owner",
            exchange: case["authority"] == "ok",
            relay: case["relay"].as_u64(),
        };
        let mut sim = Sim::new(None);
        prior(&mut sim, case["prior"].as_str().expect("prior"));
        let result = &case["result"];
        let clock = sim.clock();
        let answered = admit(path, &case["body"], &mut host)
            .and_then(|plan| apply(&plan, &mut sim.run, clock));
        match answered {
            Err(refusal) => {
                assert_eq!(result["error"], refusal.code, "{label}");
                assert_eq!(
                    result.get("message").and_then(Value::as_str),
                    refusal.message.as_deref(),
                    "{label}: message"
                );
                let fix = refusal.fix.map(Value::Object);
                assert_eq!(result.get("fix").cloned(), fix, "{label}: fix");
                assert!(
                    case.get("after").is_none(),
                    "{label}: refused but the oracle ran it"
                );
            }
            Ok(applied) => {
                ok += 1;
                assert_eq!(
                    result["ok"], true,
                    "{label}: port admitted, oracle: {result}"
                );
                let snap = sim.run.snapshot();
                sim.exec(applied.cmds);
                let after_apply = sim.run.snapshot();
                assert_eq!(
                    result["state"], after_apply.state,
                    "{label}: answered state"
                );
                assert_eq!(
                    result["reason"],
                    after_apply.reason.map_or(Value::Null, Into::into),
                    "{label}"
                );
                let mut keys: Vec<&str> = snap.entries().iter().map(|(k, _)| *k).collect();
                if applied.rebuild_requested {
                    keys.push("rebuild_requested");
                }
                let want: Vec<&str> = result["keys"]
                    .as_array()
                    .expect("keys")
                    .iter()
                    .filter_map(Value::as_str)
                    .collect();
                assert_eq!(want, keys, "{label}: answer keys");
                assert_eq!(
                    result["rebuild_requested"].as_bool(),
                    applied.rebuild_requested.then_some(true),
                    "{label}"
                );
                let after = &case["after"];
                let was_live = matches!(case["prior"].as_str(), Some("running" | "serving"));
                if path == DemoPath::Stop && was_live {
                    assert_eq!(
                        after["at_teardown"],
                        sim.run.snapshot().state,
                        "{label}: at teardown"
                    );
                    sim.give("teardown", &json!({"ok": true}));
                } else {
                    assert!(after.get("at_teardown").is_none(), "{label}");
                }
                if path == DemoPath::Rebuild && was_live {
                    sim.give("teardown", &json!({"ok": true}));
                }
                if matches!(path, DemoPath::Start | DemoPath::Rebuild) {
                    assert_eq!(
                        sim.blocked,
                        Blocked::Step("model".into()),
                        "{label}: first step of the fresh run"
                    );
                }
                let last = sim.run.snapshot();
                assert_eq!(after["final_state"], last.state, "{label}: settled state");
                assert_eq!(
                    after["reason"],
                    last.reason.map_or(Value::Null, Into::into),
                    "{label}"
                );
                assert_eq!(
                    after["run_id"],
                    last.run_id.map_or(Value::Null, Into::into),
                    "{label}"
                );
                assert_eq!(
                    after["step"],
                    last.step.map_or(Value::Null, Into::into),
                    "{label}"
                );
            }
        }
    }
    assert_eq!(ok, 38, "the oracle admitted exactly the recorded cases");
}

/// Proves: B-RUN-02 (the plan of an admitted action: a ceiling body only on start).
#[test]
fn only_start_takes_an_execution_ceiling() {
    let ceiling = json!({
        "max_execution_download_bytes": 0, "max_combined_rss_bytes": 8_589_934_592u64,
        "max_additional_disk_bytes": 8_589_934_592u64, "max_wall_seconds": 1800, "run_count": 1
    });
    let mut host = Host {
        owner: true,
        exchange: true,
        relay: Some(0),
    };
    let body = json!({"execution_ceiling": ceiling});
    assert_eq!(
        admit(DemoPath::Start, &body, &mut host),
        Ok(Plan::Start {
            ceiling: Some(ceiling)
        })
    );
    assert_eq!(
        admit(DemoPath::Stop, &body, &mut host).unwrap_err().code,
        "invalid_request"
    );
    assert_eq!(
        admit(DemoPath::Rebuild, &body, &mut host).unwrap_err().code,
        "invalid_request"
    );
}
