// SPDX-License-Identifier: AGPL-3.0-only
//! Historical lifecycle models at typed policy/I/O seams, not product ports.
//! The common scheduler owns every completion. See INCIDENTS.md for provenance.
use crate::{Scheduler, SimResult, model::*, span};
use budgets::BudgetId as B;

mod mailbox;

impl Incident {
    pub fn is_extended(self) -> bool {
        !matches!(self, Self::F4 | Self::F15 | Self::F16)
    }
    pub fn obligation_id(self) -> &'static str {
        match self {
            Self::F1 => "O:path:mycelium_a13_macos/token_owner.py",
            Self::F2 => "O:path:mycelium_a13_macos/managed_control.py",
            Self::F3 => "O:path:mycelium_a13_macos/demo_target.py",
            Self::F4 => "O:path:mycelium_a13_macos/worker.py",
            Self::F5 => "O:path:mycelium_a13_macos/published_status.py",
            Self::F13 => "O:path:mycelium_a13_macos/public_relay.py",
            Self::F14 => "O:path:mycelium_a13_macos/layer_split.py",
            Self::F15 | Self::F15b => "O:path:mycelium_a13_macos/demo_chain.py",
            Self::F16 => "O:path:mycelium_a13_macos/chain_compile.py",
        }
    }
    pub fn required_transitions(self) -> &'static [&'static str] {
        match self {
            Self::F1 => &[
                "policy_install",
                "membership_renewal",
                "late_request",
                "authority_expiry",
            ],
            Self::F2 => &[
                "traffic_batch",
                "prune_attempt",
                "mailbox_reopen",
                "replay_attempt",
                "fresh_after_reopen",
            ],
            Self::F3 => &[
                "initial_report",
                "owner_restart",
                "announcement",
                "recovery_attempt",
                "stale_generation",
            ],
            Self::F5 => &[
                "compile_enter",
                "publish_enter",
                "status_request",
                "status_deadline",
                "phase_release",
            ],
            Self::F13 => &[
                "relay_start",
                "renewal_attempt",
                "cross_window_request",
                "relay_close",
            ],
            Self::F14 => &["placement", "reservation_attempt", "staging_attempt"],
            Self::F15b => &[
                "wall_selection",
                "worker_start",
                "worker_ready",
                "later_request",
            ],
            _ => &["start_attempt", "stop", "close_completion"],
        }
    }
}
pub(crate) fn ms(id: B) -> SimResult<u64> {
    Ok(span(id)?.millis())
}
pub(crate) fn bytes(id: B) -> SimResult<u64> {
    id.bytes()
        .map(|b| b.get())
        .map_err(|e| format!("budget:{e:?}"))
}
pub(crate) fn count(id: B) -> SimResult<u64> {
    id.count()
        .map(|n| n.get())
        .map_err(|e| format!("budget:{e:?}"))
}
pub(crate) fn put(o: &mut Outcome, k: &str, v: u64) {
    o.metrics.insert(k.into(), v);
}
pub(crate) fn inc(o: &mut Outcome, k: &str) {
    *o.metrics.entry(k.into()).or_default() += 1;
}
pub(crate) fn chat(o: &mut Outcome, now: u64) {
    o.served += 1;
    o.successful_chats += 1;
    reached(o, "chat_success", now);
}

#[derive(Clone)]
pub(crate) enum Envelope<E> {
    Input(Command),
    Step(E),
}
pub(crate) fn at<E>(q: &mut Scheduler<Envelope<E>>, t: u64, task: u32, event: E) -> SimResult<()> {
    q.at(t, task, Envelope::Step(event))
}
/// Shared deterministic command admission. No generated Begin means no progress.
pub(crate) fn drive<E: Clone>(
    c: &Config,
    input: &[Input],
    initial: E,
    mut step: impl FnMut(E, &mut Scheduler<Envelope<E>>, &mut Outcome) -> SimResult<()>,
) -> SimResult<Outcome> {
    let mut q = Scheduler::default();
    for i in input {
        q.at(i.at_ms, i.task, Envelope::Input(i.command.clone()))?;
    }
    let mut o = Outcome::default();
    let mut begun = false;
    let mut events = 0;
    while let Some(item) = q.pop()? {
        events += 1;
        if events > 10_000 {
            return Err("event_limit".into());
        }
        let now = q.clock.mono_ms;
        match item.event {
            Envelope::Input(Command::Begin) => {
                if begun {
                    return Err("duplicate_begin".into());
                }
                begun = true;
                reached(&mut o, "start_attempt", now);
                if c.mode == Mode::AlwaysRefuse
                    || !c.owner_local_allowed
                    || !c.member_granted
                    || now >= c.authority_until_ms
                {
                    reached(&mut o, "authority_refused", now);
                    continue;
                }
                o.starts += 1;
                at(&mut q, now, 1, initial.clone())?;
            }
            Envelope::Input(Command::WallJump { delta_ms }) => {
                q.clock.jump_wall(delta_ms)?;
                reached(&mut o, "wall_jump", now);
            }
            Envelope::Input(Command::Observe) => reached(&mut o, "observe", now),
            Envelope::Step(e) => step(e, &mut q, &mut o)?,
        }
    }
    Ok(o)
}

pub(crate) fn run(c: &Config, input: &[Input]) -> SimResult<Outcome> {
    match c.incident {
        Incident::F1 => lease(c, input),
        Incident::F2 => mailbox::run(c, input),
        Incident::F3 => reports(c, input),
        Incident::F5 => status(c, input),
        Incident::F13 => relay(c, input),
        Incident::F14 => placement(c, input),
        Incident::F15b => owner_wall(c, input),
        _ => Err("wrong_incident_dispatch".into()),
    }
}

#[derive(Clone)]
enum LeaseEvent {
    Install,
    Renew,
    Request,
    Expiry,
}
fn lease(c: &Config, input: &[Input]) -> SimResult<Outcome> {
    let ttl = ms(B::MembershipTtl)?;
    let cadence = ms(B::LeaseRenewalInterval)?;
    let mut auth = 0;
    let mut pin = 0;
    let mut cap = 0;
    let mut origin = 0;
    let mut policy_expiry = 0;
    drive(c, input, LeaseEvent::Install, |e, q, o| {
        let now = q.clock.mono_ms;
        match e {
            LeaseEvent::Install => {
                origin = now;
                cap = add(now, ms(B::WorkerDefaultWall)?)?.min(c.authority_until_ms);
                auth = add(now, ttl)?.min(cap);
                pin = auth;
                policy_expiry = if c.mode == Mode::Historical { pin } else { cap };
                put(o, "installed_policy_expiry_ms", policy_expiry);
                put(o, "max_pin_expiry_ms", pin);
                put(o, "authority_cap_ms", cap);
                put(o, "initial_pin_ttl_ms", ttl);
                put(o, "renew_interval_ms", cadence);
                put(o, "policy_installs", 1);
                reached(o, "policy_install", now);
                at(q, now, 3, LeaseEvent::Request)?;
                at(q, add(now, cadence)?, 1, LeaseEvent::Renew)?;
                for windows in [1, 2] {
                    at(
                        q,
                        add(now, add(ttl * windows, cadence)?)?,
                        3,
                        LeaseEvent::Request,
                    )?;
                }
                at(q, cap, 0, LeaseEvent::Expiry)?;
            }
            LeaseEvent::Renew if now < cap => {
                auth = add(now, ttl)?.min(cap);
                reached(o, "membership_renewal", now);
                inc(o, "membership_renewals");
                if c.mode == Mode::Corrected {
                    pin = auth;
                    put(o, "max_pin_expiry_ms", pin);
                    inc(o, "pin_renewals");
                    reached(o, "pin_renewal", now);
                }
                put(o, "latest_membership_expiry_ms", auth);
                at(q, add(now, cadence)?, 1, LeaseEvent::Renew)?;
            }
            LeaseEvent::Request => {
                let late = now >= add(origin, ttl)?;
                if late {
                    reached(o, "late_request", now);
                }
                if now < pin && now < auth && now < cap && now < policy_expiry {
                    chat(o, now);
                    if late {
                        inc(o, "late_requests_served");
                    }
                } else if now < auth && now < cap {
                    inc(o, "valid_authority_losses");
                }
            }
            LeaseEvent::Expiry => {
                reached(o, "authority_expiry", now);
                if now < pin && now < auth && now < cap && now < policy_expiry {
                    chat(o, now);
                } else {
                    inc(o, "post_cap_refusals");
                }
            }
            _ => {}
        }
        Ok(())
    })
}

#[derive(Clone)]
enum ReportEvent {
    Initial,
    Restart,
    Announcement,
    Recover,
    Stale,
}
fn reports(c: &Config, input: &[Input]) -> SimResult<Outcome> {
    let mut owner_generation = 1;
    let member_identity = (7u64, 1u64, c.authority_until_ms);
    let saved_member = member_identity;
    let mut report_generation = Some(1);
    drive(c, input, ReportEvent::Initial, |e, q, o| {
        let now = q.clock.mono_ms;
        match e {
            ReportEvent::Initial => {
                reached(o, "initial_report", now);
                inc(o, "reports_delivered");
                chat(o, now);
                at(
                    q,
                    add(now, ms(B::MemberRestartGrace)?)?,
                    1,
                    ReportEvent::Restart,
                )?;
            }
            ReportEvent::Restart => {
                owner_generation += 1;
                report_generation = None;
                inc(o, "owner_restarts");
                reached(o, "owner_restart", now);
                put(o, "member_restarts", 0);
                put(o, "member_reconsents", 0);
                put(
                    o,
                    "member_identity_preserved",
                    u64::from(member_identity == saved_member),
                );
                at(q, now, 2, ReportEvent::Announcement)?;
                at(q, add(now, ms(B::OwnerPoll)?)?, 3, ReportEvent::Recover)?;
                at(q, now, 3, ReportEvent::Stale)?;
            }
            ReportEvent::Announcement => {
                reached(o, "announcement", now);
                if c.mode == Mode::Corrected {
                    report_generation = Some(owner_generation);
                    reached(o, "capability_report_delivered", now);
                    inc(o, "reports_delivered");
                } else {
                    reached(o, "report_suppressed", now);
                }
            }
            ReportEvent::Recover => {
                reached(o, "recovery_attempt", now);
                if report_generation == Some(owner_generation) && now < saved_member.2 {
                    chat(o, now);
                    inc(o, "recovered_requests");
                } else {
                    inc(o, "recovery_without_report");
                }
            }
            ReportEvent::Stale => {
                reached(o, "stale_generation", now);
                // Exercise both an old owner-incarnation report and an old
                // member-generation report through the receiver predicate.
                for (owner, member) in [(1, member_identity.1), (owner_generation, 0)] {
                    if !report_current(owner, member, owner_generation, member_identity.1) {
                        inc(o, "stale_generation_refusals");
                    } else {
                        report_generation = Some(owner);
                    }
                }
            }
        }
        Ok(())
    })
}

fn report_current(owner: u64, member: u64, current_owner: u64, current_member: u64) -> bool {
    owner == current_owner && member == current_member
}
#[derive(Clone)]
enum StatusEvent {
    Enter(u8),
    Read,
    Deadline,
    Release(u8),
}
fn status(c: &Config, input: &[Input]) -> SimResult<Outcome> {
    // Synthetic barrier holds at the recorded range endpoints. These compositions
    // reuse L1 slots, NOT a claim that worker/rollover budgets measured status.
    let compile = add(ms(B::WorkerStartup)?, ms(B::WorkerCleanupReserve)?)?;
    let publish = add(ms(B::RolloverMargin)?, ms(B::WorkerStartup)?)?;
    let response_budget = ms(B::OwnerPoll)?; // same 0.5 s discriminator as L5-STATUS
    let mut held = false;
    let mut pending = None;
    let mut responded = false;
    let mut published_phase = 0;
    drive(c, input, StatusEvent::Enter(0), |e, q, o| {
        let now = q.clock.mono_ms;
        match e {
            StatusEvent::Enter(phase) => {
                held = true;
                responded = false;
                published_phase = phase;
                let hold = if phase == 0 { compile } else { publish };
                put(o, "compile_hold_ms", compile);
                put(o, "publish_hold_ms", publish);
                put(o, "status_budget_ms", response_budget);
                reached(
                    o,
                    if phase == 0 {
                        "compile_enter"
                    } else {
                        "publish_enter"
                    },
                    now,
                );
                at(q, add(now, response_budget)?, 2, StatusEvent::Read)?;
                at(
                    q,
                    add(now, add(response_budget, response_budget)?)?,
                    2,
                    StatusEvent::Deadline,
                )?;
                at(q, add(now, hold)?, 1, StatusEvent::Release(phase))?;
            }
            StatusEvent::Read => {
                reached(o, "status_request", now);
                if c.mode == Mode::Corrected {
                    responded = true;
                    inc(o, "status_responses");
                    if held {
                        inc(o, "reads_while_blocked");
                    }
                    inc(
                        o,
                        if published_phase == 0 {
                            "compile_status_responses"
                        } else {
                            "publish_status_responses"
                        },
                    );
                    put(o, "max_status_wait_ms", 0);
                    reached(o, "published_progress_read", now);
                } else {
                    pending = Some(now);
                }
            }
            StatusEvent::Deadline => {
                reached(o, "status_deadline", now);
                if !responded && held {
                    inc(o, "blocked_status_reads");
                }
            }
            StatusEvent::Release(phase) => {
                held = false;
                reached(o, "phase_release", now);
                inc(o, "phases_completed");
                if let Some(requested) = pending.take() {
                    put(o, "max_status_wait_ms", sub(now, requested)?);
                    responded = true;
                    inc(o, "status_responses");
                }
                if phase == 0 {
                    at(q, now, 1, StatusEvent::Enter(1))?;
                } else {
                    chat(o, now);
                }
            }
        }
        Ok(())
    })
}

/// Typed serving observation. Failure means not-serving, not renewed authority.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServingProbe {
    Serving,
    NotServing,
    Failed,
}
#[derive(Debug, Clone)]
pub struct Relay {
    pub process: u64,
    pub url: &'static str,
    pub deadline_ms: u64,
    pub granted_ms: u64,
    pub renewals: u64,
    pub closed: bool,
    cap_ms: u64,
    window_ms: u64,
}
impl Relay {
    pub fn new(now: u64, window_ms: u64, cap_ms: u64) -> SimResult<Self> {
        if window_ms < ms(B::RelayMinimumWindow)?
            || window_ms > ms(B::QuickRelayMaximumWindow)?
            || cap_ms < window_ms
            || cap_ms > ms(B::RelayGrantedWindowCap)?
        {
            return Err("relay_consent_invalid".into());
        }
        Ok(Self {
            process: 1,
            url: "https://model.invalid",
            deadline_ms: add(now, window_ms)?,
            granted_ms: window_ms,
            renewals: 0,
            closed: false,
            cap_ms,
            window_ms,
        })
    }
    pub fn renewal_due(&self) -> SimResult<u64> {
        sub(
            self.deadline_ms,
            ms(B::RelayRenewalLead)?.min(self.window_ms / count(B::RenewalDivisor)?),
        )
    }
    pub fn cap_ms(&self) -> u64 {
        self.cap_ms
    }
    pub fn tick(&mut self, now: u64, probe: ServingProbe, mode: Mode) -> SimResult<()> {
        if self.closed {
            return Ok(());
        }
        if now >= self.deadline_ms {
            self.closed = true;
            return Ok(());
        }
        if mode == Mode::Corrected && probe == ServingProbe::Serving && now >= self.renewal_due()? {
            let granted = add(self.granted_ms, self.window_ms)?;
            if granted <= self.cap_ms {
                self.granted_ms = granted;
                self.renewals += 1;
                self.deadline_ms = add(now, self.window_ms)?;
            }
        }
        Ok(())
    }
    pub fn stop(&mut self) {
        self.closed = true;
    }
}
#[derive(Clone)]
enum RelayEvent {
    Start,
    Renew,
    Deadline(u64),
    Request(bool),
}
fn relay(c: &Config, input: &[Input]) -> SimResult<Outcome> {
    let mut state = None::<Relay>;
    let mut origin = 0;
    drive(c, input, RelayEvent::Start, |e, q, o| {
        let now = q.clock.mono_ms;
        match e {
            RelayEvent::Start => {
                origin = now;
                let r = Relay::new(
                    now,
                    ms(B::RelayWindow)?,
                    ms(B::RelayGrantedWindowCap)?.min(sub(c.authority_until_ms, now)?),
                )?;
                reached(o, "relay_start", now);
                put(o, "process_starts", 1);
                put(o, "url_changes", 0);
                at(q, r.renewal_due()?, 1, RelayEvent::Renew)?;
                at(q, r.deadline_ms, 0, RelayEvent::Deadline(r.deadline_ms))?;
                at(
                    q,
                    add(r.deadline_ms, ms(B::OwnerPoll)?)?,
                    3,
                    RelayEvent::Request(true),
                )?;
                at(q, now, 3, RelayEvent::Request(false))?;
                state = Some(r);
            }
            RelayEvent::Renew => {
                let r = state.as_mut().ok_or("relay_missing")?;
                reached(o, "renewal_attempt", now);
                r.tick(now, ServingProbe::Serving, c.mode)?;
                put(o, "renewals", r.renewals);
                put(o, "granted_ms", r.granted_ms);
                if r.deadline_ms > add(now, ms(B::RelayRenewalLead)?)? {
                    at(q, r.renewal_due()?, 1, RelayEvent::Renew)?;
                    at(q, r.deadline_ms, 0, RelayEvent::Deadline(r.deadline_ms))?;
                    at(
                        q,
                        add(now, ms(B::OwnerPoll)?)?,
                        3,
                        RelayEvent::Request(false),
                    )?;
                }
            }
            RelayEvent::Deadline(deadline) => {
                let r = state.as_mut().ok_or("relay_missing")?;
                if deadline == r.deadline_ms {
                    r.tick(now, ServingProbe::Serving, c.mode)?;
                    if r.closed {
                        reached(o, "relay_close", now);
                        put(o, "closed_at_ms", now);
                    }
                }
            }
            RelayEvent::Request(crossing) => {
                let r = state.as_ref().ok_or("relay_missing")?;
                if crossing {
                    reached(o, "cross_window_request", now);
                }
                if !r.closed && now < r.deadline_ms {
                    chat(o, now);
                    if now > add(origin, ms(B::RelayWindow)?)? {
                        inc(o, "requests_after_first_window");
                    }
                } else if now < c.authority_until_ms {
                    inc(o, "relay_service_losses");
                }
            }
        }
        Ok(())
    })
}

#[derive(Clone)]
enum PlacementEvent {
    Select,
    Reserve,
    Stage,
    Serve,
}
pub fn storage_consent(saved: Option<u64>) -> SimResult<u64> {
    Ok(saved.unwrap_or(bytes(B::FreshStorageConsent)?))
}
pub fn select_owner_share(mode: Mode, storage: u64) -> SimResult<u64> {
    let candidates = [
        bytes(B::HistoricalOwnerShare)?,
        bytes(B::CorrectedOwnerShare)?,
    ];
    candidates
        .into_iter()
        .find(|share| mode == Mode::Historical || *share <= storage)
        .ok_or_else(|| "member_storage_cap_below_share:OwnerLocalStorage".into())
}
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct DiskShares {
    pub owner: u64,
    pub member: u64,
}
/// Recorded max_disk_bytes include the oracle's staging allowance. They are
/// planner estimates, not measurements, and must both fit the respective caps.
pub fn select_route_shares(mode: Mode, owner_cap: u64, member_cap: u64) -> SimResult<DiskShares> {
    #[derive(serde::Deserialize)]
    struct Estimates {
        historical_member_disk_bytes: u64,
        corrected_member_disk_bytes: u64,
    }
    let estimates: Estimates =
        serde_json::from_str(include_str!("../fixtures/f14-member-estimates.json"))
            .map_err(|e| format!("placement_fixture:{e}"))?;
    let candidates = [
        DiskShares {
            owner: bytes(B::HistoricalOwnerShare)?,
            member: estimates.historical_member_disk_bytes,
        },
        DiskShares {
            owner: bytes(B::CorrectedOwnerShare)?,
            member: estimates.corrected_member_disk_bytes,
        },
    ];
    candidates
        .into_iter()
        .find(|share| {
            mode == Mode::Historical || (share.owner <= owner_cap && share.member <= member_cap)
        })
        .ok_or_else(|| "member_storage_cap_below_share:RouteStorage".into())
}
fn placement(c: &Config, input: &[Input]) -> SimResult<Outcome> {
    let storage = storage_consent(Some(bytes(B::SavedStorageConsent)?))?;
    let mut selected = DiskShares::default();
    let mut reserved = DiskShares::default();
    let mut staged = DiskShares::default();
    drive(c, input, PlacementEvent::Select, |e, q, o| {
        let now = q.clock.mono_ms;
        match e {
            PlacementEvent::Select => {
                reached(o, "placement", now);
                put(o, "owner_standing_grants", 0);
                put(o, "owner_storage_cap", storage);
                put(o, "member_storage_cap", storage);
                put(o, "member_wall_ms", ms(B::MemberChosenWall)?);
                put(o, "fresh_default_bytes", storage_consent(None)?);
                // Replay the two recorded complete partitions, without claiming
                // this finite candidate model is the full layer-cost search.
                selected = select_route_shares(c.mode, storage, storage)?;
                put(o, "selected_share_bytes", selected.owner);
                put(o, "member_selected_bytes", selected.member);
                at(q, now, 2, PlacementEvent::Reserve)?;
            }
            PlacementEvent::Reserve => {
                reached(o, "reservation_attempt", now);
                if selected.owner <= storage && selected.member <= storage {
                    reserved = selected;
                } else {
                    o.diagnostics
                        .push("member_storage_cap_below_share:OwnerLocalStorage".into());
                }
                put(o, "reserved_bytes", reserved.owner);
                put(o, "member_reserved_bytes", reserved.member);
                at(q, add(now, ms(B::OwnerPoll)?)?, 2, PlacementEvent::Stage)?;
            }
            PlacementEvent::Stage => {
                reached(o, "staging_attempt", now);
                if reserved == selected {
                    staged = selected;
                }
                put(o, "staged_bytes", staged.owner);
                put(o, "member_staged_bytes", staged.member);
                at(q, add(now, ms(B::OwnerPoll)?)?, 3, PlacementEvent::Serve)?;
            }
            PlacementEvent::Serve => {
                if staged.owner != 0
                    && staged.member != 0
                    && staged.owner <= storage
                    && staged.member <= storage
                    && now < c.authority_until_ms
                {
                    chat(o, now);
                }
            }
        }
        Ok(())
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Contributor {
    OwnerLocal,
    Joined,
}
pub fn wall_limit(
    role: Contributor,
    cap_ms: Option<u64>,
    proposal_ms: u64,
    mode: Mode,
) -> SimResult<u64> {
    let fallback = if role == Contributor::OwnerLocal && mode == Mode::Corrected {
        proposal_ms
    } else {
        ms(B::UnknownMemberWall)?
    };
    Ok(cap_ms.unwrap_or(fallback).min(proposal_ms))
}
#[derive(Clone)]
enum OwnerEvent {
    Start,
    Ready,
    Request,
}
fn owner_wall(c: &Config, input: &[Input]) -> SimResult<Outcome> {
    let mut owner_deadline = 0;
    let mut member_deadline = 0;
    drive(c, input, OwnerEvent::Start, |e, q, o| {
        let now = q.clock.mono_ms;
        match e {
            OwnerEvent::Start => {
                let proposal = ms(B::WorkerDefaultWall)?;
                owner_deadline = add(
                    now,
                    wall_limit(Contributor::OwnerLocal, None, proposal, c.mode)?,
                )?
                .min(c.authority_until_ms);
                member_deadline = add(
                    now,
                    wall_limit(
                        Contributor::Joined,
                        Some(ms(B::MemberChosenWall)?),
                        proposal,
                        c.mode,
                    )?,
                )?
                .min(c.authority_until_ms);
                put(o, "owner_standing_grants", 0);
                put(o, "proposed_wall_ms", proposal);
                put(o, "owner_worker_deadline_ms", owner_deadline);
                put(o, "member_worker_deadline_ms", member_deadline);
                put(
                    o,
                    "unknown_member_wall_ms",
                    wall_limit(Contributor::Joined, None, proposal, c.mode)?,
                );
                reached(o, "wall_selection", now);
                reached(o, "worker_start", now);
                at(q, add(now, ms(B::ObservedStart)?)?, 1, OwnerEvent::Ready)?;
                at(
                    q,
                    add(now, add(ms(B::ObservedStart)?, ms(B::UnknownMemberWall)?)?)?,
                    3,
                    OwnerEvent::Request,
                )?;
            }
            OwnerEvent::Ready | OwnerEvent::Request => {
                reached(
                    o,
                    if matches!(e, OwnerEvent::Ready) {
                        "worker_ready"
                    } else {
                        "later_request"
                    },
                    now,
                );
                if now < owner_deadline && now < member_deadline {
                    chat(o, now);
                } else {
                    inc(o, "worker_expiry_losses");
                }
            }
        }
        Ok(())
    })
}

/// Independent observed-result contracts, not scheduling calls. Numeric reference
/// assertions (source constants and arithmetic) additionally live in the tests.
pub(crate) fn violations(c: &Config, o: &Outcome) -> Vec<String> {
    let mut v = Vec::new();
    let m = |key: &str| o.metrics.get(key).copied().unwrap_or(0);
    if o.starts == 0 || o.successful_chats == 0 {
        v.push("authorized_progress_unreached".into());
    }
    for t in c.incident.required_transitions() {
        if !o.transitions.contains_key(*t) {
            v.push(format!("transition_unreached:{t}"));
        }
    }
    let fault = match c.incident {
        Incident::F1 if m("valid_authority_losses") > 0 || m("late_requests_served") < 2 => {
            Some("token_pin_expired_with_valid_authority")
        }
        Incident::F2 if m("peak_bytes") > m("mailbox_ceiling") => {
            Some("mailbox_byte_ceiling_exceeded")
        }
        Incident::F3 if m("recovered_requests") == 0 => Some("owner_restart_report_missing"),
        Incident::F5 if m("blocked_status_reads") > 0 || m("reads_while_blocked") != 2 => {
            Some("status_read_blocked_by_slow_work")
        }
        Incident::F13 if m("relay_service_losses") > 0 || m("requests_after_first_window") == 0 => {
            Some("quick_relay_expired_while_serving")
        }
        Incident::F14 if m("selected_share_bytes") > m("owner_storage_cap") => {
            Some("owner_share_exceeds_storage_consent")
        }
        Incident::F15b
            if m("owner_worker_deadline_ms") < m("proposed_wall_ms").min(c.authority_until_ms) =>
        {
            Some("owner_received_unknown_member_wall")
        }
        _ => None,
    };
    if let Some(f) = fault {
        v.push(f.into());
    }
    if c.incident == Incident::F2
        && (m("replay_refusals_after_reopen") != 3 || m("fresh_after_reopen") != 1)
    {
        v.push("replay_or_fresh_progress_contract".into());
    }
    v
}
