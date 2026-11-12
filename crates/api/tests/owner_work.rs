// SPDX-License-Identifier: AGPL-3.0-only
//! PH3b S4a: the owner's work port over `api::joined_owner` (gap G4a).
//!
//! v0.1.4 call chain (f4e366e9): `DemoApp` (app.py:505) -> `_action` start (app.py:1362) ->
//! `demo_app.action` (demo_app.py:109-133) -> `OwnerRun` steps publish/targets/activate
//! (owner_run.py:30-40) -> `demo_chain.py:890-985` -> `chain_compile.start_serving`
//! (chain_compile.py:308-341) -> `workflow.run` (workflow.py:277-345); serving loop
//! (owner_run.py:252-257) -> `rollover_due` / `ceiling_reached` (demo_chain.py:1125-1164);
//! `status()` monitor (owner_run.py:171-185) -> demo_chain.py:1166-1186, lease-expired member
//! (demo_chain.py:532-546, control_session.py:315-326).
//!
//! Model-free: a real TLS seed with real pairing and standing consent, the real artifact
//! carrier over iroh loopback, and a TEST MEMBER behind the owner's `MemberLink` that runs the
//! member's real receive (`JoinedShard` + `ChunkSession`). The test member has no native worker:
//! it refuses `prepare_native` by name and never fabricates a resident proof. Clocks are injected.
use api::{
    app_host,
    compiler_inputs::Error,
    handoff,
    joined_owner::{
        link::{MemberLink, Refusal, Stage},
        work::{self, Admission, Clocks, OwnerWork, Plan, RouteOrder, WorkMember},
    },
    joined_shard::{
        self, JoinedShard, route,
        serve::{self, StageCompute},
    },
    m16_route::Codec,
    member::Session,
    peer,
    preferences::{resource_policy, standing_consent},
    schema::PublicError,
};
use budgets::{BudgetId as B, Entries, Span};
use owner_run::{
    Clock, Identity as RunIdentity, OwnerRun, Reason, StepOutcome,
    chain::{PortError, StepError},
    machine::{CeilingProbe, MonitorProbe},
    state::RunState,
    steps::{activate::ActivationPort, publish::WorkPort, slices::SlicesPort},
};
use protocol::{artifact_grant::ChunkRequest, identity::Identity};
use runtime::{
    iroh_artifact::ChunkSession, iroh_host::LoopbackHost, sidecar_artifact::serve_limits,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use transport::{Channel, Clock as _};

static NEXT: AtomicU64 = AtomicU64::new(0);

fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn seconds(id: B) -> f64 {
    id.span(Entries::ZERO).unwrap().millis() as f64 / 1000.0
}

/// What a test member saw and received.
#[derive(Default)]
struct Seen {
    commands: Vec<String>,
    received: Option<Vec<u8>>,
    /// Owner cancel requests that found an admitted operation.
    cancels: usize,
    /// When the member was asked to discard its stage.
    discard_at: Option<Instant>,
    /// A serving member's controlled stage compute, in order: (`step`, session position),
    /// (`reset_attempt`, 0), (`reset`, 0), (`close`, 0), (`abort`, 0), then its
    /// (`discard`, 0) answer.
    compute: Vec<(&'static str, u64)>,
    /// The member's own serve outcome (its stage server's report or refusal).
    served: Option<Result<Value, String>>,
}
/// A scripted answer of a test member to one command (by name).
#[derive(Clone, Copy, PartialEq, Debug)]
enum Act {
    /// Wait (custody already taken for `receive`) until the owner cancels or the test releases.
    Hold,
    /// Never answer (a wedged member).
    Silent,
}
/// The member dispatcher's rules (examples/shard_member/control.rs): at most one admitted
/// operation, each with its own cancel flag set only while it is admitted; `holding` names the
/// held command; `released` ends every hold (the test's guard on any exit).
#[derive(Default)]
struct Ops {
    active: Option<Arc<AtomicBool>>,
    holding: Option<String>,
    released: bool,
}
#[derive(Default)]
struct Control {
    ops: Mutex<Ops>,
    changed: Condvar,
}
impl Control {
    fn lock(&self) -> std::sync::MutexGuard<'_, Ops> {
        self.ops.lock().unwrap_or_else(|p| p.into_inner())
    }
    /// `cancel_operation` of the admitted operation (none admitted: nothing happens).
    fn cancel(&self, seen: &Mutex<Seen>) {
        let ops = self.lock();
        if let Some(flag) = &ops.active {
            flag.store(true, Ordering::SeqCst);
            seen.lock().unwrap_or_else(|p| p.into_inner()).cancels += 1;
        }
        drop(ops);
        self.changed.notify_all();
    }
    fn release(&self) {
        self.lock().released = true;
        self.changed.notify_all();
    }
    /// Hold `name` until cancelled (true) or released (false).
    fn hold(&self, name: &str, cancelled: &AtomicBool) -> bool {
        let mut ops = self.lock();
        ops.holding = Some(name.to_owned());
        self.changed.notify_all();
        loop {
            if cancelled.load(Ordering::SeqCst) || ops.released {
                ops.holding = None;
                return cancelled.load(Ordering::SeqCst);
            }
            ops = self.changed.wait(ops).unwrap_or_else(|p| p.into_inner());
        }
    }
    /// Record a compute event under the dispatcher lock (no lost wake-up for [`Control::wait`]).
    fn record(&self, seen: &Mutex<Seen>, event: (&'static str, u64)) {
        let ops = self.lock();
        seen.lock()
            .unwrap_or_else(|p| p.into_inner())
            .compute
            .push(event);
        drop(ops);
        self.changed.notify_all();
    }
    /// Publish a serve outcome under the same lock as `wait`, then wake its observer.
    fn served(&self, seen: &Mutex<Seen>, report: Result<Value, String>) {
        let ops = self.lock();
        seen.lock().unwrap_or_else(|p| p.into_inner()).served = Some(report);
        drop(ops);
        self.changed.notify_all();
    }
    /// Wait (bounded) until `done` holds for what the member saw.
    fn wait(&self, seen: &Mutex<Seen>, done: impl Fn(&Seen) -> bool, within: Duration) -> bool {
        let end = Instant::now() + within;
        let mut ops = self.lock();
        loop {
            if done(&seen.lock().unwrap_or_else(|p| p.into_inner())) {
                return true;
            }
            let left = end.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return false;
            }
            ops = self
                .changed
                .wait_timeout(ops, left)
                .unwrap_or_else(|p| p.into_inner())
                .0;
        }
    }
    /// Wait (bounded) until the member holds `name`.
    fn holding(&self, name: &str, within: Duration) -> bool {
        let end = Instant::now() + within;
        let mut ops = self.lock();
        while ops.holding.as_deref() != Some(name) {
            let left = end.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return false;
            }
            ops = self
                .changed
                .wait_timeout(ops, left)
                .unwrap_or_else(|p| p.into_inner())
                .0;
        }
        true
    }
}
/// Releases every hold of a member when the test ends (also on an assertion failure).
struct Release(Arc<Control>);
impl Drop for Release {
    fn drop(&mut self) {
        self.0.release();
    }
}
/// TEST MEMBER (test double, not product code): a joined member behind the owner's link that
/// answers `consent_status` with its own consent and policy, runs the member's real shard
/// receive (custody first, as the member) and discards what it holds; it has no native worker, so
/// `prepare_native` is refused by name. Its own thread answers in order, like the member process.
struct TestMember {
    root: PathBuf,
    signer: Identity,
    secret: iroh::SecretKey,
    session: Session,
    seen: Arc<Mutex<Seen>>,
    shard: Option<JoinedShard>,
    script: BTreeMap<&'static str, Act>,
    control: Arc<Control>,
    /// A SERVING test member (controlled admission): its stage script; `None` refuses
    /// `prepare_native` by name, as before.
    serving: Option<Serving>,
    /// The offer it received and the controlled document it answered `prepare_native` with.
    offer: Option<Value>,
    proof: Option<Value>,
}
const NO_NATIVE: &str = "test_member_native_unavailable";
const CANCELLED: &str = "member_operation_cancelled";
impl TestMember {
    fn carrier(&self) -> Identity {
        Identity::from_secret(
            &self.secret.public().to_string(),
            &self.signer.secret_bytes(),
        )
        .unwrap()
    }
    /// A scripted hold at this point of `name`: Err(cancelled) when the owner cancelled it.
    fn pause(&self, name: &str, cancelled: &AtomicBool) -> Result<(), String> {
        if self.script.get(name) == Some(&Act::Hold) && self.control.hold(name, cancelled) {
            return Err(CANCELLED.into());
        }
        Ok(())
    }
    fn answer(&mut self, command: &Value, cancelled: &AtomicBool) -> Result<Value, Refusal> {
        let refused = |code: &str| Refusal(code.into());
        match command["command"].as_str() {
            Some("consent_status") => Ok(json!({
                "consent": standing_consent::load(&self.root).map_err(|e| refused(e.0))?,
                "policy": resource_policy::read(&self.root).map_err(|e| refused(e.code))?})),
            Some("receive") => self.receive(command, cancelled).map_err(|e| refused(&e)),
            Some("prepare_native") => {
                self.pause("prepare_native", cancelled)
                    .map_err(|e| refused(&e))?;
                if self.serving.is_none() || self.offer.is_none() {
                    return Err(refused(NO_NATIVE));
                }
                // CONTROLLED, not a resident proof: only the test's controlled admission
                // reads it; the product's proof verifier never sees it.
                let proof =
                    json!({"controlled_proof":self.session.node_id(),"worker_pid":CONTROLLED_PID});
                self.proof = Some(proof.clone());
                Ok(proof)
            }
            Some("discard") => {
                self.seen
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .discard_at = Some(Instant::now());
                self.control.record(&self.seen, ("discard", 0));
                Ok(match self.shard.take() {
                    Some(mut shard) => {
                        shard.discard().map_err(|e| refused(e.0))?;
                        json!({"inert_bytes_discarded":true,"route_ready":false})
                    }
                    None => {
                        json!({"inert_bytes_discarded":false,"nothing_owned":true,"route_ready":false})
                    }
                })
            }
            _ => Err(refused("test_member_command_unexpected")),
        }
    }
    /// The member's real receive: custody first (kept on any failure), then every chunk over
    /// the carrier, then verify.
    fn receive(&mut self, command: &Value, cancelled: &AtomicBool) -> Result<Value, String> {
        if self.shard.is_some() {
            return Err("shard_already_owned".into());
        }
        let offer = &command["offer"];
        self.offer = Some(offer.clone());
        let carrier = self.carrier();
        let mut shard = JoinedShard::begin(
            &self.root,
            &self.session,
            &self.signer,
            &carrier,
            offer,
            app_host::now(),
        )
        .map_err(|e| e.0.to_owned())?;
        let received = self
            .pause("receive", cancelled)
            .and_then(|()| self.fetch(command, &mut shard, &carrier));
        self.shard = Some(shard);
        received
    }
    fn fetch(
        &self,
        command: &Value,
        shard: &mut JoinedShard,
        carrier: &Identity,
    ) -> Result<Value, String> {
        let offer = &command["offer"];
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        let clock = api::managed_carrier::HostClock::default();
        let deadline = || {
            clock
                .monotonic()
                .checked_add(B::WorkerStartup.span(Entries::ZERO).unwrap())
                .unwrap()
        };
        let mut host = LoopbackHost::bind_with_secret(&rt, &clock, deadline(), self.secret.clone())
            .map_err(|e| format!("{e:?}"))?;
        let source: iroh::EndpointId = command["source_id"].as_str().unwrap().parse().unwrap();
        let socket: std::net::SocketAddr =
            serde_json::from_value(command["source_socket"].clone()).map_err(|e| e.to_string())?;
        let connection = host
            .connect_channel(
                &iroh::EndpointAddr::new(source).with_ip_addr(socket),
                Channel::ArtifactChunk,
                &clock,
                deadline(),
            )
            .map_err(|e| format!("{e:?}"))?;
        let mut link = ChunkSession::new(
            connection,
            &rt,
            &clock,
            self.secret.public(),
            serve_limits().unwrap().wire,
        )
        .map_err(|e| e.0)?;
        let size = offer["message"]["transfer"]["size_bytes"].as_u64().unwrap();
        let mut offset = 0;
        while offset < size {
            let length = protocol::artifact_grant::MAX_CHUNK_BYTES.min(size - offset);
            let request = ChunkRequest {
                request_id: format!("chunk-{offset}"),
                source_endpoint_id: source.to_string(),
                object_digest: offer["message"]["transfer"]["content_digest"]
                    .as_str()
                    .unwrap()
                    .into(),
                offset_bytes: offset,
                length_bytes: length,
            };
            shard
                .fetch_chunk_checked(
                    &mut link,
                    &request,
                    &self.session,
                    &self.signer,
                    carrier,
                    &app_host::now,
                    deadline(),
                    &|| Ok(()),
                )
                .map_err(|e| e.0.to_owned())?;
            offset += length;
        }
        link.finish();
        shard
            .verify(&self.session, &self.signer, carrier, &app_host::now)
            .map_err(|e| e.0.to_owned())?;
        let ops = self.control.lock();
        self.seen.lock().unwrap_or_else(|p| p.into_inner()).received =
            Some(fs::read(shard.path()).unwrap());
        drop(ops);
        self.control.changed.notify_all();
        host.close(&clock, deadline())
            .map_err(|e| format!("{e:?}"))?;
        drop(host);
        Ok(json!({"received_bytes":size,"route_ready":false}))
    }
    /// The member process: one admitted operation at a time, answered in order. A serving
    /// member binds its route carrier (with its registered carrier key) at `carrier_address`
    /// and runs the member's REAL stage server (`joined_shard::serve`) over a controlled compute.
    fn serve(
        mut self,
        inbox: mpsc::Receiver<(Value, Arc<AtomicBool>)>,
        replies: mpsc::Sender<Result<Value, Refusal>>,
    ) {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let clock = api::managed_carrier::HostClock::default();
        let until = |id: B| {
            clock
                .monotonic()
                .checked_add(id.span(Entries::ZERO).unwrap())
                .unwrap()
        };
        let mut host: Option<LoopbackHost<'_>> = None;
        while let Ok((command, cancelled)) = inbox.recv() {
            let name = command["command"].as_str().unwrap_or_default().to_owned();
            let reply = match name.as_str() {
                "carrier_address" => {
                    if host.is_none() {
                        host = Some(
                            LoopbackHost::bind_with_secret(
                                &rt,
                                &clock,
                                until(B::WorkerStartup),
                                self.secret.clone(),
                            )
                            .unwrap(),
                        );
                    }
                    let addr = host.as_ref().unwrap().address().unwrap();
                    Ok(json!({"endpoint_id":addr.id.to_string(),
                        "socket":addr.ip_addrs().next().unwrap()}))
                }
                "serve" => {
                    let served = self.serve_route(host.as_ref(), &rt, &clock, &command, cancelled);
                    if let Some(mut h) = host.take() {
                        h.close(&clock, until(B::RouterClose)).unwrap();
                    }
                    served
                }
                _ => self.answer(&command, &cancelled),
            };
            if self.script.get(name.as_str()) == Some(&Act::Silent) {
                continue;
            }
            self.control.lock().active = None;
            if replies.send(reply).is_err() {
                break;
            }
        }
    }
    /// `serve`: the member's real stage server over its controlled compute until the epoch ends.
    fn serve_route(
        &mut self,
        host: Option<&LoopbackHost<'_>>,
        rt: &tokio::runtime::Runtime,
        clock: &api::managed_carrier::HostClock,
        command: &Value,
        cancelled: Arc<AtomicBool>,
    ) -> Result<Value, Refusal> {
        let (Some(host), Some(offer), Some(proof), Some(script)) = (
            host,
            self.offer.clone(),
            self.proof.clone(),
            self.serving.as_mut(),
        ) else {
            return Err(Refusal("test_member_not_prepared".into()));
        };
        let serve_for = script.serve_for.unwrap_or(span(B::MemberChosenWall));
        if script.old_member {
            let row = self
                .session
                .shard_identity(&self.signer, app_host::now())
                .map_err(|_| Refusal("test_member_invalid".into()))?;
            let checked = route::verify_member(
                &command["route"],
                &row,
                &offer,
                &protocol::canonical::digest(&proof).unwrap(),
                app_host::now(),
            );
            let report = checked.map(|_| json!({})).map_err(|e| e.0.to_owned());
            self.control.served(&self.seen, report.clone());
            return report.map_err(Refusal);
        }
        let mut compute = Controlled {
            offer,
            owned: true,
            position: 0,
            steps: 0,
            script,
            seen: self.seen.clone(),
            control: self.control.clone(),
        };
        let (session, signer) = (&self.session, &self.signer);
        let live = || {
            session
                .shard_identity(signer, app_host::now())
                .map_err(|_| Error("test_member_invalid"))
        };
        let report = serve::serve(
            &serve::Serve {
                host,
                runtime: rt,
                clock,
                current: &live,
                cancelled,
                deadline: Instant::now() + serve_for,
            },
            &mut compute,
            &command["route"],
            &protocol::canonical::digest(&proof).unwrap(),
        );
        self.control.served(&self.seen, report.clone());
        report.map_err(Refusal)
    }
}
/// The (fake) worker pid a serving test member's controlled document names.
const CONTROLLED_PID: u32 = 4242;
/// One serving test member's stage script.
#[derive(Default)]
struct Serving {
    /// The final stage: emits these token ids by session position (else activations).
    tokens: Vec<u32>,
    /// Every session reset fails (the compute cannot release its state).
    reset_fails: bool,
    /// The step with this global index signals `.1`, then waits for `.2` (a chat in flight).
    hold: Option<(u64, mpsc::Sender<()>, mpsc::Receiver<()>)>,
    /// The member's own serve deadline (its consent wall ending while the epoch is idle);
    /// default: the member's chosen wall.
    serve_for: Option<Duration>,
    /// A member of a build before EP1: its serve runs only the v1 route check
    /// (`route::verify_member`, unchanged) and answers its refusal.
    old_member: bool,
}
const VOCAB: usize = 64;
/// Controlled final-stage logits: argmax at `token` (MYAC header of the native link).
fn logits(token: u32) -> Vec<u8> {
    let mut out = b"MYAC\x01\x03\x03\x01".to_vec();
    out.extend(((VOCAB * 4) as u64).to_be_bytes());
    for d in [1u32, 1, VOCAB as u32] {
        out.extend(d.to_be_bytes());
    }
    for i in 0..VOCAB {
        out.extend((if i as u32 == token { 1.0f32 } else { 0.0f32 }).to_le_bytes());
    }
    out
}
/// CONTROLLED stage compute of a serving test member (test double): scripted activation and
/// logit bytes, never model output; every call is recorded in order.
struct Controlled<'a> {
    offer: Value,
    owned: bool,
    /// Steps since the last reset (0 = PREFILL), and since the serve began.
    position: u64,
    steps: u64,
    script: &'a mut Serving,
    seen: Arc<Mutex<Seen>>,
    control: Arc<Control>,
}
impl Controlled<'_> {
    fn log(&self, event: (&'static str, u64)) {
        self.control.record(&self.seen, event);
    }
}
impl StageCompute for Controlled<'_> {
    fn offer(&self) -> &Value {
        &self.offer
    }
    /// EPF's per-pass liveness (test double): the member's membership is current.
    fn live(&self, current: serve::Current<'_>) -> Result<(), Error> {
        current().map(|_| ())
    }
    fn worker_pid(&self) -> Option<u32> {
        self.owned.then_some(CONTROLLED_PID)
    }
    fn step(
        &mut self,
        _: Option<&[u32]>,
        _: &[u8],
        current: serve::Current<'_>,
        cancel: Arc<AtomicBool>,
        _: Instant,
    ) -> Result<Vec<u8>, Error> {
        current()?;
        if cancel.load(Ordering::SeqCst) {
            return Err(Error("compiler_stopped"));
        }
        let (t, position) = (self.steps, self.position);
        self.steps += 1;
        self.position += 1;
        self.log(("step", position));
        if let Some((_, entered, open)) = self.script.hold.take_if(|h| h.0 == t) {
            entered.send(()).unwrap();
            if let Err(e) = open.recv_timeout(span(B::WorkerStartup)) {
                eprintln!("held step gate: {e}");
            }
        }
        if self.script.tokens.is_empty() {
            return Ok(vec![t as u8; 16]);
        }
        Ok(logits(
            self.script.tokens[position as usize % self.script.tokens.len()],
        ))
    }
    fn close(&mut self) -> Result<Value, Error> {
        self.owned = false;
        self.log(("close", 0));
        Ok(json!({"context_closed":true}))
    }
    fn abort(&mut self) -> Result<Value, Error> {
        self.owned = false;
        self.log(("abort", 0));
        Ok(json!({"context_closed":false,"worker_reaped":true}))
    }
    fn reset(&mut self, current: serve::Current<'_>) -> Result<Value, Error> {
        current()?;
        self.log(("reset_attempt", 0));
        if self.script.reset_fails {
            return Err(Error("native_session_reset_invalid"));
        }
        self.position = 0;
        self.log(("reset", 0));
        Ok(json!({"controlled_reset":true}))
    }
}
/// The owner's link to a test member (as `RemoteMember`: a busy member refuses at once, a reply
/// not in time is `member_reply_timeout`).
struct TestLink {
    commands: Option<mpsc::Sender<(Value, Arc<AtomicBool>)>>,
    busy: mpsc::Sender<Result<Value, Refusal>>,
    replies: mpsc::Receiver<Result<Value, Refusal>>,
    control: Arc<Control>,
    seen: Arc<Mutex<Seen>>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl TestLink {
    fn start(member: TestMember) -> Self {
        let (commands, inbox) = mpsc::channel();
        let (busy, replies) = mpsc::channel();
        let answers = busy.clone();
        let (control, seen) = (member.control.clone(), member.seen.clone());
        let thread = std::thread::spawn(move || member.serve(inbox, answers));
        Self {
            commands: Some(commands),
            busy,
            replies,
            control,
            seen,
            thread: Some(thread),
        }
    }
    /// The owner's `cancel_operation` for whatever operation is admitted.
    fn canceller(&self) -> work::Cancel {
        let (control, seen) = (self.control.clone(), self.seen.clone());
        Arc::new(move || control.cancel(&seen))
    }
}
impl MemberLink for TestLink {
    fn begin(&mut self, command: Value) -> Result<(), Refusal> {
        self.seen
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .commands
            .push(command["command"].as_str().unwrap_or_default().to_owned());
        let mut ops = self.control.lock();
        if ops.active.is_some() {
            drop(ops);
            return self
                .busy
                .send(Err(Refusal("operator_busy".into())))
                .map_err(|_| Refusal("member_control_closed".into()));
        }
        let cancelled = Arc::new(AtomicBool::new(false));
        ops.active = Some(cancelled.clone());
        drop(ops);
        self.commands
            .as_ref()
            .ok_or_else(|| Refusal("member_control_closed".into()))?
            .send((command, cancelled))
            .map_err(|_| Refusal("member_control_closed".into()))
    }
    fn finish(&mut self, within: Duration) -> Result<Value, Refusal> {
        self.replies
            .recv_timeout(within)
            .map_err(|_| Refusal("member_reply_timeout".into()))?
    }
}
impl Drop for TestLink {
    fn drop(&mut self) {
        self.control.release();
        self.commands.take();
        if let Some(t) = self.thread.take()
            && t.join().is_err()
        {
            eprintln!("test member thread panicked");
        }
    }
}

/// Model-free codec: the epoch is never opened by these tests.
struct NoCodec;
impl Codec for NoCodec {
    fn encode(&self, _: &str) -> Result<Vec<u32>, PublicError> {
        Err(PublicError::new(409, "test_codec_unused"))
    }
    fn decode(&self, _: u32) -> Result<String, PublicError> {
        Err(PublicError::new(409, "test_codec_unused"))
    }
}

/// What the test keeps of a handed-over member: what it saw, its expected bytes, its dispatcher.
type Watched = (Arc<Mutex<Seen>>, Vec<u8>, Arc<Control>);
struct Joined {
    node_id: String,
    member: TestMember,
    registration: Value,
    expected: Vec<u8>,
    seen: Arc<Mutex<Seen>>,
}
/// A real owner seed and `n` members paired, joined, renewed, consented and bound.
struct Swarm {
    root: PathBuf,
    owner: PathBuf,
    seed: app_host::SeedRuntime<app_host::Unmanaged>,
    members: Vec<Joined>,
    document: Value,
    objects: PathBuf,
}
impl Drop for Swarm {
    fn drop(&mut self) {
        self.seed.close();
        if let Err(e) = fs::remove_dir_all(&self.root) {
            eprintln!("swarm root cleanup: {e}");
        }
    }
}
fn swarm(n: usize) -> Swarm {
    let root = std::env::temp_dir().join(format!(
        "s4a-work-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir(&root).unwrap();
    let root = fs::canonicalize(root).unwrap();
    let owner = root.join("owner");
    fs::create_dir(&owner).unwrap();
    let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "s4a-swarm", None).unwrap();
    let (seed_signer, _, _) = app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/source-slices.json"
    ))
    .unwrap();
    let case = &fixture["cases"][0];
    let objects = root.join("objects");
    fs::create_dir(&objects).unwrap();
    let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
    resource_policy::write(&owner, &policy).unwrap();
    let mut members = Vec::new();
    let (mut files, mut plans) = (serde_json::Map::new(), serde_json::Map::new());
    for i in 0..n {
        let member_root = root.join(format!("member{i}"));
        fs::create_dir(&member_root).unwrap();
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let issuer = handoff::Coordinator {
            signer: Identity::from_secret(seed_signer.endpoint_id(), &seed_signer.secret_bytes())
                .unwrap(),
            swarm_id: "s4a-swarm".into(),
            seed_url: seed.seed_url.clone(),
        };
        let ttl = seconds(B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37) as u64;
        let approved = handoff::approve_pairing(&owner, true, Some(&issuer), &request.to_value(), &json!(ttl),
            &json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}),
            app_host::now(), &handoff::Entropy::os).unwrap();
        let options = seed.options();
        let random = |n| app_host::hex(&app_host::random(n).unwrap());
        let env = peer::JoinEnv {
            signer: &signer,
            clock: &app_host::now,
            random_hex: &random,
            seed: &options,
        };
        let mut accepted =
            peer::accept_handoff(&member_root, true, &approved.to_value(), &env).unwrap();
        peer::renew(&member_root, true, accepted.session.as_mut(), &env).unwrap();
        let session = accepted.session.unwrap();
        resource_policy::write(&member_root, &policy).unwrap();
        standing_consent::grant(&member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
        let binding = session.binding().unwrap();
        standing_consent::bind(&member_root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).unwrap();
        let seen = Arc::new(Mutex::new(Seen::default()));
        let member = TestMember {
            root: member_root,
            signer,
            secret: iroh::SecretKey::generate(),
            session: session.clone(),
            seen: seen.clone(),
            shard: None,
            script: BTreeMap::new(),
            control: Arc::new(Control::default()),
            serving: None,
            offer: None,
            proof: None,
        };
        let registration =
            joined_shard::register(&session, &member.signer, &member.carrier(), app_host::now())
                .unwrap();
        // The compiler's rows for fixture node i, bound to this member.
        let node_id = session.node_id().to_owned();
        let fixture_node = format!("node-{i}");
        let mut recipe = case["node_plans"][&fixture_node].clone();
        recipe["node_id"] = json!(node_id);
        recipe.as_object_mut().unwrap().remove("node_plan_digest");
        recipe["node_plan_digest"] = json!(protocol::canonical::digest(&recipe).unwrap());
        let file = case["node_files"][&fixture_node][0].clone();
        let name = file["path"]
            .as_str()
            .unwrap()
            .strip_prefix("deployment/")
            .unwrap();
        let encoded = case["shard_files"][name].as_str().unwrap();
        let bytes: Vec<u8> = (0..encoded.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
            .collect();
        let hex = file["content_digest"]
            .as_str()
            .unwrap()
            .strip_prefix("sha256:")
            .unwrap();
        fs::write(objects.join(hex), &bytes).unwrap();
        files.insert(node_id.clone(), json!([file]));
        plans.insert(node_id.clone(), recipe);
        members.push(Joined {
            node_id,
            member,
            registration,
            expected: bytes,
            seen,
        });
    }
    Swarm {
        root,
        owner,
        seed,
        members,
        document: json!({"node_files":files,"node_slice_plans":plans}),
        objects,
    }
}
impl Swarm {
    fn plan(&self, wall: u64) -> Plan {
        let ids: Vec<&String> = self.members.iter().map(|m| &m.node_id).collect();
        Plan {
            run_id: "s4a-run".into(),
            deployment_id: "12345678-1234-5678-9234-abcdefabcdef".into(),
            model: json!({"model_id":"Qwen/Qwen3-0.6B","resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca","manifest_digest":"sha256:00"}),
            document: self.document.clone(),
            objects: self.objects.clone(),
            journal: self.root.join("journal"),
            ranges: ids
                .iter()
                .enumerate()
                .map(|(i, id)| ((*id).clone(), [14 * i as u64, 14 * (i as u64 + 1)]))
                .collect(),
            tokens: 32,
            forbidden_source: self.root.join("snapshot"),
            worker_walls: ids.iter().map(|id| ((*id).clone(), wall)).collect(),
            owner_root: self.owner.clone(),
        }
    }
    /// Hand the members to an owner work port (in stage order).
    fn work(&mut self, clocks: Clocks) -> (OwnerWork, Vec<Watched>) {
        let plan = self.plan(600);
        self.work_with(clocks, plan)
    }
    fn work_with(&mut self, clocks: Clocks, plan: Plan) -> (OwnerWork, Vec<Watched>) {
        let mut taken = Vec::new();
        let mut members = Vec::new();
        for j in self.members.drain(..) {
            taken.push((j.seen.clone(), j.expected.clone(), j.member.control.clone()));
            let link = TestLink::start(j.member);
            members.push(WorkMember {
                node_id: j.node_id,
                cancel: link.canceller(),
                link: Box::new(link),
                registration: j.registration,
            });
        }
        let work = OwnerWork::new(
            self.seed.coordinator.clone(),
            members,
            plan,
            None,
            Arc::new(NoCodec),
            clocks,
        )
        .unwrap();
        (work, taken)
    }
}
fn host_clocks() -> Clocks {
    Clocks {
        mono: Arc::new(|| 0.0),
        wall: Arc::new(app_host::now),
    }
}
fn settled(state: &Value) -> bool {
    matches!(
        state["state"].as_str(),
        Some("awaiting_activation" | "failed" | "stopped")
    )
}

/// G4a: start_serving runs consent -> transfer for every participant (real carrier, the member
/// verifies the exact compiled bytes), then native prepare on every stage; the test member has
/// no native worker, so the workflow fails with ITS code (v0.1.4 workflow.py:337-343: a closed
/// collaborator code becomes the workflow's error_code).
#[test]
fn the_work_port_transfers_every_stage_then_asks_each_member_for_its_native_prepare() {
    let mut s = swarm(2);
    let (mut work, members) = s.work(host_clocks());
    let started = work.start_serving(&json!({"max_wall_seconds":600}));
    assert_eq!(
        started.map(|v| v["registry_origin"].clone()),
        Ok(Value::Null)
    );
    let state = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(state["state"], "failed", "{state}");
    assert_eq!(state["error_code"], NO_NATIVE, "{state}");
    for (seen, expected, _) in &members {
        let seen = seen.lock().unwrap();
        assert_eq!(
            seen.commands,
            ["consent_status", "receive", "prepare_native", "discard"],
            "consent, transfer, native prepare, then (R1) the stage released on the failure"
        );
        assert_eq!(seen.received.as_deref(), Some(expected.as_slice()));
    }
    assert_eq!(
        state["transferred"]
            .as_array()
            .map(|rows| rows.iter().map(|r| r["received_bytes"].clone()).collect()),
        Some(
            members
                .iter()
                .map(|(_, e, _)| json!(e.len()))
                .collect::<Vec<_>>()
        ),
        "{state}"
    );
    assert_eq!(
        state["cleanup_error"],
        Value::Null,
        "every discard answered"
    );
    work.teardown().unwrap();
}

/// The owner only REQUESTS consent: a member whose standing consent was withdrawn is refused
/// `member_consent_required` before any offer reaches it; the run fails with that code.
#[test]
fn a_member_without_its_own_bound_consent_is_refused_before_any_offer() {
    let mut s = swarm(2);
    standing_consent::withdraw(&s.members[1].member.root, app_host::now()).unwrap();
    let (mut work, members) = s.work(host_clocks());
    let started = work.start_serving(&json!({"max_wall_seconds":600}));
    assert!(started.is_ok(), "{started:?}");
    let state = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(state["state"], "failed", "{state}");
    assert_eq!(state["error_code"], "member_consent_required", "{state}");
    let first = members[0].0.lock().unwrap();
    assert_eq!(
        first.commands,
        ["consent_status", "receive", "discard"],
        "R1: the transferred member releases its stage"
    );
    assert_eq!(first.received.as_deref(), Some(members[0].1.as_slice()));
    let second = members[1].0.lock().unwrap();
    assert_eq!(second.commands, ["consent_status"], "never offered");
    assert!(second.received.is_none());
    drop((first, second));
    work.teardown().unwrap();
}

/// `_stop_deadline` (demo_chain.py:1240-1253): the owner's cleanup wait for a run whose smallest
/// worker wall is `wall` seconds, over `members` peers.
fn stop_allowance(wall: u64, members: u64) -> Duration {
    Duration::from_millis(
        B::EffectiveStopAllowance
            .wall_span(Span::from_millis(wall * 1000), Entries::new(members))
            .unwrap()
            .millis(),
    )
}

/// R1 (REVIEW-lanePH3B-S4A F1): one stage refuses its native prepare while the other stage's
/// prepare is still running (both begin before either is awaited). Every exit releases what
/// members hold, as v0.1.4 teardown requests stage cleanup per offer (demo_chain.py:1283-1320,
/// 1380; DemoApp -> owner_run teardown -> `AppDemoChain.teardown` :1322): the running prepare is
/// cancelled (`cancel_operation`), its reply drained, then EVERY transferred member gets
/// `discard` and answers it; the run reports no unproven cleanup.
#[test]
fn a_prepare_refused_by_one_stage_cancels_the_other_and_every_member_discards_its_stage() {
    let mut s = swarm(2);
    s.members[1]
        .member
        .script
        .insert("prepare_native", Act::Hold);
    let (mut work, members) = s.work(host_clocks());
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let started = work.start_serving(&json!({"max_wall_seconds":600}));
    assert!(started.is_ok(), "{started:?}");
    let state = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(state["state"], "failed", "{state}");
    assert_eq!(state["error_code"], NO_NATIVE, "{state}");
    for (seen, _, _) in &members {
        assert_eq!(
            seen.lock().unwrap().commands,
            ["consent_status", "receive", "prepare_native", "discard"]
        );
    }
    assert!(
        members[1].0.lock().unwrap().cancels >= 1,
        "the other stage's running prepare is cancelled"
    );
    assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    work.teardown().unwrap();
}

/// R1: a member that never answers its `discard` leaves the run's cleanup unproven
/// (`owner_target_cleanup_unproven`, demo_chain.py:1392-1396), recorded, never swallowed, and the
/// owner stops waiting at its stop allowance (demo_chain.py:1240-1253, here a 60 s worker wall).
#[test]
fn a_member_that_never_answers_its_discard_leaves_cleanup_unproven_within_the_stop_allowance() {
    let mut s = swarm(2);
    s.members[1].member.script.insert("discard", Act::Silent);
    let plan = s.plan(60);
    let (mut work, members) = s.work_with(host_clocks(), plan);
    let started = work.start_serving(&json!({"max_wall_seconds":60}));
    assert!(started.is_ok(), "{started:?}");
    let bound = stop_allowance(60, 2);
    let state = work.wait_state(settled, span(B::WorkerStartup) + bound);
    let ended = Instant::now();
    assert_eq!(state["state"], "failed", "{state}");
    assert_eq!(state["error_code"], NO_NATIVE, "{state}");
    assert_eq!(
        state["cleanup_error"], "owner_target_cleanup_unproven",
        "{state}"
    );
    for (seen, _, _) in &members {
        assert_eq!(
            seen.lock().unwrap().commands.last().map(String::as_str),
            Some("discard")
        );
    }
    let asked = members[1].0.lock().unwrap().discard_at.unwrap();
    assert!(
        ended.duration_since(asked) <= bound + span(B::OwnerPoll),
        "gave up within the stop allowance: {:?} > {bound:?}",
        ended.duration_since(asked)
    );
    work.teardown().unwrap();
}

/// What a Stop answered: teardown's result and the final workflow state.
type Stopped = (Result<(), String>, Value);
/// Stop `work` on its own thread; the result arrives on the receiver (the thread hands the port
/// back so a late teardown still joins).
fn stop(mut work: OwnerWork) -> (mpsc::Receiver<Stopped>, std::thread::JoinHandle<OwnerWork>) {
    let (done, ended) = mpsc::channel();
    let stopper = std::thread::spawn(move || {
        let result = work.teardown();
        if done.send((result, work.state())).is_err() {
            eprintln!("stop waiter gone");
        }
        work
    });
    (ended, stopper)
}

/// F4 (REVIEW-lanePH3B-S4A): Stop is prompt in the shipped app (`OwnerRun.stop` sets the stop
/// event every wait polls, owner_run.py:275-283; teardown bounded by `_stop_deadline`,
/// demo_chain.py:1240-1254). A Stop while a member is receiving its shard (custody taken, the
/// owner's source waiting for the member's connection) interrupts that receive
/// (`cancel_operation`), wakes the source and ends within the stop allowance; the member then
/// discards what it holds (R1) and the workflow is `stopped`.
#[test]
fn a_stop_during_a_held_receive_interrupts_it_and_ends_within_the_stop_allowance() {
    let mut s = swarm(2);
    s.members[0].member.script.insert("receive", Act::Hold);
    let plan = s.plan(60);
    let (mut work, members) = s.work_with(host_clocks(), plan);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let started = work.start_serving(&json!({"max_wall_seconds":60}));
    assert!(started.is_ok(), "{started:?}");
    assert!(members[0].2.holding("receive", span(B::WorkerStartup)));
    let bound = stop_allowance(60, 2);
    let (ended, _stopper) = stop(work);
    let stopped = ended.recv_timeout(bound);
    assert!(
        stopped.is_ok(),
        "Stop waited past the stop allowance ({bound:?}) on the held receive"
    );
    let (result, state) = stopped.unwrap();
    assert_eq!(result, Ok(()));
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    let first = members[0].0.lock().unwrap();
    assert!(first.cancels >= 1, "the held receive is cancelled");
    assert_eq!(first.commands, ["consent_status", "receive", "discard"]);
    assert!(
        members[1].0.lock().unwrap().commands.is_empty(),
        "the next member is never reached"
    );
}

/// F4: a Stop while both stages prepare their native workers cancels both prepares, ends within
/// the stop allowance, and both members discard their stage (R1).
#[test]
fn a_stop_during_held_native_prepares_cancels_both_and_every_member_discards() {
    let mut s = swarm(2);
    for m in &mut s.members {
        m.member.script.insert("prepare_native", Act::Hold);
    }
    let plan = s.plan(60);
    let (mut work, members) = s.work_with(host_clocks(), plan);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let started = work.start_serving(&json!({"max_wall_seconds":60}));
    assert!(started.is_ok(), "{started:?}");
    for (_, _, control) in &members {
        assert!(control.holding("prepare_native", span(B::WorkerStartup)));
    }
    let bound = stop_allowance(60, 2);
    let (ended, _stopper) = stop(work);
    let stopped = ended.recv_timeout(bound);
    assert!(
        stopped.is_ok(),
        "Stop waited past the stop allowance ({bound:?}) on the held prepares"
    );
    let (result, state) = stopped.unwrap();
    assert_eq!(result, Ok(()));
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    for (seen, _, _) in &members {
        let seen = seen.lock().unwrap();
        assert!(seen.cancels >= 1, "every running prepare is cancelled");
        assert_eq!(
            seen.commands,
            ["consent_status", "receive", "prepare_native", "discard"]
        );
    }
}

/// F5 (REVIEW-lanePH3B-S4A): the owner pushes the stage shards in the slices step, BEFORE publish
/// starts the run's execution clock, as v0.1.4 members download before publish (descriptors step
/// demo_chain.py:562-725, the clock set only at publish :896; DemoApp app.py:505 -> _action start
/// app.py:1362 -> demo_app.py:109-133 -> OwnerRun steps owner_run.py:30-40). So the members' worker
/// wall (rollover, ceiling, route expiry) is not eaten by the transfer: after `node_slice_plans`
/// every member holds its verified stage and nothing was prepared; `start_serving` (publish) only
/// asks for the native prepare. Whole mode: no slice plan is announced (U1).
#[test]
fn the_slices_step_pushes_every_stage_before_publish_and_start_serving_only_prepares() {
    let mut s = swarm(2);
    let (mut work, members) = s.work(host_clocks());
    let plans = SlicesPort::node_slice_plans(&mut work);
    assert_eq!(plans, Ok(None));
    for (seen, expected, _) in &members {
        let seen = seen.lock().unwrap();
        assert_eq!(
            seen.commands,
            ["consent_status", "receive"],
            "pushed in the slices step"
        );
        assert_eq!(seen.received.as_deref(), Some(expected.as_slice()));
    }
    assert_eq!(
        work.state()["state"],
        "prepared",
        "the workflow starts at publish"
    );
    let started = work.start_serving(&json!({"max_wall_seconds":600}));
    assert!(started.is_ok(), "{started:?}");
    let state = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(state["state"], "failed", "{state}");
    assert_eq!(state["error_code"], NO_NATIVE, "{state}");
    for (seen, _, _) in &members {
        assert_eq!(
            seen.lock().unwrap().commands,
            ["consent_status", "receive", "prepare_native", "discard"],
            "publish only prepares: no second transfer"
        );
    }
    work.teardown().unwrap();
}

/// F5 + F4: a Stop while the slices step pushes a shard (the step blocks in the port) is prompt:
/// the host's stopper interrupts the member's receive, the step ends `Stopped` within the stop
/// allowance, and the teardown that follows releases the member's stage (R1).
#[test]
fn a_stop_while_the_slices_step_pushes_ends_the_step_within_the_stop_allowance() {
    let mut s = swarm(2);
    s.members[0].member.script.insert("receive", Act::Hold);
    let plan = s.plan(60);
    let (work, members) = s.work_with(host_clocks(), plan);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let stopper = work.stopper();
    let (done, ended) = mpsc::channel();
    let step = std::thread::spawn(move || {
        let mut work = work;
        let plans = SlicesPort::node_slice_plans(&mut work);
        if done.send(plans).is_err() {
            eprintln!("slices waiter gone");
        }
        work
    });
    assert!(
        members[0].2.holding("receive", span(B::WorkerStartup)),
        "the slices step pushes the shard"
    );
    stopper();
    assert!(
        members[0].0.lock().unwrap().cancels >= 1,
        "the stopper itself interrupts the receive"
    );
    let bound = stop_allowance(60, 2);
    assert_eq!(ended.recv_timeout(bound), Ok(Err(StepError::Stopped)));
    let (ended, _stopper) = stop(step.join().unwrap());
    let (result, state) = ended.recv_timeout(bound).unwrap();
    assert_eq!(result, Ok(()));
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(
        members[0].0.lock().unwrap().commands,
        ["consent_status", "receive", "discard"]
    );
}

fn walled(walls: &[(&str, u64)], started: Option<f64>) -> RunState {
    let mut state = RunState::new("s4a-run", "s4a-deployment", Reason::Start);
    let min = walls.iter().map(|(_, w)| *w).min().unwrap();
    state.work_limits = Some(json!({"max_wall_seconds":min}));
    state.work_nodes = Some(Value::Object(
        walls
            .iter()
            .map(|(n, w)| {
                (
                    (*n).to_owned(),
                    json!({"work_limits":{"max_wall_seconds":w}}),
                )
            })
            .collect(),
    ));
    state.execution_clock_started_at = started;
    state
}

/// J12: the run's clock starts at publish (`execution_clock_started_at`) and the members' REAL
/// worker wall bounds it: a 600 s wall rolls over at 480 s (lead = min(max(30, 120), 300)) and
/// hits the ceiling at 570 s (`execution_ceiling_reached`); v0.1.4 test_consented_wall_limits.py:76
/// pins 600/480/570. The smallest member wall wins; no clock or no wall is never due.
#[test]
fn rollover_is_due_at_480_s_and_the_ceiling_at_570_s_of_a_600_s_worker_wall() {
    let t0 = 1000.0;
    let state = walled(&[("node-a", 600), ("node-b", 900)], Some(t0));
    assert_eq!(work::rollover_due(&state, t0 + 479.999), Some(false));
    assert_eq!(work::rollover_due(&state, t0 + 480.0), Some(true));
    assert_eq!(
        work::ceiling_reached(&state, t0 + 569.999),
        CeilingProbe::Code(None)
    );
    assert_eq!(
        work::ceiling_reached(&state, t0 + 570.0),
        CeilingProbe::Code(Some("execution_ceiling_reached".into()))
    );
    let short = walled(&[("node-a", 120), ("node-b", 900)], Some(t0));
    assert_eq!(
        work::rollover_due(&short, t0 + 59.999),
        Some(false),
        "lead = wall/2"
    );
    assert_eq!(work::rollover_due(&short, t0 + 60.0), Some(true));
    assert_eq!(
        work::ceiling_reached(&short, t0 + 90.0),
        CeilingProbe::Code(Some("execution_ceiling_reached".into()))
    );
    let unstarted = walled(&[("node-a", 600)], None);
    assert_eq!(work::rollover_due(&unstarted, t0 + 1e6), Some(false));
    assert_eq!(
        work::ceiling_reached(&unstarted, t0 + 1e6),
        CeilingProbe::Code(None)
    );
}

/// A serving owner run (sans-IO machine, every step done).
fn serving_run() -> OwnerRun {
    let mut run = OwnerRun::new(Box::new(|| RunIdentity {
        run_id: "s4a-run".into(),
        deployment_id: "s4a-deployment".into(),
    }));
    let clock = Clock {
        mono: 0.0,
        wall: 0.0,
    };
    run.start("start", None, clock).unwrap();
    for _ in owner_run::machine::STEPS {
        run.step_finished(StepOutcome::Ok);
    }
    assert!(run.serving());
    run
}

/// The port's own probes on the injected run clock drive the machine: at 480 s the serving run
/// switches (rollover), and with the rollover not yet due the ceiling ends it
/// `execution_ceiling_reached`.
#[test]
fn the_ports_probes_switch_the_run_at_480_s_and_end_it_at_the_570_s_ceiling() {
    let mut s = swarm(2);
    let now = Arc::new(Mutex::new(0.0f64));
    let read = now.clone();
    let (mut work, _) = s.work(Clocks {
        mono: Arc::new(move || *read.lock().unwrap()),
        wall: Arc::new(app_host::now),
    });
    let state = walled(&[("node-a", 600), ("node-b", 600)], Some(50.0));
    *now.lock().unwrap() = 50.0 + 480.0;
    let mut run = serving_run();
    run.serving_poll(work.rollover_due(&state), work.ceiling_reached(&state));
    assert_eq!(run.snapshot().rollover.state, "switching");
    *now.lock().unwrap() = 50.0 + 570.0;
    let mut run = serving_run();
    run.serving_poll(Some(false), work.ceiling_reached(&state));
    assert_eq!(
        run.snapshot().error_code.as_deref(),
        Some("execution_ceiling_reached")
    );
    work.teardown().unwrap();
}

/// J13: a participant whose seed lease ran out (lease + the members' clock skew, the member's own
/// fence point) is gone for the owner: the monitor answers `joined_route_member_stale`
/// (v0.1.4 `managed_control_member_stale`) and the serving run fails with it as
/// `owner_run.error_code`. A renewed member is not stale. Seed clock injected.
#[test]
fn a_member_whose_seed_lease_ran_out_fails_the_serving_run_joined_route_member_stale() {
    let mut s = swarm(2);
    let coordinator = s.seed.coordinator.clone();
    let seed_now = Arc::new(Mutex::new(None::<f64>));
    let at = seed_now.clone();
    let view: api::m16_route::SeedMembers = Arc::new(move |node: &str| {
        let row = coordinator.member(node).ok()?;
        let lease = row["lease_expires_at"].as_f64()?;
        Some((row, at.lock().unwrap().unwrap_or(lease - 1.0)))
    });
    let stale_node = s.members[1].node_id.clone();
    let lease = s.seed.coordinator.member(&stale_node).unwrap()["lease_expires_at"]
        .as_f64()
        .unwrap();
    let (work, _) = s.work(host_clocks());
    let mut work = work.with_seed_view(view);
    let state = walled(&[("node-a", 600)], None);
    assert_eq!(work.monitor(&state), MonitorProbe::Code(None), "renewed");
    *seed_now.lock().unwrap() =
        Some(lease + seconds(B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31));
    let probe = work.monitor(&state);
    assert_eq!(
        probe,
        MonitorProbe::Code(Some("joined_route_member_stale".into()))
    );
    let mut run = serving_run();
    run.monitor_result(probe);
    let status = run.snapshot();
    assert_eq!(
        status.error_code.as_deref(),
        Some("joined_route_member_stale")
    );
    assert_eq!(status.failed_step, Some("serving"));
    work.teardown().unwrap();
}

/// F7 (REVIEW-lanePH3B-S4A): the work port's copy of SL's member-stale rule
/// (`m16_route::observe_member_leases`; v0.1.4 control_session.py:315-326 via demo_chain.py:532-546)
/// says stale only once the seed clock is past the lease PLUS the members' clock skew (the
/// member's own fence point), and only for the seed row of the generation the run admitted (a
/// re-joined member's new row is the membership paths' business). Kills M6 (no skew tolerance)
/// and M7 (generation ignored). The same rule lives in m16_route.rs (lane EP2: share it).
#[test]
fn the_member_stale_rule_keeps_the_skew_tolerance_and_reads_only_the_admitted_generation() {
    let mut s = swarm(2);
    let node = s.members[1].node_id.clone();
    let lease = s.seed.coordinator.member(&node).unwrap()["lease_expires_at"]
        .as_f64()
        .unwrap();
    let coordinator = s.seed.coordinator.clone();
    let seed_now = Arc::new(Mutex::new(lease));
    let rejoined = Arc::new(AtomicBool::new(false));
    let (at, again, watched) = (seed_now.clone(), rejoined.clone(), node.clone());
    let view: api::m16_route::SeedMembers = Arc::new(move |n: &str| {
        if n != watched {
            return None;
        }
        let mut row = coordinator.member(n).ok()?;
        if again.load(Ordering::SeqCst) {
            row["generation"] = json!(row["generation"].as_u64()? + 1);
        }
        Some((row, *at.lock().unwrap()))
    });
    let (work, _) = s.work(host_clocks());
    let mut work = work.with_seed_view(view);
    let state = walled(&[("node-a", 600)], None);
    let skew = seconds(B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31);
    assert_eq!(
        work.monitor(&state),
        MonitorProbe::Code(None),
        "at the lease"
    );
    *seed_now.lock().unwrap() = lease + skew / 2.0;
    assert_eq!(
        work.monitor(&state),
        MonitorProbe::Code(None),
        "inside the members' clock skew"
    );
    *seed_now.lock().unwrap() = lease + skew;
    assert_eq!(
        work.monitor(&state),
        MonitorProbe::Code(Some("joined_route_member_stale".into()))
    );
    rejoined.store(true, Ordering::SeqCst);
    assert_eq!(
        work.monitor(&state),
        MonitorProbe::Code(None),
        "another generation's row is not the admitted member"
    );
    work.teardown().unwrap();
}

fn activation(result: Result<(), PortError>) -> Result<(), String> {
    result.map_err(|e| e.code_or("activation_unexpected"))
}

/// F9 M3: activation is accepted only for the started proposal and only once every stage is
/// prepared (`awaiting_activation`; v0.1.4 `activate_serving`, workflow.py:277-345, reached from
/// `_step_activate` demo_chain.py:980): before publish, while a stage is still transferring and
/// for another proposal it is refused `activation_failed`.
#[test]
fn activation_before_every_stage_is_prepared_is_refused() {
    let mut s = swarm(2);
    s.members[0].member.script.insert("receive", Act::Hold);
    let (mut work, members) = s.work(host_clocks());
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let proposal = json!({"max_wall_seconds":600});
    let refused = Err("activation_failed".to_owned());
    assert_eq!(
        activation(ActivationPort::activate(&mut work, &proposal)),
        refused,
        "before publish"
    );
    let started = work.start_serving(&proposal);
    assert!(started.is_ok(), "{started:?}");
    assert!(members[0].2.holding("receive", span(B::WorkerStartup)));
    assert_eq!(
        activation(ActivationPort::activate(&mut work, &proposal)),
        refused,
        "a stage is still transferring"
    );
    assert_eq!(
        activation(ActivationPort::activate(
            &mut work,
            &json!({"max_wall_seconds":60})
        )),
        refused,
        "another proposal"
    );
    work.teardown().unwrap();
}

/// F9 M9/M10: every route of the run expires at publish + the SMALLEST member wall (v0.1.4
/// `_smallest_worker_wall`, demo_chain.py:1125-1130: no route outlives a member's consented wall),
/// neither the rehearsal default (MemberChosenWall) nor the largest wall. Two distinct walls,
/// both unlike the default, on an injected publish wall clock.
#[test]
fn every_route_expires_at_publish_plus_the_smallest_of_two_distinct_member_walls() {
    let mut s = swarm(2);
    let (small, large) = (120u64, 300u64);
    let mut plan = s.plan(large);
    let ids: Vec<String> = s.members.iter().map(|m| m.node_id.clone()).collect();
    plan.worker_walls = BTreeMap::from([(ids[0].clone(), large), (ids[1].clone(), small)]);
    assert!(seconds(B::MemberChosenWall) != small as f64);
    let publish = app_host::now();
    let clocks = Clocks {
        mono: Arc::new(|| 0.0),
        wall: Arc::new(move || publish),
    };
    let (mut work, _) = s.work_with(clocks, plan);
    let started = work.start_serving(&json!({"max_wall_seconds":large}));
    assert!(started.is_ok(), "{started:?}");
    assert_eq!(
        work.state()["route_expires_at"],
        json!(publish + small as f64)
    );
    let state = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(state["route_expires_at"], json!(publish + small as f64));
    work.teardown().unwrap();
}

/// F9 M11: `teardown` hands every member link back in stage order, whether the work started or
/// not (members stay joined; the run never closes their links), each link still answering.
#[test]
fn teardown_hands_every_member_back_in_stage_order_started_or_not() {
    for started in [false, true] {
        let mut s = swarm(2);
        let ids: Vec<String> = s.members.iter().map(|m| m.node_id.clone()).collect();
        let (mut work, _) = s.work(host_clocks());
        if started {
            let begun = work.start_serving(&json!({"max_wall_seconds":600}));
            assert!(begun.is_ok(), "{begun:?}");
            work.wait_state(settled, span(B::WorkerStartup));
        }
        work.teardown().unwrap();
        let mut back = work.take_members();
        assert_eq!(
            back.iter().map(|m| m.node_id.clone()).collect::<Vec<_>>(),
            ids,
            "started: {started}"
        );
        for m in &mut back {
            // F7: the canceller is required by type; it is callable (nothing admitted here).
            (m.cancel)();
            let status = m
                .link
                .call(json!({"command":"consent_status"}), span(B::WorkerStartup));
            assert!(status.is_ok(), "started: {started}: {status:?}");
        }
    }
}

// ---- Lane EP2: one route epoch per run (model-free, real carrier and member stage servers) ----
//
// v0.1.4 call chain: DemoApp (app.py:505) -> `_action` start (app.py:1362) -> demo_app.py:109-133 ->
// OwnerRun steps (owner_run.py:30-40) -> demo_chain.py:890-985 -> `workflow.run` (workflow.py:277-345)
// -> `launch.run` -> `run_registry_server` (mycelium_live/supervisor.py:4196): the registry stack
// serves the run's ONE route until the stop event (supervisor.py:4427-4428); every chat is a request
// on it (chat.py:300-398 -> `route.infer`, route.py:4592-4620); Stop / rollover / ceiling tear the
// route down (demo_chain.py:1322-1335).

#[path = "support/sse_client.rs"]
mod sse_client;

/// TEST ADMISSION (test double): each serving test member answers `prepare_native` with a
/// CONTROLLED document (no native worker, no resident proof) and the route over them is signed
/// with the test swarm's seed key, as tests/joined_route_carrier.rs signs its controlled routes.
/// The owner's epoch, carrier, gateway and every member's stage server are the product's.
struct ControlledAdmission {
    seed: Identity,
    swarm: String,
    /// Per stage: (offer, controlled document, range, prepare tokens).
    prepared: Vec<(Value, Value, [u64; 2], usize)>,
}
impl Admission for ControlledAdmission {
    fn prepare(
        &mut self,
        stages: &mut [Stage<'_>],
        tokens: usize,
        forbidden_source: &Path,
        within: Duration,
    ) -> Result<(), Refusal> {
        for st in stages.iter_mut() {
            st.member.begin(
                json!({"command":"prepare_native","range":st.range,"tokens":tokens,
                "nonce":"controlled","forbidden_source":forbidden_source}),
            )?;
        }
        self.prepared.clear();
        for st in stages.iter_mut() {
            let document = st.member.finish(within)?;
            self.prepared
                .push((st.offer.clone(), document, st.range, tokens));
        }
        Ok(())
    }
    fn issue(&mut self, o: &RouteOrder<'_>) -> Result<Value, Refusal> {
        let digest = |v: &Value| protocol::canonical::digest(v).unwrap();
        let stages: Vec<Value> = self.prepared.iter().enumerate().map(|(i, (offer, document, range, tokens))| {
            let m = &offer["message"];
            json!({"stage_index":i,"placement_id":format!("{}:{}-{}",m["member"]["node_id"].as_str().unwrap(),range[0],range[1]),
                "member":m["member"],"carrier_endpoint_id":m["registration"]["message"]["carrier_endpoint_id"],
                "run":m["run"],"offer_digest":digest(offer),"proof_digest":digest(document),"range":range,
                "worker_pid":document["worker_pid"],"tokens":tokens,"memory":4_000_000_000u64})
        }).collect();
        let message = json!({"protocol":o.protocol,"swarm_id":self.swarm,"deployment_id":o.deployment_id,
            "deployment_epoch":o.epoch,"topology_version":o.epoch,"model_id":o.model["model_id"],
            "resolved_commit":o.model["resolved_commit"],"manifest_digest":o.model["manifest_digest"],
            "owner_endpoint_id":o.owner_endpoint_id,"stages":stages,"issued_at":o.now,
            "expires_at":o.expires_at,"route_ready":false,"scope":route::SCOPE});
        Ok(
            json!({"protocol":"mycelium.joined_shard.signed.v1","signature":self.seed.sign(&message).unwrap(),
            "verification_key":self.seed.public_key_record(),"message":message}),
        )
    }
}
/// Model-free prompt codec of the serving tests (token ids are carrier test values).
struct Prompt;
impl Codec for Prompt {
    fn encode(&self, _: &str) -> Result<Vec<u32>, PublicError> {
        Ok((0..4).collect())
    }
    fn decode(&self, token: u32) -> Result<String, PublicError> {
        Ok(format!("<{token}>"))
    }
}
/// Tokens of one chat and the final stage's scripted token ids.
const CHAT_TOKENS: u64 = 4;
const SCRIPTED: [u32; 4] = [11, 12, 13, 14];
impl Swarm {
    /// Hand the members to a work port that admits them through the controlled admission;
    /// `scripts` per stage (the last stage's emits [`SCRIPTED`]).
    fn serving(&mut self, plan: Plan, scripts: Vec<Serving>) -> (OwnerWork, Vec<Watched>) {
        let (seed, _, _) =
            app_host::identity_in(&self.owner, app_host::SEED_IDENTITY_FILE).unwrap();
        let mut taken = Vec::new();
        let mut members = Vec::new();
        for (mut j, script) in self.members.drain(..).zip(scripts) {
            j.member.serving = Some(script);
            taken.push((j.seen.clone(), j.expected.clone(), j.member.control.clone()));
            let link = TestLink::start(j.member);
            members.push(WorkMember {
                node_id: j.node_id,
                cancel: link.canceller(),
                link: Box::new(link),
                registration: j.registration,
            });
        }
        let work = OwnerWork::new(
            self.seed.coordinator.clone(),
            members,
            plan,
            None,
            Arc::new(Prompt),
            host_clocks(),
        )
        .unwrap()
        .with_admission(Box::new(ControlledAdmission {
            seed,
            swarm: self.seed.coordinator.swarm_id().to_owned(),
            prepared: Vec::new(),
        }));
        (work, taken)
    }
    /// A 60 s run plan whose model names a well-formed manifest digest.
    fn serving_plan(&self) -> Plan {
        let mut plan = self.plan(60);
        plan.model["manifest_digest"] =
            json!(protocol::canonical::digest(&json!("manifest")).unwrap());
        plan
    }
}
/// Two stage scripts: the final stage emits [`SCRIPTED`].
fn stage_scripts() -> Vec<Serving> {
    vec![
        Serving::default(),
        Serving {
            tokens: SCRIPTED.to_vec(),
            ..Serving::default()
        },
    ]
}
/// start_serving -> every stage prepared -> activation -> the run's route serving.
fn activate(work: &mut OwnerWork) -> Value {
    let proposal = json!({"max_wall_seconds":60});
    let started = work.start_serving(&proposal);
    assert!(started.is_ok(), "{started:?}");
    let staged = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(staged["state"], "awaiting_activation", "{staged}");
    let activated = ActivationPort::activate(work, &proposal);
    assert!(activated.is_ok(), "{activated:?}");
    let serving = work.wait_state(
        |s| {
            matches!(
                s["state"].as_str(),
                Some("api_running" | "failed" | "stopped")
            )
        },
        span(B::WorkerStartup),
    );
    assert_eq!(serving["state"], "api_running", "{serving}");
    serving
}
fn bearer(gateway: &Value) -> String {
    format!(
        "Authorization: Bearer {}\r\n",
        gateway["bearer"].as_str().unwrap()
    )
}
fn gateway_addr(gateway: &Value) -> std::net::SocketAddr {
    gateway["addr"].as_str().unwrap().parse().unwrap()
}
/// Submit one chat on the run's gateway (`POST /v1/inference`).
fn post(gateway: &Value) -> (u16, Value) {
    let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":"p","max_new_tokens":CHAT_TOKENS,
        "workload_profile_id":"interactive","qos_class":"interactive","qualification":gateway["qualification"]});
    sse_client::http(
        gateway_addr(gateway),
        "POST",
        "/v1/inference",
        &bearer(gateway),
        &doc.to_string(),
    )
}
/// The accepted chat's event stream (reader thread owned by the caller).
fn events(
    gateway: &Value,
    accepted: &Value,
) -> (
    mpsc::Receiver<(Value, Instant)>,
    std::thread::JoinHandle<()>,
) {
    let (tx, rx) = mpsc::channel();
    let (addr, path) = (
        gateway_addr(gateway),
        accepted["stream_path"].as_str().unwrap().to_owned(),
    );
    let auth = format!(
        "{}X-Mycelium-Session: {}\r\n",
        bearer(gateway),
        accepted["session_token"].as_str().unwrap()
    );
    let reader = std::thread::spawn(move || {
        sse_client::stream(addr, path, auth, tx, span(B::WorkerStartup))
    });
    (rx, reader)
}
/// Events until the chat's terminal (bounded per event).
fn until_terminal(rx: &mpsc::Receiver<(Value, Instant)>) -> Vec<Value> {
    let mut all = Vec::new();
    loop {
        let Ok((event, _)) = rx.recv_timeout(span(B::WorkerStartup)) else {
            return all;
        };
        let done = matches!(
            event["type"].as_str(),
            Some("completed" | "cancelled" | "failed")
        );
        all.push(event);
        if done {
            return all;
        }
    }
}
/// One whole chat: accepted, streamed to its terminal.
fn chat(gateway: &Value) -> Vec<Value> {
    let (status, accepted) = post(gateway);
    assert_eq!(status, 202, "chat refused: {accepted}");
    let (rx, reader) = events(gateway, &accepted);
    let all = until_terminal(&rx);
    reader.join().unwrap();
    all
}
fn token_ids(all: &[Value]) -> Vec<u64> {
    all.iter()
        .filter(|e| e["type"] == "token")
        .map(|e| e["token_id"].as_u64().unwrap())
        .collect()
}
fn count(seen: &Seen, event: &str) -> usize {
    seen.compute.iter().filter(|(e, _)| *e == event).count()
}
/// Index of the first compute event named `event`.
fn first(seen: &Seen, event: &str) -> Option<usize> {
    seen.compute.iter().position(|(e, _)| *e == event)
}

/// EP2 item 1: a run opens ONE route epoch at activation and serves every chat on it. Each
/// member is prepared once and serves once; every chat's tokens are the final stage's; each
/// chat's end resets both stages' sessions (chat k starts after k-1 resets) and the epoch number
/// and gateway never change. The run's Stop closes the epoch FIRST (each member's serve ends
/// `owner_closed`, its compute closed once, no operator cancel of the serve), then each member
/// discards its stage (S4aF R1); the workflow ends `stopped` with its cleanup proven.
#[test]
fn a_run_serves_every_chat_on_one_epoch_with_one_prepare_per_member() {
    let mut s = swarm(2);
    let plan = s.serving_plan();
    let (mut work, members) = s.serving(plan, stage_scripts());
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let serving = activate(&mut work);
    assert_eq!(serving["epoch"], 1, "{serving}");
    let gateway = work.gateway().unwrap();
    const CHATS: usize = 3;
    for k in 1..=CHATS {
        let all = chat(&gateway);
        assert_eq!(
            all.last().map(|e| e["type"].clone()),
            Some(json!("completed")),
            "chat {k}: {all:?}"
        );
        assert_eq!(
            token_ids(&all),
            SCRIPTED.iter().map(|t| u64::from(*t)).collect::<Vec<_>>(),
            "chat {k}: a fresh session (position 0) on the final stage"
        );
        for (seen, _, control) in &members {
            assert!(
                control.wait(seen, |s| count(s, "reset") >= k, span(B::WorkerStartup)),
                "chat {k}'s end resets the stage session: {:?}",
                seen.lock().unwrap().compute
            );
        }
        let state = work.state();
        assert_eq!(state["epoch"], 1, "chat {k}: one epoch per run: {state}");
        assert_eq!(state["state"], "api_running", "chat {k}: {state}");
        assert_eq!(work.gateway().unwrap()["addr"], gateway["addr"], "chat {k}");
    }
    for (seen, _, _) in &members {
        let seen = seen.lock().unwrap();
        assert_eq!(
            seen.commands,
            [
                "consent_status",
                "receive",
                "prepare_native",
                "carrier_address",
                "serve"
            ],
            "one prepare and one serve per member for {CHATS} chats"
        );
        assert_eq!(count(&seen, "reset"), CHATS, "{:?}", seen.compute);
        assert_eq!(
            count(&seen, "close") + count(&seen, "abort"),
            0,
            "compute kept"
        );
    }
    work.teardown().unwrap();
    let state = work.state();
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    for (seen, _, _) in &members {
        let seen = seen.lock().unwrap();
        assert_eq!(
            seen.commands,
            [
                "consent_status",
                "receive",
                "prepare_native",
                "carrier_address",
                "serve",
                "discard"
            ]
        );
        assert_eq!(
            seen.cancels, 0,
            "the epoch's close ended the serve, not a cancel"
        );
        let served = seen.served.clone().unwrap().unwrap();
        assert_eq!(served["teardown"], "owner_closed", "{served}");
        assert_eq!(served["requests_served"], CHATS, "{served}");
        assert_eq!(count(&seen, "close"), 1, "{:?}", seen.compute);
        assert!(
            first(&seen, "close") < first(&seen, "discard"),
            "{:?}",
            seen.compute
        );
    }
}

/// EP2 item 1: Stop while chat 2 is in flight (its final stage held mid-step) closes the epoch
/// BEFORE the members' cleanup: no member serve is cancelled by the owner, each member's compute
/// is released by the epoch's close before its `discard`, the workflow ends `stopped` with its
/// cleanup proven within the stop allowance, and the next run over the same members receives its
/// shards again (rollover / the next Start).
#[test]
fn a_stop_mid_chat_closes_the_epoch_before_the_members_discard_and_the_next_run_receives() {
    let mut s = swarm(2);
    let plan = s.serving_plan();
    // The next run (rollover, or the next Start) has its own run id.
    let mut next = s.serving_plan();
    next.run_id = "s4a-run-next".into();
    let (entered, held) = mpsc::channel();
    let (open, gate) = mpsc::channel();
    let mut scripts = stage_scripts();
    // Chat 1 takes CHAT_TOKENS steps; hold chat 2's second step on the final stage.
    scripts[1].hold = Some((CHAT_TOKENS + 1, entered, gate));
    let (mut work, members) = s.serving(plan, scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    activate(&mut work);
    let gateway = work.gateway().unwrap();
    let first_chat = chat(&gateway);
    assert_eq!(
        first_chat.last().map(|e| e["type"].clone()),
        Some(json!("completed")),
        "{first_chat:?}"
    );
    let (status, accepted) = post(&gateway);
    assert_eq!(status, 202, "chat 2 refused: {accepted}");
    let (_rx, _reader) = events(&gateway, &accepted);
    assert!(
        held.recv_timeout(span(B::WorkerStartup)).is_ok(),
        "chat 2 runs on the run's epoch"
    );
    let bound = stop_allowance(60, 2);
    let (ended, _stopper) = stop(work);
    open.send(()).unwrap();
    let stopped = ended.recv_timeout(bound + span(B::WorkerStartup));
    assert!(stopped.is_ok(), "Stop mid chat 2 did not end");
    let (result, state) = stopped.unwrap();
    assert_eq!(result, Ok(()));
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    for (seen, _, _) in &members {
        let seen = seen.lock().unwrap();
        assert_eq!(
            seen.cancels, 0,
            "Stop closed the epoch; it never cancelled a serve"
        );
        assert_eq!(
            seen.commands,
            [
                "consent_status",
                "receive",
                "prepare_native",
                "carrier_address",
                "serve",
                "discard"
            ]
        );
        let released = first(&seen, "close").or(first(&seen, "abort"));
        assert!(released.is_some(), "{:?}", seen.compute);
        assert!(released < first(&seen, "discard"), "{:?}", seen.compute);
        assert!(
            seen.served.as_ref().is_some_and(Result::is_ok),
            "{:?}",
            seen.served
        );
    }
    // The next run over the same members: each receives its shard again.
    let mut work = _stopper.join().unwrap();
    let back = work.take_members();
    assert_eq!(back.len(), 2);
    let mut again = OwnerWork::new(
        s.seed.coordinator.clone(),
        back,
        next,
        None,
        Arc::new(NoCodec),
        host_clocks(),
    )
    .unwrap();
    assert_eq!(SlicesPort::node_slice_plans(&mut again), Ok(None));
    for (seen, expected, _) in &members {
        let seen = seen.lock().unwrap();
        assert_eq!(
            seen.commands[6..],
            ["consent_status", "receive"],
            "{:?}",
            seen.commands
        );
        assert_eq!(seen.received.as_deref(), Some(expected.as_slice()));
    }
    again.teardown().unwrap();
}

/// EP2 item 1, the rule for a retired epoch (shipped app, ruling 23): an epoch that can no longer
/// serve (here: the final stage cannot reset its session after chat 1, EP1's failed reset) is
/// NEVER re-opened or re-prepared during the run. The run keeps `api_running` on epoch 1 and the
/// work port does not fail it (the monitor answers nothing); the next chat is refused by the
/// retired route (v0.1.4: `route_not_open` route.py:4614-4620 -> chat 503
/// `chat_route_not_qualified` chat.py:113-120; supervisor.py:4427-4428 serves on until Stop).
/// Stop then releases every member as on any exit.
#[test]
fn a_retired_epoch_is_never_reopened_and_later_chats_are_refused_until_stop() {
    let mut s = swarm(2);
    let plan = s.serving_plan();
    let mut scripts = stage_scripts();
    scripts[1].reset_fails = true;
    let (mut work, members) = s.serving(plan, scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    activate(&mut work);
    let gateway = work.gateway().unwrap();
    let all = chat(&gateway);
    assert_eq!(
        all.last().map(|e| e["type"].clone()),
        Some(json!("completed")),
        "{all:?}"
    );
    let (seen, _, control) = &members[1];
    assert!(
        control.wait(
            seen,
            |s| count(s, "reset_attempt") == 1 && s.served.is_some(),
            span(B::WorkerStartup)
        ),
        "the final stage's reset failed and its epoch ended: {:?}",
        seen.lock().unwrap().compute
    );
    let (status, refused) = post(&gateway);
    assert_eq!(status, 409, "{refused}");
    assert!(refused.to_string().contains("route_dropped"), "{refused}");
    let state = work.state();
    assert_eq!(state["state"], "api_running", "{state}");
    assert_eq!(state["epoch"], 1, "{state}");
    assert_eq!(
        work.monitor(&walled(&[("node-a", 60)], None)),
        MonitorProbe::Code(None)
    );
    for (seen, _, _) in &members {
        assert_eq!(
            seen.lock().unwrap().commands,
            [
                "consent_status",
                "receive",
                "prepare_native",
                "carrier_address",
                "serve"
            ],
            "never re-prepared, never re-served"
        );
    }
    work.teardown().unwrap();
    let state = work.state();
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    for (seen, _, _) in &members {
        assert_eq!(
            seen.lock().unwrap().commands.last().map(String::as_str),
            Some("discard")
        );
    }
}

// ---- REVIEW-lanePH3B-S4AF F2/F3/F4: the whole exit is ONE deadline from the stop allowance ----
//
// v0.1.4: `OwnerRun.stop` sets the event every wait polls (owner_run.py:275-283); teardown
// (demo_chain.py:1322-1335) waits for the members' cleanup until ONE `_stop_deadline`
// (demo_chain.py:1240-1253), a member not proven by then is `owner_target_cleanup_unproven`
// (:1392-1396). The owner makes no blocking member call past it.

/// F2 (reviewer probe P2): member 0 never answers its receive (its Mac went silent after the bytes
/// moved). Stop still ends within the stop allowance; member 0's cleanup is recorded unproven.
#[test]
fn a_stop_with_a_member_silent_on_its_receive_ends_within_the_stop_allowance() {
    let mut s = swarm(2);
    s.members[0].member.script.insert("receive", Act::Silent);
    let plan = s.plan(60);
    let (mut work, members) = s.work_with(host_clocks(), plan);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    assert!(work.start_serving(&json!({"max_wall_seconds":60})).is_ok());
    let (seen, _, control) = &members[0];
    assert!(
        control.wait(seen, |s| s.received.is_some(), span(B::WorkerStartup)),
        "member 0 received its shard"
    );
    let bound = stop_allowance(60, 2);
    let (ended, _stopper) = stop(work);
    let stopped = ended.recv_timeout(bound + span(B::OwnerPoll));
    assert!(
        stopped.is_ok(),
        "Stop waited past the stop allowance ({bound:?}) on a silent member"
    );
    let (result, state) = stopped.unwrap();
    assert_eq!(result, Ok(()));
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(
        state["cleanup_error"], "owner_target_cleanup_unproven",
        "{state}"
    );
    assert_eq!(state["member_cleanup"][0]["discarded"], false, "{state}");
    assert!(
        members[1].0.lock().unwrap().commands.is_empty(),
        "member 1 never reached"
    );
}

/// F2 + F3 + F4: member 0 never answers its native prepare while member 1's prepare runs. Stop
/// ends within ONE stop allowance; member 1 (later in stage order) is still cancelled, drained and
/// discarded with its proof, member 0 alone is unproven.
#[test]
fn a_silent_member_early_in_stage_order_leaves_only_its_own_cleanup_unproven() {
    let mut s = swarm(2);
    s.members[0]
        .member
        .script
        .insert("prepare_native", Act::Silent);
    s.members[1]
        .member
        .script
        .insert("prepare_native", Act::Hold);
    let plan = s.plan(60);
    let (mut work, members) = s.work_with(host_clocks(), plan);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    assert!(work.start_serving(&json!({"max_wall_seconds":60})).is_ok());
    assert!(
        members[1]
            .2
            .holding("prepare_native", span(B::WorkerStartup))
    );
    let (seen, _, control) = &members[0];
    assert!(control.wait(
        seen,
        |s| s.commands.last().map(String::as_str) == Some("prepare_native"),
        span(B::WorkerStartup)
    ));
    let bound = stop_allowance(60, 2);
    let (ended, _stopper) = stop(work);
    let stopped = ended.recv_timeout(bound + span(B::OwnerPoll));
    assert!(
        stopped.is_ok(),
        "Stop waited past ONE stop allowance ({bound:?})"
    );
    let (result, state) = stopped.unwrap();
    assert_eq!(result, Ok(()));
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(
        state["cleanup_error"], "owner_target_cleanup_unproven",
        "{state}"
    );
    let rows = state["member_cleanup"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let row = |node: &str| rows.iter().find(|r| r["node_id"] == node).cloned();
    let (first, second) = (&s_node(&members, 0, &state), &s_node(&members, 1, &state));
    assert_eq!(
        row(first).map(|r| r["discarded"].clone()),
        Some(json!(false)),
        "{state}"
    );
    assert_eq!(
        row(second).map(|r| r["discarded"].clone()),
        Some(json!(true)),
        "{state}"
    );
    let later = members[1].0.lock().unwrap();
    assert!(later.cancels >= 1, "member 1's prepare is cancelled");
    assert_eq!(
        later.commands,
        ["consent_status", "receive", "prepare_native", "discard"]
    );
}
/// The node id of stage `i` as the workflow's transfer rows name it.
fn s_node(_: &[Watched], i: usize, state: &Value) -> String {
    state["transferred"][i]["node_id"]
        .as_str()
        .unwrap()
        .to_owned()
}

// ---- REVIEW-laneEP F3/F9: an epoch retired while idle refuses with a named code, promptly ----
//
// v0.1.4: a route that stops serving is `route_not_open` (route.py:4614-4620) and the chat answers
// 503 `chat_route_not_qualified` (chat.py:113-120) at once; the run itself is ended only by Stop,
// rollover, the ceiling or the monitor (ruling 23). The owner work port watches its open epoch
// every owner poll: a member whose serve ended (its reply arrived), or an epoch the carrier
// already retired, makes readiness and the next submission refuse with a named code instead of
// readiness reading OK and the next chat failing at the carrier or the 30 s hop deadline.

/// GET /v1/qualification/current every few ms until it is refused or `within` ran out.
fn readiness_within(gateway: &Value, within: Duration) -> (u16, Value) {
    let end = Instant::now() + within;
    loop {
        let (status, body) = sse_client::http(
            gateway_addr(gateway),
            "GET",
            "/v1/qualification/current",
            &bearer(gateway),
            "",
        );
        if status != 200 || Instant::now() >= end {
            return (status, body);
        }
        std::thread::sleep(span(B::OwnerPoll) / 4);
    }
}

/// F3: member 1's own serve deadline ends while the epoch is idle (its consent wall). Within a few
/// owner polls readiness and the next chat are refused `joined_route_member_ended`; the run stays
/// `api_running` and shows the retirement; Stop still discards every member.
#[test]
fn a_member_that_ended_its_epoch_while_idle_is_refused_by_name_within_a_few_owner_polls() {
    let mut s = swarm(2);
    let plan = s.serving_plan();
    let mut scripts = stage_scripts();
    scripts[1].serve_for = Some(span(B::OwnerPoll) * 4);
    let (mut work, members) = s.serving(plan, scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    activate(&mut work);
    let gateway = work.gateway().unwrap();
    let (seen, _, control) = &members[1];
    assert!(
        control.wait(seen, |s| s.served.is_some(), span(B::WorkerStartup)),
        "member 1's serve ended at its own deadline"
    );
    let (status, ready) = readiness_within(&gateway, span(B::OwnerPoll) * 6);
    assert_ne!(
        status, 200,
        "readiness after a member ended the epoch: {ready}"
    );
    assert!(
        ready.to_string().contains("joined_route_member_ended"),
        "{ready}"
    );
    let posted = Instant::now();
    let (status, refused) = post(&gateway);
    // The gateway's status for an epoch's idle-retirement code (as `joined_route_member_stale`):
    // 400 (requests.rs submit table; 409 is only for the qualification-change codes).
    assert_eq!(status, 400, "{refused}");
    assert!(
        refused.to_string().contains("joined_route_member_ended"),
        "{refused}"
    );
    assert!(
        posted.elapsed() < span(B::OwnerPoll) * 4,
        "refused promptly"
    );
    let state = work.state();
    assert_eq!(state["state"], "api_running", "{state}");
    assert_eq!(
        state["epoch_retired"], "joined_route_member_ended",
        "{state}"
    );
    work.teardown().unwrap();
    let state = work.state();
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    assert_eq!(
        state["serve_reports"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["node_id"].is_string())
            .count(),
        2,
        "the ended serve's report is kept beside the other's: {state}"
    );
}

/// EP2F F2: a member-ended retirement closes the OTHER member without a run Stop.
#[test]
fn an_idle_member_failure_closes_the_other_members_serve_within_bound() {
    let mut s = swarm(2);
    let plan = s.serving_plan();
    let mut scripts = stage_scripts();
    scripts[1].serve_for = Some(span(B::OwnerPoll) * 4);
    let (mut work, members) = s.serving(plan, scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    activate(&mut work);
    let (ended, _, ended_control) = &members[1];
    assert!(ended_control.wait(ended, |s| s.served.is_some(), span(B::WorkerStartup)));
    let began = Instant::now();
    let (other, _, control) = &members[0];
    let closed = control.wait(other, |s| s.served.is_some(), span(B::OwnerPoll) * 6);
    let elapsed = began.elapsed();
    let before_stop = other.lock().unwrap().served.clone();
    work.teardown().unwrap();
    eprintln!("EP2F F2 other_closed={closed} elapsed={elapsed:?} before_stop={before_stop:?}");
    assert!(
        closed,
        "the other member still serves after an idle retirement: {elapsed:?}"
    );
    assert_eq!(before_stop.unwrap().unwrap()["teardown"], "owner_closed");
    assert_eq!(work.state()["cleanup_error"], Value::Null);
}

/// F9: the last chat completed, then its own reset failed and retired the epoch. Readiness no
/// longer reads OK while the next submission is refused: both say `route_dropped`.
#[test]
fn readiness_after_a_completed_then_retired_epoch_agrees_with_the_refused_submission() {
    let mut s = swarm(2);
    let plan = s.serving_plan();
    let mut scripts = stage_scripts();
    scripts[1].reset_fails = true;
    let (mut work, members) = s.serving(plan, scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    activate(&mut work);
    let gateway = work.gateway().unwrap();
    let all = chat(&gateway);
    assert_eq!(
        all.last().map(|e| e["type"].clone()),
        Some(json!("completed")),
        "{all:?}"
    );
    let (seen, _, control) = &members[1];
    assert!(
        control.wait(
            seen,
            |s| count(s, "reset_attempt") == 1,
            span(B::WorkerStartup)
        ),
        "the final stage's reset was attempted"
    );
    let (status, ready) = readiness_within(&gateway, span(B::OwnerPoll) * 6);
    assert_ne!(
        status, 200,
        "readiness after a completed-then-retired epoch: {ready}"
    );
    assert!(ready.to_string().contains("route_dropped"), "{ready}");
    let (status, refused) = post(&gateway);
    assert_eq!(status, 409, "{refused}");
    assert!(refused.to_string().contains("route_dropped"), "{refused}");
    work.teardown().unwrap();
    assert_eq!(work.state()["state"], "stopped");
}

/// EP2 item 3 (REVIEW-laneEP F4): an OLD member (a build before EP1) paired with this owner's v2
/// route refuses it at its v1 route check (`joined_shard_signature_invalid`) and never accepts the
/// owner's carrier dial; the activation fails when the carrier's connect deadline runs out. The
/// owner names the cause: the run fails `joined_route_member_v2_unsupported` (the owner signed the
/// route itself, so a member's signature refusal of it means the member cannot read v2), and the
/// member's own code stays in the serve report.
#[test]
fn an_old_member_paired_with_a_v2_route_fails_the_activation_with_a_named_owner_code() {
    let mut s = swarm(2);
    let plan = s.serving_plan();
    let mut scripts = stage_scripts();
    scripts[0].old_member = true;
    // The new member's serve waits for the owner's dial until its own deadline (its setup does
    // not observe the cancel: member side, joined_shard/serve.rs); bounded here so the test ends.
    scripts[1].serve_for = Some(span(B::WorkerStartup));
    let (mut work, members) = s.serving(plan, scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let proposal = json!({"max_wall_seconds":60});
    assert!(work.start_serving(&proposal).is_ok());
    let staged = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(staged["state"], "awaiting_activation", "{staged}");
    let began = Instant::now();
    let activated = ActivationPort::activate(&mut work, &proposal);
    assert!(activated.is_ok(), "{activated:?}");
    let ended = work.wait_state(
        |s| {
            matches!(
                s["state"].as_str(),
                Some("api_running" | "failed" | "stopped")
            )
        },
        span(B::WorkerStartup) * 6,
    );
    eprintln!(
        "old member: activation ended {:?} after {:?}",
        ended["state"],
        began.elapsed()
    );
    let (seen, _, _) = &members[0];
    assert_eq!(
        seen.lock().unwrap().served.clone(),
        Some(Err("joined_shard_signature_invalid".to_owned()))
    );
    assert_eq!(ended["state"], "failed", "{ended}");
    assert_eq!(
        ended["error_code"], "joined_route_member_v2_unsupported",
        "{ended}"
    );
    assert!(
        ended["serve_reports"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["serve_error"] == "joined_shard_signature_invalid"),
        "{ended}"
    );
    eprintln!("old member: cleanup {}", ended["member_cleanup"]);
    let torn = Instant::now();
    work.teardown().unwrap();
    eprintln!(
        "old member: teardown after the failure took {:?}",
        torn.elapsed()
    );
}
