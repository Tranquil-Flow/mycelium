// SPDX-License-Identifier: AGPL-3.0-only
//! A deterministic stand-in for the oracle probe's threads: it performs the machine's commands
//! exactly as `r11-probe-owner-run.py`'s fake chain did (blocked at a step or teardown until the
//! script gives an outcome), so a machine trace can be compared with the executed one.
#![allow(dead_code)]
pub mod compile_double;
pub mod vworld;

use budgets::{BudgetId, Entries};
use std::time::Duration;

fn span(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).map_or(0, |s| s.millis()))
}
/// The loop's poll interval under test: the registered owner-run poll (1 s) divided by 1,000 so that a test
/// replays a run in milliseconds (the production value is `Timing::registered().poll`).
pub fn tick() -> Duration {
    span(BudgetId::PyMyceliumA13MacosOwnerRunOwnerrunCeilingPollSL155) / 1000
}
/// How long a test waits for an explicit condition before it fails: the registered rebuild join allowance.
pub fn patience() -> Duration {
    span(BudgetId::PyMyceliumA13MacosOwnerRunOwnerrunRebuildJoinSecondsL284)
}
use owner_run::machine::{
    CeilingProbe, Clock, Cmd, Identity, MonitorProbe, OwnerRun, OwnerRunError, StepOutcome,
};
use serde_json::{Map, Value, json};

pub const ORACLE_FIXTURE_CLOCK: f64 = 1000.0;

#[derive(Clone, Debug, PartialEq)]
pub enum Blocked {
    No,
    Step(String),
    Teardown,
}

pub struct Facts {
    pub monitor: Value,
    pub ceiling: Value,
    pub rollover_due: bool,
}

pub struct Sim {
    pub run: OwnerRun,
    pub mono: f64,
    pub blocked: Blocked,
    /// a step is inside `ctx.wait_until(lambda: False)`: only Stop ends it
    pub waiting: bool,
    pub stop_flag: bool,
    pub facts: Facts,
    pub entered: Vec<Value>,
    pub ctx: (String, String, &'static str, Option<Value>),
    pub rebuild_join_patched: bool,
}

pub fn obj(v: &Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

impl Sim {
    pub fn new(backoff_base: Option<f64>) -> Self {
        let mut n = 0;
        let mut run = OwnerRun::new(Box::new(move || {
            n += 1;
            Identity {
                run_id: format!("run-{n}"),
                deployment_id: format!("dep-{n}"),
            }
        }));
        if let Some(base) = backoff_base {
            run.backoff.base_seconds = base;
        }
        Self {
            run,
            mono: ORACLE_FIXTURE_CLOCK,
            blocked: Blocked::No,
            waiting: false,
            stop_flag: false,
            facts: Facts {
                monitor: Value::Null,
                ceiling: Value::Null,
                rollover_due: false,
            },
            entered: Vec::new(),
            ctx: (String::new(), String::new(), "start", None),
            rebuild_join_patched: false,
        }
    }

    pub fn clock(&self) -> Clock {
        Clock {
            mono: self.mono,
            wall: self.mono,
        }
    }

    /// Perform commands as the oracle's run thread would.
    pub fn exec(&mut self, cmds: Vec<Cmd>) {
        // A Stop that arrived in the same instant is set before the thread looks (`check_stopped`).
        let simultaneous_stop = cmds.iter().any(|c| matches!(c, Cmd::SignalStop));
        if simultaneous_stop {
            self.stop_flag = true;
        }
        for cmd in cmds {
            match cmd {
                Cmd::NewRun {
                    run_id,
                    deployment_id,
                    reason,
                    ceiling,
                    ..
                } => {
                    self.ctx = (run_id, deployment_id, reason.as_str(), ceiling);
                    // a fresh context starts with a clear stop event
                    self.stop_flag = simultaneous_stop;
                }
                Cmd::RunStep { step } => {
                    if self.stop_flag {
                        let next = self.run.step_finished(StepOutcome::Stopped);
                        self.exec(next);
                    } else {
                        self.record(&format!("step:{step}"), None);
                        self.blocked = Blocked::Step(step.to_owned());
                    }
                }
                Cmd::Teardown { failed } => {
                    self.record("teardown", Some(failed));
                    self.blocked = Blocked::Teardown;
                }
                Cmd::SignalStop => {
                    self.stop_flag = true;
                    if self.waiting {
                        self.waiting = false;
                        let next = self.run.step_finished(StepOutcome::Stopped);
                        self.exec(next);
                    }
                }
            }
        }
    }

    fn record(&mut self, at: &str, failed: Option<bool>) {
        self.entered.push(json!({
            "at": at, "reason": self.ctx.2, "run_id": self.ctx.0, "deployment_id": self.ctx.1,
            "ceiling": self.ctx.3, "failed": failed,
        }));
    }

    /// The chain gives the outcome of the step or teardown it is blocked in.
    pub fn give(&mut self, key: &str, outcome: &Value) {
        let want = match &self.blocked {
            Blocked::Step(s) => format!("step:{s}"),
            Blocked::Teardown => "teardown".to_owned(),
            Blocked::No => String::new(),
        };
        assert_eq!(want, key, "the chain is not blocked at {key}");
        self.blocked = Blocked::No;
        for d in outcome["progress"].as_array().into_iter().flatten() {
            self.run.progress(d.as_str());
        }
        for (k, v) in obj(&outcome["result"]) {
            self.run.set_result(&k, v);
        }
        let next = if key == "teardown" {
            let result = match outcome["exception"].as_str() {
                Some(class) => Err(class.to_owned()),
                None => Ok(()),
            };
            self.run.teardown_finished(result, self.clock())
        } else if let Some(e) = outcome.get("error") {
            let fix = e.get("fix").filter(|f| !f.is_null()).map(obj);
            let err = OwnerRunError::with(
                e["code"].as_str().unwrap_or_default(),
                e["message"].as_str(),
                fix,
            );
            self.run.step_finished(StepOutcome::Refused(err))
        } else if let Some(class) = outcome["exception"].as_str() {
            self.run.step_finished(StepOutcome::Exception {
                class: class.to_owned(),
            })
        } else if outcome["wait"] == true {
            if self.stop_flag {
                self.run.step_finished(StepOutcome::Stopped)
            } else {
                self.waiting = true;
                Vec::new()
            }
        } else {
            self.run.step_finished(StepOutcome::Ok)
        };
        self.exec(next);
        self.settle();
    }

    fn ceiling_probe(&self) -> CeilingProbe {
        match &self.facts.ceiling {
            Value::Null => CeilingProbe::Code(None),
            Value::String(s) => CeilingProbe::Code(Some(s.clone())),
            other => CeilingProbe::Raised {
                class: other["raise"].as_str().unwrap_or("Exception").to_owned(),
            },
        }
    }

    /// The time the oracle's threads get between two probe operations: the backoff wait ends or
    /// notices a Stop, and the serving loop polls its ceiling (every poll interval).
    pub fn settle(&mut self) {
        for _ in 0..4 {
            let cmds = self.run.tick(self.clock());
            self.exec(cmds);
            if self.run.serving() && self.run.thread_alive() {
                let cmds = self
                    .run
                    .serving_poll(Some(self.facts.rollover_due), self.ceiling_probe());
                self.exec(cmds);
            }
        }
    }

    /// `OwnerRun.status()`: the ceiling poll, then (still serving) the monitor.
    pub fn live_status(&mut self) {
        if self.run.serving() {
            let cmds = self
                .run
                .serving_poll(Some(self.facts.rollover_due), self.ceiling_probe());
            self.exec(cmds);
        }
        if self.run.serving() {
            let probe = match &self.facts.monitor {
                Value::Null => MonitorProbe::Code(None),
                Value::String(s) => MonitorProbe::Code(Some(s.clone())),
                other => MonitorProbe::Raised {
                    class: other["raise"].as_str().unwrap_or("Exception").to_owned(),
                },
            };
            let cmds = self.run.monitor_result(probe);
            self.exec(cmds);
        }
        self.settle();
    }

    /// The snapshot as the probe normalised it.
    pub fn norm_snapshot(&self, flat_counts: bool) -> Value {
        let mut v = self.run.snapshot().to_value();
        let o = v.as_object_mut().expect("status is an object");
        let started = !o["started_at"].is_null();
        o.insert("started_at".into(), started.into());
        if flat_counts {
            for row in o["cleanup_errors"].as_array_mut().into_iter().flatten() {
                let n = row["count"].as_u64().unwrap_or(0);
                row["count"] = u64::from(n >= 1).into();
            }
        }
        o.insert("active".into(), self.run.active().into());
        v
    }
}
