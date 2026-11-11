// SPDX-License-Identifier: AGPL-3.0-only
//! Lane PH3b S3: the owner's run in the PACKAGED host, as the shipped Mac app runs it (v0.1.4 @
//! f4e366e9). `#demo-start` (run.js:192) -> `_Handler.do_POST` (app.py:1936-2010) ->
//! `DemoApp.action` (app.py:1306) -> `_action` (app.py:1362-1365) -> `demo_app.action`
//! (demo_app.py:109-133: `owner_role_required`, `owner_swarm_required`, `relay_required`) ->
//! `OwnerRun.start`; the page renders `status.owner_run` (`DemoApp.status`, app.py:1244-1283 ->
//! `demo_app.owner_status`, demo_app.py:136-150; run.js:94-138). Quit is `DemoApp.close`
//! (app.py:1815-1863) -> `demo_app.close` (demo_app.py:199-209: `run.stop(); run.join(60)`).
//!
//! Model-free: a loopback repository serves a tiny snapshot of the pinned run model (the manifest
//! oracle's accepted `qwen2_monolithic` case), so the owner's model step passes; the local agent's
//! descriptor names an owned executable FILE as the sidecar (never started), as
//! `api::model_routes_e2e` does. With no joined Mac the run ends at descriptors with
//! `two_macs_required` (demo_chain.py:724-725).
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use api::acquisition::{HttpTransport, Transport};
use budgets::BudgetId as B;
use desktop::lifecycle::Poll;
use macs::{Mac, Permissive, millis, state_root};
use node_service::{
    packaged::{self, RunConfig},
    packaged_network::NetworkConfig,
};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::JoinHandle,
    time::Instant,
};

const MANIFEST_ORACLE: &str = include_str!("../../api/tests/fixtures/oracle_manifest.json");
/// v0.1.4's owner acquisition root of the pinned run model (`'model-source-' + resolved_commit[:12]`).
const SOURCE: &str = "model-source-7ae557604adf";

/// `config.json` + `model.safetensors` (BF16 [1] tensors) of the manifest oracle's accepted case.
fn tiny_snapshot() -> Vec<(String, Vec<u8>)> {
    let doc: Value = serde_json::from_str(MANIFEST_ORACLE).unwrap();
    let case = doc["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["label"] == "qwen2_monolithic")
        .unwrap();
    let config = case["files"]["config.json"]["text"]
        .as_str()
        .unwrap()
        .to_owned();
    let spec = &case["files"]["model.safetensors"];
    let mut tensors: Vec<&Value> = spec["tensors"].as_array().unwrap().iter().collect();
    tensors.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
    let seed = spec["seed"].as_u64().unwrap() as usize;
    let mut header = serde_json::Map::new();
    let (mut offset, mut data) = (0usize, Vec::new());
    for (i, tensor) in tensors.iter().enumerate() {
        header.insert(
            tensor[0].as_str().unwrap().to_owned(),
            json!({"dtype": "BF16", "shape": [1], "data_offsets": [offset, offset + 2]}),
        );
        data.extend((0..2).map(|j| ((seed + i + j) % 251) as u8));
        offset += 2;
    }
    let header = serde_json::to_vec(&Value::Object(header)).unwrap();
    let mut weights = (header.len() as u64).to_le_bytes().to_vec();
    weights.extend(header);
    weights.extend(data);
    vec![
        ("config.json".into(), config.into_bytes()),
        ("model.safetensors".into(), weights),
    ]
}

/// A one-thread loopback repository of the pinned model (revision document + `resolve` files).
struct Repository {
    origin: String,
    stop: Arc<AtomicBool>,
    server: Option<JoinHandle<()>>,
}
impl Repository {
    fn start() -> Self {
        let files = tiny_snapshot();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        let server = std::thread::spawn(move || {
            let model = &api::model_catalog::registered()[1];
            let (id, commit) = (model.model_id, model.resolved_commit);
            while !stopping.load(Ordering::SeqCst) {
                let Ok((mut stream, _)) = listener.accept() else {
                    std::thread::sleep(millis(B::OwnerPoll) / 50);
                    continue;
                };
                stream.set_nonblocking(false).unwrap();
                let mut head = Vec::new();
                let mut byte = [0u8; 1];
                while !head.ends_with(b"\r\n\r\n") {
                    match stream.read(&mut byte) {
                        Ok(1) => head.push(byte[0]),
                        _ => break,
                    }
                }
                let text = String::from_utf8_lossy(&head).into_owned();
                let target = text.split(' ').nth(1).unwrap_or_default().to_owned();
                let body = if target == format!("/api/models/{id}/revision/{commit}?blobs=true") {
                    let siblings: Vec<Value> = files
                        .iter()
                        .map(|(path, bytes)| {
                            let sha = protocol::canonical::sha256_prefixed(bytes)[7..].to_owned();
                            json!({"rfilename": path, "size": bytes.len(),
                                "lfs": {"size": bytes.len(), "sha256": sha}})
                        })
                        .collect();
                    Some(
                        json!({"id": id, "modelId": id, "sha": commit, "private": false, "siblings": siblings})
                            .to_string()
                            .into_bytes(),
                    )
                } else if let Some(rest) = target.strip_prefix(&format!("/{id}/resolve/{commit}/")) {
                    let path = rest.split('?').next().unwrap_or_default();
                    files
                        .iter()
                        .find(|(p, _)| p == path)
                        .map(|(_, b)| b.clone())
                } else {
                    None
                };
                let reply = match body {
                    Some(body) => {
                        let mut out = format!(
                            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            body.len()
                        )
                        .into_bytes();
                        out.extend(body);
                        out
                    }
                    None => b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        .to_vec(),
                };
                stream.write_all(&reply).unwrap_or_default();
            }
        });
        Self {
            origin,
            stop,
            server: Some(server),
        }
    }
    fn transport(&self) -> Arc<dyn Transport> {
        Arc::new(HttpTransport::loopback_for_tests(&self.origin))
    }
}
impl Drop for Repository {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(server) = self.server.take() {
            server.join().unwrap();
        }
    }
}

/// An owned, executable regular file standing for the installed native sidecar: the descriptor
/// names it; nothing here ever starts it (`installed_sidecar`, native_runtime.py:8-22).
fn sidecar_file(base: &Path) -> PathBuf {
    let path = base.join("mycelium-iroh-sidecar");
    std::fs::write(&path, b"#!/bin/sh\nexit 1\n").unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
    path
}

/// One packaged Mac over the loopback repository with the run's composition seams.
fn launch(state: &Path, repository: &Repository, run: RunConfig) -> Mac {
    let (core, mut cleanup) = packaged::core_with_run(
        state,
        repository.transport(),
        Arc::new(Permissive),
        NetworkConfig::default(),
        run,
    )
    .unwrap();
    use desktop::lifecycle::Cleanup;
    cleanup.bind_clock(Instant::now()).unwrap();
    Mac {
        core,
        cleanup,
        state: state.to_owned(),
    }
}

fn consent() -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only",
        "storage_bytes": api::model_catalog::storage_consent_ceiling(),
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}

/// `/api/create`, then `/api/model/download` of the tiny snapshot until the owner's acquisition
/// prepared it (its `status.json` in v0.1.4's `model-source-*` root).
fn create_and_prepare(mac: &Mac) {
    let created = mac.post("/api/create", json!({"consent": consent()}));
    assert_eq!(created.0, 200, "create: {}", created.1);
    let grant = json!({"consent": {"download_acknowledged": true,
        "max_download_bytes": 10_000_000u64, "storage_limit_bytes": 10_000_000u64}});
    let started = mac.post("/api/model/download", grant);
    assert_eq!(started.0, 200, "download: {}", started.1);
    let status = mac.until(
        "the owner's model prepared",
        |m| {
            std::fs::read(m.state.join(SOURCE).join("status.json"))
                .ok()
                .and_then(|b| serde_json::from_slice(&b).ok())
                .unwrap_or(Value::Null)
        },
        |s| matches!(s["state"].as_str(), Some("prepared" | "failed" | "cancelled")),
    );
    assert_eq!(status["state"], "prepared", "{status}");
}

fn owner_run(mac: &Mac) -> Value {
    mac.status()["owner_run"].clone()
}

#[test]
fn the_idle_run_is_shown_and_start_needs_the_owners_swarm() {
    let base = state_root("run-idle");
    let repository = Repository::start();
    let mac = launch(&base, &repository, RunConfig::default());
    // `demo_app.owner_status` of an owner that never started a run: the ten pending steps.
    let idle = owner_run(&mac);
    assert_eq!(idle["state"], "idle", "{idle}");
    assert_eq!(idle["steps"].as_array().map(Vec::len), Some(10), "{idle}");
    assert_eq!(idle["steps"][0]["name"], "model", "{idle}");
    assert_eq!(idle["steps"][2]["name"], "descriptors", "{idle}");
    assert_eq!(idle["route_ready"], false, "{idle}");
    // No swarm yet: `ensure_exchange(app) is None` -> `owner_swarm_required` (demo_app.py:121-122).
    let refused = mac.post("/api/demo/start", json!({}));
    assert_eq!(refused, (400, json!({"error": "owner_swarm_required"})));
    // A non-empty body other than `{execution_ceiling}` -> `invalid_request` (demo_app.py:119-120).
    let refused = mac.post("/api/demo/start", json!({"model": "x"}));
    assert_eq!(refused.0, 400, "{}", refused.1);
    assert_eq!(refused.1["error"], "invalid_request", "{}", refused.1);
    assert_eq!(mac.quit(), Ok(Poll::Verified));
}

#[test]
fn start_with_no_joined_mac_fails_two_macs_required_with_the_steps() {
    let base = state_root("run-two-macs");
    let repository = Repository::start();
    let sidecar = sidecar_file(&base);
    let state = base.join("owner");
    std::fs::create_dir(&state).unwrap();
    let mac = launch(
        &state,
        &repository,
        RunConfig {
            sidecar: Some(sidecar),
            ..RunConfig::default()
        },
    );
    create_and_prepare(&mac);
    let (code, started) = mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{started}");
    assert_eq!(started["state"], "running", "{started}");
    assert_eq!(started["reason"], "start", "{started}");
    let run = mac.until(
        "the run ended",
        owner_run,
        |r| matches!(r["state"].as_str(), Some("failed" | "stopped" | "serving")),
    );
    assert_eq!(run["protocol"], "mycelium.a13_owner_run.v1", "{run}");
    assert_eq!(run["state"], "failed", "{run}");
    assert_eq!(run["failed_step"], "descriptors", "{run}");
    assert_eq!(run["error_code"], "two_macs_required", "{run}");
    assert_eq!(run["steps"][0]["state"], "done", "{run}");
    assert_eq!(
        run["steps"][0]["detail"],
        "Qwen/Qwen2.5-0.5B-Instruct \u{b7} whole snapshot per Mac",
        "{run}"
    );
    assert_eq!(run["steps"][1]["state"], "done", "{run}");
    assert_eq!(run["steps"][2]["detail"], "1 of 1 Macs reported", "{run}");
    assert_eq!(run["participants"].as_array().map(Vec::len), Some(1), "{run}");
    assert_eq!(run["participants"][0]["label"], "This Mac (owner)", "{run}");
    assert_eq!(run["participants"][0]["state"], "reported", "{run}");
    // A finished run does not hold the next Start back (`owner_run_active` only while active).
    let (code, again) = mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{again}");
    let run = mac.until(
        "the second run ended",
        owner_run,
        |r| r["state"] == "failed" && r["run_id"] == again["run_id"],
    );
    assert_eq!(run["error_code"], "two_macs_required", "{run}");
    assert_eq!(mac.quit(), Ok(Poll::Verified));
}

/// The owner's own agent cannot describe itself without the installed sidecar: the run waits in
/// descriptors with the reason on the owner's card (`FX-L10-LOCAL-AGENT`). That is a run in flight.
fn start_a_waiting_run(tag: &str, repository: &Repository) -> (Mac, RunConfig) {
    let base = state_root(tag);
    let state = base.join("owner");
    std::fs::create_dir(&state).unwrap();
    let config = RunConfig {
        sidecar: Some(base.join("sidecar-not-installed")),
        ..RunConfig::default()
    };
    let mac = launch(&state, repository, config.clone());
    create_and_prepare(&mac);
    let (code, started) = mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{started}");
    let run = mac.until("the run waits in descriptors", owner_run, |r| {
        r["step"] == "descriptors"
            && r["participants"][0]["detail"] == "native_sidecar_installation_invalid"
    });
    assert_eq!(run["state"], "running", "{run}");
    assert_eq!(run["participants"][0]["state"], "error", "{run}");
    (mac, config)
}

#[test]
fn stop_mid_run_ends_it_stopped() {
    let repository = Repository::start();
    let (mac, _) = start_a_waiting_run("run-stop", &repository);
    // `OwnerRun.stop` (owner_run.py:275-282): the answer is the run's own snapshot.
    let (code, stopping) = mac.post("/api/demo/stop", json!({}));
    assert_eq!(code, 200, "{stopping}");
    assert!(
        matches!(stopping["state"].as_str(), Some("stopping" | "stopped")),
        "{stopping}"
    );
    let run = mac.until("the run stopped", owner_run, |r| r["state"] != "stopping");
    assert_eq!(run["state"], "stopped", "{run}");
    assert_eq!(run["failed_step"], Value::Null, "{run}");
    // Rebuild needs a run that serves (`owner_run_rebuild_requires_run`).
    let refused = mac.post("/api/demo/rebuild", json!({}));
    assert_eq!(refused.0, 400, "{}", refused.1);
    assert_eq!(
        refused.1["error"], "owner_run_rebuild_requires_run",
        "{}",
        refused.1
    );
    assert_eq!(mac.quit(), Ok(Poll::Verified));
}

#[test]
fn quit_during_a_run_stops_it_and_joins_it_within_the_allowance() {
    let repository = Repository::start();
    let (mac, config) = start_a_waiting_run("run-quit", &repository);
    let began = Instant::now();
    // `demo_app.close`: `run.stop(); run.join(60)` before the other closes.
    assert_eq!(mac.quit(), Ok(Poll::Verified));
    assert!(began.elapsed() < millis(B::WorkerStartup) * 4);
    let view = config.observed();
    let view = view.expect("the composition installed the owner run");
    assert!(!view.active(), "the run still owns its driver after Quit");
    let after = view.status().map(|s| s.to_value()).unwrap_or(Value::Null);
    assert_eq!(after["state"], "stopped", "{after}");
}
