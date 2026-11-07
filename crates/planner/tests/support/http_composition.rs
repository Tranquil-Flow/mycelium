// SPDX-License-Identifier: AGPL-3.0-only
//! Component composition, not the installed application's service owner. The
//! transport, Desktop guards/status, signed caps, planner, Core and cache are real.
//! This test host supplies action wiring and inert artifact bytes, never a model
//! load, native compiler process, remote worker or measured latency qualification.
use super::*;
use api::{
    desktop::{Desktop, Service, Session},
    http,
    schema::{PublicError, Snapshot},
    status::PublishedStatus,
};
use artifacts::{ArtifactKey, Cache};
use std::{
    collections::BTreeMap,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
fn budget() -> Duration {
    Duration::from_millis(span(B::WorkerStartup).millis())
}
struct Hold {
    entered: mpsc::Sender<()>,
    release: Mutex<mpsc::Receiver<()>>,
}
struct Run {
    core: node::Core,
    prepared: Option<Prepared>,
}
struct Composition {
    run: Mutex<Run>,
    fixture: Value,
    case: Value,
    members: Vec<AdmittedMember>,
    cache: Cache,
    required: BTreeMap<String, ArtifactKey>,
    destination: PathBuf,
    status: Arc<PublishedStatus>,
    revision: AtomicU64,
    stage_calls: AtomicU64,
    hold: Option<Hold>,
    compiler_fault: bool,
}
impl Composition {
    fn publish(&self, error: Option<&str>) -> Result<(), PublicError> {
        let mut value: Value =
            serde_json::from_str(include_str!("../../../api/contracts/status-example.json"))
                .unwrap();
        value["owner_run"] = json!({"error_code": error});
        self.status.publish(
            self.revision.fetch_add(1, Ordering::SeqCst),
            Snapshot::parse(value)?,
        )
    }
    fn start(&self, body: &serde_json::Map<String, Value>) -> Result<Value, PublicError> {
        let request = Value::Object(body.clone());
        if request["model_id"] != self.case["model_id"] {
            return Err(PublicError::new(400, "model_resources_unregistered"));
        }
        let mut run = self.run.lock().unwrap();
        let mut phases = phases();
        phases.compile = span(B::WorkerStartup);
        let trusted = input(&self.fixture, &self.case);
        let mut prepared = prepare(
            &mut run.core,
            PlanInputs {
                request: &request,
                ..trusted
            },
            &self.members,
            now(0),
            phases,
            restart(),
        )
        .map_err(|e| match e {
            Error::Planner(e) => PublicError::new(409, e.code),
            Error::Control(node::Error::Refused(code)) => PublicError::new(409, code),
            _ => PublicError::new(500, "control_failure"),
        })?;
        let result = prepared.compile(&mut run.core, &mut || now(0), |_| {
            if let Some(hold) = &self.hold {
                hold.entered.send(()).unwrap();
                hold.release.lock().unwrap().recv_timeout(budget()).unwrap();
            }
            if self.compiler_fault {
                let f: Value =
                    serde_json::from_str(include_str!("../fixtures/assignments.json")).unwrap();
                let mut invalid = f[0]["input"].clone();
                invalid["manifest"]["num_layers"] = json!(4);
                return planner::compile::compile_assignments(&invalid);
            }
            self.stage_calls.fetch_add(1, Ordering::SeqCst);
            self.cache
                .materialize_assignment("inert-stage-canary", &self.required, &self.destination)
                .map_err(|e| planner::Error::new(e.code))
        });
        let placement = prepared.placement().clone();
        run.prepared = Some(prepared);
        match result {
            Ok(staged) => Ok(json!({"placement":placement,"staged":staged})),
            Err(planner::staged::CompileFailure::Failed { cause, stop }) => {
                // Preserve both errors in tests rather than converting cleanup failure
                // to success. No starts were dispatched by this inert test host.
                assert_eq!(stop.unwrap().len(), self.members.len());
                Err(PublicError::new(409, cause.code))
            }
            Err(planner::staged::CompileFailure::Rejected(e)) => Err(PublicError::new(409, e.code)),
        }
    }
    fn stop(&self) -> Result<Value, PublicError> {
        let mut run = self.run.lock().unwrap();
        let stop = run.core.request_stop(now(0)).unwrap();
        // Consumers were never started. Filesystem retirement still needs exact
        // inventory/digest verification, and no L6 capacity is released on failure.
        self.cache
            .retire_materialization("inert-stage-canary", &self.required, &self.destination)
            .map_err(|e| PublicError::new(409, e.code))?;
        let prepared = run.prepared.as_ref().unwrap();
        for ticket in tickets(prepared) {
            run.core.cleaned(&observed(&ticket, true), now(0)).unwrap();
        }
        run.core.finish_stop(&stop.fence).unwrap();
        Ok(json!({"stopped":true}))
    }
}
impl Service for Composition {
    fn action(
        &self,
        path: &str,
        body: &serde_json::Map<String, Value>,
    ) -> Result<Value, PublicError> {
        let result = match path {
            "/api/run" => self.start(body),
            "/api/stop" => self.stop(),
            _ => Err(PublicError::new(404, "not_found")),
        };
        self.publish(result.as_ref().err().map(|e| e.code))?;
        result
    }
}
struct Host {
    listener: TcpListener,
    app: Arc<Desktop<Composition>>,
    service: Arc<Composition>,
    _temp: Temp,
}
impl Host {
    fn new(fault: &str, hold: Option<Hold>) -> Self {
        let temp = Temp::new();
        std::fs::set_permissions(&temp.0, std::fs::Permissions::from_mode(0o700)).unwrap();
        let (fixture, mut case) = fixture();
        if fault == "physical" {
            case["members"][0]["available_storage_bytes"] = json!(1);
        }
        let (core, members) = setup(
            &temp,
            &case,
            if fault == "consent" { 1 } else { 2_000_000_000 },
        );
        let cache = Cache::open(
            &temp.0.join("cache"),
            B::SavedStorageConsent.bytes().unwrap(),
            B::ControlArchiveAllowance.bytes().unwrap(),
            B::PyLanChunkMyceliumA13MacosPeerModelSourceL99
                .bytes()
                .unwrap(),
        )
        .unwrap();
        let artifacts: Value = serde_json::from_str(include_str!(
            "../../../artifacts/tests/fixtures/materialization.json"
        ))
        .unwrap();
        let required: BTreeMap<String, ArtifactKey> =
            serde_json::from_value(artifacts["materializations"][0]["required"].clone()).unwrap();
        for (index, row) in artifacts["objects"].as_array().unwrap().iter().enumerate() {
            let key: ArtifactKey = serde_json::from_value(row["key"].clone()).unwrap();
            if !required.values().any(|k| k == &key) {
                continue;
            }
            let hex = row["payload_hex"].as_str().unwrap();
            let bytes: Vec<_> = (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect();
            let source = temp.0.join(format!("source-{index}"));
            std::fs::write(&source, bytes).unwrap();
            cache.store(&key, &source).unwrap();
        }
        let status = Arc::new(PublishedStatus::new());
        let service = Arc::new(Composition {
            run: Mutex::new(Run {
                core,
                prepared: None,
            }),
            fixture,
            case,
            members,
            cache,
            required,
            destination: temp.0.join("stage"),
            status: status.clone(),
            revision: AtomicU64::new(0),
            stage_calls: AtomicU64::new(0),
            hold,
            compiler_fault: fault == "compiler",
        });
        service.publish(None).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let app = Arc::new(Desktop::new(
            listener.local_addr().unwrap().to_string(),
            service.clone(),
            status,
            Session::new(
                "launch-fixture".into(),
                "session-fixture".into(),
                Instant::now() + budget(),
                budget(),
            )
            .unwrap(),
        ));
        let host = Self {
            listener,
            app,
            service,
            _temp: temp,
        };
        assert_eq!(
            host.exchange(
                "POST",
                "/session",
                "application/x-www-form-urlencoded",
                b"launch=launch-fixture"
            )
            .0,
            303
        );
        host
    }
    fn begin(
        &self,
        method: &str,
        path: &str,
        content_type: &str,
        body: &[u8],
    ) -> (TcpStream, JoinHandle<()>) {
        let address = self.listener.local_addr().unwrap();
        let mut client = TcpStream::connect(address).unwrap();
        client.set_read_timeout(Some(budget())).unwrap();
        client.set_write_timeout(Some(budget())).unwrap();
        let (server, _) = self.listener.accept().unwrap();
        let app = self.app.clone();
        let thread = thread::spawn(move || {
            http::serve_connection(server, budget(), |r| app.preflight(r), |r| app.handle(r))
                .unwrap();
        });
        write!(client, "{method} {path} HTTP/1.1\r\nHost: {address}\r\nOrigin: http://{address}\r\nCookie: a13session=session-fixture\r\nX-Mycelium-Action: 1\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
        client.write_all(body).unwrap();
        client.shutdown(std::net::Shutdown::Write).unwrap();
        (client, thread)
    }
    fn exchange(&self, method: &str, path: &str, content_type: &str, body: &[u8]) -> (u16, Value) {
        let (mut client, thread) = self.begin(method, path, content_type, body);
        let reply = receive(&mut client);
        thread.join().unwrap();
        reply.unwrap()
    }
    fn start(&self) -> (u16, Value) {
        self.exchange(
            "POST",
            "/api/run",
            "application/json",
            &serde_json::to_vec(&self.service.case).unwrap(),
        )
    }
}
fn receive(client: &mut TcpStream) -> std::io::Result<(u16, Value)> {
    let mut bytes = String::new();
    client.read_to_string(&mut bytes)?;
    let (headers, body) = bytes.split_once("\r\n\r\n").unwrap();
    let status = headers.split_whitespace().nth(1).unwrap().parse().unwrap();
    Ok((
        status,
        if body.is_empty() {
            Value::Null
        } else {
            serde_json::from_str(body).unwrap()
        },
    ))
}
// O:incident:F14: original saved 2 GB case reaches real staged publication;
// insufficient current consent/disk refuses before any staging or durable Start.
#[test]
fn http_planner_admission_precedes_staging_and_ignores_claimed_caps() {
    for fault in ["positive", "consent", "physical"] {
        let host = Host::new(fault, None);
        let before = host.service.run.lock().unwrap().core.state().clone();
        let mut request = host.service.case.clone();
        if fault == "consent" {
            for row in request["members"].as_array_mut().unwrap() {
                row["caps"]["storage_bytes"] = json!(B::FreshStorageConsent.bytes().unwrap().get());
            }
        }
        let reply = host.exchange(
            "POST",
            "/api/run",
            "application/json",
            &serde_json::to_vec(&request).unwrap(),
        );
        if fault == "positive" {
            assert_eq!(reply.0, 200);
            assert_eq!(reply.1["placement"], host.service.case["expected"]);
            assert_eq!(reply.1["staged"]["route_ready"], false);
            assert_eq!(host.service.stage_calls.load(Ordering::SeqCst), 1);
            assert_eq!(
                host.service.run.lock().unwrap().core.state().workers.len(),
                2
            );
            let mut held = host
                .service
                .cache
                .verify_materialization(
                    "inert-stage-canary",
                    &host.service.required,
                    &host.service.destination,
                )
                .unwrap();
            for (name, file) in &mut held {
                let mut bytes = Vec::new();
                file.file.read_to_end(&mut bytes).unwrap();
                assert_eq!(bytes.len() as u64, host.service.required[name].size_bytes);
            }
        } else {
            assert_eq!(reply.0, 409);
            assert_eq!(
                reply.1["error"],
                if fault == "consent" {
                    "member_storage_cap_below_share"
                } else {
                    "member_disk_insufficient_for_share"
                }
            );
            assert_eq!(host.service.stage_calls.load(Ordering::SeqCst), 0);
            assert!(!host.service.destination.exists());
            assert_eq!(host.service.run.lock().unwrap().core.state(), &before);
        }
    }
}
// O:incident:F5. The actual Prepared::compile callback is held while holding
// this host's Core mutex; GET status must finish BEFORE that callback is released.
#[test]
fn http_status_completes_while_real_prepared_compile_is_held() {
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let host = Host::new(
        "positive",
        Some(Hold {
            entered: entered_tx,
            release: Mutex::new(release_rx),
        }),
    );
    let (mut start, start_thread) = host.begin(
        "POST",
        "/api/run",
        "application/json",
        &serde_json::to_vec(&host.service.case).unwrap(),
    );
    entered_rx.recv_timeout(budget()).unwrap();
    assert!(host.service.run.try_lock().is_err());
    let (mut status, status_thread) = host.begin("GET", "/api/status", "application/json", b"");
    let status_result = receive(&mut status);
    // Always unblock our own worker before asserting, including a failed response.
    release_tx.send(()).unwrap();
    let start_result = receive(&mut start);
    status_thread.join().unwrap();
    start_thread.join().unwrap();
    let (code, snapshot) = status_result.unwrap();
    assert_eq!(code, 200);
    assert!(snapshot["owner_run"]["error_code"].is_null());
    assert_eq!(start_result.unwrap().0, 200);
}
// O:incident:F16: actual metadata compiler refusal survives HTTP projection;
// a failed staging close cannot fabricate cleanup or release durable capacity.
#[test]
fn http_compiler_and_staging_close_failures_stay_explicit() {
    let host = Host::new("compiler", None);
    let reply = host.start();
    assert_eq!(reply, (409, json!({"error":"manifest_digest_mismatch"})));
    assert_eq!(host.service.stage_calls.load(Ordering::SeqCst), 0);
    assert!(!host.service.destination.exists());
    {
        let run = host.service.run.lock().unwrap();
        assert_eq!(run.core.state().phase, node::RunPhase::Stopping);
        assert_eq!(run.core.state().workers.len(), 2);
    }
    let status = host.exchange("GET", "/api/status", "application/json", b"");
    assert_eq!(
        status.1["owner_run"]["error_code"],
        "manifest_digest_mismatch"
    );

    let host = Host::new("positive", None);
    assert_eq!(host.start().0, 200);
    let foreign = host.service.destination.join("foreign");
    std::fs::write(&foreign, b"not owned by retirement").unwrap();
    let before = host.service.cache.status().unwrap();
    let reply = host.exchange("POST", "/api/stop", "application/json", b"{}");
    assert_eq!(reply, (409, json!({"error":"artifact_staging_foreign"})));
    assert_eq!(std::fs::read(&foreign).unwrap(), b"not owned by retirement");
    assert_eq!(host.service.cache.status().unwrap(), before);
    assert_eq!(
        host.service.run.lock().unwrap().core.state().workers.len(),
        2
    );
    let status = host.exchange("GET", "/api/status", "application/json", b"");
    assert_eq!(
        status.1["owner_run"]["error_code"],
        "artifact_staging_foreign"
    );
    std::fs::remove_file(foreign).unwrap(); // Only remove this test's injected residue.
    assert_eq!(
        host.exchange("POST", "/api/stop", "application/json", b"{}")
            .0,
        200
    );
    assert!(
        host.service
            .run
            .lock()
            .unwrap()
            .core
            .state()
            .workers
            .is_empty()
    );
    assert!(!host.service.destination.exists());
}
