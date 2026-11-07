// SPDX-License-Identifier: AGPL-3.0-only
//! Source: managed_control.py:1290-1445 @ f4e366e9. Wakeups shorten a hold;
//! they never supply authority or move the caller's absolute deadline.
use super::*;
use std::{
    sync::{Arc, Condvar, Mutex},
    time::{Duration, Instant},
};
#[derive(Default)]
pub struct DeliveryWake {
    generation: Mutex<u64>,
    changed: Condvar,
}
impl DeliveryWake {
    pub fn generation(&self) -> Result<u64> {
        Ok(*self
            .generation
            .lock()
            .map_err(|_| Error::Refused("delivery_wakeup_poisoned"))?)
    }
    pub(super) fn notify(&self) -> Result<()> {
        let mut generation = self
            .generation
            .lock()
            .map_err(|_| Error::Refused("delivery_wakeup_poisoned"))?;
        *generation = generation
            .checked_add(1)
            .ok_or(Error::Refused("delivery_generation_exhausted"))?;
        self.changed.notify_all();
        Ok(())
    }
    /// Snapshot before attempting delivery, then wait against that exact snapshot.
    pub fn wait_until(&self, observed: u64, deadline: Instant) -> Result<bool> {
        let mut generation = self
            .generation
            .lock()
            .map_err(|_| Error::Refused("delivery_wakeup_poisoned"))?;
        while *generation == observed {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(false);
            }
            (generation, _) = self
                .changed
                .wait_timeout(generation, remaining)
                .map_err(|_| Error::Refused("delivery_wakeup_poisoned"))?;
        }
        Ok(true)
    }
}
impl Mailbox {
    pub fn delivery_wake(&self) -> Arc<DeliveryWake> {
        self.wake.clone()
    }
    /// Connections serving one mailbox share this signal. Cross-process callers
    /// still retain the bounded step fallback; this is not an interprocess wakeup.
    pub fn with_delivery_wake(mut self, wake: Arc<DeliveryWake>) -> Self {
        self.wake = wake;
        self
    }
    fn may_hold(&self, s: &Value, now: f64) -> Result<bool> {
        if s["protocol"] != POLL2 {
            return Ok(false);
        }
        let active = s["active_claims"]
            .as_object()
            .ok_or(Error::Refused("managed_control_active_claim_invalid"))?;
        for id in active.keys() {
            let expiry: Option<f64> = self
                .store
                .connection
                .query_row(
                    "SELECT expires FROM control_dispatch WHERE command_id=?1",
                    [id],
                    |r| r.get(0),
                )
                .optional()?;
            if !expiry.is_some_and(|end| end.is_finite() && end < now) {
                return Ok(false);
            }
        }
        Ok(true)
    }
    /// Each attempt ends its IMMEDIATE transaction before parking. Only v2 empty
    /// receipts without live obligations may be held; delivered receipts stay exact.
    /// Host supplies an L1-derived step and its original absolute monotonic deadline.
    pub fn poll_held(
        &mut self,
        raw: &[u8],
        deadline: Instant,
        step: budgets::Span,
        mut clock: impl FnMut() -> f64,
        mut token: impl FnMut() -> Result<String>,
    ) -> Result<Vec<u8>> {
        require(step.millis() > 0, "managed_control_poll_step_invalid")?;
        let env = protocol::strict::object(raw, protocol::managed::MAX_FRAME_BYTES)
            .map_err(|_| Error::Refused("managed_control_json_invalid"))?;
        let s = &env["statement"];
        require(
            s["protocol"] == POLL || s["protocol"] == POLL2,
            "managed_control_protocol_invalid",
        )?;
        loop {
            let observed = self.wake.generation()?;
            let mut now = 0.0;
            let response = self.dispatch_until(
                raw,
                || {
                    now = clock();
                    now
                },
                &mut token,
                Some(deadline),
            )?;
            let reply: Value = serde_json::from_slice(&response)
                .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
            if reply["statement"]["delivery_kind"] != "empty"
                || !self.may_hold(s, now)?
                || Instant::now() >= deadline
            {
                return Ok(response);
            }
            let remaining = s["expires_at"]
                .as_f64()
                .ok_or(Error::Refused("managed_control_expiry_invalid"))?
                - now;
            if remaining <= 0.0 {
                return Ok(response);
            }
            let ttl = Duration::try_from_secs_f64(remaining)
                .map_err(|_| Error::Refused("managed_control_expiry_invalid"))?;
            let wait = Duration::from_millis(step.millis()).min(ttl);
            let end = Instant::now()
                .checked_add(wait)
                .ok_or(Error::Refused("managed_control_deadline_overflow"))?
                .min(deadline);
            self.wake.wait_until(observed, end)?;
            if Instant::now() >= deadline {
                return Ok(response);
            }
        }
    }
}
