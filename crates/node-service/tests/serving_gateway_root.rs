// SPDX-License-Identifier: AGPL-3.0-only
//! S1 serving root R-C: the packaged `/chat/api/send`, its events and Stop go through the
//! oracle-checked `api::gateway::Gateway` (v0.1.4 `RequestGatewayService`, service.py:440-507,
//! 780-822, 578-621 @ f4e366e9) with `node_service::router_port` as its backend over the node
//! service's driver job (`RouterSessionBackend`, backend.py:186-375, 703-761).
//!
//! Every test drives the real `DesktopServices` request path with a scripted driver that runs a
//! real signed receipt session and a real signed cleanup quorum (as `tests/cancel_ack.rs`), over
//! a fresh private state root. The qualification source is scripted: it answers the node's own
//! development record and can drop it or change one binding field at a chosen read. Refusal codes
//! come from the executed oracle fixture behind `api::gateway_host`
//! (`crates/api/tests/fixtures/oracle_gateway_admission.json`).
#![cfg(feature = "desktop-shell")]
use api::{
    gateway::{Binding, Qualification, QualificationSource, Unavailable},
    schema::PublicError,
};
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use desktop::{
    lifecycle::Poll,
    services::{ServiceRequest, Services},
};
use node_service::{
    desktop_host::DesktopServices,
    receipts,
    router_port::{self, Ledger},
    service::{Driver, Job, NodeService},
};
use router::cleanup::{CleanupPeer, CleanupRound, Terminal};
use serde_json::{Value, json};
use std::{
    num::NonZeroUsize,
    sync::{Arc, Condvar, Mutex, mpsc},
    time::{Duration, Instant},
};

const GATEWAY_ORACLE: &str = include_str!("../../api/tests/fixtures/oracle_gateway_admission.json");

fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// The test's own re-check spacing (a tenth of the owner poll), not a protocol deadline.
fn step() -> Duration {
    millis(B::OwnerPoll) / 10
}

// ----- the executed oracle's refusal codes -----

/// `(binding field, code)` from `qualification_per_submit` (one changed field per submit), then
/// `("route", code)` from `revalidation_at_worker_start` (the route dropped before the worker
/// started it). The gateway's run-start revalidation uses the same `_compare` as its capture
/// (qualification.py:30-47), so the field codes are the oracle's answer at run start too.
fn oracle_codes() -> Vec<(String, String)> {
    let doc: Value = serde_json::from_str(GATEWAY_ORACLE).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let scenario = |label: &str| {
        doc["scenarios"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["label"] == label)
            .unwrap()
            .clone()
    };
    let mut codes = Vec::new();
    let mut field: Option<String> = None;
    for op in scenario("qualification_per_submit")["ops"]
        .as_array()
        .unwrap()
    {
        match op["op"].as_str().unwrap() {
            "binding_field" => field = Some(op["field"].as_str().unwrap().to_owned()),
            "submit" => {
                if let Some(field) = field.take() {
                    codes.push((field, op["error"]["code"].as_str().unwrap().to_owned()));
                }
            }
            _ => {}
        }
    }
    assert_eq!(codes.len(), 10, "one code per binding field");
    let start = scenario("revalidation_at_worker_start");
    let drain = start["ops"]
        .as_array()
        .unwrap()
        .iter()
        .find(|o| o["op"] == "drain")
        .unwrap();
    let events = drain["events"].as_array().unwrap();
    assert_eq!(
        (events.len(), &events[0][1], &events[1][1]),
        (2, &json!("accepted"), &json!("failed"))
    );
    codes.push(("route".into(), events[1][4].as_str().unwrap().to_owned()));
    codes
}

// ----- scripted qualification source -----

fn base() -> Binding {
    router_port::binding(&node_service::native::development_qualification().unwrap()).unwrap()
}
fn changed(mut b: Binding, field: &str) -> Binding {
    match field {
        "deployment_id" => b.deployment_id.push_str("-moved"),
        "deployment_epoch" => b.deployment_epoch += 1,
        "topology_version" => b.topology_version += 1,
        "path_manifest_digest" => b.path_manifest_digest = format!("sha256:{}", "1".repeat(64)),
        "qualification_id" => b.qualification_id.push_str("-next"),
        "model_id" => b.model_id.push_str("-other"),
        "resolved_commit" => b.resolved_commit = "b".repeat(40),
        "manifest_digest" => b.manifest_digest = format!("sha256:{}", "3".repeat(64)),
        "qualification_digest" => b.qualification_digest = format!("sha256:{}", "4".repeat(64)),
        "stage_load_proof_digests" => b
            .stage_load_proof_digests
            .push(format!("sha256:{}", "5".repeat(64))),
        other => panic!("unknown binding field {other}"),
    }
    b
}
#[derive(Clone)]
enum Mode {
    /// The node's own development record (admitted as today).
    Ready,
    /// No current record (`route_dropped`).
    Dropped,
    /// The first admission read of a send answers the record, the next one (the gateway's
    /// capture) the record with this field changed.
    CaptureChanged(&'static str),
    /// Admission reads answer the record; every read for an admitted request (run start, per
    /// token, before `completed`) answers it with this field changed, or nothing for `route`.
    RunChanged(String),
}
struct SourceState {
    mode: Mode,
    admissions: usize,
    reads: Vec<Option<String>>,
}
struct Source {
    record: Binding,
    state: Mutex<SourceState>,
}
impl Source {
    fn new(mode: Mode) -> Arc<Self> {
        Arc::new(Self {
            record: base(),
            state: Mutex::new(SourceState {
                mode,
                admissions: 0,
                reads: Vec::new(),
            }),
        })
    }
    fn set(&self, mode: Mode) {
        let mut s = self.state.lock().unwrap();
        s.mode = mode;
        s.admissions = 0;
    }
    fn take_reads(&self) -> Vec<Option<String>> {
        std::mem::take(&mut self.state.lock().unwrap().reads)
    }
}
impl QualificationSource for Source {
    fn current(&self, request_id: Option<&str>) -> Result<Option<Qualification>, Unavailable> {
        let mut s = self.state.lock().unwrap();
        s.reads.push(request_id.map(str::to_owned));
        if request_id.is_none() {
            s.admissions += 1;
        }
        let ready = |binding| {
            Ok(Some(Qualification {
                binding,
                route_ready: true,
            }))
        };
        match (&s.mode, request_id) {
            (Mode::Dropped, _) => Ok(None),
            (Mode::CaptureChanged(field), None) if s.admissions >= 2 => {
                ready(changed(self.record.clone(), field))
            }
            (Mode::RunChanged(field), Some(_)) if field == "route" => Ok(None),
            (Mode::RunChanged(field), Some(_)) => ready(changed(self.record.clone(), field)),
            _ => ready(self.record.clone()),
        }
    }
}

// ----- scripted driver: a real signed receipt session and cleanup quorum -----

#[derive(Default)]
struct Seen {
    documents: Vec<Value>,
    tokens_sent: usize,
    holding: bool,
    cancel_seen: bool,
    settled: Option<&'static str>,
    returned: usize,
    release: bool,
}
#[derive(Default)]
struct Rig {
    seen: Mutex<Seen>,
    changed: Condvar,
}
impl Rig {
    fn update(&self, f: impl FnOnce(&mut Seen)) {
        f(&mut self.seen.lock().unwrap());
        self.changed.notify_all();
    }
    fn read<T>(&self, f: impl FnOnce(&Seen) -> T) -> T {
        f(&self.seen.lock().unwrap())
    }
    fn wait(&self, what: &str, ready: impl Fn(&Seen) -> bool) {
        let deadline = Instant::now() + millis(B::WorkerStartup);
        let mut s = self.seen.lock().unwrap();
        while !ready(&s) {
            assert!(Instant::now() < deadline, "timeout: {what}");
            s = self.changed.wait_timeout(s, step()).unwrap().0;
        }
    }
    fn release(&self) {
        self.update(|s| s.release = true);
    }
}
#[derive(Clone, Copy, PartialEq)]
enum Hold {
    Never,
    /// After its tokens, until the job is cancelled.
    UntilCancelled,
    /// After its tokens, until released (a cancellation is only noted).
    UntilReleased,
    /// After its signed settle, until released, before its run returns.
    AfterSettle,
}
struct Script {
    rig: Arc<Rig>,
    tokens: usize,
    hold: Hold,
}
impl Driver for Script {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        self.rig.update(|s| s.documents.push(job.document.clone()));
        let result = self.script(job);
        self.rig.update(|s| s.returned += 1);
        result.map_err(|e| {
            eprintln!("scripted driver: {e}");
            PublicError::new(503, "scripted_driver_failed")
        })
    }
}
impl Script {
    /// Bounded: a driver never outlives the test's own budget.
    fn hold(&self, job: &Job, until: impl Fn(&Seen, bool) -> bool) {
        let deadline = Instant::now() + millis(B::WorkerStartup);
        let mut s = self.rig.seen.lock().unwrap();
        s.holding = true;
        self.rig.changed.notify_all();
        loop {
            let cancelled = job.cancelled();
            if cancelled && !s.cancel_seen {
                s.cancel_seen = true;
                self.rig.changed.notify_all();
            }
            if until(&s, cancelled) || Instant::now() >= deadline {
                break;
            }
            s = self.rig.changed.wait_timeout(s, step()).unwrap().0;
        }
        s.holding = false;
        self.rig.changed.notify_all();
    }
    fn script(&self, job: &mut Job) -> node_service::loopback::Result<()> {
        let owner = receipts::identity("serving-owner")?;
        let member = receipts::identity("serving-member")?;
        let (mut source, session) = receipts::Receipts::open(
            &owner,
            (&member.public_key_record(), member.endpoint_id()),
            &job.id,
            job.generation,
            &job.document,
            Duration::ZERO,
            job.deadline.saturating_duration_since(Instant::now()),
        )?;
        job.install(session)?;
        let stage = protocol::canonical::digest(&json!("serving-gateway-root"))?;
        for index in 0..self.tokens {
            let last = index + 1 == self.tokens;
            let envelope = source.sign(&owner, &[17 + index as u32], last, &stage)?;
            job.receipt(envelope, 0., |_| Ok(()))?;
            job.token(index, format!("t{index}"))?;
            self.rig.update(|s| s.tokens_sent += 1);
        }
        match self.hold {
            Hold::UntilCancelled => self.hold(job, |_, cancelled| cancelled),
            Hold::UntilReleased => self.hold(job, |s, _| s.release),
            Hold::Never | Hold::AfterSettle => {}
        }
        let cancelled = job.cancelled();
        let now = MonoInstant::new(ClockDomain(1), 0);
        let deadline = now
            .checked_add(
                B::StageCleanup
                    .span(Entries::ZERO)
                    .map_err(|e| format!("{e:?}"))?,
            )
            .map_err(|e| format!("{e:?}"))?;
        let mut round = CleanupRound::new(
            source.subject.clone(),
            vec![CleanupPeer {
                node: "b-member".into(),
                host: "loopback-mac".into(),
                process: json!(std::process::id()),
                run: format!("slice-{}", job.generation),
                deployment: "slice-development".into(),
                endpoint: member.endpoint_id().into(),
                key: member.public_key_record(),
                generation: json!(job.generation),
            }],
            deadline,
        )?;
        round.accept(
            "b-member",
            &receipts::cleaned(&member, &source.subject, job.generation, "b-member")?,
            || (now, true),
        )?;
        // Controlled inert environment, explicitly not a native residue proof.
        let proof = round.finish(|| Ok(()), || (now, true))?;
        let outcome = if cancelled {
            Terminal::Cancelled
        } else {
            Terminal::Completed
        };
        job.settle(proof, outcome, now)?;
        self.rig.update(|s| {
            s.settled = Some(if cancelled { "cancelled" } else { "completed" });
        });
        if self.hold == Hold::AfterSettle {
            self.hold(job, |s, _| s.release);
        }
        Ok(())
    }
}

// ----- the packaged request path -----

fn private() -> std::path::PathBuf {
    use std::os::unix::fs::DirBuilderExt;
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "serving-gateway-root-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
fn compose(rig: &Arc<Rig>, tokens: usize, hold: Hold, source: Arc<Source>) -> (Host, Ledger) {
    let node = NodeService::new(
        Arc::new(Script {
            rig: rig.clone(),
            tokens,
            hold,
        }),
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    let (host, ledger) =
        router_port::packaged_host(node, source, NonZeroUsize::new(4).unwrap(), &private());
    (Arc::new(host), ledger)
}
type Host = Arc<DesktopServices>;
fn request(host: &DesktopServices, method: &str, path: &str, body: Option<Value>) -> (u16, Value) {
    let (tx, rx) = mpsc::sync_channel(1);
    let channel = if path.starts_with("/chat/") {
        "x-mycelium-chat"
    } else {
        "x-mycelium-action"
    };
    host.submit(
        ServiceRequest {
            method: method.into(),
            target: path.into(),
            headers: vec![
                (channel.into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: body.map_or_else(Vec::new, |b| serde_json::to_vec(&b).unwrap()),
        },
        Box::new(move |reply| tx.send(reply).unwrap()),
    );
    let reply = rx.recv_timeout(millis(B::WorkerStartup)).unwrap();
    (
        reply.status,
        serde_json::from_slice(&reply.body).unwrap_or(Value::Null),
    )
}
fn send(host: &DesktopServices, prompt: &str, tokens: u64) -> (u16, Value) {
    request(
        host,
        "POST",
        "/chat/api/send",
        Some(json!({"prompt": prompt, "max_new_tokens": tokens})),
    )
}
fn events(host: &DesktopServices, id: &str, after: u64) -> Value {
    let (status, doc) = request(
        host,
        "POST",
        "/chat/api/events",
        Some(json!({"request_id": id, "after": after})),
    );
    assert_eq!(status, 200, "{doc}");
    doc
}
/// Poll the page's events from `after` until `done` (or `until` tokens); every token text.
fn drain_until(
    host: &DesktopServices,
    id: &str,
    after: u64,
    until: Option<usize>,
) -> (Vec<String>, Value) {
    let deadline = Instant::now() + millis(B::WorkerStartup);
    let (mut after, mut tokens) = (after, Vec::new());
    loop {
        let doc = events(host, id, after);
        for t in doc["tokens"].as_array().unwrap() {
            tokens.push(t.as_str().unwrap().to_owned());
        }
        after = doc["next"].as_u64().unwrap();
        if doc["done"] == true || until.is_some_and(|n| tokens.len() >= n) {
            return (tokens, doc);
        }
        assert!(Instant::now() < deadline, "request never finished: {doc}");
    }
}
fn drain(host: &DesktopServices, id: &str, after: u64) -> (Vec<String>, Value) {
    drain_until(host, id, after, None)
}
fn shut(host: &DesktopServices) {
    host.begin_shutdown(Instant::now() + millis(B::StageCleanup))
        .unwrap();
    let deadline = Instant::now() + millis(B::StageCleanup);
    while Instant::now() < deadline {
        match host.poll_shutdown() {
            Ok(Poll::Pending) => std::thread::sleep(step()),
            _ => return,
        }
    }
}
fn refusal(reply: &(u16, Value)) -> (u16, &str) {
    (reply.0, reply.1["error"].as_str().unwrap_or("<accepted>"))
}

// ----- the five verifier tests -----

/// Each packaged submit carries the qualification captured AT SUBMIT TIME (qualification.py
/// `capture` on every submit, :30-35; the binding compare, :70-91): the chat edge reads the
/// current record as v0.1.4's chat client does (chat.py:111-121) and submits its binding, the
/// gateway captures again and compares, and the admitted request is revalidated at run start,
/// per token and before `completed` (service.py:780-822). A send after the record dropped or
/// moved is refused; the next ready send is admitted and its job carries the record.
#[test]
fn serving_gateway_root_chat_send_captures_qualification() {
    let codes = oracle_codes();
    let stale = codes
        .iter()
        .find(|(f, _)| f == "qualification_id")
        .map(|(_, c)| c.clone())
        .unwrap();
    let rig = Arc::new(Rig::default());
    let source = Source::new(Mode::Ready);
    let (host, _) = compose(&rig, 1, Hold::Never, source.clone());
    let record = router_port::document(&source.record);
    assert_eq!(
        record,
        node_service::native::development_qualification().unwrap()
    );

    let (status, first) = send(&host, "one", 1);
    assert_eq!(status, 200, "{first}");
    let id = first["request_id"].as_str().unwrap().to_owned();
    let (tokens, last) = drain(&host, &id, 0);
    // Two admission reads (the chat edge's current record and the gateway's capture), then the
    // admitted request's revalidations: run start, its one token, before `completed`.
    let run = Some(id.clone());
    assert_eq!(
        source.take_reads(),
        [None, None, run.clone(), run.clone(), run],
        "qualification reads of the first send"
    );
    assert_eq!(tokens, ["t0"]);
    assert_eq!(
        (&last["error"], &last["terminal"]),
        (&Value::Null, &json!("completed"))
    );
    assert_eq!(rig.read(|s| s.documents.len()), 1);
    assert_eq!(
        rig.read(|s| s.documents[0]["qualification"].clone()),
        record
    );

    // The record dropped: this send is refused at its own read, nothing runs.
    source.set(Mode::Dropped);
    assert_eq!(
        refusal(&send(&host, "two", 1)),
        (503, "chat_route_not_qualified")
    );
    // The record moved between the chat edge's read and the gateway's capture: the oracle's
    // capture code, nothing runs.
    source.set(Mode::CaptureChanged("qualification_id"));
    assert_eq!(refusal(&send(&host, "three", 1)), (409, stale.as_str()));
    assert_eq!(rig.read(|s| s.documents.len()), 1, "a refused send ran");

    // Ready again: admitted, and its job carries the record captured for it.
    source.set(Mode::Ready);
    source.take_reads();
    let (status, fourth) = send(&host, "four", 1);
    assert_eq!(status, 200, "{fourth}");
    let id = fourth["request_id"].as_str().unwrap().to_owned();
    let (tokens, _) = drain(&host, &id, 0);
    assert_eq!(tokens, ["t0"]);
    assert_eq!(source.take_reads()[..2], [None, None]);
    assert_eq!(rig.read(|s| s.documents.len()), 2);
    assert_eq!(
        rig.read(|s| s.documents[1]["qualification"].clone()),
        record
    );
    shut(&host);
}

/// The qualification changed between capture and run: for every binding field and for a
/// dropped route, the packaged request ends `failed` with the executed oracle's code
/// (`revalidate` at worker start, service.py:786; `_compare`, qualification.py:70-91), after
/// `accepted` and without a token, and the driver never starts.
#[test]
fn serving_gateway_root_stale_qualification_refused() {
    let codes = oracle_codes();
    let mut matched = Vec::new();
    for (field, code) in &codes {
        if field != "route" {
            assert_ne!(changed(base(), field), base(), "{field} must change");
        }
        let rig = Arc::new(Rig::default());
        let source = Source::new(Mode::RunChanged(field.clone()));
        let (host, ledger) = compose(&rig, 1, Hold::Never, source.clone());
        let (status, accepted) = send(&host, "stale", 1);
        assert_eq!(status, 200, "{field}: {accepted}");
        let id = accepted["request_id"].as_str().unwrap().to_owned();
        let (tokens, last) = drain(&host, &id, 0);
        let got = (
            tokens.len(),
            last["terminal"].clone(),
            last["error"].clone(),
            rig.read(|s| s.documents.len()),
            ledger.driver_cancels(),
        );
        assert_eq!(
            got,
            (0, json!("failed"), json!(code), 0, 0),
            "{field}: oracle answers failed {code}"
        );
        assert_eq!(
            source.take_reads(),
            [None, None, Some(id)],
            "{field}: one run-start revalidation"
        );
        matched.push(field.clone());
        shut(&host);
    }
    assert_eq!(matched.len(), 11);
}

/// Stop reaches the driver's canceller exactly once (`_cancel_backend_once`, service.py:1321-1352
/// -> `RouterSessionBackend._cancel_once`, backend.py:703-761) and the request ends `cancelled`
/// from the driver's own signed settle. A second Stop after the terminal reaches nothing.
#[test]
fn serving_gateway_root_cancel_reaches_driver() {
    let rig = Arc::new(Rig::default());
    let (host, ledger) = compose(&rig, 1, Hold::UntilCancelled, Source::new(Mode::Ready));
    let (status, accepted) = send(&host, "stop me", 2);
    assert_eq!(status, 200, "{accepted}");
    let id = accepted["request_id"].as_str().unwrap().to_owned();
    let (tokens, _) = drain_until(&host, &id, 0, Some(1));
    assert_eq!(tokens, ["t0"]);
    rig.wait("driver holding", |s| s.holding);
    assert_eq!(ledger.driver_cancels(), 0, "nothing cancelled yet");

    let stopped = request(&host, "POST", "/api/serving/stop", Some(json!({})));
    assert_eq!(stopped, (200, json!({"stopped": true})));
    assert!(
        rig.read(|s| s.cancel_seen),
        "the driver saw the cancellation"
    );
    assert_eq!(rig.read(|s| s.settled), Some("cancelled"));
    assert_eq!(ledger.driver_cancels(), 1, "driver canceller calls");
    let (rest, last) = drain(&host, &id, 1);
    assert!(rest.is_empty(), "{rest:?}");
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &Value::Null)
    );
    // Near miss: Stop with the request already terminal.
    let again = request(&host, "POST", "/api/serving/stop", Some(json!({})));
    assert_eq!(again, (200, json!({"stopped": true})));
    assert_eq!(
        ledger.driver_cancels(),
        1,
        "a terminal request was cancelled again"
    );
    shut(&host);
}

/// `completed` is published only after the driver settled: its signed cleanup quorum settled the
/// job AND its run returned (RouterSessionBackend returns `completed` from the router's terminal
/// request status, backend.py:317-330). While the driver is held after its settle, the page sees
/// its tokens but not `done`.
#[test]
fn serving_gateway_root_terminal_after_driver_settles() {
    let rig = Arc::new(Rig::default());
    let (host, _) = compose(&rig, 2, Hold::AfterSettle, Source::new(Mode::Ready));
    let (status, accepted) = send(&host, "settle first", 2);
    assert_eq!(status, 200, "{accepted}");
    let id = accepted["request_id"].as_str().unwrap().to_owned();
    let (tokens, _) = drain_until(&host, &id, 0, Some(2));
    assert_eq!(tokens, ["t0", "t1"]);
    // The page keeps polling as it would; whenever it sees `done`, the driver must have returned.
    let check = |doc: &Value| {
        let (settled, held, returned) = rig.read(|s| (s.settled, s.holding, s.returned));
        if doc["done"] == true && returned == 0 {
            rig.release();
            panic!("published {doc} before the driver returned (settled={settled:?} held={held})");
        }
    };
    let deadline = Instant::now() + millis(B::WorkerStartup);
    while !rig.read(|s| s.settled.is_some() && s.holding) {
        check(&events(&host, &id, 2));
        assert!(Instant::now() < deadline, "the driver never settled");
    }
    // Held after its settle for a whole registry poll: never `done` meanwhile.
    let window = Instant::now() + millis(B::RegistryPoll);
    let mut polls = 0;
    while Instant::now() < window {
        check(&events(&host, &id, 2));
        polls += 1;
        std::thread::sleep(step());
    }
    assert!(polls >= 2, "{polls} polls");
    rig.release();
    let (rest, last) = drain(&host, &id, 2);
    assert!(rest.is_empty(), "{rest:?}");
    assert_eq!(
        rig.read(|s| s.returned),
        1,
        "completed before the driver returned"
    );
    assert_eq!(rig.read(|s| s.settled), Some("completed"));
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("completed"), &Value::Null)
    );
    shut(&host);
}

/// `GET /api/status` answers from the published snapshot while the driver is held and a Stop is
/// waiting for it (as `api::status_responsiveness::status_completes_while_backend_and_publication_are_held`;
/// app.py:1315-1326 @ f4e366e9, incident 68119358).
#[test]
fn serving_gateway_root_status_responsive_while_driver_held() {
    let rig = Arc::new(Rig::default());
    let (host, _) = compose(&rig, 1, Hold::UntilReleased, Source::new(Mode::Ready));
    // An idle Stop publishes the first status snapshot (nothing published answers 503).
    assert_eq!(
        request(&host, "POST", "/api/serving/stop", Some(json!({}))),
        (200, json!({"stopped": true}))
    );
    let (status, accepted) = send(&host, "hold", 2);
    assert_eq!(status, 200, "{accepted}");
    let id = accepted["request_id"].as_str().unwrap().to_owned();
    drain_until(&host, &id, 0, Some(1));
    rig.wait("driver holding", |s| s.holding);
    let stopper = host.clone();
    let stop =
        std::thread::spawn(move || request(&stopper, "POST", "/api/serving/stop", Some(json!({}))));
    rig.wait("cancellation reached the held driver", |s| s.cancel_seen);
    // Near miss: a send while the cancel is settling is the chat edge's busy answer, at once.
    assert_eq!(refusal(&send(&host, "again", 1)), (429, "chat_busy"));
    let (tx, rx) = mpsc::channel();
    let reader = host.clone();
    let started = Instant::now();
    std::thread::spawn(move || {
        let _ = tx.send(request(&reader, "GET", "/api/status", None));
    });
    let answer = rx.recv_timeout(millis(B::ModelSelection));
    let elapsed = started.elapsed();
    let still_held = rig.read(|s| s.holding) && !stop.is_finished();
    // Always release and join, including a broken host.
    rig.release();
    let stopped = stop.join().unwrap();
    eprintln!(
        "status_ms={} held={still_held} answered={}",
        elapsed.as_secs_f64() * 1000.0,
        answer.is_ok()
    );
    let (code, doc) = answer.expect("status must answer while the driver is held");
    assert!(still_held, "the driver was released before status answered");
    assert!(elapsed < millis(B::ModelSelection));
    assert_eq!(code, 200, "{doc}");
    assert_eq!(doc["label"], "Rust loopback composition");
    assert_eq!(doc["serving"]["route_ready"], false);
    assert_eq!(stopped, (200, json!({"stopped": true})));
    let (_, last) = drain(&host, &id, 1);
    assert_eq!(last["terminal"], "cancelled");
    assert_eq!(rig.read(|s| s.documents.len()), 1, "the busy send ran");
    shut(&host);
}

/// Near miss: a relaunch between submit and terminal. The new process's gateway does not know
/// the previous process's request (v0.1.4 keeps chat requests in memory: `chat_request_unknown`),
/// and serves its own.
#[test]
fn serving_gateway_root_relaunch_forgets_the_previous_request() {
    let held = Arc::new(Rig::default());
    let (before, _) = compose(&held, 1, Hold::UntilCancelled, Source::new(Mode::Ready));
    let (status, accepted) = send(&before, "before relaunch", 2);
    assert_eq!(status, 200, "{accepted}");
    let old = accepted["request_id"].as_str().unwrap().to_owned();
    held.wait("first process holding", |s| s.holding);
    let rig = Arc::new(Rig::default());
    let (after, _) = compose(&rig, 1, Hold::Never, Source::new(Mode::Ready));
    assert_eq!(
        refusal(&request(
            &after,
            "POST",
            "/chat/api/events",
            Some(json!({"request_id": old, "after": 0}))
        )),
        (404, "chat_request_unknown")
    );
    let (status, fresh) = send(&after, "after relaunch", 1);
    assert_eq!(status, 200, "{fresh}");
    let (tokens, last) = drain(&after, fresh["request_id"].as_str().unwrap(), 0);
    assert_eq!((tokens.len(), &last["terminal"]), (1, &json!("completed")));
    shut(&before);
    shut(&after);
}
