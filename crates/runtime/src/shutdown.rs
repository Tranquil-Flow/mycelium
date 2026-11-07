// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId, Entries, MonoInstant, Refusal};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StopReason {
    Stop,
    Rollover,
    Failure(&'static str),
}
impl StopReason {
    pub fn code(self) -> &'static str {
        match self {
            Self::Stop => "chat_cancelled",
            Self::Rollover => "chat_switching_runs",
            Self::Failure(code) => code,
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    CancelChats,
    Listener,
    GatewayDrain,
    Registry,
    TargetProof,
    Announcement,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub phase: Phase,
    pub code: &'static str,
}
/// Adapter success means that phase completed, never merely spawned a task.
/// Gateway drain is not request cleanup authority; registry must verify terminals.
pub trait ShutdownEffects {
    fn now(&self) -> MonoInstant;
    fn cancel_chats(&mut self, reason: StopReason) -> Result<(), &'static str>;
    fn close_listener(&mut self) -> Result<(), &'static str>;
    fn drain_gateway(&mut self, deadline: MonoInstant) -> Result<(), &'static str>;
    fn close_registry(&mut self, deadline: MonoInstant) -> Result<(), &'static str>;
    fn prove_targets(&mut self, deadline: MonoInstant) -> Result<(), &'static str>;
    fn announce_stopped(&mut self) -> Result<(), &'static str>;
}
pub struct ShutdownReport {
    pub failures: Vec<Failure>,
}
impl ShutdownReport {
    pub fn proven(&self) -> bool {
        self.failures.is_empty()
    }
}
fn bound(now: MonoInstant, outer: MonoInstant, id: BudgetId) -> Result<MonoInstant, Refusal> {
    now.same_domain(outer)?;
    let local = now.checked_add(id.span(Entries::ZERO)?)?;
    Ok(if local.ticks() < outer.ticks() {
        local
    } else {
        outer
    })
}
fn bounded_phase<E: ShutdownEffects>(
    e: &mut E,
    outer: MonoInstant,
    id: Option<BudgetId>,
    call: impl FnOnce(&mut E, MonoInstant) -> Result<(), &'static str>,
) -> Result<(), &'static str> {
    e.now()
        .same_domain(outer)
        .map_err(|_| "shutdown_clock_domain")?;
    let deadline = match id {
        Some(id) => bound(e.now(), outer, id).map_err(|_| "shutdown_budget_invalid")?,
        None => outer,
    };
    // Cleanup is attempted even at expiry, with no remaining wait allowance.
    // Adapters must honor this deadline; expiry cannot be counted as success.
    call(e, deadline)?;
    e.now()
        .same_domain(deadline)
        .map_err(|_| "shutdown_clock_domain")?;
    if e.now().ticks() >= deadline.ticks() {
        return Err("shutdown_deadline_expired");
    }
    Ok(())
}
/// Caller already stopped admission under its own start/stop fence.
pub fn shutdown(
    e: &mut impl ShutdownEffects,
    reason: StopReason,
    outer: MonoInstant,
) -> Result<ShutdownReport, Refusal> {
    e.now().same_domain(outer)?;
    let mut failures = Vec::new();
    let mut record = |phase, result: Result<(), &'static str>| {
        if let Err(code) = result {
            failures.push(Failure { phase, code });
        }
    };
    record(Phase::CancelChats, e.cancel_chats(reason));
    record(Phase::Listener, e.close_listener());
    record(
        Phase::GatewayDrain,
        bounded_phase(e, outer, Some(BudgetId::CancelAndCleanup), |e, d| {
            e.drain_gateway(d)
        }),
    );
    // L15: registry cleanup runs even after gateway drain failure.
    record(
        Phase::Registry,
        bounded_phase(e, outer, Some(BudgetId::RouterClose), |e, d| {
            e.close_registry(d)
        }),
    );
    record(
        Phase::TargetProof,
        bounded_phase(e, outer, None, |e, d| e.prove_targets(d)),
    );
    record(Phase::Announcement, e.announce_stopped());
    Ok(ShutdownReport { failures })
}
