// SPDX-License-Identifier: AGPL-3.0-only
//! Observation/recovery tooling only; no member mutation capability exists.
use crate::{Fault, FaultKind, Result, harness_budgets::KeeperLimits};
use serde::{Deserialize, Serialize};
use serde_json::Value;
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct State {
    pub recoveries: Vec<u64>,
    pub switching_since: Option<u64>,
    pub starting_since: Option<u64>,
    pub last_ms: Option<u64>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Decision {
    Healthy,
    PublicUnhealthy,
    Recover,
    Switching,
    Starting,
    BudgetExhausted,
    RolloverTimeout,
    StartupTimeout,
    OwnerUnavailable,
}
pub struct MemberClock {
    pub peer: String,
    pub age_ms: Option<u64>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Alert {
    pub code: &'static str,
    pub peer: Option<String>,
}
/// Implementations must own a single-writer check lease. save is a durable CAS;
/// load must read committed storage, not an optimistic in-memory cache. Status
/// uses the candidate-bound renewing adapter. No arbitrary process API is exposed.
pub trait Io {
    fn begin_check(&mut self) -> Result<()> {
        Ok(())
    }
    fn owner_status(&mut self) -> Result<Value>;
    fn member_clocks(&mut self) -> Result<Vec<MemberClock>>;
    fn public_healthy(&mut self) -> Result<bool>;
    fn load(&mut self) -> Result<State>;
    fn save(&mut self, prior: &State, next: &State) -> Result<()>;
    fn alert(&mut self, alert: Alert) -> Result<()>;
    fn start(&mut self) -> Result<()>;
}
#[derive(Debug)]
pub struct Check {
    pub decision: Decision,
    pub start_submitted: bool,
    pub serving_observed: bool,
    pub alerts: Vec<Alert>,
}
pub fn switching(status: &Value) -> bool {
    matches!(
        status["owner_run"]["rollover"]["state"].as_str(),
        Some("switching" | "backoff")
    ) || status["chat"]["switching_runs"] == true
        || crate::http::switching(status)
}
pub fn recoverable(status: &Value) -> bool {
    let run = &status["owner_run"];
    // Deliberately require a present explicit proof field. Missing data must not
    // turn a partial or failed status response into permission to Start.
    let clean = run.get("cleanup_unproven") == Some(&Value::Null);
    clean
        && (run["state"] == "stopped"
            || (run["state"] == "failed"
                && run["error_code"] == "rollover_members_changed"
                && run["rollover"]["state"] == "failed"))
}
impl State {
    pub fn decide(
        &mut self,
        now: u64,
        status: &Value,
        healthy: bool,
        limits: KeeperLimits,
    ) -> Result<Decision> {
        if limits.attempts == 0
            || [
                limits.recovery_window,
                limits.rollover_grace,
                limits.startup_grace,
                limits.member_silence,
            ]
            .contains(&budgets::Span::ZERO)
        {
            return Err(Fault::new("harness_budget_invalid", FaultKind::Input));
        }
        if self.last_ms.is_some_and(|t| now < t) {
            return Err(Fault::new("keeper_clock_rollback", FaultKind::Observation));
        }
        if self.recoveries.len() as u64 > limits.attempts
            || self.recoveries.iter().any(|t| *t > now)
            || self.recoveries.windows(2).any(|w| w[0] > w[1])
            || [self.switching_since, self.starting_since]
                .into_iter()
                .flatten()
                .any(|t| t > now)
        {
            return Err(Fault::new("keeper_state_invalid", FaultKind::Observation));
        }
        self.last_ms = Some(now);
        self.recoveries
            .retain(|t| now - t < limits.recovery_window.millis());
        if switching(status) {
            let since = *self.switching_since.get_or_insert(now);
            return Ok(if now - since < limits.rollover_grace.millis() {
                Decision::Switching
            } else {
                Decision::RolloverTimeout
            });
        }
        self.switching_since = None;
        if status["owner_run"]["state"] == "serving" {
            self.starting_since = None;
            return Ok(if healthy {
                Decision::Healthy
            } else {
                Decision::PublicUnhealthy
            });
        }
        if recoverable(status) {
            self.starting_since = None;
            return Ok(if (self.recoveries.len() as u64) < limits.attempts {
                Decision::Recover
            } else {
                Decision::BudgetExhausted
            });
        }
        if matches!(
            status["owner_run"]["state"].as_str(),
            Some("running" | "starting")
        ) {
            let since = *self.starting_since.get_or_insert(now);
            return Ok(if now - since < limits.startup_grace.millis() {
                Decision::Starting
            } else {
                Decision::StartupTimeout
            });
        }
        self.starting_since = None;
        Ok(Decision::OwnerUnavailable)
    }
}
fn notify(
    io: &mut impl Io,
    alerts: &mut Vec<Alert>,
    code: &'static str,
    peer: Option<String>,
) -> Result<()> {
    let a = Alert { code, peer };
    io.alert(a.clone())?;
    alerts.push(a);
    Ok(())
}
fn same_swarm(status: &Value, swarm: &str) -> bool {
    !swarm.is_empty() && status["swarm"]["swarm_id"] == swarm
}
pub fn check(io: &mut impl Io, now: u64, swarm: &str, limits: KeeperLimits) -> Result<Check> {
    io.begin_check()?;
    let mut alerts = vec![];
    // Observe both independently before handling either failure; owner blindness
    // must not hide a wedged member. A member observation never grants restart.
    let owner = io.owner_status();
    let members = io.member_clocks();
    match members {
        Ok(rows) => {
            for row in rows {
                match row.age_ms {
                    Some(age) if age > limits.member_silence.millis() => {
                        notify(io, &mut alerts, "member_silent", Some(row.peer))?
                    }
                    None => notify(io, &mut alerts, "member_clock_unobserved", Some(row.peer))?,
                    Some(_) => (),
                }
            }
        }
        Err(f) => notify(io, &mut alerts, f.code, None)?,
    }
    let status = match owner {
        Ok(s) if same_swarm(&s, swarm) => s,
        Ok(_) => {
            notify(io, &mut alerts, "owner_swarm_changed", None)?;
            Value::Null
        }
        Err(f) => {
            notify(io, &mut alerts, f.code, None)?;
            Value::Null
        }
    };
    let healthy = match io.public_healthy() {
        Ok(v) => v,
        Err(f) => {
            notify(io, &mut alerts, f.code, None)?;
            false
        }
    };
    let prior = io.load()?;
    let mut state = prior.clone();
    let mut decision = state.decide(now, &status, healthy, limits)?;
    // Legacy pure policy classifies stopped as recoverable. The operational
    // keeper is narrower: only the observed failed rollover authorizes Start;
    // an intentional operator stop must never be automatically undone.
    if decision == Decision::Recover && status["owner_run"]["state"] != "failed" {
        decision = Decision::OwnerUnavailable;
        notify(
            io,
            &mut alerts,
            "owner_start_requires_failed_rollover",
            None,
        )?;
    }
    if decision == Decision::Recover {
        let current = io.owner_status()?;
        if !same_swarm(&current, swarm)
            || switching(&current)
            || !recoverable(&current)
            || current["owner_run"]["state"] != "failed"
        {
            decision = Decision::OwnerUnavailable;
            notify(io, &mut alerts, "recovery_superseded", None)?;
        } else {
            state.recoveries.push(now);
        }
    }
    io.save(&prior, &state)?;
    if io.load()? != state {
        return Err(Fault::new(
            "keeper_state_readback_mismatch",
            FaultKind::Observation,
        ));
    }
    let mut result = Check {
        decision,
        start_submitted: false,
        serving_observed: false,
        alerts,
    };
    if decision == Decision::Recover {
        // Reservation remains spent on rejection, timeout or ambiguous response.
        // Never replay a POST inside a check, and never infer serving from 200.
        io.start()?;
        result.start_submitted = true;
        let after = io.owner_status()?;
        if !same_swarm(&after, swarm) {
            return Err(Fault::new("owner_swarm_changed", FaultKind::Observation));
        }
        result.serving_observed = after["owner_run"]["state"] == "serving";
    } else if !matches!(
        decision,
        Decision::Healthy | Decision::Switching | Decision::Starting
    ) {
        let code = match decision {
            Decision::BudgetExhausted => "keeper_budget_exhausted",
            Decision::RolloverTimeout => "keeper_rollover_timeout",
            Decision::StartupTimeout => "keeper_startup_timeout",
            Decision::PublicUnhealthy => "public_unhealthy",
            _ => "owner_unavailable",
        };
        notify(io, &mut result.alerts, code, None)?;
    }
    Ok(result)
}

/// Compose the native status/session adapter with existing keeper persistence,
/// public observation and Start capabilities. No browser credential is fabricated.
pub struct NativeKeeper<I, C, K> {
    pub owner: crate::native_session::Adapter<I, C>,
    pub operations: K,
    pub status_budget: budgets::Span,
    end: Option<u64>,
    renewal_available: bool,
}
impl<I: crate::native_session::Io, C: crate::effects::Clock, K: Io> NativeKeeper<I, C, K> {
    pub fn new(
        owner: crate::native_session::Adapter<I, C>,
        operations: K,
        status_budget: budgets::Span,
    ) -> Self {
        Self {
            owner,
            operations,
            status_budget,
            end: None,
            renewal_available: false,
        }
    }
}
impl<I: crate::native_session::Io, C: crate::effects::Clock, K: Io> Io for NativeKeeper<I, C, K> {
    fn begin_check(&mut self) -> Result<()> {
        if self.status_budget == budgets::Span::ZERO {
            return Err(Fault::new("native_check_budget_invalid", FaultKind::Input));
        }
        self.end = Some(
            self.owner
                .clock
                .now()
                .checked_add(self.status_budget.millis())
                .ok_or(Fault::new("native_check_budget_invalid", FaultKind::Input))?,
        );
        self.renewal_available = true;
        self.operations.begin_check()
    }
    fn owner_status(&mut self) -> Result<Value> {
        let left = self
            .end
            .and_then(|end| end.checked_sub(self.owner.clock.now()))
            .filter(|left| *left > 0)
            .ok_or(Fault::new("owner_status_timeout", FaultKind::Observation))?;
        self.owner
            .check_with_allowance(
                budgets::Span::from_millis(left),
                &mut self.renewal_available,
            )
            .map(|s| s.value)
    }
    fn member_clocks(&mut self) -> Result<Vec<MemberClock>> {
        self.operations.member_clocks()
    }
    fn public_healthy(&mut self) -> Result<bool> {
        self.operations.public_healthy()
    }
    fn load(&mut self) -> Result<State> {
        self.operations.load()
    }
    fn save(&mut self, prior: &State, next: &State) -> Result<()> {
        self.operations.save(prior, next)
    }
    fn alert(&mut self, a: Alert) -> Result<()> {
        self.operations.alert(a)
    }
    fn start(&mut self) -> Result<()> {
        self.operations.start()
    }
}
