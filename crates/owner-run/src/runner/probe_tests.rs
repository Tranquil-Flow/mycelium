// SPDX-License-Identifier: AGPL-3.0-only
//! Real Runner idle callback dispatch versus the pinned source's status/poll.
//! Context enters serving through the real machine with scripted completed steps;
//! no host workflow, native activation or cleanup is claimed.
use super::*;
use serde_json::json;

struct Fixed;
impl Clocks for Fixed {
    fn mono(&self) -> f64 {
        0.0
    }
    fn wall(&self) -> f64 {
        0.0
    }
}
struct Scripted {
    spec: Value,
    calls: Vec<String>,
}
impl Chain for Scripted {
    fn new_run(&mut self, _: &str, _: &str, _: Reason, _: Option<&Value>, _: Option<&Value>) {
        panic!("not called by idle");
    }
    fn run_step(&mut self, _: &str, _: &mut dyn StepEnv) -> Result<(), StepError> {
        panic!("not called by idle");
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        panic!("not called by idle");
    }
    fn rollover_due(&mut self) -> Option<bool> {
        self.calls.push("rollover_due".into());
        Some(self.spec["due"] == true)
    }
    fn ceiling_reached(&mut self) -> CeilingProbe {
        self.calls.push("ceiling_reached".into());
        if self.spec["ceiling_error"] == true {
            CeilingProbe::Raised {
                class: "RuntimeError".into(),
            }
        } else {
            CeilingProbe::Code(self.spec["ceiling"].as_str().map(str::to_owned))
        }
    }
    fn monitor(&mut self) -> MonitorProbe {
        self.calls.push("monitor".into());
        if self.spec["monitor_error"] == true {
            MonitorProbe::Raised {
                class: "RuntimeError".into(),
            }
        } else {
            MonitorProbe::Code(self.spec["monitor"].as_str().map(str::to_owned))
        }
    }
}
#[test]
fn serving_probe_call_order_matches_source_before_side_effects() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../tests/fixtures/oracle_hook_order_r17.json"
    ))
    .unwrap();
    let cases = fixture["cases"].as_object().unwrap();
    assert!(!cases.is_empty());
    for (name, case) in cases {
        let mut run = OwnerRun::new(Box::new(|| Identity {
            run_id: "run-1".into(),
            deployment_id: "dep-1".into(),
        }));
        let cmds = run
            .start(
                "start",
                None,
                Clock {
                    mono: 0.0,
                    wall: 0.0,
                },
            )
            .unwrap();
        assert!(
            cmds.iter()
                .any(|c| matches!(c, Cmd::RunStep { step: "model" }))
        );
        for _ in crate::machine::STEPS {
            // Consume the exact machine's next step completion, not a second controller.
            let cmds = run.step_finished(StepOutcome::Ok);
            assert!(!cmds.iter().any(|c| matches!(c, Cmd::Teardown { .. })));
        }
        assert!(run.serving() && run.thread_alive());
        let probe = Arc::new(Probe {
            done: Mutex::new(false),
            finished: Condvar::new(),
        });
        let mut queue = Queue {
            closing: true,
            ..Queue::default()
        };
        if case["spec"]["status"] == true {
            queue.probes.push(probe.clone());
        }
        let shared = Shared {
            run: Mutex::new(run),
            queue: Mutex::new(queue),
            wake: Condvar::new(),
            stop: StopEvent::new(),
            clocks: Arc::new(Fixed),
            timing: Timing::registered(),
        };
        let mut chain = Scripted {
            spec: case["spec"].clone(),
            calls: vec![],
        };
        idle(&shared, &mut chain);
        assert_eq!(
            json!(chain.calls),
            case["calls"],
            "{name}: source callback order and count"
        );
        assert_eq!(
            lock(&shared.run).snapshot().to_value(),
            case["snapshot"],
            "{name}: complete public result"
        );
        assert_eq!(
            shared.stop.is_set(),
            case["stop"].as_bool().unwrap(),
            "{name}: stop event"
        );
        if case["spec"]["status"] == true {
            assert!(*lock(&probe.done));
        }
        println!("ORACLE-HOOK-ORDER-R17 {name}");
    }
}
