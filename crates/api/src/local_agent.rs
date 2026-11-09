// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's own Mac as a member of its run: `DemoTargetAgent(local=True)` (`demo_target.py:172-557 @ f4e366e9`),
//! the part the chain reaches before observation. Each tick reads the owner's announcement, answers it with this
//! Mac's state and, once per run, a signed installation descriptor (re-signed before half its life is gone while the
//! owner still collects), and hands every report to the owner's inbox.
//!
//! Announced bootstrap offers invoke the host's authenticated staging/worker collaborator;
//! its absence is an explicit refusal, never a fabricated `bootstrap_running` report.
//! NOT ported here: owner-requested target cleanup, share refusal, slice fetch and work acceptance.
//! With no offer pending, `collect`/`observe`/`compile` remains `reported` as in the source.
use crate::installation_descriptor::Failure;
use crate::slice_fetch::SliceTransport;
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value, json};
use std::path::PathBuf;

pub const MEMBER_REPORT: &str = "mycelium.a13_demo_member_report.v1";

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

type ReportKey = (Value, String, Option<String>, bool);

#[derive(Default)]
pub struct LocalAgent {
    announcement: Option<Value>,
    state: String,
    detail: Option<String>,
    descriptor_for: Option<(String, String)>,
    descriptor: Option<Value>,
    last_report: Option<ReportKey>,
    last_report_at: f64,
}

fn text(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(Value::as_str).map(str::to_owned)
}

impl LocalAgent {
    pub fn new() -> Self {
        Self {
            state: "idle".into(),
            ..Self::default()
        }
    }

    fn set(&mut self, state: &str, detail: Option<&str>) {
        self.state = state.to_owned();
        self.detail = detail.map(str::to_owned);
    }

    /// `status()`.
    pub fn status(&self) -> Value {
        let a = self.announcement.clone().unwrap_or_else(|| json!({}));
        json!({
            "state": self.state, "detail": self.detail, "progress": Value::Null, "error_http": Value::Null,
            "phase": a.get("phase").cloned().unwrap_or(Value::Null),
            "run_id": a.get("run_id").cloned().unwrap_or(Value::Null),
            "model_id": a.get("model").and_then(|m| m.get("model_id")).cloned().unwrap_or(Value::Null),
            "local": true,
            "sources": Value::Null,
        })
    }

    /// `_consent(action)` under `OwnerRunConsent`.
    fn consent(&mut self, host: &mut dyn AgentHost) -> bool {
        let reason = if host.run_active() {
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
        let key: ReportKey = (
            run_id.clone(),
            self.state.clone(),
            self.detail.clone(),
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
        report.insert("progress".into(), Value::Null);
        report.insert("descriptor".into(), descriptor.unwrap_or(Value::Null));
        report.insert("issued_at".into(), json!(issued_at));
        if host.sink(&Value::Object(report)).is_ok() {
            self.last_report = Some(key);
            self.last_report_at = now;
        }
    }

    /// `_step(a)` for the local agent; an `Err` is the exception `_set_step_error` records.
    fn step(&mut self, host: &mut dyn AgentHost) -> Result<(), Failure> {
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
        let wanted = (run_id, deployment_id);
        if self.descriptor_for.as_ref() != Some(&wanted) {
            if !self.consent(host) {
                return Ok(());
            }
            self.descriptor = Some(self.make_descriptor(host, &a)?);
            self.descriptor_for = Some(wanted);
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
        if let Some(offer) = text(&a, "bootstrap_offer_id") {
            if !self.consent(host) {
                return Ok(());
            }
            self.set("preparing", None);
            host.bootstrap(
                &text(&a, "run_id").unwrap_or_default(),
                &text(&a, "deployment_id").unwrap_or_default(),
                &offer,
            )?;
            self.set("bootstrap_running", None);
            return Ok(());
        }
        self.set("reported", None);
        Ok(())
    }

    /// Seam only (RED baseline): no slice fetch exists to wait for.
    pub fn join_slice(&mut self) {}

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
