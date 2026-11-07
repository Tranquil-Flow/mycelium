// SPDX-License-Identifier: AGPL-3.0-only
//! Qualification clock only: real L6 policy/grants, injected elapsed wall time.
//! No shortened consent or alternative planner/reservation authority.
use super::*;
use std::sync::atomic::{AtomicU64, Ordering};
pub(super) struct LifecycleOwner {
    pub retained: Mutex<Option<node::Core>>,
    pub incarnation: String,
    pub timed_qualification: bool,
    origin: Instant,
    wall_origin: u64,
    offset: AtomicU64,
}
impl LifecycleOwner {
    pub fn new(timed_qualification: bool) -> Result<Self> {
        Ok(Self {
            retained: Mutex::new(None),
            incarnation: service::random_id().map_err(|e| e.code)?,
            timed_qualification,
            origin: Instant::now(),
            wall_origin: receipts::wall_ms()?,
            offset: AtomicU64::new(0),
        })
    }
    pub fn now(&self) -> Result<node::Now> {
        let elapsed = u64::try_from(self.origin.elapsed().as_millis())?;
        let ticks = elapsed
            .checked_add(self.offset.load(Ordering::SeqCst))
            .ok_or("qualification_clock_overflow")?;
        Ok(node::Now {
            mono: MonoInstant::new(ClockDomain(1), ticks),
            wall: WallExpiry::from_unix_millis(
                self.wall_origin
                    .checked_add(ticks)
                    .ok_or("qualification_wall_overflow")?,
            ),
        })
    }
    pub fn advance_to(&self, target: MonoInstant) -> Result<()> {
        if !self.timed_qualification {
            return Err("qualification_clock_not_enabled".into());
        }
        let now = self.now()?.mono;
        target
            .same_domain(now)
            .map_err(|e| format!("qualification clock: {e:?}"))?;
        let delta = target.ticks().saturating_sub(now.ticks());
        self.offset
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_add(delta))
            .map_err(|_| "qualification_clock_overflow")?;
        Ok(())
    }
}
/// Drive the actual L6 deadline, renewing authenticated owner leases through the
/// live member process as injected time advances. Only the clock is simulated.
pub(super) fn timed_stop(
    lifecycle: &LifecycleOwner,
    core: &mut node::Core,
    owner: &Identity,
    membership: &crate::enrollment::LocalMembership,
    wire: &crate::control::Wire<'_>,
    member: &mut crate::control::Peer,
    member_key: &Value,
) -> Result<Value> {
    let at = core
        .state()
        .rollover_at
        .ok_or("rollover_deadline_missing")?;
    let mut renewals = Vec::new();
    loop {
        let now = lifecycle.now()?;
        if now.mono.ticks() >= at.ticks {
            break;
        }
        let next = now
            .mono
            .checked_add(span(B::LeaseRenewalInterval))
            .map_err(|e| format!("renewal clock: {e:?}"))?;
        lifecycle.advance_to(if next.ticks() < at.ticks {
            next
        } else {
            at.mono()
        })?;
        let now = lifecycle.now()?;
        let fresh = member.call(
            wire,
            "renew_prepare",
            json!({"qualification_now":now.wall.unix_millis()}),
            B::WorkerStartup,
        )?;
        if fresh["join"]["message"]["sender_endpoint_id"]
            != core.state().members["b-member"].endpoint
        {
            return Err("renewal_endpoint_changed".into());
        }
        let acceptance =
            crate::enrollment::accept(owner, &membership.invitation, &fresh["join"], 1, now.wall)?;
        let reply = member.call(
            wire,
            "renew",
            json!({"qualification_now":now.wall.unix_millis(),"acceptance":acceptance}),
            B::WorkerStartup,
        )?;
        protocol::identity::verify(&reply["statement"], &reply["signature"], member_key)?;
        if reply["statement"]["accepted"] != canonical::digest(&acceptance)? {
            return Err("renewal_ack_binding".into());
        }
        renewals.push(json!({"join":fresh["join"],"acceptance":acceptance,"ack":reply}));
        let expiry = lease_expiry(&acceptance)?;
        core.report(
            "b-member",
            "1",
            core.state().epoch,
            expiry,
            lifecycle.now()?,
        )
        .map_err(|e| format!("renewal report: {e:?}"))?;
    }
    let trigger = lifecycle.now()?;
    let stops = core
        .tick(trigger)
        .map_err(|e| format!("wall tick: {e:?}"))?;
    let expected: std::collections::BTreeSet<_> = core
        .state()
        .workers
        .values()
        .map(|w| w.ticket.node.clone())
        .collect();
    let actual: std::collections::BTreeSet<_> = stops
        .into_iter()
        .filter_map(|e| match e {
            node::Effect::Stop { ticket, .. } => Some(ticket.node),
            _ => None,
        })
        .collect();
    if expected != actual || actual.is_empty() || core.state().rollover_members.is_none() {
        return Err("wall_timed_stop_not_emitted".into());
    }
    Ok(
        json!({"clock":"locally-admitted-qualification-only","rollover_at":at,"trigger_monotonic":trigger.mono.ticks(),"trigger_wall_ms":trigger.wall.unix_millis(),"owner_wall_cap_ms":core.state().owner_policy.wall_ms,"stopped_nodes":actual,"renewals":renewals,"pending_continuation":core.state().rollover_members.is_some()}),
    )
}
pub(super) fn lease_expiry(acceptance: &Value) -> Result<WallExpiry> {
    let millis = acceptance["message"]["lease_expires_at"]
        .as_f64()
        .ok_or("lease_time")?
        * 1000.;
    if !millis.is_finite() || millis < 0. || millis > ((1u64 << 53) - 1) as f64 {
        return Err("lease_time".into());
    }
    Ok(WallExpiry::from_unix_millis(millis.floor() as u64))
}
