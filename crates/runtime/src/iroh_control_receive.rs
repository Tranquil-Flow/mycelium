// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded NodeControl receive boundary. Transport possession is not command
//! authorization: dispatch must authenticate signed payloads and commit replay
//! admission before performing effects. One borrowed delivery applies backpressure.
use crate::iroh_control::{ControlError, control_maximum};
use crate::iroh_host::{allowance, outside_runtime};
use budgets::MonoInstant;
use iroh::endpoint::{Connection, SendStream};
use transport::{Channel, Clock, ConnectionFence};

pub struct NodeControlReceiver<'a, C: Clock, F: Fn() -> ConnectionFence> {
    connection: Connection,
    runtime: &'a tokio::runtime::Runtime,
    clock: &'a C,
    fence: ConnectionFence,
    current: F,
}
pub struct ControlDelivery<'s, 'a, C: Clock, F: Fn() -> ConnectionFence> {
    receiver: &'s mut NodeControlReceiver<'a, C, F>,
    send: SendStream,
    bytes: Vec<u8>,
    deadline: MonoInstant,
}
impl<'a, C: Clock, F: Fn() -> ConnectionFence> NodeControlReceiver<'a, C, F> {
    pub fn new(
        connection: Connection,
        runtime: &'a tokio::runtime::Runtime,
        clock: &'a C,
        fence: ConnectionFence,
        current: F,
    ) -> Result<Self, ControlError> {
        if connection.remote_id().as_bytes() != &fence.endpoint_id {
            return Err(ControlError("control_peer_mismatch"));
        }
        if connection.alpn() != Channel::NodeControl.alpn() {
            return Err(ControlError("control_alpn_mismatch"));
        }
        if current() != fence {
            return Err(ControlError("peer_rotated"));
        }
        Ok(Self {
            connection,
            runtime,
            clock,
            fence,
            current,
        })
    }
    fn current_binding(&self, deadline: MonoInstant) -> Result<std::time::Duration, ControlError> {
        if (self.current)() != self.fence {
            return Err(ControlError("peer_rotated"));
        }
        outside_runtime().map_err(|_| ControlError("carrier_unavailable"))?;
        allowance(self.clock, deadline).map_err(|_| ControlError("control_read_timeout"))
    }
    pub fn receive(
        &mut self,
        deadline: MonoInstant,
    ) -> Result<ControlDelivery<'_, 'a, C, F>, ControlError> {
        let wait = self.current_binding(deadline)?;
        let cap = control_maximum()?;
        let (send, bytes) = self.runtime.block_on(async {
            tokio::time::timeout(wait, async {
                let (send, mut recv) = self
                    .connection
                    .accept_bi()
                    .await
                    .map_err(|_| ControlError("control_read_failed"))?;
                let bytes = recv
                    .read_to_end(cap)
                    .await
                    .map_err(|_| ControlError("invalid_control_call"))?;
                if bytes.is_empty() {
                    return Err(ControlError("invalid_control_call"));
                }
                Ok((send, bytes))
            })
            .await
            .map_err(|_| ControlError("control_read_timeout"))?
        })?;
        self.current_binding(deadline)?;
        Ok(ControlDelivery {
            receiver: self,
            send,
            bytes,
            deadline,
        })
    }
}
impl<C: Clock, F: Fn() -> ConnectionFence> ControlDelivery<'_, '_, C, F> {
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }
    pub fn authenticated_peer(&self) -> ConnectionFence {
        self.receiver.fence
    }
    /// Only call after the host's authenticated command dispatcher completes.
    pub fn respond(mut self, bytes: &[u8]) -> Result<(), ControlError> {
        if bytes.is_empty() || bytes.len() > control_maximum()? {
            return Err(ControlError("invalid_control_result"));
        }
        let wait = self.receiver.current_binding(self.deadline)?;
        self.receiver.runtime.block_on(async {
            tokio::time::timeout(wait, self.send.write_all(bytes))
                .await
                .map_err(|_| ControlError("control_write_timeout"))?
                .map_err(|_| ControlError("control_write_failed"))?;
            self.send
                .finish()
                .map_err(|_| ControlError("control_write_failed"))
        })?;
        self.receiver.current_binding(self.deadline)?;
        Ok(())
    }
}
