// SPDX-License-Identifier: AGPL-3.0-only
//! Serial real-Iroh activation carrier. Control/consent remains a separate local
//! channel; Router ACK acknowledges bounded dispatch, never native cleanup.
use crate::{activation, loopback::Result};
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{EndpointAddr, endpoint::Connection};
use runtime::{
    iroh::IrohTransport,
    iroh_host::LoopbackHost,
    iroh_receive::{DispatchRefusal, RouterReceiver},
};
use serde_json::Value;
use std::time::Instant;
use transport::{Channel, Clock as _, ConnectionFence, Exchange, Transport as _};

pub struct Clock {
    start: Instant,
    generation: u64,
}
impl Clock {
    pub fn new(generation: u64) -> Self {
        Self {
            start: Instant::now(),
            generation,
        }
    }
    pub fn deadline(&self, id: B) -> Result<MonoInstant> {
        self.monotonic()
            .checked_add(
                id.span(Entries::ZERO)
                    .map_err(|e| format!("iroh budget: {e:?}"))?,
            )
            .map_err(|e| format!("iroh deadline: {e:?}").into())
    }
}
impl transport::Clock for Clock {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(
            ClockDomain(self.generation),
            u64::try_from(self.start.elapsed().as_millis()).unwrap_or(u64::MAX),
        )
    }
    fn wall_seconds(&self) -> f64 {
        0.
    }
}
pub fn runtime() -> Result<tokio::runtime::Runtime> {
    Ok(tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()?)
}
pub fn bind<'a>(rt: &'a tokio::runtime::Runtime, clock: &Clock) -> Result<LoopbackHost<'a>> {
    LoopbackHost::bind(rt, clock, clock.deadline(B::WorkerStartup)?)
        .map_err(|e| format!("iroh bind: {e:?}").into())
}
/// Independent transport identity, never reused as the membership signing key.
/// The existing descriptor-relative private store enforces no-follow, owner-only,
/// single-link storage and refuses corruption rather than rotating an old key.
pub fn endpoint_secret(private: &std::path::Path) -> Result<iroh::SecretKey> {
    let identity = crate::enrollment::identity(&private.join("iroh-endpoint"))?;
    let bytes = identity.secret_bytes();
    Ok(iroh::SecretKey::from_bytes(&bytes))
}
/// The owner serves signed NodeControl pulls and the signed artifact ALPN; it
/// dials children for Router data, so it does not need to accept Router.
pub const OWNER_CHANNELS: [Channel; 2] = [Channel::NodeControl, Channel::ArtifactChunk];
/// Children accept the owner's Router data connection and signed NodeControl
/// commands. They fetch artifacts as clients, so they never serve that ALPN.
pub const CHILD_CHANNELS: [Channel; 2] = [Channel::Router, Channel::NodeControl];
pub fn bind_persisted<'a>(
    rt: &'a tokio::runtime::Runtime,
    clock: &Clock,
    private: &std::path::Path,
    channels: &[Channel],
) -> Result<LoopbackHost<'a>> {
    LoopbackHost::bind_protocols(
        rt,
        clock,
        clock.deadline(B::WorkerStartup)?,
        endpoint_secret(private)?,
        channels,
    )
    .map_err(|e| format!("persistent Iroh bind: {e:?}").into())
}
pub fn address(host: &LoopbackHost<'_>) -> Result<Value> {
    let a = host.address().map_err(|e| format!("iroh address: {e:?}"))?;
    Ok(
        serde_json::json!({"id":a.id.to_string(),"addresses":a.ip_addrs().map(ToString::to_string).collect::<Vec<_>>()}),
    )
}
pub fn peer(value: &Value) -> Result<EndpointAddr> {
    let mut a = EndpointAddr::new(value["id"].as_str().ok_or("iroh_peer_id")?.parse()?);
    for addr in value["addresses"].as_array().ok_or("iroh_addresses")? {
        let addr: std::net::SocketAddr = addr.as_str().ok_or("iroh_address")?.parse()?;
        if addr.ip() != std::net::Ipv4Addr::LOCALHOST || addr.port() == 0 {
            return Err("iroh_nonlocal_address".into());
        }
        a = a.with_ip_addr(addr);
    }
    if a.addrs.is_empty() {
        return Err("iroh_addresses_empty".into());
    }
    Ok(a)
}
type Current = Box<dyn Fn() -> ConnectionFence>;
pub struct Frames<'a> {
    send: IrohTransport<'a, Clock, Current>,
    receive: RouterReceiver<'a, Clock, Current>,
    clock: &'a Clock,
    fence: ConnectionFence,
    sent: u128,
    received: u128,
    deadline: MonoInstant,
}
impl<'a> Frames<'a> {
    pub fn new(
        connection: Connection,
        rt: &'a tokio::runtime::Runtime,
        clock: &'a Clock,
        expected: &EndpointAddr,
        generation: u64,
    ) -> Result<Self> {
        let fence = ConnectionFence {
            endpoint_id: *expected.id.as_bytes(),
            local_generation: generation,
            remote_generation: generation,
        };
        Ok(Self {
            send: IrohTransport::new(
                connection.clone(),
                rt,
                clock,
                fence,
                Box::new(move || fence) as Current,
            )
            .map_err(|e| format!("iroh sender: {e:?}"))?,
            receive: RouterReceiver::new(
                connection,
                rt,
                clock,
                fence,
                Box::new(move || fence) as Current,
            )
            .map_err(|e| format!("iroh receiver: {e:?}"))?,
            clock,
            fence,
            sent: 0,
            received: 0,
            deadline: clock.deadline(B::MemberChosenWall)?,
        })
    }
    pub fn send(&mut self, bytes: &[u8]) -> Result<()> {
        let request = Exchange {
            peer: self.fence,
            channel: Channel::Router,
            message_id: self.sent.to_be_bytes(),
            payload: bytes.to_vec(),
            deadline: self.deadline,
            maximum_response_bytes: protocol::remote::REMOTE_MAX_FRAME_BYTES,
        };
        transport::exchange(&mut self.send, &request, self.clock)
            .map_err(|e| format!("iroh activation send: {e:?}"))?;
        self.sent = self.sent.checked_add(1).ok_or("iroh_sequence_overflow")?;
        Ok(())
    }
    pub fn receive(&mut self, expected_header: &Value) -> Result<Vec<u8>> {
        let delivery = self
            .receive
            .receive(self.deadline)
            .map_err(|e| format!("iroh activation receive: {e:?}"))?;
        if delivery.message_id() != self.received.to_be_bytes() {
            delivery
                .reject(DispatchRefusal::ReplayCollision)
                .map_err(|e| format!("iroh replay refusal: {e:?}"))?;
            return Err("iroh_activation_sequence".into());
        }
        if activation::unframe(delivery.payload(), expected_header).is_err() {
            delivery
                .reject(DispatchRefusal::InvalidFrame)
                .map_err(|e| format!("iroh frame refusal: {e:?}"))?;
            return Err("iroh_activation_binding".into());
        }
        let bytes = delivery.payload().to_vec();
        // This single owned slot is the dispatch target. No retry or concurrent
        // native work can observe it twice; native success has a separate receipt.
        delivery
            .acknowledge()
            .map_err(|e| format!("iroh dispatch ack: {e:?}"))?;
        self.received = self
            .received
            .checked_add(1)
            .ok_or("iroh_sequence_overflow")?;
        Ok(bytes)
    }
    pub fn close(mut self) -> Result<()> {
        self.receive.close();
        self.send
            .close(self.clock.deadline(B::RouterClose)?)
            .map_err(|e| format!("iroh carrier close: {e:?}").into())
    }
}
pub fn close(host: &mut LoopbackHost<'_>, clock: &Clock) -> Result<()> {
    host.close(clock, clock.deadline(B::RouterClose)?)
        .map_err(|e| format!("iroh endpoint close: {e:?}").into())
}
