// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{ClockDomain, MonoInstant, Span, WallExpiry};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug)]
pub struct Now {
    pub mono: MonoInstant,
    pub wall: WallExpiry,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Instant {
    pub domain: u64,
    pub ticks: u64,
}
impl From<MonoInstant> for Instant {
    fn from(m: MonoInstant) -> Self {
        Self {
            domain: m.domain().0,
            ticks: m.ticks(),
        }
    }
}
impl Instant {
    pub fn mono(self) -> MonoInstant {
        MonoInstant::new(ClockDomain(self.domain), self.ticks)
    }
}

pub use control_runtime::consent::{Binding, Caps, Consent, ConsentBounds, disclosure_digest};
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Member {
    pub binding: Binding,
    pub endpoint: String,
    pub key_digest: String,
    pub membership_generation: String,
    pub incarnation: String,
    pub consent: Option<Consent>,
    pub report_epoch: Option<u64>,
    pub report_run: Option<u64>,
    pub lease: Option<Lease>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Lease {
    pub signed_expiry: u64,
    pub deadline: Instant,
    pub renew_at: Instant,
    pub last: Instant,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ticket {
    pub epoch: u64,
    pub run: u64,
    pub node: String,
    pub incarnation: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum WorkerPhase {
    Starting,
    Running,
    Stopping,
    CleanupUnproven,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum AuthoritySource {
    OwnerLocal,
    StandingConsent,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Reservation {
    pub ticket: Ticket,
    pub caps: Caps,
    pub authority: AuthoritySource,
    pub phase: WorkerPhase,
    pub requested_at: Instant,
    pub origin: Option<Instant>,
    pub expires: Option<Instant>,
    pub stop_deadline: Option<Instant>,
    pub failure: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum RunPhase {
    Idle,
    Planned,
    Starting,
    Serving,
    Stopping,
    CleanupUnproven,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct State {
    pub owner: Binding,
    pub owner_policy: Caps,
    pub epoch: u64,
    pub run: u64,
    pub members: BTreeMap<String, Member>,
    pub phase: RunPhase,
    pub plan: Option<Plan>,
    pub workers: BTreeMap<String, Reservation>,
    pub rollover_at: Option<Instant>,
    /// Exact membership snapshot for one automatic continuation, not a retry queue.
    #[serde(default)]
    pub rollover_members: Option<BTreeMap<String, Member>>,
    pub last_clock: Option<Instant>,
    pub last_failure: Option<Diagnostic>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Diagnostic {
    pub code: String,
    pub phase: String,
    pub kind: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Plan {
    pub assignments: BTreeMap<String, Caps>,
    pub stop_ms: u64,
    pub start_ms: u64,
    pub safety_ms: u64,
}
#[derive(Clone, Debug)]
pub struct PlanningMember {
    pub node: String,
    pub caps: Caps,
    pub authority: AuthoritySource,
}
/// L7 development seam: return exact per-member shares; core independently validates
/// membership set, local-vs-consented authority, resources and restart reserves.
pub trait Planner {
    fn plan(&mut self, members: &[PlanningMember]) -> Result<Plan>;
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Effect {
    RefreshReport {
        node: String,
        epoch: u64,
    },
    Start {
        ticket: Ticket,
        caps: Caps,
    },
    Stop {
        ticket: Ticket,
        deadline: Instant,
    },
    Renew {
        node: String,
        epoch: u64,
        generation: String,
    },
}
#[derive(Debug)]
pub enum Error {
    Refused(&'static str),
    Budget(budgets::Refusal),
    Store(control_runtime::Error),
    Codec(serde_json::Error),
}
impl From<budgets::Refusal> for Error {
    fn from(e: budgets::Refusal) -> Self {
        Self::Budget(e)
    }
}
impl From<control_runtime::Error> for Error {
    fn from(e: control_runtime::Error) -> Self {
        Self::Store(e)
    }
}
impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Self::Codec(e)
    }
}
pub type Result<T> = std::result::Result<T, Error>;
pub fn span(ms: u64) -> Span {
    Span::from_millis(ms)
}
/// Immutable snapshot a planner runs on OUTSIDE the Core (see `Core::plan_request`). It cannot
/// be constructed by callers and is only accepted by the Core whose state it was taken from.
pub struct PlanningRequest {
    pub(crate) base: State,
    pub(crate) revision: u64,
    pub(crate) draft: State,
    pub(crate) members: Vec<PlanningMember>,
}
impl PlanningRequest {
    pub fn members(&self) -> &[PlanningMember] {
        &self.members
    }
}
