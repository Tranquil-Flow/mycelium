// SPDX-License-Identifier: AGPL-3.0-only
//! Signed composition of L9's raw NodeControl carrier. L9 deliberately leaves
//! signature, replay and command execution to the service, so every command and
//! reply here is a canonical signed object bound to the swarm run, generation and
//! a strictly increasing per-direction sequence. TLS admits an endpoint; the
//! pinned signing key (invitation / spawner pin) admits a command.
use crate::{iroh_frames::Clock, liveness::Watch, loopback::Result};
use budgets::{BudgetId as B, Entries};
use iroh::{EndpointAddr, EndpointId, endpoint::Connection};
use protocol::identity::Identity;
use runtime::{
    iroh_control::{ControlAuthority, ControlRequest, NodeControlClient},
    iroh_control_receive::NodeControlReceiver,
    iroh_host::LoopbackHost,
};
use serde_json::{Value, json};
use tokio::sync::watch;
use transport::{Channel, ConnectionFence};

pub const PROTOCOL: &str = "mycelium.slice.node_control.v1";
/// The v0.1.4 sidecar's `CONTROL_CALL_CONCURRENCY` (`sidecar.rs:100`) from the registry. The
/// composed driver happens to issue serial calls per peer; it does not narrow the limit.
pub fn call_slots() -> Result<Entries> {
    B::NodeControlCallConcurrency
        .count()
        .map_err(|e| format!("node control concurrency: {e:?}").into())
}

/// Run identity every post-registration frame must carry. A child learns it from
/// the owner's signed registration reply.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Ids {
    pub request_id: String,
    pub generation: u64,
}

pub struct Session {
    ids: Ids,
    bound: bool,
    peer_key: Value,
    in_seq: u64,
    out_seq: u64,
}
pub struct Frame {
    pub kind: String,
    pub seq: u64,
    pub reply_to: Option<u64>,
    pub body: Value,
}
impl Session {
    /// Owner side: run identity known, peer signing key pinned from the invitation
    /// (member) or the owner's own private leaf identity.
    pub fn owner(ids: Ids, peer_key: Value) -> Self {
        Self {
            ids,
            bound: true,
            peer_key,
            in_seq: 0,
            out_seq: 0,
        }
    }
    /// Child side: learns the run identity from the first signed owner reply.
    pub fn child(owner_key: Value) -> Self {
        Self {
            ids: Ids::default(),
            bound: false,
            peer_key: owner_key,
            in_seq: 0,
            out_seq: 0,
        }
    }
    pub fn ids(&self) -> &Ids {
        &self.ids
    }
    fn frame(
        &self,
        signer: &Identity,
        kind: &str,
        seq: u64,
        reply_to: Option<u64>,
        body: Value,
    ) -> Result<Vec<u8>> {
        let mut value = json!({"protocol":PROTOCOL,"kind":kind,"request_id":self.ids.request_id,
            "generation":self.ids.generation,"seq":seq,"reply_to":reply_to,"body":body});
        value["signature"] = signer.sign(&value)?;
        Ok(serde_json::to_vec(&value)?)
    }
    fn open(&self, bytes: &[u8]) -> Result<Frame> {
        let mut value = protocol::strict::object(bytes, api::requests::MAX_REQUEST_BODY)?;
        let signature = value
            .as_object_mut()
            .and_then(|m| m.remove("signature"))
            .ok_or("control_signature_missing")?;
        protocol::identity::verify(&value, &signature, &self.peer_key)?;
        if value["protocol"] != PROTOCOL {
            return Err("control_protocol".into());
        }
        let kind = value["kind"].as_str().ok_or("control_kind")?.to_owned();
        let carried = Ids {
            request_id: value["request_id"]
                .as_str()
                .ok_or("control_request_id")?
                .to_owned(),
            generation: value["generation"].as_u64().ok_or("control_generation")?,
        };
        // A child cannot know the run before it registers, so only the register
        // call may carry the empty identity. Everything else must match exactly.
        let registering = kind == "register" && carried == Ids::default();
        if !(registering || (self.bound && carried == self.ids)) {
            return Err("control_run_binding".into());
        }
        Ok(Frame {
            kind,
            seq: value["seq"].as_u64().ok_or("control_seq")?,
            reply_to: value["reply_to"].as_u64(),
            body: value["body"].clone(),
        })
    }
    /// Verify one inbound command (any kind) and consume its sequence number.
    pub fn accept_call(&mut self, bytes: &[u8]) -> Result<(u64, String, Value)> {
        let frame = self.open(bytes)?;
        if frame.reply_to.is_some() || frame.seq != self.in_seq {
            return Err("control_sequence".into());
        }
        self.in_seq = self
            .in_seq
            .checked_add(1)
            .ok_or("control_sequence_overflow")?;
        Ok((frame.seq, frame.kind, frame.body))
    }
    pub fn reply(&self, signer: &Identity, kind: &str, to: u64, body: Value) -> Result<Vec<u8>> {
        self.frame(signer, kind, to, Some(to), body)
    }
    pub fn call_frame(
        &mut self,
        signer: &Identity,
        kind: &str,
        body: Value,
    ) -> Result<(u64, Vec<u8>)> {
        let seq = self.out_seq;
        let bytes = self.frame(signer, kind, seq, None, body)?;
        self.out_seq = seq.checked_add(1).ok_or("control_sequence_overflow")?;
        Ok((seq, bytes))
    }
    pub fn accept_reply(&mut self, bytes: &[u8], seq: u64, kind: &str) -> Result<Value> {
        let value = protocol::strict::object(bytes, api::requests::MAX_REQUEST_BODY)?;
        if !self.bound && kind == "register" {
            // Only the signed reply can bind a child; verify before adopting.
            let mut copy = value.clone();
            let signature = copy
                .as_object_mut()
                .and_then(|m| m.remove("signature"))
                .ok_or("control_signature_missing")?;
            protocol::identity::verify(&copy, &signature, &self.peer_key)?;
            let request_id = copy["request_id"].as_str().ok_or("control_request_id")?;
            if request_id.is_empty() {
                return Err("control_request_id".into());
            }
            self.ids = Ids {
                request_id: request_id.to_owned(),
                generation: copy["generation"].as_u64().ok_or("control_generation")?,
            };
            self.bound = true;
        }
        let frame = self.open(bytes)?;
        if frame.kind != kind || frame.reply_to != Some(seq) || frame.seq != seq {
            return Err("control_reply_binding".into());
        }
        Ok(frame.body)
    }
}

fn fence_for(endpoint: &EndpointId, generation: u64) -> ConnectionFence {
    ConnectionFence {
        endpoint_id: *endpoint.as_bytes(),
        local_generation: generation,
        remote_generation: generation,
    }
}

/// Process-wide wiring for one signed control endpoint.
pub struct Wire<'a> {
    pub host: &'a LoopbackHost<'a>,
    pub clock: &'a Clock,
    pub runtime: &'a tokio::runtime::Runtime,
    pub signer: &'a Identity,
    /// The owner's worker-death watch (v0.1.4 `_watch`); children pass `None`.
    pub watch: Option<&'a Watch>,
}
/// One authenticated remote: the address we dial, its current-authority watch
/// (a revoked or rotated fence refuses calls), and the signed session state.
pub struct Peer {
    addr: EndpointAddr,
    fence: ConnectionFence,
    revoke: watch::Sender<Option<ConnectionFence>>,
    authority: ControlAuthority,
    pub session: Session,
}
impl Peer {
    pub fn new(addr: EndpointAddr, generation: u64, session: Session) -> Self {
        let fence = fence_for(&addr.id, generation);
        let (revoke, authority) = watch::channel(Some(fence));
        Self {
            addr,
            fence,
            revoke,
            authority,
            session,
        }
    }
    /// Caller cancellation of every in-flight and later call (L9: a `None` authority is
    /// "cancelled"), usable from another thread. This is how a worker's death releases the
    /// owner blocked on a reply that will never come.
    pub fn canceller(&self) -> impl Fn() + Send + Sync + 'static {
        let revoke = self.revoke.clone();
        move || {
            revoke.send_replace(None);
        }
    }
    pub fn id(&self) -> EndpointId {
        self.addr.id
    }
    pub fn call(&mut self, wire: &Wire<'_>, kind: &str, body: Value, budget: B) -> Result<Value> {
        let (seq, bytes) = self.session.call_frame(wire.signer, kind, body)?;
        let client = NodeControlClient::new(wire.host, wire.clock, call_slots()?)
            .map_err(|e| format!("node control client: {e}"))?;
        let reply = client
            .call(
                ControlRequest {
                    peer: &self.addr,
                    fence: self.fence,
                    command: &bytes,
                    deadline: wire.clock.deadline(budget)?,
                },
                &mut self.authority,
            )
            .map_err(|e| format!("node control {kind}: {e}"))?;
        self.session.accept_reply(&reply, seq, kind)
    }
}

pub enum Incoming {
    Control(Connection),
    Router(Connection),
    Artifact(Connection),
}
type Current = Box<dyn Fn() -> ConnectionFence>;
pub type ControlRx<'a> = NodeControlReceiver<'a, Clock, Current>;
impl<'a> Wire<'a> {
    /// Next admitted connection by ALPN. TLS only authenticated the endpoint:
    /// callers pin its identity before trusting anything on it.
    pub fn next(&self, wait: B) -> Result<Incoming> {
        self.alive()?;
        let accepted = self.host.accept(self.clock, self.clock.deadline(wait)?);
        // A worker's death wakes this accept with a throwaway connection: the death, not the
        // wake-up connection, is the outcome.
        self.alive()?;
        let connection = accepted.map_err(|e| format!("iroh accept: {e:?}"))?;
        let alpn = connection.alpn().to_vec();
        if alpn == Channel::NodeControl.alpn() {
            Ok(Incoming::Control(connection))
        } else if alpn == Channel::Router.alpn() {
            Ok(Incoming::Router(connection))
        } else if alpn == Channel::ArtifactChunk.alpn() {
            Ok(Incoming::Artifact(connection))
        } else {
            connection.close(1u32.into(), b"unexpected alpn");
            Err("iroh_unexpected_alpn".into())
        }
    }
    /// `Err(WorkerExited)` once a watched native worker has ended.
    pub fn alive(&self) -> Result<()> {
        self.watch.map_or(Ok(()), Watch::check)
    }
    /// Receiver bound to the connection's own authenticated endpoint; the caller
    /// separately pins that endpoint against a signed attestation.
    pub fn receiver(&self, connection: &Connection, generation: u64) -> Result<ControlRx<'a>> {
        let fence = fence_for(&connection.remote_id(), generation);
        NodeControlReceiver::new(
            connection.clone(),
            self.runtime,
            self.clock,
            fence,
            Box::new(move || fence) as Current,
        )
        .map_err(|e| format!("node control receiver: {e}").into())
    }
    /// A reply written is not a reply consumed: wait (bounded) for the caller to
    /// close its per-call connection before this side may close its endpoint.
    pub fn consumed(&self, connection: &Connection) -> bool {
        let wait = crate::service::duration(B::RouterClose);
        self.runtime.block_on(async {
            tokio::time::timeout(wait, connection.closed())
                .await
                .is_ok()
        })
    }
    /// Receive exactly one signed pull of `kind`, answer it from `handler` while
    /// the delivery is held (backpressure), then wait for the caller to consume.
    /// `remote` pins the caller's TLS endpoint once it is known.
    pub fn serve(
        &self,
        session: &mut Session,
        kind: &str,
        remote: Option<EndpointId>,
        wait: B,
        handler: impl FnOnce(&Value, EndpointId) -> Result<Value>,
    ) -> Result<(Value, EndpointId)> {
        let deadline = self.clock.deadline(wait)?;
        loop {
            let connection = match self.next(wait)? {
                Incoming::Control(c) => c,
                Incoming::Router(c) | Incoming::Artifact(c) => {
                    c.close(1u32.into(), b"unexpected carrier");
                    return Err("iroh_unexpected_carrier".into());
                }
            };
            let caller = connection.remote_id();
            if remote.is_some_and(|r| r != caller) {
                connection.close(2u32.into(), b"unpinned endpoint");
                continue;
            }
            let mut receiver = self.receiver(&connection, session.ids.generation)?;
            // The caller may die between connecting and sending: closing the connection
            // releases the receive below.
            let _release = self.watch.map(|w| close_on_death(w, &connection));
            let call = receiver.receive(deadline).map_err(|e| {
                self.alive()
                    .err()
                    .unwrap_or_else(|| format!("node control receive: {e}").into())
            })?;
            let (seq, got, body) = session.accept_call(call.bytes())?;
            if got != kind {
                return Err(format!("unexpected_control_kind:{got}").into());
            }
            let reply = handler(&body, caller)?;
            call.respond(&session.reply(self.signer, kind, seq, reply)?)
                .map_err(|e| format!("node control respond: {e}"))?;
            self.consumed(&connection);
            return Ok((body, caller));
        }
    }
}

/// Close `connection` when a watched worker ends, so a blocked read on it returns.
pub fn close_on_death<'w>(
    watch: &'w Watch,
    connection: &Connection,
) -> crate::liveness::Registration<'w> {
    let connection = connection.clone();
    watch.on_death(move || connection.close(9u32.into(), b"worker exited"))
}
/// Wake an owner blocked in `accept` when a worker ends. L9 does not expose its endpoint, so the
/// only way into that wait is to arrive at it: dial the owner's NodeControl ALPN from a
/// throwaway client endpoint. `Wire::next` then sees the recorded death instead of the caller.
pub fn dial_waker(owner: EndpointAddr, generation: u64) -> impl Fn() + Send + Sync + 'static {
    move || {
        // Detached and best effort: the dial and the throwaway endpoint's close must neither
        // delay the other wakers nor the job's own failure. The blocked wait keeps its own
        // registered deadline as the backstop.
        let owner = owner.clone();
        if let Err(error) = std::thread::Builder::new()
            .name("slice-worker-wake".into())
            .spawn(move || {
                if let Err(error) = dial_once(&owner, generation) {
                    eprintln!("worker wake dial: {error}");
                }
            })
        {
            eprintln!("worker wake thread not started: {error}");
        }
    }
}
fn dial_once(owner: &EndpointAddr, generation: u64) -> Result<()> {
    let runtime = crate::iroh_frames::runtime()?;
    let clock = Clock::new(generation);
    let mut host = crate::iroh_frames::bind(&runtime, &clock)?;
    let connection = host
        .connect_channel(
            owner,
            Channel::NodeControl,
            &clock,
            clock.deadline(B::RouterClose)?,
        )
        .map_err(|e| format!("wake dial: {e:?}"))?;
    connection.close(9u32.into(), b"worker exited");
    crate::iroh_frames::close(&mut host, &clock)
}
