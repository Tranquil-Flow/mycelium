// SPDX-License-Identifier: AGPL-3.0-only
//! Router receive/dispatch-ACK boundary. This does not replace the host's bounded
//! pending/completed deduplication and native command receipts. The dispatcher
//! must arbitrate message IDs before executing effects; ACK means dispatch
//! completed, never merely that this library read a stream.
use crate::iroh_host::{allowance, flush_finished};
use budgets::MonoInstant;
use iroh::endpoint::{Connection, SendStream};
use protocol::{
    remote::{self, RemoteKind},
    router_wire,
};
use transport::{Channel, Clock, ConnectionFence, Error};

pub struct RouterReceiver<'a, C: Clock, F: Fn() -> ConnectionFence> {
    connection: Connection,
    runtime: &'a tokio::runtime::Runtime,
    clock: &'a C,
    fence: ConnectionFence,
    current: F,
}
pub struct Delivery<'s, 'a, C: Clock, F: Fn() -> ConnectionFence> {
    receiver: &'s mut RouterReceiver<'a, C, F>,
    send: SendStream,
    id: [u8; 16],
    payload: Vec<u8>,
    deadline: MonoInstant,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DispatchRefusal {
    PeerRotated,
    ReplayCollision,
    QueueFull,
    InvalidFrame,
}
impl DispatchRefusal {
    fn bytes(self) -> &'static [u8] {
        match self {
            Self::PeerRotated => b"peer_rotated",
            Self::ReplayCollision => b"replay_collision",
            Self::QueueFull => b"queue_full",
            Self::InvalidFrame => b"invalid_frame",
        }
    }
}
impl<'a, C: Clock, F: Fn() -> ConnectionFence> RouterReceiver<'a, C, F> {
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
        })
    }
    fn check(&self, deadline: MonoInstant) -> Result<(), Error> {
        if (self.current)() != self.fence {
            self.connection.close(0u32.into(), b"peer_rotated");
            return Err(Error::Generation);
        }
        allowance(self.clock, deadline)?;
        if tokio::runtime::Handle::try_current().is_ok() {
            return Err(Error::Unavailable);
        }
        Ok(())
    }
    /// At most one Delivery can be held: its mutable borrow applies backpressure
    /// until actual dispatch has completed or the stream has been rejected.
    pub fn receive(&mut self, deadline: MonoInstant) -> Result<Delivery<'_, 'a, C, F>, Error> {
        self.check(deadline)?;
        let wait = allowance(self.clock, deadline)?;
        let (send, bytes) = self.runtime.block_on(async {
            tokio::time::timeout(wait, async {
                let (send, mut recv) = self
                    .connection
                    .accept_bi()
                    .await
                    .map_err(|_| Error::Unavailable)?;
                let bytes = recv
                    .read_to_end(remote::REMOTE_MAX_FRAME_BYTES)
                    .await
                    .map_err(|_| Error::FrameTooLarge)?;
                Ok::<_, Error>((send, bytes))
            })
            .await
            .map_err(|_| Error::Expired)?
        })?;
        self.check(deadline)?;
        let frame = remote::decode_remote_frame(&bytes).map_err(|_| Error::Unavailable)?;
        if frame.kind != RemoteKind::Transfer
            || frame.payload.len() < remote::REMOTE_GENERATION_BYTES
        {
            return Err(Error::Unavailable);
        }
        let generation = u64::from_be_bytes(
            frame.payload[..remote::REMOTE_GENERATION_BYTES]
                .try_into()
                .map_err(|_| Error::Unavailable)?,
        );
        let payload = frame.payload[remote::REMOTE_GENERATION_BYTES..].to_vec();
        let mut delivery = Delivery {
            receiver: self,
            send,
            id: frame.message_id,
            payload,
            deadline,
        };
        // sidecar.rs:2840-2846 @ f4e366e9: generation refusal precedes frame validation.
        if generation != delivery.receiver.fence.remote_generation {
            delivery.respond(RemoteKind::Error, DispatchRefusal::PeerRotated.bytes())?;
            return Err(Error::Generation);
        }
        if delivery.payload.len() > remote::REMOTE_OPERATIONAL_FRAME_BYTES
            || router_wire::decode_frame(&delivery.payload).is_err()
        {
            delivery.respond(RemoteKind::Error, DispatchRefusal::InvalidFrame.bytes())?;
            return Err(Error::Unavailable);
        }
        Ok(delivery)
    }
    pub fn close(&mut self) {
        self.connection.close(0u32.into(), b"receiver_closed");
    }
}
impl<C: Clock, F: Fn() -> ConnectionFence> Delivery<'_, '_, C, F> {
    pub fn message_id(&self) -> [u8; 16] {
        self.id
    }
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }
    pub fn authenticated_peer(&self) -> ConnectionFence {
        self.receiver.fence
    }
    /// Consume only after the host's receipt/dedup owner confirms dispatch.
    pub fn acknowledge(mut self) -> Result<(), Error> {
        self.respond(RemoteKind::Ack, b"")
    }
    pub fn reject(mut self, why: DispatchRefusal) -> Result<(), Error> {
        self.respond(RemoteKind::Error, why.bytes())
    }
    fn respond(&mut self, kind: RemoteKind, payload: &[u8]) -> Result<(), Error> {
        self.receiver.check(self.deadline)?;
        let bytes = remote::encode_remote_frame(kind, self.id, payload)
            .map_err(|_| Error::FrameTooLarge)?;
        let wait = allowance(self.receiver.clock, self.deadline)?;
        self.receiver.runtime.block_on(async {
            tokio::time::timeout(wait, self.send.write_all(&bytes))
                .await
                .map_err(|_| Error::Expired)?
                .map_err(|_| Error::Unavailable)?;
            self.send.finish().map_err(|_| Error::Unavailable)?;
            // finish() only queues the tail and FIN; a current-thread runtime
            // must still send them before the receiver goes idle.
            flush_finished(&self.send).await.map_err(|_| Error::Expired)
        })?;
        self.receiver.check(self.deadline)
    }
}
