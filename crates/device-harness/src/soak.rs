// SPDX-License-Identifier: AGPL-3.0-only
//! Deterministic observation reducer and pure verdict. Milliseconds are trace
//! units; policy durations are supplied by the named-budget caller (TODO L1).
use crate::{Fault, FaultKind, Result, http::Chat};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Run {
    pub run_id: Option<String>,
    pub state: String,
    pub route_ready: bool,
    pub participants: usize,
}
impl Run {
    pub fn from_status(v: &Value) -> Self {
        let r = &v["owner_run"];
        Self {
            run_id: r["run_id"].as_str().map(str::to_owned),
            state: r["state"].as_str().unwrap_or("").into(),
            route_ready: r["route_ready"] == true,
            participants: r["participants"].as_array().map_or(0, Vec::len),
        }
    }
    pub fn serving(&self) -> bool {
        self.state == "serving" && self.route_ready && self.participants == 2
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Rollover {
    pub from_run_id: String,
    pub to_run_id: String,
    pub start_ms: u64,
    pub last_serving_ms: Option<u64>,
    pub detected_ms: u64,
    pub end_ms: u64,
    pub gap_ms: u64,
    pub gap_upper_ms: Option<u64>,
}
#[derive(Clone, Debug, Default)]
pub struct RolloverTracker {
    pub rollovers: Vec<Rollover>,
    pub pending: Option<Rollover>,
    run_id: Option<String>,
    last_serving: Option<u64>,
    down_at: Option<u64>,
    last_at: Option<u64>,
    restarting: bool,
}
impl RolloverTracker {
    pub fn observe(&mut self, at: u64, run: &Run) -> Result<()> {
        if self.last_at.is_some_and(|t| at < t) {
            return Err(Fault::new("clock_went_backwards", FaultKind::Observation));
        }
        self.last_at = Some(at);
        if self.restarting {
            return Ok(());
        }
        let ready = run.serving();
        if !ready && self.down_at.is_none() {
            self.down_at = Some(at);
        }
        if let (Some(old), Some(new)) = (&self.run_id, &run.run_id)
            && !new.is_empty()
            && old != new
        {
            if self.pending.is_some() {
                return Err(Fault::new(
                    "rollover_replaced_before_recovery",
                    FaultKind::Product,
                ));
            }
            self.pending = Some(Rollover {
                from_run_id: old.clone(),
                to_run_id: new.clone(),
                start_ms: self.down_at.unwrap_or(at),
                last_serving_ms: self.last_serving,
                detected_ms: at,
                end_ms: at,
                gap_ms: 0,
                gap_upper_ms: None,
            });
        }
        if run.run_id.as_ref().is_some_and(|r| !r.is_empty()) {
            self.run_id = run.run_id.clone();
        }
        if ready {
            if let Some(mut p) = self.pending.take() {
                p.end_ms = at;
                p.gap_ms = at - p.start_ms;
                p.gap_upper_ms = p.last_serving_ms.map(|last| at - last);
                self.rollovers.push(p);
            }
            self.last_serving = Some(at);
            self.down_at = None;
        }
        Ok(())
    }
    pub fn begin_restart(&mut self) {
        self.restarting = true;
        self.pending = None;
        self.down_at = None;
    }
    pub fn end_restart(&mut self, at: u64, run: &Run) {
        self.restarting = false;
        self.run_id = run.run_id.clone();
        self.last_serving = run.serving().then_some(at);
        self.down_at = None;
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RestartExpectation {
    AutomaticRecovery,
    IdleUntilExplicitStart,
    DiagnosticOnly,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Restart {
    pub expectation: RestartExpectation,
    pub attempted: bool,
    pub recovered: bool,
    pub idle_observed: bool,
    pub explicit_start: bool,
    pub member_mutations: usize,
    pub endpoint_changed: Option<bool>,
    pub approval: Option<String>,
}
impl Restart {
    pub fn satisfied(&self) -> bool {
        self.attempted
            && self.recovered
            && self.member_mutations == 0
            && match self.expectation {
                RestartExpectation::AutomaticRecovery => !self.explicit_start,
                RestartExpectation::IdleUntilExplicitStart => {
                    self.idle_observed
                        && self.explicit_start
                        && self.approval.as_ref().is_some_and(|s| !s.trim().is_empty())
                }
                RestartExpectation::DiagnosticOnly => false,
            }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub requested_ms: u64,
    pub minimum_ms: u64,
    pub chat_every_ms: u64,
    pub sample_every_ms: u64,
    pub mailbox_every_ms: u64,
    pub visitor_every_ms: u64,
    pub restart_at_ms: Option<u64>,
    pub minimum_rollovers: usize,
    pub mailbox_max_bytes: Option<u64>,
    pub restart_expectation: RestartExpectation,
    pub required_steps: BTreeSet<String>,
}
impl Policy {
    pub fn validate(&self) -> Result<()> {
        if [
            self.requested_ms,
            self.minimum_ms,
            self.chat_every_ms,
            self.sample_every_ms,
            self.mailbox_every_ms,
            self.visitor_every_ms,
        ]
        .contains(&0)
            || self.minimum_rollovers == 0
            || self
                .restart_at_ms
                .is_some_and(|t| t == 0 || t >= self.requested_ms)
        {
            return Err(Fault::new("invalid_schedule", FaultKind::Input));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MailboxSample {
    pub at_ms: u64,
    pub owner: Option<u64>,
    pub member: Option<u64>,
    pub owner_storage: u64,
    pub member_storage: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HostMetricsSample {
    pub at_ms: u64,
    pub owner: crate::observe::Metrics,
    pub member: crate::observe::Metrics,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RestartWindow {
    pub begin_ms: Option<u64>,
    pub end_ms: Option<u64>,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Evidence {
    #[serde(default)]
    pub host_metrics: Vec<HostMetricsSample>,
    pub duration_ms: u64,
    pub clock_started_at_distributed_output: bool,
    pub chats: Vec<Chat>,
    pub samples: usize,
    pub metrics_samples: usize,
    pub rollovers: Vec<Rollover>,
    pub pending_rollover: bool,
    pub restart: Option<Restart>,
    #[serde(default)]
    pub restart_window: Option<RestartWindow>,
    pub mailbox_samples: Vec<MailboxSample>,
    pub published: bool,
    pub visitor_completed: Vec<bool>,
    pub errors: Vec<String>,
    pub incomplete_reason: Option<String>,
    pub product_failure: Option<String>,
    pub final_serving: bool,
    pub completed_steps: BTreeSet<String>,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Verdict {
    pub outcome: String,
    pub scenario_passed: bool,
    pub product_failed: Option<bool>,
    pub reasons: Vec<String>,
    pub restart_expectation: RestartExpectation,
    pub f3_automatic_recovery: bool,
    pub owner_restart_unavailable_chats: usize,
    pub release_qualified: bool,
}
pub fn evaluate(p: &Policy, e: &Evidence) -> Verdict {
    let mut v = Verdict {
        outcome: "failed".into(),
        scenario_passed: false,
        product_failed: None,
        reasons: vec![],
        restart_expectation: p.restart_expectation,
        f3_automatic_recovery: e.restart.as_ref().is_some_and(|r| {
            r.expectation == RestartExpectation::AutomaticRecovery && r.satisfied()
        }),
        owner_restart_unavailable_chats: 0,
        release_qualified: false,
    };
    if let Some(f) = &e.product_failure {
        v.product_failed = Some(true);
        v.reasons.push(f.clone());
        return v;
    }
    if let Some(reason) = &e.incomplete_reason {
        v.outcome = "incomplete".into();
        v.product_failed = Some(false);
        v.reasons.push(reason.clone());
        return v;
    }
    if let Err(err) = p.validate() {
        v.reasons.push(err.code.into());
        return v;
    }
    let mut require = |ok: bool, reason: &str| {
        if !ok {
            v.reasons.push(reason.into());
        }
    };
    require(
        e.clock_started_at_distributed_output,
        "soak_clock_not_distributed_output",
    );
    require(
        e.duration_ms >= p.minimum_ms.max(p.requested_ms),
        "duration_incomplete",
    );
    require(
        e.rollovers.len() >= p.minimum_rollovers,
        "no_automatic_rollover",
    );
    require(!e.pending_rollover, "rollover_not_recovered");
    let mut pairs = BTreeSet::new();
    require(
        e.rollovers.iter().all(|r| {
            !r.from_run_id.is_empty()
                && !r.to_run_id.is_empty()
                && r.from_run_id != r.to_run_id
                && r.start_ms <= r.end_ms
                && pairs.insert((&r.from_run_id, &r.to_run_id))
        }),
        "rollover_evidence_invalid",
    );
    require(!e.chats.is_empty(), "no_chats");
    require(
        e.chats.len() as u64 == p.requested_ms.div_ceil(p.chat_every_ms),
        "chat_schedule_incomplete",
    );
    let samples = p.requested_ms.div_ceil(p.sample_every_ms);
    require(e.samples as u64 >= samples, "status_schedule_incomplete");
    require(e.metrics_samples >= e.samples, "host_metrics_unobserved");
    require(
        e.host_metrics.len() == e.metrics_samples
            && e.host_metrics
                .iter()
                .all(|s| s.at_ms <= e.duration_ms && s.owner.valid() && s.member.valid())
            && e.host_metrics.windows(2).all(|w| w[0].at_ms < w[1].at_ms),
        "full_host_metrics_missing_or_invalid",
    );
    let restart_window = e.restart_window.as_ref().and_then(|w| {
        let (begin, end) = (w.begin_ms?, w.end_ms?);
        (begin < end
            && e.restart
                .as_ref()
                .is_some_and(|r| r.attempted && r.recovered))
        .then_some((begin, end))
    });
    require(
        e.chats.iter().all(|c| {
            if c.end_ms < c.start_ms {
                return false;
            }
            if c.ok()
                || (c.switching
                    && e.rollovers.iter().any(|r| {
                        c.start_ms <= r.end_ms
                            && c.end_ms >= r.last_serving_ms.unwrap_or(r.start_ms)
                    }))
            {
                return true;
            }
            let unavailable = restart_window.is_some_and(|(begin, end)| {
                begin <= c.start_ms
                    && c.end_ms <= end
                    && c.submit_http == Some(503)
                    && matches!(
                        c.submit_code.as_deref(),
                        Some("chat_not_ready" | "chat_warming_up" | "chat_switching_runs")
                    )
            });
            if unavailable {
                v.owner_restart_unavailable_chats += 1;
            }
            unavailable
        }),
        "chat_failed",
    );
    require(p.restart_at_ms.is_some(), "f3_disabled");
    require(
        e.restart
            .as_ref()
            .is_some_and(|r| r.expectation == p.restart_expectation && r.satisfied()),
        "restart_expectation_not_satisfied",
    );
    match p.mailbox_max_bytes {
        None | Some(0) => require(false, "mailbox_bound_not_specified"),
        Some(limit) => require(
            e.mailbox_samples.len() >= 2
                && e.mailbox_samples.iter().all(|s| {
                    s.owner.is_some_and(|n| n <= limit) && s.member.is_some_and(|n| n <= limit)
                }),
            "mailbox_bound_failed_or_unobserved",
        ),
    }
    require(
        !e.published || (!e.visitor_completed.is_empty() && e.visitor_completed.iter().all(|c| *c)),
        "external_visitor_unproven",
    );
    require(e.errors.is_empty(), "observation_or_schedule_errors");
    require(e.final_serving, "route_not_serving_at_soak_end");
    require(
        p.required_steps.is_subset(&e.completed_steps),
        "required_lifecycle_steps_unobserved",
    );
    if v.reasons.is_empty() {
        v.outcome = "passed".into();
        v.scenario_passed = true;
    }
    v
}
