// SPDX-License-Identifier: AGPL-3.0-only
//! Borrowed managed-receipt boundary over the admitted NodeControl carrier.
//!
//! The host owns signing, durable sequence/intent allocation, enqueue/result
//! polling, member resolution and native stop. Supply its already-prepared bytes
//! unchanged and independently pinned receipt context. This adapter neither
//! creates a ManagedControlOwner nor treats transport closure as native cleanup.
use crate::iroh_control::{ControlAuthority, ControlError, ControlRequest, NodeControlClient};
use protocol::managed_receipt::ReceiptKind;
use serde_json::Value;
use transport::Clock;

/// Trusted context of the prepared operation, never populated from its response.
/// The carrier destination is the admitted control service, which need not be
/// the native target named by this managed mailbox binding.
pub struct ReceiptBinding<'a> {
    pub kind: ReceiptKind,
    pub owner_key: &'a Value,
    pub request_id: &'a str,
    pub swarm_id: &'a str,
    pub owner_node_id: &'a str,
    pub binding: &'a Value,
    pub native_command: &'a Value,
}

/// Borrows the existing carrier and clock; does not own another endpoint,
/// signer, runtime, journal, retry policy or membership map.
pub struct ManagedControlClient<'a, 'h, 'r, C: Clock> {
    carrier: &'a NodeControlClient<'h, 'r, C>,
    clock: &'a C,
}
impl<'a, 'h, 'r, C: Clock> ManagedControlClient<'a, 'h, 'r, C> {
    pub fn new(carrier: &'a NodeControlClient<'h, 'r, C>, clock: &'a C) -> Self {
        Self { carrier, clock }
    }

    /// Return the original envelope after the same verifier and admission checks.
    pub fn call_envelope(
        &self,
        request: ControlRequest<'_>,
        authority: &mut ControlAuthority,
        expected: &ReceiptBinding<'_>,
        local: Option<&crate::managed_owner::session::CallFence>,
    ) -> Result<Value, ControlError> {
        self.exchange(request, authority, expected, local)
            .map(|(envelope, _)| envelope)
    }

    /// One exchange only. A pending/failed/reconciliation receipt remains that
    /// receipt; it is not promoted to a successful native hello or stop.
    pub fn call(
        &self,
        request: ControlRequest<'_>,
        authority: &mut ControlAuthority,
        expected: &ReceiptBinding<'_>,
    ) -> Result<Value, ControlError> {
        self.exchange(request, authority, expected, None)
            .map(|(_, receipt)| receipt)
    }

    fn exchange(
        &self,
        request: ControlRequest<'_>,
        authority: &mut ControlAuthority,
        expected: &ReceiptBinding<'_>,
        local: Option<&crate::managed_owner::session::CallFence>,
    ) -> Result<(Value, Value), ControlError> {
        if let Some(local) = local {
            local.check().map_err(|error| ControlError(error.0))?;
        }
        let deadline = request.deadline;
        let fence = request.fence;
        let bytes = self.carrier.call_fenced(request, authority, local)?;
        let envelope = protocol::strict::object(&bytes, protocol::managed::MAX_FRAME_BYTES)
            .map_err(|_| ControlError("managed_control_response_invalid"))?;
        // Sample after the completed transport exchange. A queued or delayed
        // answer must not borrow the request's earlier wall-clock reading.
        let expected = protocol::managed_receipt::ExpectedReceipt {
            kind: expected.kind,
            owner_key: expected.owner_key,
            request_id: expected.request_id,
            swarm_id: expected.swarm_id,
            owner_node_id: expected.owner_node_id,
            binding: expected.binding,
            native_command: expected.native_command,
            now: self.clock.wall_seconds(),
        };
        use budgets::{BudgetId as B, Entries};
        let seconds = |id: B| {
            id.span(Entries::ZERO)
                .map(|span| span.millis() as f64 / 1000.0)
                .map_err(|_| ControlError("budget_unavailable"))
        };
        let receipt = protocol::managed_receipt::verify(
            &envelope,
            &expected,
            protocol::TimeLimits {
                max_ttl_seconds: seconds(B::ControlTtl)?,
                clock_skew_seconds: seconds(B::ControlClockSkew)?,
            },
        )
        .map_err(|error| ControlError(error.0))?;
        crate::iroh_control::current_authority(authority, fence)?;
        crate::iroh_host::allowance(self.clock, deadline)
            .map_err(|_| ControlError("control_read_timeout"))?;
        if let Some(local) = local {
            local.check().map_err(|error| ControlError(error.0))?;
        }
        Ok((envelope, receipt))
    }
}
