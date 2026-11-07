// SPDX-License-Identifier: AGPL-3.0-only
//! Member leave (`mycelium_a13_macos/member_leave.py` @ f4e366e9): `Leave smoothly` and
//! `Instant leave` of a joined Mac. Instant: fence new work, withdraw the standing consent,
//! disconnect, cancel downloads - every step runs even when an earlier one failed. Smooth: the
//! same after a bounded drain of in-flight work, unload and publication of the withdrawal.
//! The state persists in `leave-state.json` so a restart finishes an interrupted leave.
//!
//! A smooth leave answers at once with the persisted `begin` record and drains on a daemon thread
//! named `member-leave` (`persistent`, `tests/leave_background.rs`), as the source does; an instant
//! leave runs on the caller's thread and answers complete.
use crate::{
    schema::{PublicError, Raised},
    wire::{self, Json, Text},
};
use serde_json::{Map, Value, json};
use std::{
    fs,
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
};

const PROTOCOL: &str = "mycelium.a13_member_leave.v1";
const FILE: &str = "leave-state.json";
const MODES: [&str; 2] = ["smooth", "instant"];
const STATES: [&str; 5] = ["leaving", "draining", "handing_off", "withdrawing", "left"];
const FALLBACKS: [&str; 3] = ["stall", "cap", "restart"];

/// A validated field was unhashable in the source (`TypeError: cannot use 'list' as a set element`).
#[derive(Debug, PartialEq)]
pub struct TypeError;

/// An in-flight count that cannot be observed (`adaptive_drain.inflight_count` raising).
#[derive(Debug, PartialEq)]
pub struct Unobservable;

/// Positive cleanup evidence: an owned set was cleaned, or durably owns nothing.
#[derive(Clone, Copy, Debug)]
pub enum ComputeCleanup {
    Proven,
    NoneOwned,
}

/// A host's side of a leave that may run on a background thread (`member_leave.app_steps`). The host
/// hands one out per leave (`Host::leave_worker`); it must own whatever it touches (shared state).
pub trait LeaveWorker: Send + 'static {
    /// `fence`, `unload`, `publish_withdrawal`, `disconnect`, `cancel_work`; `Err(code)` is a failed
    /// step (`None` = no usable code). `withdraw_consent` is the api's own file work.
    fn step(&mut self, _name: &str) -> Result<(), Option<String>> {
        Ok(())
    }
    /// Requests still running here (`adaptive_drain.inflight_count`); `Err` = unobservable.
    fn inflight(&mut self) -> Result<i64, Unobservable> {
        Ok(0)
    }
    /// An explicit host proof, separate from membership departure. None means
    /// this legacy worker has no compute-proof interface, not successful cleanup.
    fn compute_cleanup(&mut self) -> Option<Result<crate::leave::ComputeCleanup, String>> {
        None
    }
    /// `time.time()`.
    fn now_seconds(&self) -> f64;
}
/// A host with no real work in flight: every step succeeds at once.
pub struct IdleWorker;
impl LeaveWorker for IdleWorker {
    fn now_seconds(&self) -> f64 {
        SystemClock::default().wall()
    }
}
/// The seams a leave drives. `handoff` is `None` where no clean handoff exists (a member today).
pub trait Steps {
    /// `fence`, `unload`, `publish_withdrawal`, `withdraw_consent`, `disconnect`, `cancel_work`;
    /// `Err(code)` is a failed step (`None` = no code).
    fn step(&mut self, _name: &str) -> Result<(), Option<String>> {
        Ok(())
    }
    /// Requests still running here; `Err` = cannot be proven (counted as unchanged).
    fn inflight(&mut self) -> Result<i64, Unobservable> {
        Ok(0)
    }
    fn handoff(&mut self) -> Option<Result<bool, Unobservable>> {
        None
    }
    /// Forward the host's explicit compute proof; membership steps alone cannot
    /// manufacture one. A refusal remains durable and retryable.
    fn compute_cleanup(&mut self) -> Option<Result<crate::leave::ComputeCleanup, String>> {
        None
    }
    /// `resume_leave`: keep a leave that already happened fenced (and, when `left`, not renewing).
    fn mark(&mut self, _fence: bool, _left: bool) {}
}
pub trait Clock {
    fn monotonic(&mut self) -> f64;
    fn sleep(&mut self, seconds: f64);
    fn wall(&mut self) -> f64;
}
/// The real clocks.
pub struct SystemClock(std::time::Instant);
impl Default for SystemClock {
    fn default() -> Self {
        Self(std::time::Instant::now())
    }
}
impl Clock for SystemClock {
    fn monotonic(&mut self) -> f64 {
        self.0.elapsed().as_secs_f64()
    }
    fn sleep(&mut self, seconds: f64) {
        std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
    }
    fn wall(&mut self) -> f64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
    }
}
/// `LEAVE_SMOOTH_STALL_S`, `LEAVE_SMOOTH_CAP_S`, `POLL_S`.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub stall: f64,
    pub cap: f64,
    pub poll: f64,
}
impl Default for Limits {
    fn default() -> Self {
        let seconds = |id: budgets::BudgetId| {
            id.span(budgets::Entries::ZERO)
                .expect("fixed leave duration budget")
                .millis() as f64
                / 1000.0
        };
        Self {
            stall: seconds(budgets::BudgetId::PyMyceliumA13MacosMemberLeaveLeaveSmoothStallSL33),
            cap: seconds(budgets::BudgetId::PyMyceliumA13MacosMemberLeaveLeaveSmoothCapSL34),
            poll: seconds(budgets::BudgetId::PyMyceliumA13MacosMemberLeavePollSL35),
        }
    }
}
type Record = Map<String, Value>;

#[derive(Clone)]
pub struct MemberLeave {
    path: PathBuf,
    limits: Limits,
}
/// Keys sorted at every level (`json.dumps(sort_keys=True)`), whatever the map implementation.
fn sorted(value: &Value) -> Json {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            Json::Obj(
                keys.into_iter()
                    .map(|k| (k.clone(), sorted(&map[k])))
                    .collect(),
            )
        }
        Value::Array(items) => Json::Arr(items.iter().map(sorted).collect()),
        other => Json::from(other.clone()),
    }
}
fn os_failure(error: &std::io::Error) -> PublicError {
    crate::preferences::os_error(error)
}
impl MemberLeave {
    pub fn new(root: &Path, limits: Limits) -> Self {
        Self {
            path: root.join(FILE),
            limits,
        }
    }
    /// `_load`: the persisted record when it is a valid leave record, else `None`.
    pub fn status(&self) -> Result<Option<Record>, TypeError> {
        let Ok(raw) = fs::read(&self.path) else {
            return Ok(None);
        };
        let Ok(text) = wire::decode_json_bytes(&raw) else {
            return Ok(None);
        };
        let Some(Value::Object(value)) = wire::parse_python_json(&text) else {
            return Ok(None);
        };
        // `value.get(k) not in frozenset(...)` hashes the value: containers are a TypeError, and the
        // checks short-circuit in this order.
        let hashable = |v: Option<&Value>| !matches!(v, Some(Value::Array(_) | Value::Object(_)));
        if value.get("protocol") != Some(&Value::from(PROTOCOL)) {
            return Ok(None);
        }
        let member = |v: Option<&Value>, set: &[&str]| {
            v.and_then(Value::as_str).is_some_and(|s| set.contains(&s))
        };
        if !hashable(value.get("state")) {
            return Err(TypeError);
        }
        if !member(value.get("state"), &STATES) || !member(value.get("mode"), &MODES) {
            return Ok(None);
        }
        if !hashable(value.get("fallback")) {
            return Err(TypeError);
        }
        let fallback_ok = match value.get("fallback") {
            Some(Value::Null) | None => true,
            other => member(other, &FALLBACKS),
        };
        if !fallback_ok || !value.get("steps").is_some_and(Value::is_array) {
            return Ok(None);
        }
        Ok(Some(value))
    }
    /// `_write_private`: a 0600 temporary next to the file, fsync, rename.
    fn save(&self, record: &Record) -> Result<(), PublicError> {
        let raw = sorted(&Value::Object(record.clone())).dumps(Text::Ascii);
        let name = self
            .path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(FILE);
        let temporary =
            self.path
                .with_file_name(format!("{name}.{}.{}.tmp", std::process::id(), 1));
        let written = (|| -> std::io::Result<()> {
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .custom_flags(libc_nofollow())
                .mode(0o600)
                .open(&temporary)?;
            file.write_all(&raw)?;
            file.flush()?;
            file.sync_all()?;
            drop(file);
            fs::rename(&temporary, &self.path)
        })();
        written.map_err(|error| {
            let verdict = os_failure(&error);
            fs::remove_file(&temporary).unwrap_or(());
            verdict
        })
    }
    /// `begin`: validate and persist the start of a leave.
    pub fn begin(
        &self,
        mode: &str,
        fallback: Option<&str>,
        wall: f64,
    ) -> Result<Record, PublicError> {
        if !MODES.contains(&mode) {
            return Err(PublicError::new(409, "leave_mode_invalid").raised_by(Raised::Leave));
        }
        let record = json!({
            "protocol": PROTOCOL, "mode": mode,
            "state": if mode == "smooth" && fallback.is_none() { "draining" } else { "leaving" },
            "started_at": wall, "fallback": fallback, "steps": [], "handoff": "unavailable",
        });
        let Value::Object(record) = record else {
            return Err(PublicError::new(409, "action_not_completed"));
        };
        self.save(&record)?;
        Ok(record)
    }
    pub fn run(
        &self,
        mode: &str,
        steps: &mut dyn Steps,
        clock: &mut dyn Clock,
    ) -> Result<Record, PublicError> {
        let wall = clock.wall();
        let record = self.begin(mode, None, wall)?;
        self.execute(record, steps, clock)
    }
    /// `resume`: finish an interrupted leave after a restart (instant: nothing is in flight).
    pub fn resume(
        &self,
        steps: &mut dyn Steps,
        clock: &mut dyn Clock,
    ) -> Result<Option<Record>, ResumeError> {
        let Some(record) = self.status().map_err(|_| ResumeError::Type)? else {
            return Ok(None);
        };
        if record["state"] == "left" {
            return if cleanup_pending(&record) {
                self.execute(record, steps, clock)
                    .map(Some)
                    .map_err(ResumeError::Failed)
            } else {
                Ok(None)
            };
        }
        let wall = clock.wall();
        let record = self
            .begin("instant", Some("restart"), wall)
            .map_err(ResumeError::Failed)?;
        self.execute(record, steps, clock)
            .map(Some)
            .map_err(ResumeError::Failed)
    }
    pub fn execute(
        &self,
        mut record: Record,
        steps: &mut dyn Steps,
        clock: &mut dyn Clock,
    ) -> Result<Record, PublicError> {
        let retrying = record["state"] == "left";
        self.step(&mut record, "fence", steps);
        let smooth = record["mode"] == "smooth";
        if !retrying && smooth && record["fallback"].is_null() {
            self.smooth(&mut record, steps, clock)?;
        }
        if record["fallback"].is_null() && smooth {
            record["state"] = Value::from("withdrawing");
            self.save(&record)?;
            self.step(&mut record, "unload", steps);
            self.step(&mut record, "publish_withdrawal", steps);
        }
        // Privacy order: these always run, whatever failed before.
        self.step(&mut record, "withdraw_consent", steps);
        self.step(&mut record, "disconnect", steps);
        self.step(&mut record, "cancel_work", steps);
        if !record.get("compute_cleanup").is_some_and(cleanup_proven)
            && let Some(proof) = steps.compute_cleanup()
        {
            record.insert("compute_cleanup".into(), match proof {
                Ok(ComputeCleanup::Proven) => json!({"state":"proven","code":null}),
                Ok(ComputeCleanup::NoneOwned) => json!({"state":"none_owned","code":null}),
                Err(code) => json!({"state":"unproven","code": if code_shaped(&code) { code } else { "compute_cleanup_unproven".into() }}),
            });
        }
        record["state"] = Value::from("left");
        record.insert("finished_at".into(), json!(clock.wall()));
        self.save(&record)?;
        Ok(record)
    }
    fn step(&self, record: &mut Record, name: &str, steps: &mut dyn Steps) {
        if record["steps"].as_array().is_some_and(|rows| {
            rows.iter()
                .any(|row| row["name"] == name && row["ok"] == true)
        }) {
            return;
        }
        let row = match steps.step(name) {
            Ok(()) => json!({"name": name, "ok": true, "code": null}),
            Err(code) => {
                let code = code
                    .filter(|c| code_shaped(c))
                    .unwrap_or_else(|| "step_failed".to_owned());
                json!({"name": name, "ok": false, "code": code})
            }
        };
        if let Some(Value::Array(list)) = record.get_mut("steps") {
            if let Some(old) = list.iter_mut().find(|row| row["name"] == name) {
                *old = row;
            } else {
                list.push(row);
            }
        }
    }
    fn smooth(
        &self,
        record: &mut Record,
        steps: &mut dyn Steps,
        clock: &mut dyn Clock,
    ) -> Result<(), PublicError> {
        if let Some(outcome) = steps.handoff() {
            record["state"] = Value::from("handing_off");
            self.save(record)?;
            record["handoff"] = Value::from(match outcome {
                Ok(true) => "done",
                Ok(false) => "refused",
                Err(Unobservable) => "failed",
            });
        }
        record["state"] = Value::from("draining");
        self.save(record)?;
        let start = clock.monotonic();
        let (mut last_progress, mut best): (f64, Option<i64>) = (start, None);
        loop {
            let count = steps
                .inflight()
                .unwrap_or_else(|Unobservable| best.unwrap_or(1));
            if count <= 0 {
                return Ok(());
            }
            let now = clock.monotonic();
            if best.is_none_or(|b| count < b) {
                best = Some(count);
                last_progress = now;
            }
            if now - last_progress >= self.limits.stall {
                record["fallback"] = Value::from("stall");
                break;
            }
            if now - start >= self.limits.cap {
                record["fallback"] = Value::from("cap");
                break;
            }
            clock.sleep(self.limits.poll);
        }
        record["state"] = Value::from("leaving");
        self.save(record)
    }
}
/// `O_NOFOLLOW` (the source passes `getattr(os, "O_NOFOLLOW", 0)`).
fn libc_nofollow() -> i32 {
    0x0100
}
/// `[a-z][a-z0-9_]{0,63}` (full match).
fn code_shaped(code: &str) -> bool {
    let mut chars = code.chars();
    chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && code.len() <= 64
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}
#[derive(Debug)]
pub enum ResumeError {
    Type,
    Failed(PublicError),
}
fn cleanup_proven(proof: &Value) -> bool {
    matches!(proof["state"].as_str(), Some("proven" | "none_owned")) && proof["code"].is_null()
}
/// A finished membership action can still owe cleanup. Do not clear its rows
/// or issue a new leave just because the legacy membership label is `left`.
pub fn cleanup_pending(record: &Map<String, Value>) -> bool {
    record["steps"]
        .as_array()
        .is_some_and(|rows| rows.iter().any(|row| row["ok"] != true))
        || record
            .get("compute_cleanup")
            .is_some_and(|proof| !cleanup_proven(proof))
}
/// `leave_status`: membership progress plus any explicit compute proof.
pub fn status_view(leave: &MemberLeave) -> Result<Value, TypeError> {
    Ok(match leave.status()? {
        None => Value::Null,
        Some(record) => Value::Object(
            [
                "mode",
                "state",
                "fallback",
                "handoff",
                "steps",
                "compute_cleanup",
            ]
            .iter()
            .filter_map(|key| record.get(*key).map(|v| ((*key).to_owned(), v.clone())))
            .collect(),
        ),
    })
}
/// `resume_leave`: finish an interrupted leave; keep a completed one fenced.
pub fn resume_leave(
    leave: &MemberLeave,
    steps: &mut dyn Steps,
    clock: &mut dyn Clock,
) -> Result<Option<Record>, ResumeError> {
    let Some(record) = leave.status().map_err(|_| ResumeError::Type)? else {
        return Ok(None);
    };
    if record["state"] == "left" {
        steps.mark(true, true);
        return if cleanup_pending(&record) {
            leave.resume(steps, clock)
        } else {
            Ok(Some(record))
        };
    }
    steps.mark(true, false);
    leave.resume(steps, clock)
}
