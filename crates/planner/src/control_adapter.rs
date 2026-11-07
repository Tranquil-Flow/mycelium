// SPDX-License-Identifier: AGPL-3.0-only
//! L7 placement composed with L6's durable reservation/cleanup owner.
//! No process, network, or model I/O occurs before `prepare` succeeds.
use crate::{
    admission::{PhaseBudget, PlanInputs},
    discovery::AdmittedMember,
};
use budgets::Span;

#[derive(Clone, Copy)]
pub struct RestartBudget {
    pub stop: Span,
    pub start: Span,
    pub safety: Span,
}
#[derive(Debug)]
pub enum Error {
    Planner(crate::Error),
    Control(node::Error),
}
impl From<crate::Error> for Error {
    fn from(e: crate::Error) -> Self {
        Self::Planner(e)
    }
}
impl From<node::Error> for Error {
    fn from(e: node::Error) -> Self {
        Self::Control(e)
    }
}
#[derive(Debug)]
pub struct Prepared {
    pub(crate) placement: serde_json::Value,
    pub(crate) starts: Vec<node::Effect>,
    pub(crate) window: Option<crate::admission::CompileWindow>,
    pub(crate) attempted: bool,
    pub(crate) owner: node::Binding,
    pub(crate) identities: Vec<crate::discovery::MemberIdentity>,
}
impl Prepared {
    pub fn placement(&self) -> &serde_json::Value {
        &self.placement
    }
    pub fn starts(&self) -> &[node::Effect] {
        &self.starts
    }
    /// The v0.1.4 automatic-continuation gate, independent of member ordering.
    /// The caller must also preserve the pinned model/manifest and cleanup proof.
    pub fn check_rollover_members(&self, members: &[AdmittedMember]) -> crate::Result<()> {
        let current: Vec<_> = members.iter().map(|m| m.identity().clone()).collect();
        crate::rollover::check_members(&self.identities, &current)
    }
    /// One fresh plan only after the unchanged-member gate. L6 still owns current
    /// consent, stopped-worker cleanup and fresh deadlines; no retries are added.
    pub fn replan(
        &self,
        core: &mut node::Core,
        input: PlanInputs<'_>,
        members: &[AdmittedMember],
        now: node::Now,
        phases: PhaseBudget,
        restart: RestartBudget,
    ) -> Result<Prepared, Error> {
        self.check_rollover_members(members)?;
        prepare(core, input, members, now, phases, restart)
    }
}
pub fn prepare(
    core: &mut node::Core,
    input: PlanInputs<'_>,
    members: &[AdmittedMember],
    now: node::Now,
    phases: PhaseBudget,
    restart: RestartBudget,
) -> Result<Prepared, Error> {
    let window = check_inputs(core.state(), members, now, phases)?;
    // This adapter is constructed and consumed inside one exclusive Core call.
    // A stale independently prepared Plan cannot be installed after consent changes.
    let mut adapter = Adapter {
        input,
        members,
        now,
        phases,
        restart,
        placement: None,
        failure: None,
        preflight: Ok(()),
    };
    if let Err(e) = core.plan(&mut adapter, now) {
        return Err(match adapter.failure {
            Some(e) => Error::Planner(e),
            None => Error::Control(e),
        });
    }
    let placement = adapter
        .placement
        .ok_or_else(|| crate::Error::new("planner_result_invalid"))?;
    let starts = core.start(now)?;
    Ok(Prepared {
        placement,
        starts,
        window,
        attempted: false,
        owner: core.state().owner.clone(),
        identities: members.iter().map(|m| m.identity().clone()).collect(),
    })
}
/// Continue one L6 wall-timed rollover after environmental cleanup. `reports`
/// is one complete L5-authenticated remote collection (exclude the local owner).
/// L6 consumes membership mismatch before L7 input validation; do not retry it.
/// On success compile/stage before dispatching `Prepared::starts`, as for `prepare`.
pub fn continue_rollover(
    core: &mut node::Core,
    input: PlanInputs<'_>,
    members: &[AdmittedMember],
    reports: &[node::RolloverReport],
    now: node::Now,
    phases: PhaseBudget,
    restart: &RestartBudget,
) -> Result<Prepared, Error> {
    let checked = check_inputs(core.state(), members, now, phases);
    let mut adapter = Adapter {
        input,
        members,
        now,
        phases,
        restart: *restart,
        placement: None,
        failure: None,
        preflight: checked.as_ref().map(|_| ()).map_err(Clone::clone),
    };
    let starts = core
        .continue_rollover(reports, &mut adapter, now)
        .map_err(|e| match adapter.failure.take() {
            Some(e) => Error::Planner(e),
            None => Error::Control(e),
        })?;
    Ok(Prepared {
        placement: adapter
            .placement
            .ok_or_else(|| crate::Error::new("planner_result_invalid"))?,
        starts,
        window: checked?,
        attempted: false,
        owner: core.state().owner.clone(),
        identities: members.iter().map(|m| m.identity().clone()).collect(),
    })
}
fn check_inputs(
    state: &node::State,
    members: &[AdmittedMember],
    now: node::Now,
    phases: PhaseBudget,
) -> crate::Result<Option<crate::admission::CompileWindow>> {
    let window = if phases.compile == Span::ZERO {
        None
    } else {
        let parent = now
            .mono
            .checked_add(phases.compile)
            .map_err(|_| crate::Error::new("compile_deadline_invalid"))?;
        Some(crate::admission::CompileWindow::begin(now.mono, parent)?)
    };
    if members.len() != state.members.len() + 1 {
        return Err(crate::Error::new("member_capability_binding"));
    }
    for m in members {
        let id = m.identity();
        if id.node_id == state.owner.node_id {
            if m.source() != budgets::AuthoritySource::LocalProposal {
                return Err(crate::Error::new("owner_capability_binding"));
            }
        } else {
            let current = state
                .members
                .get(&id.node_id)
                .ok_or_else(|| crate::Error::new("member_capability_binding"))?;
            if m.source() == budgets::AuthoritySource::LocalProposal
                || id.generation.to_string() != current.membership_generation
                || id.incarnation != current.incarnation
                || id.endpoint_id != current.endpoint
                || id.verification_key_digest != current.key_digest
            {
                return Err(crate::Error::new("member_capability_binding"));
            }
        }
    }
    Ok(window)
}
struct Adapter<'a> {
    input: PlanInputs<'a>,
    members: &'a [AdmittedMember],
    now: node::Now,
    phases: PhaseBudget,
    restart: RestartBudget,
    placement: Option<serde_json::Value>,
    failure: Option<crate::Error>,
    preflight: crate::Result<()>,
}
impl node::Planner for Adapter<'_> {
    fn plan(&mut self, current: &[node::PlanningMember]) -> node::Result<node::Plan> {
        match self.propose(current) {
            Ok(plan) => Ok(plan),
            Err(e) => {
                let code = e.code;
                self.failure = Some(e);
                Err(node::Error::Refused(code))
            }
        }
    }
}
impl Adapter<'_> {
    fn propose(&mut self, current: &[node::PlanningMember]) -> crate::Result<node::Plan> {
        self.preflight.clone()?;
        if current.len() != self.members.len() {
            return Err(crate::Error::new("member_capability_binding"));
        }
        let mut constrained = Vec::new();
        for m in self.members {
            let c = current
                .iter()
                .find(|c| c.node == m.identity().node_id)
                .ok_or_else(|| crate::Error::new("member_capability_binding"))?;
            if (c.authority == node::AuthoritySource::OwnerLocal)
                != (m.source() == budgets::AuthoritySource::LocalProposal)
            {
                return Err(crate::Error::new("owner_capability_binding"));
            }
            constrained.push(m.constrained(&c.caps));
        }
        // Only a proposal: no external effects or second durable reservation owner.
        // L6 refuses a non-idle core and commits all returned shares before Start.
        let mut preview = crate::admission::ReservationBook::default();
        let ticket = preview.plan_and_reserve(
            "proposal",
            PlanInputs {
                model: self.input.model,
                policy: self.input.policy,
                resources: self.input.resources,
                request: self.input.request,
            },
            &constrained,
            self.now.mono,
            self.phases,
        )?;
        let rows = ticket.plan()["members"]
            .as_array()
            .ok_or_else(|| crate::Error::new("planner_result_invalid"))?;
        let mut assignments = std::collections::BTreeMap::new();
        for (row, m) in rows.iter().zip(&constrained) {
            let c = current
                .iter()
                .find(|c| c.node == m.identity().node_id)
                .ok_or_else(|| crate::Error::new("member_capability_binding"))?;
            let integer = |key: &str| {
                row[key]
                    .as_u64()
                    .ok_or_else(|| crate::Error::new("planner_result_invalid"))
            };
            assignments.insert(
                m.identity().node_id.clone(),
                node::Caps {
                    storage_bytes: integer("max_disk_bytes")?,
                    memory_bytes: integer("max_rss_bytes")?,
                    wall_ms: m.wall().millis(),
                    commands: c.caps.commands,
                },
            );
        }
        self.placement = Some(ticket.plan().clone());
        Ok(node::Plan {
            assignments,
            stop_ms: self.restart.stop.millis(),
            start_ms: self.restart.start.millis(),
            safety_ms: self.restart.safety.millis(),
        })
    }
}
