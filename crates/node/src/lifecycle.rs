// SPDX-License-Identifier: AGPL-3.0-only
use crate::*;
use budgets::{BudgetId as B, Entries, MonoInstant, Span, WallExpiry, WorkerWindow};
use control_runtime::Store;
use std::{collections::BTreeMap, path::Path};

pub struct Core {
    store: Option<Store>,
    revision: u64,
    pub(crate) state: State,
}
fn budget(id: B) -> Result<Span> {
    Ok(id.span(Entries::ZERO)?)
}
fn bump(n: u64) -> Result<u64> {
    n.checked_add(1)
        .ok_or(Error::Refused("generation_overflow"))
}
fn clock(s: &mut State, now: Now) -> Result<()> {
    if let Some(last) = s.last_clock {
        now.mono.elapsed_since(last.mono())?;
    }
    s.last_clock = Some(now.mono.into());
    Ok(())
}
impl Core {
    /// Open/restart owner state. Reopening fences old writers and demands fresh
    /// reports; existing workers remain reserved until independently observed cleanup.
    pub fn open(
        path: &Path,
        owner: Binding,
        policy: Caps,
        now: Now,
    ) -> Result<(Self, Vec<Effect>)> {
        Self::open_with_policy(path, owner, policy, now, Span::ZERO)
    }
    /// Production composition supplies L1's registry-backed SQLite lock wait.
    pub fn open_with_policy(
        path: &Path,
        owner: Binding,
        policy: Caps,
        now: Now,
        sqlite_busy: Span,
    ) -> Result<(Self, Vec<Effect>)> {
        if !policy.positive() {
            return Err(Error::Refused("owner_policy_invalid"));
        }
        let mut store = Store::open_with_policy(path, None, sqlite_busy)?;
        let saved = store.load_document("node")?;
        let (revision, mut state) = match saved {
            Some((revision, bytes)) => {
                let mut s: State = serde_json::from_slice(&bytes)?;
                if s.owner != owner || s.owner_policy != policy {
                    return Err(Error::Refused("owner_identity_or_policy_changed"));
                }
                s.epoch = bump(s.epoch)?;
                s.plan = None;
                s.rollover_at = None;
                s.rollover_members = None;
                for m in s.members.values_mut() {
                    m.report_epoch = None;
                    m.report_run = None;
                }
                for worker in s.workers.values_mut() {
                    worker.phase = WorkerPhase::CleanupUnproven;
                    worker.failure = Some("owner_restart_cleanup_required".into());
                }
                s.phase = if s.workers.is_empty() {
                    RunPhase::Idle
                } else {
                    RunPhase::CleanupUnproven
                };
                (Some(revision), s)
            }
            None => (
                None,
                State {
                    owner,
                    owner_policy: policy,
                    epoch: 1,
                    run: 0,
                    members: BTreeMap::new(),
                    phase: RunPhase::Idle,
                    plan: None,
                    workers: BTreeMap::new(),
                    rollover_at: None,
                    rollover_members: None,
                    last_clock: None,
                    last_failure: None,
                },
            ),
        };
        // Monotonic clocks from an old boot cannot create new authority. Leases
        // retain their original domain and require a newer receipt when re-pinned.
        if state
            .last_clock
            .is_some_and(|last| last.domain == now.mono.domain().0)
        {
            clock(&mut state, now)?;
        } else {
            state.last_clock = Some(now.mono.into());
        }
        let revision = store.save_document("node", revision, &serde_json::to_vec(&state)?)?;
        let effects = state
            .members
            .keys()
            .map(|node| Effect::RefreshReport {
                node: node.clone(),
                epoch: state.epoch,
            })
            .collect();
        Ok((
            Self {
                store: Some(store),
                revision,
                state,
            },
            effects,
        ))
    }
    /// Execute an authenticated mailbox operation and the real lifecycle transition
    /// in one IMMEDIATE transaction. The transition is trusted host composition:
    /// it must map the validated statement/outcome to the appropriate core method,
    /// never perform native IO. Returned intents are persisted, not executed.
    /// The supplied wall+monotonic clock is sampled once after the SQLite lock.
    pub fn dispatch_mailbox(
        &mut self,
        mailbox: &mut control_runtime::mailbox::Mailbox,
        raw: &[u8],
        clock_source: impl FnOnce() -> Now,
        token: impl FnOnce() -> control_runtime::Result<String>,
        capacity: Entries,
        transition: impl FnOnce(
            &mut Core,
            &control_runtime::mailbox::DispatchEffects<'_, '_>,
            Now,
        ) -> Result<Vec<Effect>>,
    ) -> Result<Vec<u8>> {
        let store = self
            .store
            .as_ref()
            .ok_or(Error::Refused("node_nested_transaction"))?;
        let sampled = std::cell::Cell::new(None);
        let mut accepted = None;
        let mut failure = None;
        let result = mailbox.dispatch_with_effects(
            raw,
            || {
                let now = clock_source();
                sampled.set(Some(now));
                now.wall.unix_millis() as f64 / 1000.0
            },
            token,
            |context| {
                let action = || -> Result<()> {
                    if !context.belongs_to(store) {
                        return Err(Error::Refused("node_mailbox_database_mismatch"));
                    }
                    let (revision, bytes) = context
                        .load("node")?
                        .ok_or(Error::Refused("node_state_missing"))?;
                    let saved: State = serde_json::from_slice(&bytes)?;
                    if revision != self.revision || saved != self.state {
                        return Err(Error::Refused("node_state_stale_writer"));
                    }
                    if context.response()["owner_node_id"] != saved.owner.node_id
                        || context.statement()["swarm_id"] != saved.owner.swarm_id
                    {
                        return Err(Error::Refused("node_mailbox_owner_mismatch"));
                    }
                    let now = sampled.get().ok_or(Error::Refused("node_clock_missing"))?;
                    let mut draft = Core {
                        store: None,
                        revision,
                        state: saved,
                    };
                    let effects = transition(&mut draft, context, now)?;
                    let next = context.publish(
                        "node",
                        Some(revision),
                        &serde_json::to_value(&draft.state)?,
                    )?;
                    for (index, effect) in effects.iter().enumerate() {
                        context.enqueue_scoped(
                            "node",
                            index,
                            &serde_json::json!({"state_revision":next,"effect":effect}),
                            capacity,
                        )?;
                    }
                    accepted = Some((next, draft.state));
                    Ok(())
                };
                match action() {
                    Ok(()) => Ok(()),
                    Err(error) => {
                        failure = Some(error);
                        Err(control_runtime::Error::Refused("node_transition_failed"))
                    }
                }
            },
        );
        let response = match result {
            Ok(response) => response,
            Err(error) => return Err(failure.unwrap_or(Error::Store(error))),
        };
        if let Some((revision, state)) = accepted {
            self.revision = revision;
            self.state = state;
        }
        Ok(response)
    }
    pub fn state(&self) -> &State {
        &self.state
    }
    pub(crate) fn ensure_current(&self) -> Result<()> {
        let store = self
            .store
            .as_ref()
            .ok_or(Error::Refused("node_nested_transaction"))?;
        let (revision, bytes) = store
            .load_document("node")?
            .ok_or(Error::Refused("node_state_missing"))?;
        let saved: State = serde_json::from_slice(&bytes)?;
        if revision != self.revision || saved != self.state {
            return Err(Error::Refused("node_state_stale_writer"));
        }
        Ok(())
    }
    /// Fresh reports applied to a throw-away draft, then the planning snapshot of that draft.
    pub fn rollover_request(
        &mut self,
        reports: &[RolloverReport],
        now: Now,
    ) -> Result<RolloverRequest> {
        self.check_rollover_members(reports)?;
        let mut draft = Core {
            store: None,
            revision: self.revision,
            state: self.state.clone(),
        };
        clock(&mut draft.state, now)?;
        for report in reports {
            draft.report(
                &report.binding.target_node_id,
                &report.binding.membership_generation,
                draft.state.epoch,
                report.expiry,
                now,
            )?;
        }
        Ok(RolloverRequest {
            base: self.state.clone(),
            revision: self.revision,
            inner: draft.plan_request(now)?,
        })
    }
    /// Plan + start of the drafted rollover, committed atomically; same staleness rules.
    pub fn finish_rollover(
        &mut self,
        request: RolloverRequest,
        plan: Plan,
        now: Now,
    ) -> Result<Vec<Effect>> {
        if request.revision != self.revision || request.base != self.state {
            return Err(Error::Refused("planning_request_stale"));
        }
        let mut draft = Core {
            store: None,
            revision: request.inner.revision,
            state: request.inner.base.clone(),
        };
        let planned = draft.planned_state(request.inner, plan, now)?;
        draft.state = planned;
        let effects = draft.start(now)?;
        self.commit(draft.state)?;
        Ok(effects)
    }
    pub(crate) fn commit(&mut self, s: State) -> Result<()> {
        if let Some(store) = &mut self.store {
            self.revision =
                store.save_document("node", Some(self.revision), &serde_json::to_vec(&s)?)?;
        }
        self.state = s;
        Ok(())
    }
    /// Trusted onboarding seam: caller must supply L5-authenticated current membership.
    pub fn join(&mut self, b: &control::MemberBinding) -> Result<()> {
        if b.target_node_id == self.state.owner.node_id
            || [
                &b.target_node_id,
                &b.target_endpoint_id,
                &b.target_key_digest,
                &b.membership_generation,
                &b.member_incarnation,
            ]
            .iter()
            .any(|v| v.is_empty())
        {
            return Err(Error::Refused("membership_binding_invalid"));
        }
        if self.state.phase != RunPhase::Idle {
            return Err(Error::Refused("membership_change_while_busy"));
        }
        let mut s = self.state.clone();
        if let Some(m) = s.members.get(&b.target_node_id) {
            if m.endpoint == b.target_endpoint_id
                && m.key_digest == b.target_key_digest
                && m.membership_generation == b.membership_generation
                && m.incarnation == b.member_incarnation
            {
                return Ok(());
            }
            return Err(Error::Refused("membership_replacement_requires_leave"));
        }
        s.members.insert(
            b.target_node_id.clone(),
            Member {
                binding: Binding {
                    node_id: b.target_node_id.clone(),
                    ..s.owner.clone()
                },
                endpoint: b.target_endpoint_id.clone(),
                key_digest: b.target_key_digest.clone(),
                membership_generation: b.membership_generation.clone(),
                incarnation: b.member_incarnation.clone(),
                consent: None,
                report_epoch: None,
                report_run: None,
                lease: None,
            },
        );
        self.commit(s)
    }
    pub fn consent(&mut self, consent: Consent) -> Result<()> {
        let mut s = self.state.clone();
        let m = s
            .members
            .get_mut(&consent.binding.node_id)
            .ok_or(Error::Refused("membership_required"))?;
        if consent.binding != m.binding
            || consent.disclosure_digest != disclosure_digest()
            || !consent.caps.positive()
            || consent.withdrawn_at.is_some()
        {
            return Err(Error::Refused("standing_consent_owner_mismatch"));
        }
        if let Some(old) = &m.consent
            && old.withdrawn_at.is_none()
        {
            if old.caps != consent.caps {
                return Err(Error::Refused("standing_consent_changed"));
            }
            return Ok(());
        }
        m.consent = Some(consent);
        s.plan = None;
        self.commit(s)
    }
    /// Only authenticated current membership lease receipts may enter here.
    /// A capability report alone is never a lease or execution grant.
    pub fn report(
        &mut self,
        node: &str,
        generation: &str,
        epoch: u64,
        expiry: WallExpiry,
        now: Now,
    ) -> Result<()> {
        let mut s = self.state.clone();
        clock(&mut s, now)?;
        if epoch != s.epoch {
            return Err(Error::Refused("owner_generation_stale"));
        }
        let m = s
            .members
            .get_mut(node)
            .ok_or(Error::Refused("membership_required"))?;
        if generation != m.membership_generation {
            return Err(Error::Refused("member_generation_stale"));
        }
        let remaining = expiry.remaining(now.wall)?;
        if remaining > budget(B::MembershipTtl)? {
            return Err(Error::Refused("membership_ttl_exceeded"));
        }
        let mut deadline = now.mono.checked_add(remaining)?;
        if let Some(old) = &m.lease {
            if old.deadline.domain == now.mono.domain().0 {
                old.deadline.mono().remaining(now.mono)?;
                if expiry.unix_millis() <= old.signed_expiry {
                    deadline = deadline.earliest(old.deadline.mono())?;
                }
            } else if expiry.unix_millis() <= old.signed_expiry {
                return Err(Error::Refused("fresh_lease_required_after_reboot"));
            }
        }
        m.lease = Some(Lease {
            signed_expiry: expiry.unix_millis(),
            deadline: deadline.into(),
            renew_at: now
                .mono
                .checked_add(budget(B::LeaseRenewalInterval)?)?
                .earliest(deadline)?
                .into(),
            last: now.mono.into(),
        });
        m.report_epoch = Some(epoch);
        m.report_run = Some(s.run);
        self.commit(s)
    }
    fn planning_members(s: &State, now: Now) -> Result<Vec<PlanningMember>> {
        let mut members = vec![PlanningMember {
            node: s.owner.node_id.clone(),
            caps: s.owner_policy.clone(),
            authority: AuthoritySource::OwnerLocal,
        }];
        for (node, m) in &s.members {
            let consent = m
                .consent
                .as_ref()
                .ok_or(Error::Refused("standing_consent_missing"))?;
            if consent.withdrawn_at.is_some() {
                return Err(Error::Refused("standing_consent_withdrawn"));
            }
            if consent.binding != m.binding {
                return Err(Error::Refused("standing_consent_owner_mismatch"));
            }
            if consent.disclosure_digest != disclosure_digest() {
                return Err(Error::Refused("standing_consent_required"));
            }
            if m.report_epoch != Some(s.epoch) || m.report_run != Some(s.run) {
                return Err(Error::Refused("member_report_required"));
            }
            m.lease
                .as_ref()
                .ok_or(Error::Refused("membership_lease_required"))?
                .deadline
                .mono()
                .remaining(now.mono)?;
            members.push(PlanningMember {
                node: node.clone(),
                caps: consent.caps.clone(),
                authority: AuthoritySource::StandingConsent,
            });
        }
        Ok(members)
    }
    fn validate_plan(plan: &Plan, members: &[PlanningMember]) -> Result<()> {
        if plan.assignments.len() != members.len() {
            return Err(Error::Refused("plan_member_set_mismatch"));
        }
        if plan.stop_ms == 0
            || plan.start_ms == 0
            || plan.safety_ms < budget(B::WorkerCleanupReserve)?.millis()
        {
            return Err(Error::Refused("restart_budget_invalid"));
        }
        for member in members {
            let share = plan
                .assignments
                .get(&member.node)
                .ok_or(Error::Refused("plan_member_set_mismatch"))?;
            if !member.caps.contains(share) {
                return Err(Error::Refused("plan_exceeds_consent"));
            }
            if span(share.wall_ms) > budget(B::WorkerMaximumWall)? {
                return Err(Error::Refused("worker_wall_invalid"));
            }
            let lead = B::EffectiveRolloverLead.wall_span(span(share.wall_ms), Entries::ZERO)?;
            budgets::require_rollover(
                lead,
                span(plan.stop_ms),
                span(plan.start_ms),
                span(plan.safety_ms),
            )?;
        }
        Ok(())
    }
    /// Verified development adapter seam. L7 production placement and measured-p99
    /// admission must be integrated before this core is qualified for serving.
    ///
    /// Convenience composition of [`Core::plan_request`], the planner and
    /// [`Core::apply_plan`]. The planner therefore runs while the CALLER holds `&mut self`
    /// (and any lock around the Core): it must be pure and CPU-bounded. A planner that can
    /// block, do I/O or read the Core through a shared lock must use the split API so it
    /// runs outside the lock (`tests/planner_purity.rs`).
    pub fn plan(&mut self, planner: &mut impl Planner, now: Now) -> Result<()> {
        let request = self.plan_request(now)?;
        let plan = planner.plan(request.members())?;
        self.apply_plan(request, plan, now)
    }
    /// Read-only snapshot for a planner that runs OUTSIDE the Core (`&self`: nothing can
    /// be mutated, and the caller may release its lock before planning).
    pub fn plan_request(&self, now: Now) -> Result<PlanningRequest> {
        let mut s = self.state.clone();
        clock(&mut s, now)?;
        if !s.workers.is_empty() || s.phase != RunPhase::Idle {
            return Err(Error::Refused("run_not_idle"));
        }
        let members = Self::planning_members(&s, now)?;
        Ok(PlanningRequest {
            base: self.state.clone(),
            revision: self.revision,
            draft: s,
            members,
        })
    }
    /// Commit a planner's answer. Refused unless the live state is exactly the state the
    /// request was made from, the leases still hold at `now`, and the plan independently
    /// validates against the requested members.
    pub fn apply_plan(&mut self, request: PlanningRequest, plan: Plan, now: Now) -> Result<()> {
        let s = self.planned_state(request, plan, now)?;
        self.commit(s)
    }
    pub(crate) fn planned_state(
        &self,
        request: PlanningRequest,
        plan: Plan,
        now: Now,
    ) -> Result<State> {
        if request.revision != self.revision || request.base != self.state {
            return Err(Error::Refused("planning_request_stale"));
        }
        let PlanningRequest {
            mut draft, members, ..
        } = request;
        clock(&mut draft, now)?;
        Self::planning_members(&draft, now)?;
        Self::validate_plan(&plan, &members)?;
        draft.plan = Some(plan);
        draft.rollover_members = None;
        draft.phase = RunPhase::Planned;
        Ok(draft)
    }
    pub fn start(&mut self, now: Now) -> Result<Vec<Effect>> {
        let mut s = self.state.clone();
        clock(&mut s, now)?;
        if s.phase != RunPhase::Planned || !s.workers.is_empty() {
            return Err(Error::Refused("run_not_planned"));
        }
        let members = Self::planning_members(&s, now)?;
        let plan = s.plan.as_ref().ok_or(Error::Refused("plan_missing"))?;
        Self::validate_plan(plan, &members)?;
        s.run = bump(s.run)?;
        let mut effects = Vec::new();
        for member in members {
            let caps = plan.assignments[&member.node].clone();
            let incarnation = s
                .members
                .get(&member.node)
                .map(|m| m.incarnation.clone())
                .unwrap_or_else(|| format!("owner-{}", s.epoch));
            let ticket = Ticket {
                epoch: s.epoch,
                run: s.run,
                node: member.node.clone(),
                incarnation,
            };
            s.workers.insert(
                member.node,
                Reservation {
                    ticket: ticket.clone(),
                    caps: caps.clone(),
                    authority: member.authority,
                    phase: WorkerPhase::Starting,
                    requested_at: now.mono.into(),
                    origin: None,
                    expires: None,
                    stop_deadline: None,
                    failure: None,
                },
            );
            effects.push(Effect::Start { ticket, caps });
        }
        s.phase = RunPhase::Starting;
        self.commit(s)?;
        Ok(effects)
    }
    /// The native supervisor supplies the observed origin, never receipt-arrival time.
    pub fn started(&mut self, ticket: &Ticket, origin: MonoInstant, now: Now) -> Result<()> {
        let mut s = self.state.clone();
        clock(&mut s, now)?;
        if ticket.epoch != s.epoch {
            return Err(Error::Refused("worker_generation_stale"));
        }
        for member in s.members.values() {
            let consent = member
                .consent
                .as_ref()
                .ok_or(Error::Refused("standing_consent_missing"))?;
            if consent.withdrawn_at.is_some() {
                return Err(Error::Refused("standing_consent_withdrawn"));
            }
            member
                .lease
                .as_ref()
                .ok_or(Error::Refused("membership_lease_required"))?
                .deadline
                .mono()
                .remaining(now.mono)?;
        }
        let w = s
            .workers
            .get_mut(&ticket.node)
            .ok_or(Error::Refused("worker_unknown"))?;
        if &w.ticket != ticket || w.phase != WorkerPhase::Starting {
            return Err(Error::Refused("worker_generation_stale"));
        }
        origin.elapsed_since(w.requested_at.mono())?;
        now.mono.elapsed_since(origin)?;
        let end = origin.checked_add(span(w.caps.wall_ms))?;
        end.remaining(now.mono)?;
        w.origin = Some(origin.into());
        w.expires = Some(end.into());
        w.phase = WorkerPhase::Running;
        let windows: Vec<_> = s
            .workers
            .values()
            .filter_map(|w| {
                Some(WorkerWindow {
                    origin: w.origin?.mono(),
                    wall: span(w.caps.wall_ms),
                    authorized_until: w.expires?.mono(),
                })
            })
            .collect();
        s.rollover_at = Some(budgets::deadlines(&windows, now.mono)?.rollover_at.into());
        if s.workers.values().all(|w| w.phase == WorkerPhase::Running) {
            s.phase = RunPhase::Serving;
        }
        self.commit(s)
    }
    fn stop(s: &mut State, now: Now, reason: &str) -> Result<Vec<Effect>> {
        let stop = span(
            s.plan
                .as_ref()
                .map(|p| p.stop_ms)
                .unwrap_or(budget(B::PreviousClose)?.millis()),
        );
        let mut effects = Vec::new();
        for w in s.workers.values_mut() {
            if w.phase == WorkerPhase::Stopping {
                continue;
            }
            let mut deadline = now.mono.checked_add(stop)?;
            if let Some(prior) = w.stop_deadline
                && prior.domain == now.mono.domain().0
            {
                deadline = deadline.earliest(prior.mono())?;
            }
            if let Some(expiry) = w.expires
                && expiry.domain == now.mono.domain().0
            {
                deadline = deadline.earliest(expiry.mono())?;
            }
            w.phase = WorkerPhase::Stopping;
            w.stop_deadline = Some(deadline.into());
            w.failure = Some(reason.into());
            effects.push(Effect::Stop {
                ticket: w.ticket.clone(),
                deadline: deadline.into(),
            });
        }
        s.phase = if s.workers.is_empty() {
            RunPhase::Idle
        } else {
            RunPhase::Stopping
        };
        s.rollover_at = None;
        if s.workers.is_empty() {
            s.plan = None;
        }
        Ok(effects)
    }
    /// Native adapter failure (not a peer-provided error string).
    pub fn worker_failed(&mut self, ticket: &Ticket, now: Now) -> Result<Vec<Effect>> {
        let mut s = self.state.clone();
        clock(&mut s, now)?;
        let worker = s
            .workers
            .get(&ticket.node)
            .ok_or(Error::Refused("worker_unknown"))?;
        if worker.ticket != *ticket
            || ticket.epoch != s.epoch
            || !matches!(worker.phase, WorkerPhase::Starting | WorkerPhase::Running)
        {
            return Err(Error::Refused("worker_generation_stale"));
        }
        let effects = Self::stop(&mut s, now, "worker_failed")?;
        s.last_failure = Some(Diagnostic {
            code: "worker_failed".into(),
            phase: "worker".into(),
            kind: "NativeFailure".into(),
        });
        self.commit(s)?;
        Ok(effects)
    }
    pub fn cancel(&mut self, now: Now) -> Result<Vec<Effect>> {
        let mut s = self.state.clone();
        clock(&mut s, now)?;
        let effects = Self::stop(&mut s, now, "owner_cancelled")?;
        s.rollover_members = None;
        self.commit(s)?;
        Ok(effects)
    }
    pub fn tick(&mut self, now: Now) -> Result<Vec<Effect>> {
        let mut s = self.state.clone();
        clock(&mut s, now)?;
        let mut effects = Vec::new();
        let mut must_stop = None;
        for (node, m) in &mut s.members {
            if let Some(l) = &mut m.lease {
                if l.deadline.domain != now.mono.domain().0 || l.deadline.ticks <= now.mono.ticks()
                {
                    must_stop = Some("membership_expired");
                } else if l.renew_at.ticks <= now.mono.ticks() {
                    l.renew_at = now
                        .mono
                        .checked_add(budget(B::LeaseRenewalInterval)?)?
                        .earliest(l.deadline.mono())?
                        .into();
                    effects.push(Effect::Renew {
                        node: node.clone(),
                        epoch: s.epoch,
                        generation: m.membership_generation.clone(),
                    });
                }
            }
        }
        if s.rollover_at.is_some_and(|d| d.ticks <= now.mono.ticks()) {
            must_stop = Some("automatic_rollover");
        }
        for w in s.workers.values_mut() {
            if w.phase == WorkerPhase::Starting
                && now.mono.elapsed_since(w.requested_at.mono())? >= budget(B::WorkerStartup)?
            {
                must_stop = Some("worker_start_timeout");
            }
            if w.phase == WorkerPhase::Stopping
                && w.stop_deadline.is_some_and(|d| d.ticks <= now.mono.ticks())
            {
                w.phase = WorkerPhase::CleanupUnproven;
                w.failure = Some("worker_stop_timeout".into());
                s.phase = RunPhase::CleanupUnproven;
                s.last_failure = Some(Diagnostic {
                    code: "worker_stop_timeout".into(),
                    phase: "stop".into(),
                    kind: "DeadlineExceeded".into(),
                });
            }
        }
        if let Some(reason) = must_stop
            && matches!(s.phase, RunPhase::Serving | RunPhase::Starting)
        {
            s.rollover_members = (reason == "automatic_rollover").then(|| s.members.clone());
            effects.extend(Self::stop(&mut s, now, reason)?);
        }
        self.commit(s)?;
        Ok(effects)
    }
    /// Trusted cleanup observation seam, NOT a signed peer's asserted cleanup.
    /// Concrete supervisors must prove both process exit and owned-resource absence.
    pub fn cleaned(&mut self, observation: &crate::CleanupObservation, now: Now) -> Result<()> {
        let ticket = observation.ticket();
        let mut s = self.state.clone();
        clock(&mut s, now)?;
        let w = s
            .workers
            .get_mut(&ticket.node)
            .ok_or(Error::Refused("worker_unknown"))?;
        if &w.ticket != ticket
            || !matches!(
                w.phase,
                WorkerPhase::Stopping | WorkerPhase::CleanupUnproven
            )
        {
            return Err(Error::Refused("cleanup_generation_or_phase_invalid"));
        }
        if !observation.proven() {
            w.phase = WorkerPhase::CleanupUnproven;
            w.failure = Some("rollover_cleanup_unproven".into());
            s.phase = RunPhase::CleanupUnproven;
            s.last_failure = Some(Diagnostic {
                code: "rollover_cleanup_unproven".into(),
                phase: "cleanup".into(),
                kind: "ResiduePresent".into(),
            });
            self.commit(s)?;
            return Err(Error::Refused("rollover_cleanup_unproven"));
        }
        s.workers
            .remove(&ticket.node)
            .ok_or(Error::Refused("worker_unknown"))?;
        if s.workers.is_empty() {
            s.phase = RunPhase::Idle;
            s.plan = None;
            s.rollover_at = None;
        }
        self.commit(s)
    }
    pub fn withdraw(&mut self, node: &str, now: Now) -> Result<Vec<Effect>> {
        let mut s = self.state.clone();
        clock(&mut s, now)?;
        let consent = s
            .members
            .get_mut(node)
            .ok_or(Error::Refused("membership_required"))?
            .consent
            .as_mut()
            .ok_or(Error::Refused("standing_consent_missing"))?;
        consent.withdrawn_at = Some(now.wall.unix_millis());
        let effects = Self::stop(&mut s, now, "standing_consent_withdrawn")?;
        self.commit(s)?;
        Ok(effects)
    }
}
