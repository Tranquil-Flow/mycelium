// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's own Mac as a member of its run: `DemoTargetAgent(local=True)` (`demo_target.py:172-557 @ f4e366e9`),
//! the part the chain reaches before observation. Each tick reads the owner's announcement, answers it with this
//! Mac's state and, once per run, a signed installation descriptor (re-signed before half its life is gone while the
//! owner still collects), and hands every report to the owner's inbox.
//!
//! Announced bootstrap offers invoke the host's authenticated staging/worker collaborator;
//! its absence is an explicit refusal, never a fabricated `bootstrap_running` report.
//!
//! The work phase (`demo_target.py:449-556`): an owner-requested cleanup runs first; this Mac's
//! announced share must fit its consent caps (refused by name before any fetch); in slice mode it
//! fetches and verifies exactly its own slice ([`crate::slice_fetch`], on a thread, Stop between
//! segments) and reports `downloading_slice` then `slice_ready`; the owner's own contributor previews
//! and accepts its work offer; the state is then derived from its native worker (`work_running`,
//! `serving`, a blocked worker's code, or what it is still waiting for). Every state is reported by
//! the member itself; the owner never synthesises one. A member with no owner-served slice source is
//! refused by name (`slice_repository_fallback_not_ported`): the ranged repository download is S1's.
use crate::installation_descriptor::Failure;
use crate::slice_fetch::{self, SliceTransport};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value, json};
use std::{
    collections::BTreeSet,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::JoinHandle,
};

pub const MEMBER_REPORT: &str = "mycelium.a13_demo_member_report.v1";

/// `_ensure_whole_model(a)` of a joined Mac: the announced whole snapshot is here, or what it waits for.
#[derive(Clone, Debug, PartialEq)]
pub enum WholeModel {
    Ready,
    Waiting {
        state: String,
        detail: Option<String>,
        progress: Option<Value>,
    },
}

/// A failed exchange call. The oracle exposes only the call site's closed code, not exception text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExchangeUnavailable;

/// What the agent reads and writes outside itself.
pub trait AgentHost {
    /// `source()`: `exchange.announcement(local)`; `Err` is any exception.
    fn announcement(&mut self) -> Result<Option<Value>, ExchangeUnavailable>;
    /// The contributor's node id (`actor.member.node_id`); `None` when this Mac has no membership.
    fn member(&mut self) -> Option<String>;
    /// `OwnerRunConsent._active()`.
    fn run_active(&mut self) -> bool;
    /// A joined Mac's `StandingConsentGate`: the closed reason its standing grant does not allow
    /// work now (`standing_consent_missing` / `_withdrawn` / `_unbound` / `_owner_mismatch`).
    fn standing_refusal(&mut self) -> Option<String> {
        Some("standing_consent_missing".into())
    }
    /// `app._resource_check()`: a closed reason when a power / thermal / network gate refuses.
    fn resource_check(&mut self) -> Option<String>;
    /// The pinned snapshot on this Mac (`acquisition.prepared_source()`): model id, commit.
    fn prepared_model(&mut self) -> Result<(String, String), Failure>;
    /// `_make_descriptor` signs: whole snapshot (`None`) or the per-run slice root (`Some(manifest digest)`).
    fn describe(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        slice_manifest_digest: Option<&str>,
    ) -> Result<Value, Failure>;
    /// Accept the announced, authenticated bootstrap offer and start the actual
    /// hello-only worker. Success means the owned worker is running, not merely
    /// a staging ACK, signed receipt, or local transport connection.
    fn bootstrap(&mut self, _run: &str, _deployment: &str, _offer: &str) -> Result<(), Failure> {
        Err(Failure::new("RuntimeError", "native_bootstrap_unavailable"))
    }
    /// `actor.pending_cleanup()`: the owner's signed request to clean this member's stage, if any.
    fn pending_cleanup(&mut self) -> Result<Option<Value>, Failure> {
        Ok(None)
    }
    /// `/api/control/stop` then `/api/staging/cleanup/accept` for `request` (`_cleanup_if_requested`).
    fn cleanup(&mut self, _request: &Value) -> Result<(), Failure> {
        Err(Failure::new("RuntimeError", "member_cleanup_unavailable"))
    }
    /// `/api/control/stop`: no owner run may keep a worker.
    fn stop_native(&mut self) -> Result<(), Failure> {
        Err(Failure::new("RuntimeError", "member_cleanup_unavailable"))
    }
    /// `actor.status()['native_control']` (an empty object when this Mac has none).
    fn native_control(&mut self) -> Result<Value, Failure> {
        Ok(json!({}))
    }
    /// `actor.pending_stage()`: the pending signed (bootstrap or work) offer, if any.
    fn pending_offer(&mut self) -> Result<Option<Value>, Failure> {
        Ok(None)
    }
    /// `/api/work/preview` then `/api/work/accept` under `limits` (the owner's own contributor).
    fn accept_work(&mut self, _limits: &Value) -> Result<(), Failure> {
        Err(Failure::new("RuntimeError", "member_work_unavailable"))
    }
    /// `consent.member_caps(app)`: the caps this Mac's share must fit (an object; `{}` = none known).
    fn member_caps(&mut self) -> Result<Value, Failure> {
        Ok(json!({}))
    }
    /// `consent.storage_cap(app)`.
    fn storage_cap(&mut self) -> Result<u64, Failure> {
        Err(Failure::new("KeyError", "demo_step_failed"))
    }
    /// `slice_root(run_id)`: the per-run slice root the descriptor named.
    fn slice_root(&mut self, _run_id: &str) -> Result<PathBuf, Failure> {
        Err(Failure::new("ValueError", "slice_destination_invalid"))
    }
    /// The owner-served byte source for `announcement`'s slice; `None` = no peer source (the
    /// repository fallback is not ported here).
    fn slice_transport(&mut self, _announcement: &Value) -> Option<Box<dyn SliceTransport>> {
        None
    }
    /// A joined Mac in whole-snapshot mode: `_ensure_whole_model(a)` (never called for the owner's own
    /// contributor, whose snapshot is the owner's).
    fn whole_model(&mut self, _announcement: &Value) -> Result<WholeModel, Failure> {
        Ok(WholeModel::Ready)
    }
    /// `sink(report)` = `exchange.submit_local(local, report)`.
    fn sink(&mut self, report: &Value) -> Result<(), ExchangeUnavailable>;
    /// `self._clock()`.
    fn clock(&mut self) -> f64;
}

fn report_interval() -> f64 {
    BudgetId::PyFinalMyceliumA13MacosDemoTargetReportIntervalL177C82
        .span(Entries::ZERO)
        .map_or(f64::NAN, |s| s.millis() as f64 / 1000.0)
}

/// `_code(error, default)`: a closed lower-case code survives, anything else becomes the call site's default.
fn closed(code: &str, default: &str) -> String {
    let mut chars = code.chars();
    let ok = chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && code.len() <= 96
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_');
    if ok { code } else { default }.to_owned()
}

type ReportKey = (Value, String, Option<String>, String, bool);

/// `_slice_result`: nothing yet, this run's slice verified, or this run's fetch refused.
#[derive(Clone, Debug, Default, PartialEq)]
enum SliceResult {
    #[default]
    None,
    Done(String),
    Failed(String, String),
}

/// The slice fetch thread (`a13-slice-fetch`) and where it leaves its outcome.
struct SliceJob {
    handle: JoinHandle<()>,
    outcome: Arc<Mutex<Option<SliceResult>>>,
}

#[derive(Default)]
pub struct LocalAgent {
    announcement: Option<Value>,
    state: String,
    detail: Option<String>,
    progress: Option<Value>,
    descriptor_for: Option<(String, String)>,
    descriptor: Option<Value>,
    last_report: Option<ReportKey>,
    last_report_at: f64,
    bootstrap_done: BTreeSet<String>,
    work_done: BTreeSet<String>,
    slice_job: Option<SliceJob>,
    slice_result: SliceResult,
    slice_stop: Arc<AtomicBool>,
    /// `local=False`: a joined Mac (`DemoTargetAgent(app, consent=StandingConsentGate(), ...)`).
    remote: bool,
}

/// `_code(error, default)` over a native worker's error code.
fn native_code(native: &Value, default: &str) -> String {
    closed(native["error_code"].as_str().unwrap_or_default(), default)
}

/// `model_resources.check_member_caps(row, caps)`: a known integer cap below the share refuses it.
pub fn check_member_caps(share: &Value, caps: &Value) -> Result<Option<&'static str>, Failure> {
    let cap = |key: &str| {
        caps.get(key)
            .filter(|v| v.is_i64() || v.is_u64())
            .and_then(Value::as_f64)
    };
    let need = |key: &str| {
        share
            .get(key)
            .and_then(Value::as_f64)
            .ok_or_else(|| Failure::new("KeyError", "demo_step_failed"))
    };
    if let Some(cap) = cap("max_memory_bytes")
        && cap < need("max_rss_bytes")?
    {
        return Ok(Some("member_memory_cap_below_share"));
    }
    if let Some(cap) = cap("storage_bytes")
        && cap < need("max_disk_bytes")?
    {
        return Ok(Some("member_storage_cap_below_share"));
    }
    Ok(None)
}

fn seconds(id: BudgetId) -> u64 {
    id.span(Entries::ZERO).map_or(0, |s| s.millis() / 1000)
}

fn text(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(Value::as_str).map(str::to_owned)
}

impl LocalAgent {
    pub fn new() -> Self {
        let mut agent = Self::default();
        agent.state = "idle".into();
        agent
    }

    /// The agent of a JOINED Mac (`local=False`): it reports over the signed exchange, needs the
    /// owner's announced bootstrap offer itself before it starts its worker, and leaves work offers
    /// to its standing acceptor.
    pub fn remote() -> Self {
        let mut agent = Self::new();
        agent.remote = true;
        agent
    }

    /// `_set(state, detail, progress=None)`.
    fn set(&mut self, state: &str, detail: Option<&str>) {
        self.set_progress(state, detail, None);
    }
    fn set_progress(&mut self, state: &str, detail: Option<&str>, progress: Option<Value>) {
        self.state = state.to_owned();
        self.detail = detail.map(str::to_owned);
        self.progress = progress;
    }

    /// `status()`.
    pub fn status(&self) -> Value {
        let a = self.announcement.clone().unwrap_or_else(|| json!({}));
        json!({
            "state": self.state, "detail": self.detail,
            "progress": self.progress.clone().unwrap_or(Value::Null), "error_http": Value::Null,
            "phase": a.get("phase").cloned().unwrap_or(Value::Null),
            "run_id": a.get("run_id").cloned().unwrap_or(Value::Null),
            "model_id": a.get("model").and_then(|m| m.get("model_id")).cloned().unwrap_or(Value::Null),
            "local": !self.remote,
            "sources": Value::Null,
        })
    }

    /// `_consent(action)` under `OwnerRunConsent`.
    fn consent(&mut self, host: &mut dyn AgentHost) -> bool {
        let reason = if self.remote {
            // `StandingConsentGate.allows`: the join lane's grant, bound to this membership.
            host.standing_refusal().or_else(|| host.resource_check())
        } else if host.run_active() {
            host.resource_check()
        } else {
            Some("owner_run_not_active".to_owned())
        };
        match reason {
            None => true,
            Some(reason) => {
                let state = if reason.starts_with("standing_consent") {
                    "waiting_for_consent"
                } else {
                    "idle"
                };
                self.set(state, Some(&reason));
                false
            }
        }
    }

    /// `_make_descriptor(a)`.
    fn make_descriptor(&mut self, host: &mut dyn AgentHost, a: &Value) -> Result<Value, Failure> {
        let run = text(a, "run_id").unwrap_or_default();
        let deployment = text(a, "deployment_id").unwrap_or_default();
        if a.get("slice_mode")
            .is_some_and(owner_run::steps::exchange::truthy)
        {
            let digest = text(a, "manifest_digest").unwrap_or_default();
            return host.describe(&run, &deployment, Some(&digest));
        }
        let (model_id, commit) = host.prepared_model()?;
        let model = a.get("model").cloned().unwrap_or_else(|| json!({}));
        if (Some(model_id), Some(commit))
            != (text(&model, "model_id"), text(&model, "resolved_commit"))
        {
            return Err(Failure::new("ValueError", "demo_model_mismatch"));
        }
        host.describe(&run, &deployment, None)
    }

    /// `_descriptor_aging()`: less than half of the descriptor's life is left.
    fn aging(&self, host: &mut dyn AgentHost) -> bool {
        let statement = self.descriptor.as_ref().and_then(|d| d.get("statement"));
        let number = |k: &str| {
            statement
                .and_then(|s| s.get(k))
                .and_then(Value::as_f64)
                .filter(|v| v.is_finite())
        };
        let (Some(issued), Some(expires)) = (number("issued_at"), number("expires_at")) else {
            return false;
        };
        if expires <= issued {
            return false;
        }
        host.clock() >= issued + (expires - issued) / 2.0
    }

    /// `_report(announcement, force=)`.
    fn report(&mut self, host: &mut dyn AgentHost, force: bool) {
        let a = self.announcement.clone();
        let model = a
            .as_ref()
            .and_then(|a| a.get("model"))
            .filter(|m| owner_run::steps::exchange::truthy(m))
            .map(|m| json!({"model_id": m["model_id"], "resolved_commit": m["resolved_commit"]}));
        let run_id = a
            .as_ref()
            .and_then(|a| a.get("run_id"))
            .cloned()
            .unwrap_or(Value::Null);
        let deployment_id = a
            .as_ref()
            .and_then(|a| a.get("deployment_id"))
            .cloned()
            .unwrap_or(Value::Null);
        let current = match (&self.descriptor_for, &a) {
            (Some((run, dep)), Some(a)) => {
                text(a, "run_id").as_deref() == Some(run)
                    && text(a, "deployment_id").as_deref() == Some(dep)
            }
            _ => false,
        };
        let descriptor = if current {
            self.descriptor.clone()
        } else {
            None
        };
        let issued_at = host.clock();
        let progress = self.progress.clone().unwrap_or(Value::Null);
        let key: ReportKey = (
            run_id.clone(),
            self.state.clone(),
            self.detail.clone(),
            progress.to_string(),
            descriptor.is_some(),
        );
        let now = host.clock();
        if !force
            && self.last_report.as_ref() == Some(&key)
            && now - self.last_report_at < report_interval()
        {
            return;
        }
        let mut report = Map::new();
        report.insert("protocol".into(), MEMBER_REPORT.into());
        report.insert("run_id".into(), run_id);
        report.insert("deployment_id".into(), deployment_id);
        report.insert("state".into(), self.state.clone().into());
        report.insert("detail".into(), self.detail.clone().into());
        report.insert("model".into(), model.unwrap_or(Value::Null));
        report.insert("progress".into(), progress);
        report.insert("descriptor".into(), descriptor.unwrap_or(Value::Null));
        report.insert("issued_at".into(), json!(issued_at));
        if host.sink(&Value::Object(report)).is_ok() {
            self.last_report = Some(key);
            self.last_report_at = now;
        }
    }

    /// `_cleanup_if_requested(actor)`: true when the owner asked for cleanup (done, or waiting for consent).
    fn cleanup_if_requested(&mut self, host: &mut dyn AgentHost) -> Result<bool, Failure> {
        let Some(request) = host.pending_cleanup()? else {
            return Ok(false);
        };
        if !self.consent(host) {
            return Ok(true);
        }
        self.set("cleaning_up", None);
        host.cleanup(&request)?;
        Ok(true)
    }

    /// `_share_refusal(a)`: a closed code when this Mac's caps cannot hold its announced share.
    fn share_refusal(
        &mut self,
        host: &mut dyn AgentHost,
        a: &Value,
    ) -> Result<Option<String>, Failure> {
        let caps = host.member_caps()?;
        let limits = a
            .get("work_limits")
            .filter(|v| owner_run::steps::exchange::truthy(v))
            .cloned()
            .unwrap_or_else(|| json!({}));
        if let Some(wall) = limits.get("max_wall_seconds").filter(|v| !v.is_null()) {
            let reserve = seconds(BudgetId::WorkerCleanupReserve);
            let maximum =
                seconds(BudgetId::PyMyceliumA13MacosStandingConsentMaxWallSecondsMaximumL102);
            let Some(wall) = wall
                .as_u64()
                .filter(|w| (reserve + 1..=maximum).contains(w))
            else {
                return Ok(Some("member_wall_limit_invalid".into()));
            };
            if caps["max_wall_seconds"]
                .as_i64()
                .is_some_and(|cap| i128::from(wall) > i128::from(cap))
            {
                return Ok(Some("member_wall_cap_below_share".into()));
            }
        }
        match a.get("member_resources").filter(|v| v.is_object()) {
            Some(share) => Ok(check_member_caps(share, &caps)?.map(str::to_owned)),
            None => Ok(None),
        }
    }

    /// Move a finished fetch's outcome into `_slice_result` (the thread wrote it there in the source).
    fn collect_slice(&mut self) {
        if self
            .slice_job
            .as_ref()
            .is_some_and(|job| job.handle.is_finished())
        {
            self.join_slice();
        }
    }

    /// `join_slice()`: wait for a running fetch to end (tests and shutdown).
    pub fn join_slice(&mut self) {
        if let Some(job) = self.slice_job.take() {
            if job.handle.join().is_err() {
                eprintln!("slice fetch thread panicked");
            }
            let outcome = job
                .outcome
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .take();
            self.slice_result = outcome
                .unwrap_or_else(|| SliceResult::Failed(String::new(), "slice_fetch_failed".into()));
        }
    }

    /// `_drive_slice(a, node_plan)`: start (or watch) the fetch of exactly this member's slice.
    fn drive_slice(
        &mut self,
        host: &mut dyn AgentHost,
        a: &Value,
        node_plan: &Value,
        run_id: &str,
    ) -> Result<(), Failure> {
        // A fetch can end after the step's own collect: read its result before deciding to start
        // one (`_drive_slice` reads `_slice_result` the thread already wrote).
        self.collect_slice();
        let root = host.slice_root(run_id)?;
        if self
            .slice_job
            .as_ref()
            .is_some_and(|job| !job.handle.is_finished())
        {
            let progress = slice_fetch::slice_progress(node_plan, &root);
            self.set_progress("downloading_slice", None, progress);
            return Ok(());
        }
        if let SliceResult::Failed(run, code) = &self.slice_result
            && run == run_id
        {
            let code = code.clone();
            self.set("error", Some(&code));
            return Ok(());
        }
        if !self.consent(host) {
            return Ok(());
        }
        let mut cap = host.storage_cap()?;
        if let Some(disk) = a["member_resources"]["max_disk_bytes"].as_u64() {
            cap = cap.min(disk);
        }
        let expected = node_plan["node_plan_digest"]
            .as_str()
            .ok_or_else(|| Failure::new("KeyError", "demo_step_failed"))?
            .to_owned();
        let Some(mut transport) = host.slice_transport(a) else {
            let code = slice_fetch::REPOSITORY_FALLBACK_NOT_PORTED;
            self.slice_result = SliceResult::Failed(run_id.to_owned(), code.into());
            self.set("error", Some(code));
            return Ok(());
        };
        let outcome = Arc::new(Mutex::new(None));
        let (plan, run, stop, out, dir) = (
            node_plan.clone(),
            run_id.to_owned(),
            self.slice_stop.clone(),
            outcome.clone(),
            root.clone(),
        );
        let handle = std::thread::Builder::new()
            .name("a13-slice-fetch".into())
            .spawn(move || {
                let result = slice_fetch::fetch_node_slice(
                    &plan,
                    &expected,
                    &dir,
                    transport.as_mut(),
                    cap,
                    cap,
                    &|| stop.load(Ordering::SeqCst),
                );
                *out.lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(match result {
                    Ok(_) => SliceResult::Done(run),
                    Err(error) => SliceResult::Failed(run, closed(&error.0, "slice_fetch_failed")),
                });
            })
            .map_err(|_| Failure::new("RuntimeError", "slice_fetch_failed"))?;
        self.slice_job = Some(SliceJob { handle, outcome });
        let progress = slice_fetch::slice_progress(node_plan, &root);
        self.set_progress("downloading_slice", None, progress);
        Ok(())
    }

    /// `_work_waiting(offer, run_id, bootstrap, native)` for the owner's own contributor.
    fn work_waiting(
        offer: Option<&Value>,
        run_id: &str,
        bootstrap: Option<&str>,
        native: &Value,
    ) -> String {
        let state = &native["state"];
        if state == "running" {
            return "bootstrap_control_running".into();
        }
        if !(state.is_null() || ["stopped", "closed", "prepared"].iter().any(|s| state == s)) {
            let shown = state
                .as_str()
                .map_or_else(|| state.to_string(), str::to_owned);
            return native_code(native, &format!("native_worker_{shown}"));
        }
        let published = offer.is_some_and(|o| {
            o["run_id"] == run_id
                && o["offer_id"].as_str().is_some()
                && o["offer_id"].as_str() != bootstrap
        });
        if published {
            "work_not_accepted".into()
        } else {
            "work_offer_not_published".into()
        }
    }

    /// `_step(a)` for the local agent; an `Err` is the exception `_set_step_error` records.
    fn step(&mut self, host: &mut dyn AgentHost) -> Result<(), Failure> {
        self.collect_slice();
        let a = self.announcement.clone();
        if host.member().is_none() {
            self.set("idle", Some("membership_required"));
            return Ok(());
        }
        let phase = a
            .as_ref()
            .and_then(|a| text(a, "phase"))
            .unwrap_or_else(|| "idle".into());
        if phase == "idle" || phase == "stopped" {
            self.slice_stop.store(true, Ordering::SeqCst);
            let cleaned = self.cleanup_if_requested(host)?;
            // No owner run (idle) or the run ended (stopped): never keep a worker.
            if !cleaned && host.native_control()?["state"] == "running" {
                host.stop_native()?;
            }
            self.set(
                if phase == "stopped" {
                    "stopped"
                } else {
                    "idle"
                },
                None,
            );
            return Ok(());
        }
        let a = a.unwrap_or_else(|| json!({}));
        let (Some(run_id), Some(deployment_id)) = (text(&a, "run_id"), text(&a, "deployment_id"))
        else {
            return Err(Failure::new("KeyError", "demo_step_failed"));
        };
        let wanted = (run_id.clone(), deployment_id.clone());
        // 0. Whole-snapshot mode (a joined Mac): it needs the owner's whole pinned snapshot before
        //    it can describe its installation.
        if self.remote
            && !a
                .get("slice_mode")
                .is_some_and(owner_run::steps::exchange::truthy)
            && self.descriptor_for.as_ref() != Some(&wanted)
            && let WholeModel::Waiting {
                state,
                detail,
                progress,
            } = host.whole_model(&a)?
        {
            self.set_progress(&state, detail.as_deref(), progress);
            return Ok(());
        }
        // 1. Descriptor (once per run).
        if self.descriptor_for.as_ref() != Some(&wanted) {
            self.slice_stop.store(true, Ordering::SeqCst);
            if self
                .slice_job
                .as_ref()
                .is_some_and(|job| !job.handle.is_finished())
            {
                self.set("cleaning_up", None);
                return Ok(());
            }
            if !self.consent(host) {
                return Ok(());
            }
            self.descriptor = Some(self.make_descriptor(host, &a)?);
            self.descriptor_for = Some(wanted);
            self.bootstrap_done.clear();
            self.work_done.clear();
            self.slice_result = SliceResult::None;
            self.slice_stop = Arc::new(AtomicBool::new(false));
            self.set("reported", None);
            self.report(host, true);
        } else if phase == "collect" && self.aging(host) {
            if !self.consent(host) {
                return Ok(());
            }
            self.descriptor = Some(self.make_descriptor(host, &a)?);
            self.set("reported", None);
            self.report(host, true);
        }
        // 2. Owner-requested cleanup (bootstrap after observation, or teardown).
        if self.cleanup_if_requested(host)? {
            return Ok(());
        }
        let offer = match host.pending_offer() {
            Ok(offer) => offer,
            // The owner can request cleanup between the separate cleanup and stage reads.
            Err(error) if error.code == "offer_cleanup_requested" => {
                if self.cleanup_if_requested(host)? {
                    return Ok(());
                }
                return Err(error);
            }
            Err(error) => return Err(error),
        };
        // 3. Bootstrap (hello-only, model load denied).
        let early = matches!(phase.as_str(), "collect" | "observe" | "compile");
        let bootstrap = text(&a, "bootstrap_offer_id");
        // Past the owner's early phases (and always on a joined Mac) the worker starts only for the
        // announced offer this Mac holds itself, once (`demo_target.py:495-497`): a work offer held
        // after a descriptor reset never re-runs the bootstrap.
        let held = offer
            .as_ref()
            .is_some_and(|o| bootstrap.is_some() && o["offer_id"].as_str() == bootstrap.as_deref());
        if let Some(id) = &bootstrap
            && ((early && !self.remote) || (held && !self.bootstrap_done.contains(id)))
        {
            if !self.consent(host) {
                return Ok(());
            }
            self.set("preparing", None);
            host.bootstrap(&run_id, &deployment_id, id)?;
            self.bootstrap_done.insert(id.clone());
            self.set("bootstrap_running", None);
            return Ok(());
        }
        let mut native = host.native_control()?;
        if early {
            let running = native["state"] == "running";
            self.set(
                if running {
                    "bootstrap_running"
                } else {
                    "reported"
                },
                None,
            );
            return Ok(());
        }
        // 4. This Mac's share must fit its caps: refuse by name before any fetch.
        if let Some(refusal) = self.share_refusal(host, &a)? {
            self.set("error", Some(&refusal));
            return Ok(());
        }
        // 5. Slice (slice mode): fetch only this member's verified slice.
        let slice_mode = a
            .get("slice_mode")
            .is_some_and(owner_run::steps::exchange::truthy);
        let node_plan = a
            .get("node_slice_plan")
            .filter(|v| owner_run::steps::exchange::truthy(v))
            .cloned();
        if slice_mode
            && let Some(plan) = &node_plan
            && self.slice_result != SliceResult::Done(run_id.clone())
        {
            self.drive_slice(host, &a, plan, &run_id)?;
            if self.slice_result != SliceResult::Done(run_id.clone()) {
                return Ok(());
            }
        }
        // 6. Work: the owner's own contributor previews/accepts its offer under the owner's Start.
        let limits = a
            .get("work_limits")
            .filter(|v| owner_run::steps::exchange::truthy(v));
        if let (false, Some(offer), Some(limits)) = (self.remote, &offer, limits) {
            let id = offer["offer_id"]
                .as_str()
                .ok_or_else(|| Failure::new("KeyError", "demo_step_failed"))?
                .to_owned();
            if offer["run_id"] == run_id.as_str()
                && !self.work_done.contains(&id)
                && bootstrap.as_deref() != Some(id.as_str())
            {
                if !self.consent(host) {
                    return Ok(());
                }
                host.accept_work(limits)?;
                self.work_done.insert(id);
                native = host.native_control()?;
            }
        }
        let state = &native["state"];
        if phase == "serving" && state == "blocked" {
            // The node's control loop is over: report its own closed code instead of waiting.
            let code = native_code(&native, "native_worker_blocked");
            self.set("error", Some(&code));
            return Ok(());
        }
        let admitted = ["work_admitted", "model_load_authorized"].iter().any(|k| {
            native
                .get(*k)
                .is_some_and(owner_run::steps::exchange::truthy)
        });
        if state == "running" && admitted {
            self.set(
                if phase == "serving" {
                    "serving"
                } else {
                    "work_running"
                },
                None,
            );
        } else if slice_mode && node_plan.is_some() {
            let progress = self.progress.clone();
            self.set_progress("slice_ready", None, progress);
        } else {
            let detail = Self::work_waiting(offer.as_ref(), &run_id, bootstrap.as_deref(), &native);
            self.set("reported", Some(&detail));
        }
        Ok(())
    }

    /// `holds_offer()`: tell a standing acceptor to leave the pending offer to this agent.
    pub fn holds_offer(&mut self) -> bool {
        self.collect_slice();
        let Some(a) = self.announcement.as_ref() else {
            return false;
        };
        let phase = text(a, "phase");
        match phase.as_deref() {
            None | Some("idle" | "stopped") => false,
            Some("work" | "serving") => {
                a.get("slice_mode")
                    .is_some_and(owner_run::steps::exchange::truthy)
                    && !matches!(&self.slice_result, SliceResult::Done(r) if a["run_id"] == r.as_str())
            }
            Some(_) => true,
        }
    }

    /// `member_limits(offer)`: this Mac's share-derived worker limits for `offer`'s run.
    pub fn member_limits(&self, offer: &Value) -> Option<Value> {
        let a = self.announcement.as_ref()?;
        (offer.is_object()
            && offer.get("run_id") == a.get("run_id")
            && a["work_limits"].is_object())
        .then(|| a["work_limits"].clone())
    }

    /// `close()`: ask a running slice fetch to stop at its next segment.
    pub fn close(&mut self) {
        self.slice_stop.store(true, Ordering::SeqCst);
    }

    /// `tick()`: the status after one step.
    pub fn tick(&mut self, host: &mut dyn AgentHost) -> Value {
        match host.announcement() {
            Err(ExchangeUnavailable) => {
                self.set("error", Some("demo_announcement_unavailable"));
                return self.status();
            }
            Ok(announcement) => self.announcement = announcement,
        }
        if let Err(failure) = self.step(host) {
            self.set("error", Some(&closed(&failure.code, "demo_step_failed")));
        }
        self.report(host, false);
        self.status()
    }
}

impl Drop for LocalAgent {
    fn drop(&mut self) {
        self.close();
        if let Some(job) = self.slice_job.take()
            && job.handle.join().is_err()
        {
            eprintln!("slice fetch thread panicked");
        }
    }
}
