// SPDX-License-Identifier: AGPL-3.0-only
//! Asynchronous application seam: monotonic wall-limit rollover and explicit Stop.
use crate::*;
use budgets::WallExpiry;
/// A single completed descriptor collection. The caller authenticates each
/// binding/lease through L5; omission means collection loss, NOT a retry request.
pub struct RolloverReport {
    pub binding: control::MemberBinding,
    pub expiry: WallExpiry,
}
/// Exact owner/run cancellation capability; not constructible from wire input.
pub struct StopFence {
    pub(crate) owner: Binding,
    pub(crate) epoch: u64,
    pub(crate) run: u64,
}
pub struct StopRequest {
    pub fence: StopFence,
    pub effects: Vec<Effect>,
}
/// All Core reservations were settled by environmental CleanupObservations.
/// This is not a router request-terminal proof, nor a proof of unregistered devices.
pub struct StopProof {
    pub(crate) epoch: u64,
    pub(crate) run: u64,
}
impl StopProof {
    pub fn epoch(&self) -> u64 {
        self.epoch
    }
    pub fn run(&self) -> u64 {
        self.run
    }
}
impl Core {
    pub fn request_stop(&mut self, now: Now) -> Result<StopRequest> {
        let effects = self.cancel(now)?;
        Ok(StopRequest {
            fence: StopFence {
                owner: self.state().owner.clone(),
                epoch: self.state().epoch,
                run: self.state().run,
            },
            effects,
        })
    }
    pub fn finish_stop(&self, fence: &StopFence) -> Result<StopProof> {
        self.ensure_current()?;
        let s = self.state();
        if s.owner != fence.owner || s.epoch != fence.epoch || s.run != fence.run {
            return Err(Error::Refused("cleanup_generation_or_phase_invalid"));
        }
        if s.phase != RunPhase::Idle || !s.workers.is_empty() || s.plan.is_some() {
            return Err(Error::Refused("cleanup_unproven"));
        }
        Ok(StopProof {
            epoch: fence.epoch,
            run: fence.run,
        })
    }
    /// Consume ONE descriptor collection after the old run's cleanup. The
    /// caller must not retry a partial collection: v0.1.4 treats it as fatal.
    /// The whole fresh report/plan/start transition is committed atomically.
    pub fn continue_rollover(
        &mut self,
        reports: &[RolloverReport],
        planner: &mut impl Planner,
        now: Now,
    ) -> Result<Vec<Effect>> {
        // Same purity contract as `Core::plan`: callers whose planner may block use
        // `rollover_request` + `finish_rollover` outside their lock.
        let request = self.rollover_request(reports, now)?;
        let plan = planner.plan(request.members())?;
        self.finish_rollover(request, plan, now)
    }
    pub(crate) fn check_rollover_members(&mut self, reports: &[RolloverReport]) -> Result<()> {
        let expected = self
            .state
            .rollover_members
            .as_ref()
            .ok_or(Error::Refused("rollover_not_pending"))?;
        if !self.state.workers.is_empty() || self.state.phase != RunPhase::Idle {
            return Err(Error::Refused("rollover_cleanup_unproven"));
        }
        let mut seen = std::collections::BTreeSet::new();
        let same = reports.len() == expected.len()
            && reports.iter().all(|r| {
                let b = &r.binding;
                seen.insert(&b.target_node_id)
                    && expected.get(&b.target_node_id).is_some_and(|m| {
                        m.endpoint == b.target_endpoint_id
                            && m.key_digest == b.target_key_digest
                            && m.membership_generation == b.membership_generation
                            && m.incarnation == b.member_incarnation
                    })
            })
            && self.state.members.len() == expected.len()
            && expected.iter().all(|(id, old)| {
                self.state.members.get(id).is_some_and(|m| {
                    m.binding == old.binding
                        && m.endpoint == old.endpoint
                        && m.key_digest == old.key_digest
                        && m.membership_generation == old.membership_generation
                        && m.incarnation == old.incarnation
                })
            });
        if !same {
            let mut s = self.state.clone();
            s.rollover_members = None;
            s.last_failure = Some(Diagnostic {
                code: "rollover_members_changed".into(),
                phase: "rollover".into(),
                kind: "MemberCollectionMismatch".into(),
            });
            self.commit(s)?;
            return Err(Error::Refused("rollover_members_changed"));
        }
        Ok(())
    }
}
/// Snapshot for a rollover planner that runs outside the Core lock.
pub struct RolloverRequest {
    pub(crate) base: State,
    pub(crate) revision: u64,
    pub(crate) inner: PlanningRequest,
}
impl RolloverRequest {
    pub fn members(&self) -> &[PlanningMember] {
        self.inner.members()
    }
}
