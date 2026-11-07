// SPDX-License-Identifier: AGPL-3.0-only
//! F5 MODEL control: held fake backend and held collector, not native load.
//! Real authenticated concurrent TCP status requests must complete before either
//! phase is released. Source: app.py:1315-1326 @ f4e366e9 / incident 68119358.
use api::{
    actions::{Action, Backend, Control},
    desktop::{Desktop, Session},
    http::serve_connection,
    schema::{PublicError, Snapshot},
    status::StatusOwner,
};
use budgets::BudgetId;
use journey_tests::{duration, http::Client};
use serde_json::{Map, Value, json};
use std::{
    net::{Ipv4Addr, TcpListener},
    sync::{Arc, mpsc},
    thread,
    time::Instant,
};
fn snapshot(label: &str) -> Snapshot {
    let mut value: Value =
        serde_json::from_str(include_str!("../contracts/status-example.json")).unwrap();
    value["label"] = json!(label);
    Snapshot::parse(value).unwrap()
}
struct Slow {
    owner: Arc<StatusOwner>,
    entered: mpsc::Sender<&'static str>,
    release: mpsc::Receiver<()>,
}
impl Slow {
    fn hold(&self, phase: &'static str) -> Result<(), PublicError> {
        self.entered
            .send(phase)
            .map_err(|_| PublicError::new(503, "test_gate_closed"))?;
        self.release
            .recv_timeout(duration(BudgetId::WorkerStartup).unwrap())
            .map_err(|_| PublicError::new(503, "test_gate_expired"))
    }
}
impl Backend for Slow {
    fn prepare(&mut self, _: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        Ok(())
    }
    fn execute(&mut self, _: Action, _: &Map<String, Value>) -> Result<Value, PublicError> {
        self.hold("backend")?;
        Ok(json!({"state":"finished"}))
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        self.owner
            .refresh(|| {
                self.hold("collector")?;
                Ok((1, snapshot("after")))
            })
            .map_err(|e| e.primary)
    }
}
#[test]
fn status_completes_while_backend_and_publication_are_held() {
    for (path, body) in [
        (
            "/api/serving/compile",
            json!({"model_preparation_acknowledged":true}),
        ),
        ("/api/serving/stop", json!({})),
    ] {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let host = listener.local_addr().unwrap().to_string();
        let origin = format!("http://{host}");
        let owner = Arc::new(StatusOwner::new(1).unwrap());
        owner.refresh(|| Ok((1, snapshot("before")))).unwrap();
        let (entered, entered_rx) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        let control = Control::new(Slow {
            owner: owner.clone(),
            entered,
            release: release_rx,
        });
        let session = Session::new(
            "launch".into(),
            "session".into(),
            Instant::now() + duration(BudgetId::WorkerStartup).unwrap(),
            duration(BudgetId::WorkerStartup).unwrap(),
        )
        .unwrap();
        let app = Arc::new(Desktop::new(
            host,
            Arc::new(control),
            owner.status(),
            session,
        ));
        let server = thread::spawn(move || {
            let mut workers = Vec::new();
            for _ in 0..5 {
                let (stream, _) = listener.accept().unwrap();
                let app = app.clone();
                workers.push(thread::spawn(move || {
                    serve_connection(
                        stream,
                        duration(BudgetId::WorkerStartup).unwrap(),
                        |r| app.preflight(r),
                        |r| app.handle(r),
                    )
                }));
            }
            workers
                .into_iter()
                .map(|w| w.join().unwrap())
                .collect::<Vec<_>>()
        });
        let mut client = Client::new(&origin).unwrap();
        client.sign_in("launch").unwrap();
        let client = Arc::new(client);
        let action_client = client.clone();
        let action = thread::spawn(move || action_client.post(path, &body));
        let mut responses = Vec::new();
        for phase in ["backend", "collector"] {
            assert_eq!(
                entered_rx
                    .recv_timeout(duration(BudgetId::WorkerStartup).unwrap())
                    .unwrap(),
                phase
            );
            let started = Instant::now();
            let response = client.get("/api/status");
            let elapsed = started.elapsed();
            let still_held = !action.is_finished();
            // Always release and join, including the deliberately broken MODEL.
            release.send(()).unwrap();
            responses.push((phase, response, elapsed, still_held));
        }
        let action_result = action.join().unwrap();
        let after = client.get("/api/status");
        let server_results = server.join().unwrap();
        for (phase, response, elapsed, still_held) in responses {
            eprintln!(
                "F5 path={path} phase={phase} status_ms={} held={still_held} success={} budget=ModelSelection",
                elapsed.as_secs_f64() * 1000.0,
                response.is_ok()
            );
            let response = response
                .expect("status must complete while slow backend/collector remains blocked");
            assert!(elapsed < duration(BudgetId::ModelSelection).unwrap());
            assert!(still_held);
            assert_eq!(response.status, 200);
            assert_eq!(response.json().unwrap()["label"], "before");
        }
        assert_eq!(action_result.unwrap().status, 200);
        assert_eq!(after.unwrap().json().unwrap()["label"], "after");
        for result in server_results {
            result.unwrap();
        }
    }
}
