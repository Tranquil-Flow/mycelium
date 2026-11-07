// SPDX-License-Identifier: AGPL-3.0-only
//! Feature-gated native Router-ALPN carrier. An already established Connection
//! is injected; this module never builds endpoints, discovers peers or starts a
//! relay. Endpoint/runtime lifetime belongs to the host. Cancel closes this
//! exclusive carrier; it is NOT request/native cleanup proof. NodeControl and
//! ArtifactChunk have different protocols and deliberately remain unsupported.
use crate::iroh_wire;
use budgets::MonoInstant;
use iroh::endpoint::Connection;
use protocol::remote::REMOTE_MAX_FRAME_BYTES;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;
use transport::{Channel, Clock, ConnectionFence, Error, Exchange, Response, Transport};

/// An interrupt request for this exclusive carrier, NOT a cleanup receipt.
/// Request/native terminal arbitration remains with the lifecycle owner.
pub struct CarrierCancellation {
    connection: Connection,
    requested: Arc<AtomicBool>,
}
impl CarrierCancellation {
    pub fn request_cancel(&self) {
        self.requested.store(true, Ordering::Release);
        self.connection.close(0u32.into(), b"cancelled");
    }
}

pub struct IrohTransport<'a, C: Clock, F: Fn() -> ConnectionFence> {
    connection: Connection,
    runtime: &'a tokio::runtime::Runtime,
    clock: &'a C,
    fence: ConnectionFence,
    current: F,
    closed: bool,
    cancellation: Arc<AtomicBool>,
}
impl<'a, C: Clock, F: Fn() -> ConnectionFence> IrohTransport<'a, C, F> {
    /// Caller supplies the current authenticated member-generation binding.
    /// Possession of a connection alone never authenticates that generation.
    pub fn new(
        connection: Connection,
        runtime: &'a tokio::runtime::Runtime,
        clock: &'a C,
        fence: ConnectionFence,
        current: F,
    ) -> Result<Self, Error> {
        if connection.remote_id().as_bytes() != &fence.endpoint_id {
            return Err(Error::Authentication);
        }
        if connection.alpn() != Channel::Router.alpn() {
            return Err(Error::Channel);
        }
        if current() != fence {
            return Err(Error::Generation);
        }
        Ok(Self {
            connection,
            runtime,
            clock,
            fence,
            current,
            closed: false,
            cancellation: Arc::new(AtomicBool::new(false)),
        })
    }
    pub fn cancellation_handle(&self) -> CarrierCancellation {
        CarrierCancellation {
            connection: self.connection.clone(),
            requested: self.cancellation.clone(),
        }
    }
    fn allowance(&self, deadline: MonoInstant) -> Result<Duration, Error> {
        let now = self.clock.monotonic();
        deadline.same_domain(now).map_err(|_| Error::ClockDomain)?;
        let span = deadline.remaining(now).map_err(|_| Error::Expired)?;
        Ok(Duration::from_millis(span.millis()))
    }
    fn fence(&self, request: &Exchange) -> Result<(), Error> {
        if self.closed {
            return Err(Error::Unavailable);
        }
        if self.cancellation.load(Ordering::Acquire) {
            return Err(Error::Cancelled);
        }
        if request.channel != Channel::Router {
            return Err(Error::Channel);
        }
        if request.peer.endpoint_id != self.fence.endpoint_id {
            return Err(Error::Authentication);
        }
        if request.peer != self.fence || (self.current)() != self.fence {
            return Err(Error::Generation);
        }
        Ok(())
    }
    fn retire(&mut self, deadline: MonoInstant) -> Result<(), Error> {
        self.closed = true;
        self.connection.close(0u32.into(), b"retired");
        // Even expired cleanup closes admission/connection before refusing proof.
        let allowance = self.allowance(deadline)?;
        if tokio::runtime::Handle::try_current().is_ok() {
            return Err(Error::CleanupUnproven);
        }
        self.runtime.block_on(async {
            tokio::time::timeout(allowance, self.connection.closed())
                .await
                .map_err(|_| Error::CleanupUnproven)?;
            Ok::<(), Error>(())
        })?;
        self.allowance(deadline)?;
        Ok(())
    }
}
impl<C: Clock, F: Fn() -> ConnectionFence> Transport for IrohTransport<'_, C, F> {
    fn exchange(&mut self, request: &Exchange) -> Result<Response, Error> {
        self.fence(request)?;
        let allowance = self.allowance(request.deadline)?;
        if tokio::runtime::Handle::try_current().is_ok() {
            return Err(Error::Unavailable);
        }
        let bytes = iroh_wire::transfer(request)?;
        let result = self.runtime.block_on(async {
            tokio::time::timeout(allowance, async {
                let (mut send, mut recv) = self
                    .connection
                    .open_bi()
                    .await
                    .map_err(|_| Error::Unavailable)?;
                send.write_all(&bytes)
                    .await
                    .map_err(|_| Error::Unavailable)?;
                send.finish().map_err(|_| Error::Unavailable)?;
                let response = recv
                    .read_to_end(REMOTE_MAX_FRAME_BYTES)
                    .await
                    .map_err(|_| Error::Unavailable)?;
                iroh_wire::acknowledgement(&response, request)
            })
            .await
            .map_err(|_| Error::Expired)?
        });
        if let Err(error) = result {
            // No retry or infer-start replay. Retire all stream ownership on any
            // ambiguous transport completion; the host must reconcile separately.
            self.closed = true;
            self.connection.close(0u32.into(), b"exchange_failed");
            return Err(if self.cancellation.load(Ordering::Acquire) {
                Error::Cancelled
            } else {
                error
            });
        }
        if let Err(error) = self
            .allowance(request.deadline)
            .and_then(|_| self.fence(request))
        {
            self.closed = true;
            self.connection.close(0u32.into(), b"authority_changed");
            return Err(error);
        }
        Ok(Response {
            authenticated_peer: self.fence,
            channel: Channel::Router,
            message_id: request.message_id,
            payload: Vec::new(),
        })
    }
    fn cancel(&mut self, _message_id: [u8; 16], deadline: MonoInstant) -> Result<(), Error> {
        self.retire(deadline)
    }
    fn close(&mut self, deadline: MonoInstant) -> Result<(), Error> {
        self.retire(deadline)
    }
}
