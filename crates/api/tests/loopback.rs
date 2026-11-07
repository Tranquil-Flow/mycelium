// SPDX-License-Identifier: AGPL-3.0-only
//! Real loopback adapter, controlled fake service. NOT a model/worker journey.
use api::{
    desktop::{Desktop, Service, Session},
    http::serve_connection,
    schema::{PublicError, Snapshot},
    status::PublishedStatus,
};
use budgets::BudgetId;
use journey_tests::{compare, duration, http::Client, project};
use serde_json::{Map, Value, json};
use std::{
    net::{Ipv4Addr, TcpListener},
    sync::{Arc, Mutex, mpsc},
    thread,
    time::Instant,
};

struct Fake {
    entered: mpsc::Sender<String>,
    gate: Mutex<mpsc::Receiver<()>>,
}
impl Service for Fake {
    fn action(&self, path: &str, body: &Map<String, Value>) -> Result<Value, PublicError> {
        match path {
            "/api/serving/compile" | "/api/swarm/publish" | "/api/serving/stop" => {
                // Hold a backend lock until the test releases it. Status MUST NOT
                // acquire this lock or wait for this service call to complete.
                let wait = self
                    .gate
                    .lock()
                    .map_err(|_| PublicError::new(503, "fake_poisoned"))?;
                self.entered
                    .send(path.into())
                    .map_err(|_| PublicError::new(503, "fake_entered_closed"))?;
                wait.recv()
                    .map_err(|_| PublicError::new(503, "fake_gate_closed"))?;
                Ok(json!({"route_ready":false}))
            }
            "/api/create" if !body.contains_key("consent") => {
                Err(PublicError::new(400, "consent_required"))
            }
            _ => Err(PublicError::new(400, "unknown_action")),
        }
    }
}
fn serve(
    count: usize,
) -> (
    String,
    thread::JoinHandle<()>,
    mpsc::Receiver<String>,
    mpsc::Sender<()>,
    Arc<PublishedStatus>,
) {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let host = listener.local_addr().unwrap().to_string();
    let origin = format!("http://{host}");
    let (entered, seen) = mpsc::channel();
    let (release, wait) = mpsc::channel();
    let status = Arc::new(PublishedStatus::new());
    let value: Value =
        serde_json::from_str(include_str!("../contracts/status-example.json")).unwrap();
    status.publish(1, Snapshot::parse(value).unwrap()).unwrap();
    let session = Session::new(
        "test-launch".into(),
        "test-session".into(),
        Instant::now() + duration(BudgetId::WorkerStartup).unwrap(),
        duration(BudgetId::WorkerStartup).unwrap(),
    )
    .unwrap();
    let app = Arc::new(Desktop::new(
        host,
        Arc::new(Fake {
            entered,
            gate: Mutex::new(wait),
        }),
        status.clone(),
        session,
    ));
    let job = thread::spawn(move || {
        let mut workers = Vec::new();
        for _ in 0..count {
            let (stream, _) = listener.accept().unwrap();
            let app = app.clone();
            workers.push(thread::spawn(move || {
                serve_connection(
                    stream,
                    duration(BudgetId::ModelSelection).unwrap(),
                    |r| app.preflight(r),
                    |r| app.handle(r),
                )
            }));
        }
        for job in workers {
            job.join().unwrap().unwrap();
        }
    });
    (origin, job, seen, release, status)
}

#[test]
fn unchanged_l3_golden_assertions_over_actual_rust_http() {
    // Reuse exact scalar assertions and comparator from the Python-proven corpus.
    // The full L3 launcher is pinned to the certified Python executable; it is
    // intentionally NOT bypassed or changed to accept a fake Rust application.
    let (origin, job, _, _, _) = serve(5);
    let mut c = Client::new(&origin).unwrap();
    c.sign_in("test-launch").unwrap();
    let replies = [
        ("first_run", c.get("/api/status").unwrap()),
        (
            "missing_cookie_refused",
            c.request(
                "GET",
                "/api/status",
                b"",
                &[],
                false,
                BudgetId::ModelSelection,
            )
            .unwrap(),
        ),
        (
            "wrong_origin_refused",
            c.request(
                "POST",
                "/api/create",
                b"{}",
                &[
                    ("Content-Type", "application/json"),
                    ("X-Mycelium-Action", "1"),
                    ("Origin", "http://127.0.0.1:1"),
                ],
                true,
                BudgetId::ModelSelection,
            )
            .unwrap(),
        ),
        (
            "missing_consent_refused",
            c.post("/api/create", &json!({})).unwrap(),
        ),
    ];
    job.join().unwrap();
    let golden: Value = serde_json::from_str(include_str!(
        "../../../test-support/journeys/fixtures/python-v014.json"
    ))
    .unwrap();
    let corpus = golden["observations"].as_array().unwrap();
    let mut expected = Vec::new();
    let mut actual = Vec::new();
    for (id, reply) in replies {
        let old = corpus.iter().find(|o| o["id"] == id).unwrap().clone();
        let keys = old["fields"]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>();
        let mut observed = old.clone();
        observed["status"] = json!(reply.status);
        observed["fields"] = project(&reply.json().unwrap(), &keys).unwrap();
        expected.push(old);
        actual.push(observed);
    }
    compare(&json!(expected), &json!(actual)).unwrap();
    eprintln!("L3_ADAPTER_OBSERVATIONS=4; FULL_L3=NOT_RUN; BACKEND=fake");
}

#[test]
fn measured_compile_publish_close_stalls_do_not_block_status_under_load() {
    for phase in [
        "/api/serving/compile",
        "/api/swarm/publish",
        "/api/serving/stop",
    ] {
        const READERS: usize = 16;
        let (origin, job, entered, release, _) = serve(READERS + 2);
        let mut c = Client::new(&origin).unwrap();
        c.sign_in("test-launch").unwrap();
        let c = Arc::new(c);
        let action_client = c.clone();
        let started = Instant::now();
        let action = thread::spawn(move || action_client.post(phase, &json!({})));
        assert_eq!(
            entered
                .recv_timeout(duration(BudgetId::ModelSelection).unwrap())
                .unwrap(),
            phase
        );
        let (tx, rx) = mpsc::channel();
        let mut readers = Vec::new();
        for _ in 0..READERS {
            let c = c.clone();
            let tx = tx.clone();
            readers.push(thread::spawn(move || {
                let now = Instant::now();
                let reply = c.get("/api/status");
                tx.send((now.elapsed(), reply)).unwrap();
            }));
        }
        drop(tx);
        let mut measured = Vec::new();
        let mut failure = None;
        for _ in 0..READERS {
            match rx.recv_timeout(duration(BudgetId::OwnerPoll).unwrap()) {
                Ok((elapsed, reply)) => measured.push((elapsed, reply)),
                Err(e) => {
                    failure = Some(e);
                    break;
                }
            }
        }
        let still_held = !action.is_finished();
        release.send(()).unwrap();
        assert_eq!(action.join().unwrap().unwrap().status, 200);
        for reader in readers {
            reader.join().unwrap();
        }
        job.join().unwrap();
        assert!(
            failure.is_none(),
            "status waited for backend {phase}: {failure:?}"
        );
        assert!(still_held, "fault was not held while status completed");
        let latencies: Vec<_> = measured
            .iter()
            .map(|(elapsed, reply)| {
                let reply = reply.as_ref().unwrap();
                assert_eq!(reply.status, 200);
                assert_eq!(reply.json().unwrap()["serving"]["route_ready"], false);
                assert!(*elapsed < duration(BudgetId::OwnerPoll).unwrap());
                elapsed.as_micros()
            })
            .collect();
        eprintln!(
            "STALL phase={phase} readers={} max_status_us={} held_total_us={} samples_us={latencies:?}",
            latencies.len(),
            latencies.iter().max().unwrap(),
            started.elapsed().as_micros()
        );
    }
}
