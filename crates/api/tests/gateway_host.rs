// SPDX-License-Identifier: AGPL-3.0-only
//! `api::gateway::Gateway` (the multi-session host) against the EXECUTED `RequestGatewayService`
//! (FX-L10-GATEWAY-ADMISSION: `L10-evidence/r13-probe-gateway.py` -> `fixtures/oracle_gateway_admission.json`,
//! v0.1.4 f4e366e9, Python 3.14): ALL 15 scripted multi-request scenarios, including the four that need the
//! qualification gate (`qualification_per_submit`, `revalidation_of_admitted_requests`,
//! `revalidation_at_worker_start`, `revalidation_per_token`) and the backend-cancel lists
//! (`cancelled_by_backend`, compared in order after `close()`). The backend here is the probe's: `fast` (two
//! tokens, completed), `hold` (parks until released or cancelled), `hold_after_tokens`, `hold_then_tokens`.
//! The probe's harness waits for a backend to be entered / a request to be terminal at the points it calls
//! `entered()` / `terminal()`; those waits are not ops in the fixture, so `settle()` below waits (bounded) for the
//! same condition before each op: every request that has a free worker is either parked in its backend or
//! settled. Nothing here sleeps for a fixed time.
use api::{
    gateway::{
        Backend, Binding, Config, Gateway, Outcome, Qualification, QualificationSource, Refusal,
        Submission, Unavailable,
    },
    registry::{AdmissionError, LastEventId, Pull, Subscription},
};
use budgets::{BudgetId, Entries};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Condvar, Mutex},
    time::{Duration, Instant},
};

/// The test's own re-check spacing (a fiftieth of the owner poll), not a protocol deadline.
fn poll() -> Duration {
    Duration::from_millis(BudgetId::OwnerPoll.span(Entries::new(0)).unwrap().millis() / 50)
}
/// How long a wait for the gateway may take before the test calls it hung (200 re-checks of the owner poll).
fn limit() -> Duration {
    poll() * 5_000
}

fn base_binding() -> Binding {
    Binding {
        deployment_id: "11111111-1234-5678-1234-567812345678".into(),
        deployment_epoch: 1,
        topology_version: 42,
        path_manifest_digest: format!("sha256:{}", "a".repeat(64)),
        qualification_id: format!("sha256:{}", "b".repeat(64)),
        model_id: "org/model".into(),
        resolved_commit: "a".repeat(40),
        manifest_digest: format!("sha256:{}", "c".repeat(64)),
        qualification_digest: format!("sha256:{}", "d".repeat(64)),
        stage_load_proof_digests: vec![format!("sha256:{}", "e".repeat(64))],
    }
}
fn changed(mut b: Binding, field: &str) -> Binding {
    match field {
        "deployment_id" => b.deployment_id = "99999999-1234-5678-1234-567812345678".into(),
        "deployment_epoch" => b.deployment_epoch = 2,
        "topology_version" => b.topology_version = 43,
        "path_manifest_digest" => b.path_manifest_digest = format!("sha256:{}", "1".repeat(64)),
        "qualification_id" => b.qualification_id = format!("sha256:{}", "2".repeat(64)),
        "model_id" => b.model_id = "org/other".into(),
        "resolved_commit" => b.resolved_commit = "b".repeat(40),
        "manifest_digest" => b.manifest_digest = format!("sha256:{}", "3".repeat(64)),
        "qualification_digest" => b.qualification_digest = format!("sha256:{}", "4".repeat(64)),
        "stage_load_proof_digests" => {
            b.stage_load_proof_digests = vec![format!("sha256:{}", "5".repeat(64))];
        }
        other => panic!("unknown binding field {other}"),
    }
    b
}

/// The probe's `Source`: `ok`, `none` (route dropped), `not_ready` (the oracle rejects the clone as not a
/// record: `qualification_unavailable`), `raise`.
struct Source(Mutex<String>);
impl QualificationSource for Source {
    fn current(&self, _request_id: Option<&str>) -> Result<Option<Qualification>, Unavailable> {
        match self.0.lock().unwrap().as_str() {
            "none" => Ok(None),
            "not_ready" | "raise" => Err(Unavailable),
            _ => Ok(Some(Qualification {
                binding: base_binding(),
                route_ready: true,
            })),
        }
    }
}

#[derive(Default)]
struct Parked {
    /// In the backend and waiting for a release (the probe's `entered` is set at the start of `run`; a
    /// `hold_after_tokens` request has by then also emitted its tokens, which `quiet()` must see).
    entered: HashSet<String>,
    released: HashSet<String>,
    cancelled: Vec<String>,
}
struct ScriptedBackend {
    modes: HashMap<String, String>,
    parked: Mutex<Parked>,
    changed: Condvar,
}
impl ScriptedBackend {
    fn wait_release(&self, rid: &str) {
        let deadline = Instant::now() + limit();
        let mut parked = self.parked.lock().unwrap();
        parked.entered.insert(rid.into());
        while !parked.released.contains(rid) {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return;
            }
            parked = self.changed.wait_timeout(parked, left).unwrap().0;
        }
    }
    fn release(&self, rid: &str) {
        self.parked.lock().unwrap().released.insert(rid.into());
        self.changed.notify_all();
    }
}
impl Backend for ScriptedBackend {
    fn run(
        &self,
        request_id: &str,
        emit: &mut dyn FnMut(u64, &str) -> Result<(), Refusal>,
        is_cancelled: &dyn Fn() -> bool,
    ) -> Result<Outcome, Refusal> {
        {
            self.parked
                .lock()
                .unwrap()
                .entered
                .insert(request_id.into());
        }
        self.changed.notify_all();
        let mode = self.modes.get(request_id).map_or("fast", String::as_str);
        let verdict = |c: bool| {
            if c {
                Outcome::Cancelled
            } else {
                Outcome::Completed
            }
        };
        if mode == "hold" {
            self.wait_release(request_id);
            return Ok(verdict(is_cancelled()));
        }
        if mode == "hold_then_tokens" {
            self.wait_release(request_id);
        }
        emit(0, "a")?;
        emit(1, "b")?;
        if mode == "hold_after_tokens" {
            self.wait_release(request_id);
            return Ok(verdict(is_cancelled()));
        }
        Ok(Outcome::Completed)
    }
    fn cancel(&self, request_id: &str) -> bool {
        self.parked
            .lock()
            .unwrap()
            .cancelled
            .push(request_id.into());
        self.release(request_id);
        true
    }
}

struct Harness {
    gw: Gateway,
    source: Arc<Source>,
    backend: Arc<ScriptedBackend>,
    clock: Arc<Mutex<f64>>,
    drawn: Arc<Mutex<usize>>,
    subs: HashMap<String, Subscription>,
    admitted: Vec<String>,
    modes: HashMap<String, String>,
    max_concurrent: usize,
    pending_field: Option<String>,
    /// Admitted while every worker was parked in a held request: the oracle's thread pool starts these a
    /// few microseconds after a worker frees, and (the GIL keeping its main thread running) never before the
    /// probe's next blocking wait, so whether `close()` finds them started is a race in the oracle itself.
    queued_at_admission: HashSet<String>,
}
impl Harness {
    fn new(scenario: &Value, grace: f64) -> Self {
        let config = &scenario["config"];
        let get = |k: &str, d: u64| config.get(k).and_then(Value::as_u64).unwrap_or(d) as usize;
        let modes: HashMap<String, String> = scenario["modes"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
            .collect();
        let ids: Vec<String> = scenario["ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|i| i.as_str().unwrap().to_owned())
            .collect();
        let backend = Arc::new(ScriptedBackend {
            modes: modes.clone(),
            parked: Mutex::default(),
            changed: Condvar::new(),
        });
        let source = Arc::new(Source(Mutex::new("ok".into())));
        let clock = Arc::new(Mutex::new(0.0));
        let drawn = Arc::new(Mutex::new(0usize));
        let (drawn_in, mut queue) = (drawn.clone(), ids.into_iter());
        let clock_in = clock.clone();
        let max_concurrent = get("max_concurrent_requests", 4);
        let gw = Gateway::new(
            Config {
                max_buffered_events: 64,
                max_sessions: get("max_sessions", 1_024),
                max_concurrent_requests: max_concurrent,
                max_pending_requests: get("max_pending_requests", 64),
                eviction_grace: grace,
            },
            source.clone(),
            backend.clone(),
            Box::new(move || {
                *drawn_in.lock().unwrap() += 1;
                queue.next().expect("the scenario ran out of request ids")
            }),
            Arc::new(move || *clock_in.lock().unwrap()),
        )
        .unwrap();
        Self {
            gw,
            source,
            backend,
            clock,
            drawn,
            subs: HashMap::new(),
            admitted: Vec::new(),
            modes,
            max_concurrent,
            pending_field: None,
            queued_at_admission: HashSet::new(),
        }
    }
    fn mode(&self, rid: &str) -> &str {
        self.modes.get(rid).map_or("fast", String::as_str)
    }
    /// Every request that has a free worker is parked in its backend (held modes) or settled (fast ones).
    fn quiet(&self) -> bool {
        let parked = self.backend.parked.lock().unwrap();
        let mut free = self.max_concurrent;
        for rid in &self.admitted {
            let Some(done) = self.gw.worker_done(rid) else {
                continue; // evicted
            };
            if done {
                continue;
            }
            if parked.released.contains(rid) {
                return false;
            }
            if parked.entered.contains(rid) {
                if self.mode(rid) == "fast" {
                    return false;
                }
                free = free.saturating_sub(1);
            } else if free > 0 {
                return false;
            }
        }
        true
    }
    fn settle(&self) {
        let deadline = Instant::now() + limit();
        while !self.quiet() {
            assert!(Instant::now() < deadline, "the gateway never settled");
            std::thread::sleep(poll());
        }
    }
    fn wait_for(&self, what: &str, mut ready: impl FnMut() -> bool) {
        let deadline = Instant::now() + limit();
        while !ready() {
            assert!(Instant::now() < deadline, "timeout: {what}");
            std::thread::sleep(poll());
        }
    }
    fn drain(&mut self, key: &str, count: usize) -> Option<Vec<Value>> {
        let mut sub = self.subs.remove(key)?;
        let mut events = Vec::new();
        while events.len() < count {
            match self.gw.next_event(&mut sub, Duration::ZERO).unwrap() {
                Pull::Event(e) => {
                    let get = |name: &str| e.payload.get(name).cloned().unwrap_or(Value::Null);
                    events.push(json!([
                        e.sequence,
                        e.kind,
                        get("token_index"),
                        get("text"),
                        get("code"),
                        Value::Null
                    ]));
                    self.gw.ack(&mut sub, e.sequence).unwrap();
                    if matches!(e.kind.as_str(), "completed" | "failed" | "cancelled") {
                        break;
                    }
                }
                Pull::End | Pull::Pending => break,
            }
        }
        self.subs.insert(key.to_owned(), sub);
        Some(events)
    }
}

#[test]
fn the_gateway_host_equals_the_executed_request_gateway_service() {
    let doc: Value =
        serde_json::from_str(include_str!("fixtures/oracle_gateway_admission.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let grace = doc["grace"].as_f64().unwrap();
    let scenarios = doc["scenarios"].as_array().unwrap();
    assert_eq!(scenarios.len(), 15);
    let (mut ops_run, mut failures) = (0usize, Vec::new());
    for scenario in scenarios {
        let label = scenario["label"].as_str().unwrap();
        let mut h = Harness::new(scenario, grace);
        let op_count = scenario["ops"].as_array().unwrap().len();
        let mut running_at_the_end: HashSet<String> = HashSet::new();
        for (index, op) in scenario["ops"].as_array().unwrap().iter().enumerate() {
            ops_run += 1;
            let kind = op["op"].as_str().unwrap();
            if index + 1 == op_count {
                // What had begun running (parked in its backend, or done) before the last op's own settle.
                let parked = h.backend.parked.lock().unwrap();
                running_at_the_end = h
                    .admitted
                    .iter()
                    .filter(|r| parked.entered.contains(*r) || h.gw.worker_done(r) == Some(true))
                    .cloned()
                    .collect();
            }
            h.settle();
            let mut problems = Vec::new();
            let token = op.get("token").and_then(Value::as_str);
            match kind {
                "submit" => {
                    let binding = match h.pending_field.take() {
                        Some(field) => changed(base_binding(), &field),
                        None => base_binding(),
                    };
                    let busy = {
                        let parked = h.backend.parked.lock().unwrap();
                        h.admitted
                            .iter()
                            .filter(|r| {
                                parked.entered.contains(*r) && h.gw.worker_done(r) == Some(false)
                            })
                            .count()
                    };
                    let got = h.gw.submit(
                        &Submission {
                            binding,
                            max_new_tokens: 2,
                        },
                        token,
                    );
                    if let Ok(id) = &got {
                        if busy >= h.max_concurrent {
                            h.queued_at_admission.insert(id.clone());
                        }
                        h.admitted.push(id.clone());
                    }
                    let want_id = op.get("result").and_then(Value::as_str);
                    let want_code = op.get("error").map(|e| e["code"].as_str().unwrap());
                    let (id, code) = match &got {
                        Ok(id) => (Some(id.as_str()), None),
                        Err(AdmissionError(code)) => (None, Some(*code)),
                    };
                    if (id, code) != (want_id, want_code) {
                        problems.push(format!(
                            "submit {got:?} != {} / {}",
                            op["result"], op["error"]
                        ));
                    }
                    let drawn = *h.drawn.lock().unwrap();
                    if drawn as u64 != op["ids_drawn"].as_u64().unwrap() {
                        problems.push(format!("ids drawn {drawn} != {}", op["ids_drawn"]));
                    }
                }
                "source" => *h.source.0.lock().unwrap() = op["mode"].as_str().unwrap().into(),
                "binding_field" => h.pending_field = Some(op["field"].as_str().unwrap().into()),
                "release" => h.backend.release(op["rid"].as_str().unwrap()),
                "terminal" => {
                    let rid = op["rid"].as_str().unwrap();
                    h.wait_for(&format!("terminal {rid}"), || {
                        h.gw.worker_done(rid) == Some(true)
                    });
                }
                "set_time" => *h.clock.lock().unwrap() = op["offset"].as_f64().unwrap(),
                "present" => {
                    for (rid, want) in op["present"].as_object().unwrap() {
                        let got = h.gw.contains(rid);
                        if Some(got) != want.as_bool() {
                            problems.push(format!("present {rid}: {got} != {want}"));
                        }
                    }
                }
                "subscribe" => {
                    let rid = op["rid"].as_str().unwrap();
                    let key = op["key"].as_str().unwrap().to_owned();
                    let last = match op.get("last") {
                        Some(Value::Array(a)) => Some(LastEventId::Bound(
                            a[0].as_i64().unwrap(),
                            a[1].as_i64().unwrap(),
                        )),
                        Some(Value::Number(n)) => Some(LastEventId::Cursor(n.as_i64().unwrap())),
                        _ => None,
                    };
                    match (
                        h.gw.subscribe(rid, last, token),
                        op.get("result"),
                        op.get("error"),
                    ) {
                        (Ok(sub), Some(want), None) => {
                            if json!({"publisher_generation": sub.publisher_generation}) != *want {
                                problems.push(format!(
                                    "generation {} != {want}",
                                    sub.publisher_generation
                                ));
                            }
                            h.subs.insert(key, sub);
                        }
                        (Err(AdmissionError(code)), None, Some(want)) if want["code"] == code => {}
                        (got, w, e) => problems.push(format!("subscribe {got:?} vs {w:?} / {e:?}")),
                    }
                }
                "drain" => {
                    let want = op["events"].as_array();
                    let got = h.drain(op["key"].as_str().unwrap(), want.map_or(0, Vec::len));
                    if got.map(Value::Array) != want.cloned().map(Value::Array) {
                        problems.push(format!("events differ from {}", op["events"]));
                    }
                }
                "close_subscription" => {
                    let key = op["key"].as_str().unwrap();
                    if let Some(mut sub) = h.subs.remove(key) {
                        h.gw.close_subscription(&mut sub).unwrap();
                    } else if op.get("skipped").is_none() {
                        problems.push(format!("no subscription {key}"));
                    }
                }
                "cancel" => match (
                    h.gw.cancel(op["rid"].as_str().unwrap(), token),
                    op.get("result"),
                    op.get("error"),
                ) {
                    (Ok(v), Some(want), None) if want.as_bool() == Some(v) => {}
                    (Err(AdmissionError(code)), None, Some(want)) if want["code"] == code => {}
                    (got, w, e) => problems.push(format!("cancel {got:?} vs {w:?} / {e:?}")),
                },
                "close" => h.gw.close(),
                "note" => {}
                other => panic!("unmodelled op {other}"),
            }
            if !problems.is_empty() {
                failures.push(format!(
                    "{label}#{index} ({kind}): {}",
                    problems.join(" | ")
                ));
            }
        }
        // `done()` closes the service, then reads the backend's cancel list. The probe does not wait before it:
        // a request still QUEUED behind a worker that has just been cancelled (an eviction, or a release) is
        // not backend-cancelled by `close()` in the oracle (its worker had not started it; the GIL keeps the
        // main thread running), and starts a few microseconds later in a real parallel runtime. So a request
        // that had not entered its backend when the script ended may or may not be in the list; one that had
        // must be, in the oracle's order.
        let not_yet_started: HashSet<String> = h
            .admitted
            .iter()
            .filter(|r| !running_at_the_end.contains(*r) || h.queued_at_admission.contains(*r))
            .cloned()
            .collect();
        h.gw.close();
        let cancelled: Vec<String> = h
            .backend
            .parked
            .lock()
            .unwrap()
            .cancelled
            .iter()
            .filter(|r| {
                !not_yet_started.contains(*r)
                    || scenario["cancelled_by_backend"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|w| w == *r)
            })
            .cloned()
            .collect();
        let want: Vec<String> = scenario["cancelled_by_backend"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect();
        if cancelled != want {
            failures.push(format!(
                "{label}: backend cancel list {cancelled:?} != {want:?}"
            ));
        }
    }
    assert!(ops_run >= 150, "{ops_run} ops");
    assert!(
        failures.is_empty(),
        "{} differences:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
