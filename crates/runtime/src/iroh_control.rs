// SPDX-License-Identifier: AGPL-3.0-only
//! Native NodeControl carrier. Raw command/response bytes, not Router frames.
//! Signature, replay and command execution remain with the receiving service.
use crate::iroh_host::LoopbackHost;
use budgets::{Entries, MonoInstant};
use iroh::EndpointAddr;
use tokio::sync::{Semaphore, watch};
use transport::{Clock, ConnectionFence};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ControlError(pub &'static str);
impl std::fmt::Display for ControlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for ControlError {}
/// None means caller cancellation; a changed/closed watch means revoked authority.
pub type ControlAuthority = watch::Receiver<Option<ConnectionFence>>;
pub struct ControlRequest<'a> {
    pub peer: &'a EndpointAddr,
    pub fence: ConnectionFence,
    pub command: &'a [u8],
    pub deadline: MonoInstant,
}
pub struct NodeControlClient<'a, 'r, C: Clock> {
    host: &'a LoopbackHost<'r>,
    clock: &'a C,
    slots: Semaphore,
}
impl<'a, 'r, C: Clock> NodeControlClient<'a, 'r, C> {
    /// Capacity is a caller-supplied registered bound. Source sidecar uses four
    /// independent ControlCall slots; never consume Router queue slots.
    pub fn new(
        host: &'a LoopbackHost<'r>,
        clock: &'a C,
        capacity: Entries,
    ) -> Result<Self, ControlError> {
        let capacity: usize = capacity
            .get()
            .try_into()
            .map_err(|_| ControlError("control_carrier_busy"))?;
        if capacity == 0 || capacity > Semaphore::MAX_PERMITS {
            return Err(ControlError("control_carrier_busy"));
        }
        Ok(Self {
            host,
            clock,
            slots: Semaphore::new(capacity),
        })
    }
    pub fn call(
        &self,
        request: ControlRequest<'_>,
        authority: &mut ControlAuthority,
    ) -> Result<Vec<u8>, ControlError> {
        self.call_fenced(request, authority, None)
    }
    /// Add local CLIENT cancellation without replacing the live-authority watch.
    pub fn call_fenced(
        &self,
        request: ControlRequest<'_>,
        authority: &mut ControlAuthority,
        local: Option<&crate::managed_owner::session::CallFence>,
    ) -> Result<Vec<u8>, ControlError> {
        if let Some(local) = local {
            local.check().map_err(|error| ControlError(error.0))?;
        }
        use crate::iroh_host::{allowance, outside_runtime};
        use budgets::BudgetId as B;
        use iroh::endpoint::ReadToEndError;
        use tokio::time::{Instant, timeout_at};
        outside_runtime().map_err(|_| ControlError("carrier_unavailable"))?;
        self.host
            .validate_peer(request.peer)
            .map_err(|_| ControlError("invalid_control_destination"))?;
        if request.peer.id.as_bytes() != &request.fence.endpoint_id {
            return Err(ControlError("control_peer_mismatch"));
        }
        current_authority(authority, request.fence)?;
        let wait = allowance(self.clock, request.deadline)
            .map_err(|_| ControlError("control_timeout_invalid"))?;
        let ceiling = B::PyMyceliumIrohSidecarClientControlCallTimeoutCeilingSecondsL80
            .span(Entries::ZERO)
            .map_err(|_| ControlError("budget_unavailable"))?;
        if wait > std::time::Duration::from_millis(ceiling.millis()) {
            return Err(ControlError("control_timeout_exceeded"));
        }
        let maximum = control_maximum()?;
        if request.command.is_empty() || request.command.len() > maximum {
            return Err(ControlError("invalid_control_call"));
        }
        let endpoint = self
            .host
            .endpoint()
            .map_err(|_| ControlError("carrier_unavailable"))?;
        self.host.runtime().block_on(async {
            let deadline=Instant::now()+wait;
            let exchange=async {
                let _permit=timeout_at(deadline,self.slots.acquire()).await.map_err(|_| ControlError("control_carrier_busy"))?.map_err(|_| ControlError("carrier_unavailable"))?;
                current_authority(authority,request.fence)?;
                // Through the host: a direct-LAN dial is mapped to the pre-send
                // LAN transport, pinned to exactly the peer's addresses and
                // confined like every other LAN connection.
                let (target,pin)=self.host.dial_target(request.peer).map_err(|_| ControlError("invalid_control_destination"))?;
                let connection=timeout_at(deadline,endpoint.connect(target,transport::Channel::NodeControl.alpn())).await.map_err(|_| ControlError("control_connect_timeout"))?.map_err(|_| ControlError("control_connect_failed"))?;
                let connection=ControlConnection(connection);
                self.host.confine(&connection.0,pin).map_err(|_| ControlError("control_path_refused"))?;
                if connection.0.remote_id().as_bytes()!=&request.fence.endpoint_id { return Err(ControlError("control_peer_mismatch")); }
                current_authority(authority,request.fence)?;
                let (mut send,mut recv)=timeout_at(deadline,connection.0.open_bi()).await.map_err(|_| ControlError("control_open_timeout"))?.map_err(|_| ControlError("control_open_failed"))?;
                timeout_at(deadline,send.write_all(request.command)).await.map_err(|_| ControlError("control_write_timeout"))?.map_err(|_| ControlError("control_write_failed"))?;
                send.finish().map_err(|_| ControlError("control_write_failed"))?;
                let response=match timeout_at(deadline,recv.read_to_end(maximum)).await {
                    Ok(Ok(bytes)) if bytes.is_empty()=>return Err(ControlError("empty_control_call_result")),
                    Ok(Ok(bytes))=>bytes,
                    Ok(Err(ReadToEndError::TooLong))=>return Err(ControlError("control_response_too_large")),
                    Ok(Err(_))=>return Err(ControlError("control_read_failed")),
                    Err(_)=>return Err(ControlError("control_read_timeout")),
                };
                current_authority(authority,request.fence)?;
                allowance(self.clock,request.deadline).map_err(|_| ControlError("control_read_timeout"))?;
                Ok(response)
            };
            // Clone the watch so the exchange's read checks and select's changed
            // waiter never share a mutable borrow. Changes win same-turn races.
            let mut changed=authority.clone();
            let cancelled=async {match local {Some(local)=>local.cancelled().await,None=>std::future::pending().await}};
            let outcome=tokio::select! {
                biased;
                error=cancelled => Err(ControlError(error.0)),
                _ = changed.changed() => {
                    if changed.borrow().is_none() { Err(ControlError("cancelled")) } else { Err(ControlError("peer_rotated")) }
                },
                result=exchange=>{ current_authority(authority,request.fence).and_then(|()| match local {Some(local)=>local.check().map_err(|error| ControlError(error.0)),None=>Ok(())}).and(result) }
            };
            // The exchange and its call-owned connection are dropped by now, so
            // close(7) is queued but not sent. This runtime may stop being driven
            // when the call returns (an owner waits between result polls), and
            // the close timer can then drain the connection unsent: the peer sees
            // a stateless reset instead of the explicit close. Yield once so the
            // already-woken connection driver puts the close on the wire first.
            tokio::task::yield_now().await;
            outcome
        })
    }
}
pub(crate) fn current_authority(
    authority: &ControlAuthority,
    fence: ConnectionFence,
) -> Result<(), ControlError> {
    if authority.borrow().is_none() {
        return Err(ControlError("cancelled"));
    }
    if authority.has_changed().unwrap_or(true) || *authority.borrow() != Some(fence) {
        return Err(ControlError("peer_rotated"));
    }
    Ok(())
}
pub(crate) fn control_maximum() -> Result<usize, ControlError> {
    budgets::BudgetId::PyMyceliumIrohSidecarClientControlCallMaxBytesL79
        .bytes()
        .map_err(|_| ControlError("budget_unavailable"))?
        .get()
        .try_into()
        .map_err(|_| ControlError("budget_unavailable"))
}
struct ControlConnection(iroh::endpoint::Connection);
impl Drop for ControlConnection {
    fn drop(&mut self) {
        // Source sidecar.rs:1428-1433 @ f4e366e9. Not Router pooled cancellation.
        self.0.close(7u32.into(), b"control call ended");
    }
}
