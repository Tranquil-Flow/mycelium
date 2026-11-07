// SPDX-License-Identifier: AGPL-3.0-only
//! `/api/demo/start|stop|rebuild` drive the owner run (SE-L10-OWNER-RUN-HOST): the refusal ladder and the
//! run's answers come from EXECUTED v0.1.4 (`demo_app.action` over the real `OwnerRun`, FX-SLICE1-demo-action:
//! 285 cases, probe `r11-probe-demo-action.py`, copied byte-equal from SLICE-1's fixture), replayed through
//! the real route path (`Control` -> `PersistentBackend` -> `Host::demo_action` -> `DemoRuns` -> the sans-IO
//! `OwnerRun` and a driver thread) over a scripted chain that blocks in each step until the test gives its
//! outcome (the probe's fake chain). A condition is awaited, never slept for.
use api::{
    actions::{Action, Backend, Control},
    desktop::Service,
    owner_driver::{Chain, DemoRuns, StepEnv, StepError, Timing},
    persistent::{Host, PersistentBackend},
    schema::PublicError,
    wire::Json,
};
use owner_run::{
    action::DemoFacts,
    machine::{CeilingProbe, Identity, MonitorProbe, OwnerRunError, Reason},
};
use serde_json::{Map, Value, json};
use std::{
    sync::{
        Arc, Mutex,
        mpsc::{Receiver, Sender, channel},
    },
    time::{Duration, Instant},
};

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}
fn secs(n: u64) -> Duration {
    Duration::from_secs(n)
}

#[derive(Clone, Debug, PartialEq)]
enum Blocked {
    No,
    Step(String),
    Teardown,
}
/// What the scripted chain reports to the test.
enum Event {
    Step(String),
    Waiting,
    Teardown,
}
struct Script {
    events: Receiver<Event>,
    gives: Sender<Value>,
}
struct ScriptedChain {
    events: Sender<Event>,
    gives: Receiver<Value>,
}
fn outcome_error(outcome: &Value) -> Option<OwnerRunError> {
    let e = outcome.get("error")?;
    let fix = e
        .get("fix")
        .filter(|f| !f.is_null())
        .and_then(Value::as_object)
        .cloned();
    Some(OwnerRunError::with(
        e["code"].as_str().unwrap_or_default(),
        e["message"].as_str(),
        fix,
    ))
}
impl Chain for ScriptedChain {
    fn new_run(&mut self, _: &str, _: &str, _: Reason, _: Option<&Value>, _: Option<&Value>) {}
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        drop(self.events.send(Event::Step(step.to_owned())));
        let Ok(outcome) = self.gives.recv_timeout(secs(30)) else {
            return Err(StepError::Stopped);
        };
        for d in outcome["progress"].as_array().into_iter().flatten() {
            env.progress(d.as_str().unwrap_or_default());
        }
        if let Some(e) = outcome_error(&outcome) {
            return Err(StepError::Refused(e));
        }
        if let Some(class) = outcome["exception"].as_str() {
            return Err(StepError::Exception {
                class: class.to_owned(),
            });
        }
        if outcome["wait"] == true {
            // `ctx.wait_until(lambda: False)`: only Stop ends it.
            drop(self.events.send(Event::Waiting));
            while !env.pause(secs(3600).as_secs_f64()) {}
            return Err(StepError::Stopped);
        }
        Ok(())
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        drop(self.events.send(Event::Teardown));
        let Ok(outcome) = self.gives.recv_timeout(secs(30)) else {
            return Ok(());
        };
        outcome["exception"]
            .as_str()
            .map_or(Ok(()), |c| Err(c.to_owned()))
    }
    fn rollover_due(&mut self) -> Option<bool> {
        Some(false)
    }
    fn ceiling_reached(&mut self) -> CeilingProbe {
        CeilingProbe::Code(None)
    }
    fn monitor(&mut self) -> MonitorProbe {
        MonitorProbe::Code(None)
    }
}

#[derive(Clone, Copy)]
struct Facts {
    owner: bool,
    exchange: bool,
    relay: Option<u64>,
}
impl DemoFacts for Facts {
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

struct World {
    facts: Mutex<Facts>,
    runs: Mutex<DemoRuns>,
    published: Mutex<Option<Json>>,
}
struct TestHost(Arc<World>);
impl Host for TestHost {
    fn closed(&self) -> bool {
        false
    }
    fn owner(&self) -> bool {
        self.0.facts.lock().unwrap().owner
    }
    fn environment(&self, _: &str) -> Option<String> {
        None
    }
    fn device_memory_bytes(&self) -> Option<u64> {
        None
    }
    fn run_active(&self) -> bool {
        false
    }
    fn acquisition_active(&self) -> bool {
        false
    }
    fn owner_run_active(&self) -> bool {
        self.0.runs.lock().unwrap().active()
    }
    fn demo_action(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        let mut facts = *self.0.facts.lock().unwrap();
        self.0
            .runs
            .lock()
            .unwrap()
            .demo_action(action, body, &mut facts)
    }
}
struct Inner(Arc<World>);
impl Backend for Inner {
    fn prepare(&mut self, _: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        Ok(())
    }
    fn execute(&mut self, _: Action, _: &Map<String, Value>) -> Result<Value, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        let view = self.0.runs.lock().unwrap().view();
        *self.0.published.lock().unwrap() = view.status();
        Ok(())
    }
}

struct Sim {
    world: Arc<World>,
    control: Control<PersistentBackend<TestHost, Inner>>,
    script: Script,
    blocked: Blocked,
    waiting: bool,
}
impl Sim {
    fn new(facts: Facts) -> Self {
        let (event_tx, events) = channel();
        let (gives, give_rx) = channel();
        let mut chain = Some(ScriptedChain {
            events: event_tx,
            gives: give_rx,
        });
        let mut n = 0;
        let world = Arc::new(World {
            published: Mutex::new(None),
            facts: Mutex::new(facts),
            runs: Mutex::new(DemoRuns::new(
                Box::new(move || {
                    Box::new(chain.take().expect("one chain per world")) as Box<dyn Chain>
                }),
                Box::new(move || {
                    n += 1;
                    Identity {
                        run_id: format!("run-{n}"),
                        deployment_id: format!("dep-{n}"),
                    }
                }),
                Timing {
                    poll: ms(5),
                    ..Timing::registered()
                },
            )),
        });
        let dir = std::env::temp_dir().join(format!("l10-demo-routes-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let backend = PersistentBackend::new(dir, TestHost(world.clone()), Inner(world.clone()));
        Self {
            world,
            control: Control::new(backend),
            script: Script { events, gives },
            blocked: Blocked::No,
            waiting: false,
        }
    }
    fn post(&self, path: &str, body: &Value) -> Result<Json, PublicError> {
        let body = body.as_object().cloned().unwrap_or_default();
        self.control.action_json(path, &body)
    }
    fn quiescent(&self) -> bool {
        self.world.runs.lock().unwrap().quiescent()
    }
    fn status(&self) -> Value {
        self.world
            .runs
            .lock()
            .unwrap()
            .status()
            .map_or(Value::Null, |j| j.to_value())
    }
    /// Wait until the chain blocks again (a step, a teardown, a wait) or the driver has nothing left to do.
    fn settle(&mut self) {
        let deadline = Instant::now() + secs(20);
        loop {
            if let Ok(event) = self.script.events.try_recv() {
                match event {
                    Event::Step(s) => self.blocked = Blocked::Step(s),
                    Event::Teardown => self.blocked = Blocked::Teardown,
                    Event::Waiting => {
                        self.waiting = true;
                        self.blocked = Blocked::No;
                    }
                }
                return;
            }
            if self.quiescent() && self.script.events.try_recv().is_err() {
                self.blocked = Blocked::No;
                return;
            }
            assert!(
                Instant::now() < deadline,
                "the driver neither blocked nor settled"
            );
            std::thread::yield_now();
        }
    }
    fn give(&mut self, key: &str, outcome: &Value) {
        let want = match &self.blocked {
            Blocked::Step(s) => format!("step:{s}"),
            Blocked::Teardown => "teardown".to_owned(),
            Blocked::No => String::new(),
        };
        assert_eq!(want, key, "the chain is not blocked at {key}");
        self.blocked = Blocked::No;
        self.script.gives.send(outcome.clone()).unwrap();
        self.settle();
    }
}

/// Owning close must drain teardown before joining, and non-owning views retain final state.
/// The scripted worker is held in stop_event.wait, never a timing race.
#[test]
fn owning_close_drains_teardown_and_preserves_the_nonowning_view() {
    let mut sim = Sim::new(Facts {
        owner: true,
        exchange: true,
        relay: Some(0),
    });
    let view = sim.world.runs.lock().unwrap().view();
    assert!(view.status().is_none(), "lazy installation");
    sim.post("/api/demo/start", &json!({})).unwrap();
    sim.settle();
    sim.give("step:model", &json!({"wait": true}));
    drop(view.clone());
    assert!(view.active(), "dropping a view must not stop the owner");
    sim.script.gives.send(json!({"ok": true})).unwrap();
    sim.world.runs.lock().unwrap().close();
    assert!(
        matches!(sim.script.events.try_recv(), Ok(Event::Teardown)),
        "close must execute the pending teardown before joining"
    );
    assert_eq!(
        view.status().expect("final installed snapshot").to_value()["state"],
        "stopped"
    );
    assert!(!view.active());
    assert_eq!(view.while_inactive(|| "idle commit"), Some("idle commit"));
}

#[test]
fn nonowning_view_answers_while_the_host_owner_and_step_are_held() {
    let mut sim = Sim::new(Facts {
        owner: true,
        exchange: true,
        relay: Some(0),
    });
    let view = sim.world.runs.lock().unwrap().view();
    sim.post("/api/demo/start", &json!({})).unwrap();
    sim.settle();
    sim.give("step:model", &json!({"wait": true}));
    let (tx, rx) = channel();
    let held = sim.world.runs.lock().unwrap();
    let reader = std::thread::spawn(move || {
        tx.send((
            view.status(),
            view.while_inactive(|| panic!("active idle overwrite")),
        ))
        .unwrap();
    });
    let result = rx.recv_timeout(secs(20));
    drop(held);
    sim.script.gives.send(json!({"ok": true})).unwrap();
    sim.world.runs.lock().unwrap().close();
    reader.join().unwrap();
    let (status, idle): (_, Option<()>) = result.expect("view waited on host/chain lock");
    assert_eq!(status.unwrap().to_value()["state"], "running");
    assert!(idle.is_none());
}

#[test]
fn mutating_posts_publish_before_return_without_a_maintenance_tick() {
    let mut sim = Sim::new(Facts {
        owner: true,
        exchange: true,
        relay: Some(0),
    });
    let published = |sim: &Sim| {
        sim.world
            .published
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .to_value()
    };
    sim.post("/api/demo/start", &json!({})).unwrap();
    assert_eq!(published(&sim)["state"], "running");
    assert_eq!(published(&sim)["run_id"], "run-1");
    sim.settle();
    sim.give("step:model", &json!({"wait": true}));
    let rebuilt = sim
        .post("/api/demo/rebuild", &json!({}))
        .unwrap()
        .to_value();
    assert_eq!(rebuilt["rebuild_requested"], true);
    assert_eq!(published(&sim)["state"], "stopping");
    assert_eq!(published(&sim)["run_id"], "run-1");
    sim.settle();
    sim.give("teardown", &json!({"ok": true}));
    assert_eq!(sim.status()["run_id"], "run-2");
    sim.give("step:model", &json!({"wait": true}));
    sim.post("/api/demo/stop", &json!({})).unwrap();
    assert_eq!(published(&sim)["state"], "stopping");
    assert_eq!(published(&sim)["run_id"], "run-2");
    sim.settle();
    sim.give("teardown", &json!({"ok": true}));
    sim.world.runs.lock().unwrap().close();
}

#[test]
fn dropping_the_owner_joins_the_runner_but_dropping_a_view_does_not() {
    let mut sim = Sim::new(Facts {
        owner: true,
        exchange: true,
        relay: Some(0),
    });
    let view = sim.world.runs.lock().unwrap().view();
    sim.post("/api/demo/start", &json!({})).unwrap();
    sim.settle();
    sim.give("step:model", &json!({"wait": true}));
    drop(view.clone());
    assert!(view.active());
    // Disconnect script first so teardown cannot remain held on assertion unwinding.
    let Sim {
        world,
        control,
        script,
        ..
    } = sim;
    drop(script);
    drop(control);
    drop(world);
    assert_eq!(view.status().unwrap().to_value()["state"], "stopped");
    assert!(!view.active());
}

#[test]
fn prepared_idle_refresh_cannot_overwrite_a_started_run() {
    use api::run_exchange::{Announce, RunExchange};
    let mut sim = Sim::new(Facts {
        owner: true,
        exchange: true,
        relay: Some(0),
    });
    let view = sim.world.runs.lock().unwrap().view();
    let exchange = RunExchange::new(Box::new(|| 1.0));
    let prepared = exchange
        .prepare_announcement(Announce {
            phase: "idle".into(),
            ..Announce::default()
        })
        .unwrap();
    sim.post("/api/demo/start", &json!({})).unwrap();
    sim.settle();
    sim.give("step:model", &json!({"wait": true}));
    let before = exchange.announcement(None).to_value();
    assert!(
        view.while_inactive(|| exchange.commit_announcement(&prepared, true))
            .is_none()
    );
    assert_eq!(exchange.announcement(None).to_value(), before);
    sim.script.gives.send(json!({"ok": true})).unwrap();
    sim.world.runs.lock().unwrap().close();
    assert!(
        view.while_inactive(|| exchange.commit_announcement(&prepared, true))
            .is_some()
    );
}

const STEPS: [&str; 10] = [
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

fn prior(sim: &mut Sim, kind: &str) {
    if kind == "fresh" {
        return;
    }
    sim.post("/api/demo/start", &json!({}))
        .expect("prior start");
    sim.settle();
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
            for n in STEPS {
                if n == "serving" {
                    // the last step returns and the run holds
                    sim.give(&format!("step:{n}"), &json!({"ok": true}));
                } else {
                    sim.give(&format!("step:{n}"), &json!({"ok": true}));
                }
            }
            if kind == "stopped" {
                sim.post("/api/demo/stop", &json!({})).expect("prior stop");
                sim.settle();
                sim.give("teardown", &json!({"ok": true}));
            }
        }
        other => panic!("unknown prior {other}"),
    }
}

/// Proves: B-RUN-02 (a run in progress cannot be started twice; Start / Rebuild need the owner role and a run
/// to rebuild: owner_role_required, invalid_request, owner_swarm_required, owner_run_active,
/// owner_run_rebuild_requires_run, execution_ceiling_invalid answered over the real route before any run is
/// touched), B-RUN-06 and B-SWARM-18 (relay_required with its message and the start_relay fix, on Start and
/// Rebuild only), B-RUN-15 (the Rebuild answer carries rebuild_requested and the fresh run keeps its ceiling),
/// B-RUN-04 (Stop on every prior state ends in stopping then stopped, also while serving), partial B-RUN-01
/// (a started run reaches its first step on the driver thread; the other nine steps are the chain's).
#[test]
fn the_demo_routes_equal_the_executed_oracle_ladder_on_every_case() {
    let fx: Value =
        serde_json::from_str(include_str!("fixtures/oracle_demo_action.json")).expect("fixture");
    let cases = fx["cases"].as_array().expect("cases");
    assert_eq!(cases.len(), 285);
    assert_eq!(fx["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let mut admitted = 0;
    let mut routed = 0;
    for (n, case) in cases.iter().enumerate() {
        // A body that is not a JSON object never reaches the action: the desktop boundary answers
        // `invalid_request` first (api::desktop, covered by its own corpus); `action` itself is
        // replayed over those 63 cases in crates/owner-run.
        if !case["body"].is_object() {
            continue;
        }
        routed += 1;
        let label = format!(
            "case {n}: role {} authority {} relay {} prior {} {} body {}",
            case["role"],
            case["authority"],
            case["relay"],
            case["prior"],
            case["path"],
            case["body"]
        );
        let facts = Facts {
            owner: case["role"] == "owner",
            exchange: case["authority"] == "ok",
            relay: case["relay"].as_u64(),
        };
        let mut sim = Sim::new(Facts {
            owner: true,
            exchange: true,
            relay: Some(0),
        });
        prior(&mut sim, case["prior"].as_str().expect("prior"));
        *sim.world.facts.lock().unwrap() = facts;
        let path = case["path"].as_str().expect("path");
        let result = &case["result"];
        match sim.post(path, &case["body"]) {
            Err(error) => {
                assert_eq!(error.status, 400, "{label}");
                let mut want = Map::new();
                for key in ["error", "message", "fix"] {
                    if let Some(v) = result.get(key) {
                        want.insert(key.to_owned(), v.clone());
                    }
                }
                assert_eq!(Value::Object(want), error.document(), "{label}");
                assert!(
                    case.get("after").is_none(),
                    "{label}: refused but the oracle ran it"
                );
            }
            Ok(answer) => {
                admitted += 1;
                assert_eq!(result["ok"], true, "{label}: admitted, oracle: {result}");
                let value = answer.to_value();
                assert_eq!(result["state"], value["state"], "{label}: answered state");
                assert_eq!(result["reason"], value["reason"], "{label}");
                let want: Vec<&str> = result["keys"]
                    .as_array()
                    .expect("keys")
                    .iter()
                    .filter_map(Value::as_str)
                    .collect();
                let mut keys = answer.keys();
                let rebuild = keys.last() == Some(&"rebuild_requested");
                assert_eq!(want, keys, "{label}: answer keys");
                keys.clear();
                assert_eq!(
                    result["rebuild_requested"].as_bool(),
                    rebuild.then_some(true),
                    "{label}"
                );
                sim.settle();
                let after = &case["after"];
                let was_live = matches!(case["prior"].as_str(), Some("running" | "serving"));
                if path == "/api/demo/stop" && was_live {
                    assert_eq!(sim.blocked, Blocked::Teardown, "{label}");
                    assert_eq!(
                        after["at_teardown"],
                        sim.status()["state"],
                        "{label}: at teardown"
                    );
                    sim.give("teardown", &json!({"ok": true}));
                } else {
                    assert!(after.get("at_teardown").is_none(), "{label}");
                }
                if path == "/api/demo/rebuild" && was_live {
                    assert_eq!(sim.blocked, Blocked::Teardown, "{label}");
                    sim.give("teardown", &json!({"ok": true}));
                }
                if path != "/api/demo/stop" {
                    assert_eq!(
                        sim.blocked,
                        Blocked::Step("model".into()),
                        "{label}: first step of the fresh run"
                    );
                }
                let last = sim.status();
                assert_eq!(
                    after["final_state"], last["state"],
                    "{label}: settled state"
                );
                assert_eq!(after["reason"], last["reason"], "{label}");
                assert_eq!(after["run_id"], last["run_id"], "{label}");
                assert_eq!(after["step"], last["step"], "{label}");
            }
        }
        // End every run the case left alive so no driver thread outlives it: the chain sees the script gone.
        let Sim { world, script, .. } = sim;
        drop(script);
        world.runs.lock().unwrap().close();
    }
    assert_eq!(routed, 222, "every object-bodied case was routed");
    assert_eq!(
        admitted, 26,
        "the oracle admitted exactly the recorded cases"
    );
}
