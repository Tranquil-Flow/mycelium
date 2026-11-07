// SPDX-License-Identifier: AGPL-3.0-only
//! Aggregate planning reservations; L6 owns durability and actual cleanup proof.
use crate::{
    Error, Result, add,
    discovery::{AdmittedMember, MemberIdentity},
    placement::{Caps, ResourcePolicy},
    ratio::Ratio,
};
use budgets::{BudgetId, Entries, MonoInstant, Span};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};
#[derive(Clone, Copy, Debug)]
pub struct PhaseBudget {
    pub compile: Span,
    pub startup: Span,
    pub service: Span,
    pub cleanup: Span,
}
impl PhaseBudget {
    fn total(self) -> Result<Span> {
        self.compile
            .checked_add(self.startup)
            .and_then(|n| n.checked_add(self.service))
            .and_then(|n| n.checked_add(self.cleanup))
            .map_err(|_| Error::overflow())
    }
}
#[derive(Debug)]
pub struct ReservationTicket {
    id: String,
    serial: u64,
    book: Arc<()>,
    plan: Value,
    identities: Vec<MemberIdentity>,
}
impl ReservationTicket {
    pub fn id(&self) -> &str {
        &self.id
    }
    pub fn plan(&self) -> &Value {
        &self.plan
    }
    pub fn identities(&self) -> &[MemberIdentity] {
        &self.identities
    }
}
/// L6 adapter must verify all independently observed cleanup and the exact run
/// identity. Cancellation/timeout, a stale ACK, or an unsigned bool is not proof.
pub trait CleanupVerifier {
    fn verify(&self, ticket: &ReservationTicket) -> Result<()>;
}
#[derive(Clone, Copy, Default)]
struct Amount {
    disk_limit: u64,
    footprint: u64,
    memory: u64,
}
impl Amount {
    fn add(self, b: Self) -> Result<Self> {
        Ok(Self {
            disk_limit: add(self.disk_limit, b.disk_limit)?,
            footprint: add(self.footprint, b.footprint)?,
            memory: add(self.memory, b.memory)?,
        })
    }
}
struct Held {
    serial: u64,
    nodes: Vec<(MemberIdentity, Amount)>,
}
/// Exclusive mutable access makes check+reserve atomic. Compose behind an L6
/// mutex/transaction; never hold the HTTP status lock during planning.
/// Drop does not release unproven capacity. This is not durable crash recovery.
#[derive(Default)]
pub struct ReservationBook {
    book: Arc<()>,
    next: u64,
    held: BTreeMap<String, Held>,
}
pub struct PlanInputs<'a> {
    pub model: &'a Value,
    pub policy: &'a Value,
    pub resources: &'a Value,
    pub request: &'a Value,
}
impl ReservationBook {
    pub fn active(&self) -> usize {
        self.held.len()
    }
    pub fn plan_and_reserve(
        &mut self,
        id: &str,
        input: PlanInputs<'_>,
        members: &[AdmittedMember],
        now: MonoInstant,
        phases: PhaseBudget,
    ) -> Result<ReservationTicket> {
        if id.is_empty() {
            return Err(Error::new("reservation_id_invalid"));
        }
        if self.held.contains_key(id) {
            return Err(Error::new("duplicate_reservation"));
        }
        let cleanup = BudgetId::WorkerCleanupReserve
            .span(Entries::ZERO)
            .map_err(|_| Error::new("planner_phase_budget_invalid"))?;
        if phases.cleanup < cleanup || phases.service == Span::ZERO {
            return Err(Error::new("planner_phase_budget_invalid"));
        }
        let owners: Vec<_> = members
            .iter()
            .filter(|m| m.source() == budgets::AuthoritySource::LocalProposal)
            .collect();
        if owners.len() != 1 || input.request["owner_node_id"] != owners[0].identity().node_id {
            return Err(Error::new("owner_capability_binding"));
        }
        let needed = phases.total()?;
        let rp: ResourcePolicy = serde_json::from_value(input.resources.clone())
            .map_err(|_| Error::new("planner_input_invalid"))?;
        let mut used = BTreeMap::<String, Amount>::new();
        for held in self.held.values() {
            for (identity, amount) in &held.nodes {
                if let Some(current) = members
                    .iter()
                    .find(|m| m.identity().node_id == identity.node_id)
                    && current.identity() != identity
                {
                    return Err(Error::node(
                        "reservation_generation_held",
                        &identity.node_id,
                    ));
                }
                let old = used.get(&identity.node_id).copied().unwrap_or_default();
                used.insert(identity.node_id.clone(), old.add(*amount)?);
            }
        }
        let request_members = input.request["members"]
            .as_array()
            .ok_or_else(|| Error::new("planner_input_invalid"))?;
        if request_members.len() != members.len() {
            return Err(Error::new("member_capability_binding"));
        }
        let mut seen = BTreeSet::new();
        let mut rows = Vec::new();
        for (bound, request_row) in members.iter().zip(request_members) {
            let m = bound.member();
            bound.current(now)?;
            if request_row["node_id"] != m.node_id || !seen.insert(m.node_id.clone()) {
                return Err(Error::new("member_capability_binding"));
            }
            if needed > bound.wall().min(bound.remaining(now)?) {
                return Err(Error::node(
                    "member_time_insufficient_for_share",
                    &m.node_id,
                ));
            }
            // Ignore the untrusted request's claimed RAM/caps; use authenticated
            // report/local-owner inputs exclusively.
            let mut constrained = m.clone();
            let old = used.get(&m.node_id).copied().unwrap_or_default();
            let caps = m.caps.clone().unwrap_or_default();
            let physical = Ratio::int(m.memory_bytes)
                .mul(Ratio::new(
                    rp.memory_fraction[0].into(),
                    rp.memory_fraction[1].into(),
                )?)?
                .floor()?;
            let memory = caps
                .max_memory_bytes
                .unwrap_or(physical)
                .min(physical)
                .checked_sub(old.memory)
                .ok_or_else(|| Error::node("member_memory_cap_below_share", &m.node_id))?;
            let storage = caps
                .storage_bytes
                .ok_or_else(|| Error::node("member_storage_cap_required", &m.node_id))?
                .checked_sub(old.disk_limit)
                .ok_or_else(|| Error::node("member_storage_cap_below_share", &m.node_id))?;
            constrained.caps = Some(Caps {
                storage_bytes: Some(storage),
                max_memory_bytes: Some(memory),
                max_wall_seconds: caps.max_wall_seconds,
            });
            constrained.available_storage_bytes = m
                .available_storage_bytes
                .map(|n| {
                    n.checked_sub(old.footprint).ok_or_else(|| {
                        Error::node("member_disk_insufficient_for_share", &m.node_id)
                    })
                })
                .transpose()?;
            rows.push(
                serde_json::to_value(constrained)
                    .map_err(|_| Error::new("planner_input_invalid"))?,
            );
        }
        let mut request = input.request.clone();
        request["members"] = json!(rows);
        let plan = crate::plan(input.model, input.policy, input.resources, &request)?;
        let resources = plan["members"]
            .as_array()
            .ok_or_else(|| Error::new("planner_result_invalid"))?;
        if resources.len() != members.len() {
            return Err(Error::new("planner_result_invalid"));
        }
        let mut held = Vec::new();
        for (r, m) in resources.iter().zip(members) {
            let amount = Amount {
                disk_limit: r["max_disk_bytes"]
                    .as_u64()
                    .ok_or_else(|| Error::new("planner_result_invalid"))?,
                footprint: r["disk_footprint_bytes"]
                    .as_u64()
                    .ok_or_else(|| Error::new("planner_result_invalid"))?,
                memory: r["max_rss_bytes"]
                    .as_u64()
                    .ok_or_else(|| Error::new("planner_result_invalid"))?,
            };
            let previous = used.get(&m.identity().node_id).copied().unwrap_or_default();
            if let Some(available) = m.member().available_storage_bytes
                && add(previous.footprint, amount.footprint)? > available
            {
                return Err(Error::node(
                    "member_disk_insufficient_for_share",
                    &m.identity().node_id,
                ));
            }
            held.push((m.identity().clone(), amount));
        }
        let serial = self.next.checked_add(1).ok_or_else(Error::overflow)?;
        self.held.insert(
            id.into(),
            Held {
                serial,
                nodes: held,
            },
        );
        self.next = serial;
        Ok(ReservationTicket {
            id: id.into(),
            serial,
            book: self.book.clone(),
            plan,
            identities: members.iter().map(|m| m.identity().clone()).collect(),
        })
    }
    pub fn release(
        &mut self,
        ticket: &ReservationTicket,
        proof: &impl CleanupVerifier,
    ) -> Result<()> {
        if !Arc::ptr_eq(&self.book, &ticket.book)
            || self
                .held
                .get(&ticket.id)
                .is_none_or(|h| h.serial != ticket.serial)
        {
            return Err(Error::new("unknown_reservation"));
        }
        proof.verify(ticket)?;
        self.held.remove(&ticket.id);
        Ok(())
    }
}
#[derive(Debug, Clone, Copy)]
pub struct CompileWindow {
    deadline: MonoInstant,
}
impl CompileWindow {
    pub fn deadline(&self) -> MonoInstant {
        self.deadline
    }
    pub fn begin(now: MonoInstant, parent: MonoInstant) -> Result<Self> {
        parent
            .remaining(now)
            .map_err(|_| Error::new("compile_deadline_expired"))?;
        let budget = BudgetId::Compile
            .span(Entries::ZERO)
            .map_err(|_| Error::new("compile_budget_invalid"))?;
        let deadline = now
            .checked_add(budget)
            .and_then(|t| t.earliest(parent))
            .map_err(|_| Error::new("compile_deadline_invalid"))?;
        Ok(Self { deadline })
    }
    pub fn remaining(&self, now: MonoInstant) -> Result<Span> {
        self.deadline
            .remaining(now)
            .map_err(|_| Error::new("compile_deadline_expired"))
    }
}
