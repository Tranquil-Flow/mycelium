// SPDX-License-Identifier: AGPL-3.0-only
//! A virtual world for replaying the executed oracle's step scripts (`r12-probe-steps.py`): time is a
//! number that only a `pause` advances, reports/skew/observation outcomes are functions of it, and every
//! collaborator call is logged as the probe's stubs logged it. Doubles live in tests only.
#![allow(dead_code)]
use owner_run::chain::{PortError, StepEnv, StepError};
use owner_run::steps::exchange::{Announcement, ClockSkew, MemberRow, ReportsPort};
use owner_run::steps::observe::ObservePort;
use serde_json::{Map, Value, json};
use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::rc::Rc;

pub const WALL0: f64 = 1_800_000_000.0;
pub const MONO0: f64 = 1000.0;

/// `at(timeline, t)`: the last entry whose start is <= t.
pub fn at(timeline: &Value, t: f64) -> Option<&Value> {
    let mut cur = None;
    for entry in timeline.as_array()? {
        if entry[0].as_f64()? <= t + 1e-9 {
            cur = Some(&entry[1]);
        } else {
            break;
        }
    }
    cur
}

/// Python `json.dumps(x, sort_keys=True)` for the shapes the announcements carry.
pub fn py_dumps(v: &Value) -> String {
    match v {
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("\"{k}\": {}", py_dumps(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        other => other.to_string(),
    }
}

pub struct World {
    pub t: Cell<f64>,
    pub log: RefCell<Vec<String>>,
    pub spec: Value,
}

impl World {
    pub fn new(spec: &Value) -> Rc<Self> {
        Rc::new(Self {
            t: Cell::new(0.0),
            log: RefCell::new(Vec::new()),
            spec: spec.clone(),
        })
    }
    pub fn note(&self, entry: impl Into<String>) {
        self.log.borrow_mut().push(entry.into());
    }
    /// `rle(log)` as the probe emits it.
    pub fn calls(&self) -> Value {
        rle(&self.log.borrow())
    }
}

pub fn rle(log: &[String]) -> Value {
    let mut out: Vec<(String, u64)> = Vec::new();
    for item in log {
        match out.last_mut() {
            Some((last, n)) if last == item => *n += 1,
            _ => out.push((item.clone(), 1)),
        }
    }
    Value::Array(out.into_iter().map(|(s, n)| json!([s, n])).collect())
}

pub struct VEnv {
    pub world: Rc<World>,
    pub progress: Vec<String>,
    pub results: Map<String, Value>,
    pub publishes: usize,
    pub stop_at: Option<f64>,
}

impl VEnv {
    pub fn new(world: &Rc<World>) -> Self {
        Self {
            world: world.clone(),
            progress: Vec::new(),
            results: Map::new(),
            publishes: 0,
            stop_at: world.spec["stop_at"].as_f64(),
        }
    }
}

impl StepEnv for VEnv {
    fn progress(&mut self, detail: &str) {
        self.progress.push(detail.to_owned());
    }
    fn stopped(&self) -> bool {
        self.stop_at.is_some_and(|s| self.world.t.get() >= s)
    }
    fn now(&mut self) -> f64 {
        MONO0 + self.world.t.get()
    }
    fn wall(&mut self) -> f64 {
        WALL0 + self.world.t.get()
    }
    fn pause(&mut self, seconds: f64) -> bool {
        self.world.t.set(self.world.t.get() + seconds);
        self.stopped()
    }
    fn record_exception(&mut self, code: &str, class: &str) {
        panic!("a step recorded an exception ({code}, {class})")
    }
    fn set_result(&mut self, key: &str, value: Value) {
        if key == "participants" {
            self.publishes += 1;
        }
        self.results.insert(key.to_owned(), value);
    }
}

/// The scripted run exchange, coordinator and observation context.
pub struct VPorts {
    pub world: Rc<World>,
    /// `context.observations` is set once a `collect()` succeeded.
    pub collected: bool,
}

impl ReportsPort for VPorts {
    fn forget_reports(&mut self) {
        self.world.note("forget_reports");
    }
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        let per = a
            .per_node
            .as_ref()
            .map(|rows| Value::Object(rows.iter().cloned().collect::<Map<String, Value>>()));
        self.world.note(match per {
            Some(p) => format!("announce:{}:{}", a.phase, py_dumps(&p)),
            None => format!("announce:{}", a.phase),
        });
        Ok(())
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        self.world.note("reports");
        let t = self.world.t.get();
        Ok(self.world.spec["reports"]
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(node, tl)| {
                at(tl, t)
                    .filter(|r| !r.is_null())
                    .map(|r| (node.clone(), r.clone()))
            })
            .collect())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.world.note("members");
        let t = self.world.t.get();
        self.world.spec["members"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|m| {
                m.get("present")
                    .is_none_or(|p| at(p, t).is_some_and(|v| v == &Value::Bool(true)))
            })
            .map(|m| MemberRow {
                node_id: m["node_id"].as_str().unwrap().to_owned(),
                lifecycle_state: m["lifecycle_state"].as_str().map(str::to_owned),
                lease_expires_at: m["lease_expires_at"].as_f64(),
            })
            .collect()
    }
    fn own_node_id(&mut self) -> Option<String> {
        self.world.spec["own"].as_str().map(str::to_owned)
    }
    fn member_identity(&mut self, node: &str) -> Value {
        self.world.note(format!("member:{node}"));
        self.world.spec["identity"][node].clone()
    }
    fn clock_skew(&mut self, nodes: &[String]) -> Result<Option<ClockSkew>, StepError> {
        self.world.note("exceeded");
        let t = self.world.t.get();
        let row = self.world.spec.get("skew").and_then(|s| at(s, t));
        let exceeded = row
            .and_then(Value::as_object)
            .into_iter()
            .flatten()
            .filter(|(n, _)| nodes.contains(n))
            .map(|(n, v)| (n.clone(), v.as_f64().unwrap()))
            .collect();
        Ok(Some(ClockSkew {
            tolerance: self.world.spec["tolerance"].as_f64().unwrap_or(60.0),
            exceeded,
        }))
    }
    fn tick_local(&mut self) {
        self.world.note("tick");
    }
}

fn scripted_error(code: &str) -> PortError {
    if code.starts_with('!') {
        PortError::Failed {
            code: None,
            text: "Not a code!".into(),
        }
    } else {
        PortError::Failed {
            code: Some(code.to_owned()),
            text: code.to_owned(),
        }
    }
}

impl ObservePort for VPorts {
    fn prepare(&mut self, _: &str, _: &str, _: &[Value]) -> Result<(), PortError> {
        self.world.note("prepare");
        let row =
            at(&self.world.spec["observe"]["prepare"], self.world.t.get()).and_then(Value::as_str);
        match row {
            Some("ok") | None => Ok(()),
            Some("OwnerRunError") => Err(PortError::Refused(owner_run::OwnerRunError::new(
                "prepare_owner_refusal",
            ))),
            Some(code) => Err(scripted_error(code)),
        }
    }
    fn rebuild(&mut self, _: &str, _: &str, _: &[Value]) -> Result<String, PortError> {
        self.world.note("rebuild");
        let row = at(&self.world.spec["observe"]["prepare"], self.world.t.get())
            .and_then(Value::as_str)
            .unwrap_or("ok");
        match row {
            "ok" => Ok("awaiting_bootstrap_workers".into()),
            "awaiting_bootstrap_workers" | "compiling" => Ok(row.into()),
            code => Err(scripted_error(code)),
        }
    }
    fn facts_nodes(&mut self) -> Vec<String> {
        let mut v: Vec<String> = self.world.spec["observe"]["facts"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| n.as_str().unwrap().to_owned())
            .collect();
        v.sort();
        v
    }
    fn offers(&mut self) -> Vec<(String, String)> {
        self.world.spec["observe"]["offers"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| {
                let n = n.as_str().unwrap();
                (n.to_owned(), format!("offer-{n}"))
            })
            .collect()
    }
    fn collect(&mut self) -> Result<(), PortError> {
        self.world.note("collect");
        let row = at(&self.world.spec["observe"]["collect"], self.world.t.get())
            .and_then(Value::as_str)
            .unwrap_or("ok");
        if row == "ok" {
            self.collected = true;
            Ok(())
        } else {
            Err(scripted_error(row))
        }
    }
    fn observed_hosts(&mut self) -> Vec<Option<String>> {
        if !self.collected {
            return Vec::new();
        }
        self.world.spec["observe"]["hosts"]
            .as_array()
            .unwrap()
            .iter()
            .map(|h| h.as_str().map(str::to_owned))
            .collect()
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        self.world.note("retire");
        let row = at(&self.world.spec["observe"]["retire"], self.world.t.get())
            .and_then(Value::as_str)
            .unwrap_or("ok");
        if row == "ok" {
            Ok(())
        } else {
            Err(scripted_error(row))
        }
    }
}
