// SPDX-License-Identifier: AGPL-3.0-only
//! `POST /api/model/download` and `/api/model/cancel` on the REAL Rust headless process equal the executed
//! oracle: the certified v0.1.4 executable (Seatbelt, `--offline`) driven by
//! `L10-evidence/r18-probe-model-routes.py` (sha256 ecb3b78c2c0dabf8b21bd088bfeb1f29a6185f01ece8d9a0bde808647ee89768),
//! recorded in `fixtures/oracle_model_routes.json` (sha256 0b1a54cef77bcd70c76bfc196ecba11a295338149eb85956f7f48a2f293d1321).
//! Every refusal (shape, membership, grant, resource guard), the offline refusal of the public hub
//! (`offline_model_source_required`, no socket), the retry after a failure, cancel, stop/start and the status
//! fields (`acquisition` with the model identity, `acquisition_policy_error`, `resource_observation`) are
//! compared step by step. The resource guard observes through the test seam `MYCELIUM_L10_TEST_RESOURCE_PROBE`
//! (a script printing the observation the oracle's own helper measured in the sandbox: battery, nominal,
//! unmetered); the real helper is `native/resource_probe`.
use budgets::{BudgetId, Entries};
use rustix::process::{Pid, Signal, kill_process, test_kill_process};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

const FIXTURE: &str = include_str!("fixtures/oracle_model_routes.json");
const GB: u64 = 1_000_000_000;

fn exe_dir() -> PathBuf {
    let exe = std::env::current_exe().expect("exe");
    exe.parent()
        .and_then(Path::parent)
        .expect("profile")
        .with_file_name("release")
}
fn build() {
    let target = exe_dir();
    let target = target.parent().expect("target");
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Cargo.toml");
    let output = Command::new(std::env::var("CARGO").unwrap_or_else(|_| "cargo".into()))
        .args([
            "build",
            "--offline",
            "--locked",
            "-j",
            "2",
            "-p",
            "api",
            "--release",
        ])
        .args(["--example", "headless"])
        .arg("--manifest-path")
        .arg(manifest)
        .arg("--target-dir")
        .arg(target)
        .output()
        .expect("cargo is runnable");
    assert!(
        output.status.success(),
        "nested build failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
fn budget(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::new(0)).expect("budget").millis())
}
fn patience() -> Duration {
    budget(BudgetId::ModelSelection) * 4
}
fn poll_every() -> Duration {
    budget(BudgetId::OwnerPoll)
}

struct Instance {
    child: Child,
    root: PathBuf,
    port: u16,
    cookie: String,
}
impl Instance {
    fn launch(root: &Path, probe: &Path) -> Self {
        Self::launch_with(root, probe, None)
    }
    /// `hub`: a loopback repository origin; the process then runs online (not `--offline`).
    fn launch_with(root: &Path, probe: &Path, hub: Option<&str>) -> Self {
        Self::launch_with_sidecar(root, probe, hub, None)
    }
    fn launch_with_sidecar(
        root: &Path,
        probe: &Path,
        hub: Option<&str>,
        sidecar: Option<&Path>,
    ) -> Self {
        fs::create_dir_all(root).expect("root");
        fs::set_permissions(root, fs::Permissions::from_mode(0o700)).expect("mode");
        // The executed oracle recorded these downloads with v0.1.4's starter `a9-route`, and the
        // loopback repository serves that model's tiny Qwen2 snapshot (`tiny_repository`). v0.2's
        // starter is Qwen3-0.6B (B13, `contracts/divergences.json` `run_model_default`), so the
        // installation takes `a9-route` explicitly, as a saved picker choice: these tests are
        // about the download, not the default.
        api::preferences::run_model::save_choice(root, "a9-route").expect("saved run model");
        let mut command = Command::new(exe_dir().join("examples/headless"));
        command.env_remove("MYCELIUM_L10_TEST_SIDECAR");
        if let Some(sidecar) = sidecar {
            command.env("MYCELIUM_L10_TEST_SIDECAR", sidecar);
        }
        command.args(["--headless"]);
        if hub.is_none() {
            command.arg("--offline");
        } else {
            command.env("MYCELIUM_L10_TEST_HUB_ORIGIN", hub.unwrap_or_default());
        }
        let mut child = command
            .args(["--state-root"])
            .arg(root)
            .env("HOME", root)
            .env("MYCELIUM_L10_TEST_RESOURCE_PROBE", probe)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(
                fs::File::create(root.with_extension("stderr")).expect("stderr file"),
            ))
            .spawn()
            .expect("the headless example was built");
        let mut first = String::new();
        BufReader::new(child.stdout.take().expect("stdout"))
            .read_line(&mut first)
            .expect("first line");
        assert!(
            first.contains("launcher_started"),
            "{first:?} stderr: {}",
            fs::read_to_string(root.with_extension("stderr")).unwrap_or_default()
        );
        let form = fs::read_to_string(root.join("open-demo.html")).expect("launch form");
        let attribute = |name: &str| {
            let key = format!("{name}=\"");
            form.split_once(&key)
                .and_then(|(_, rest)| rest.split_once('"'))
                .map(|(value, _)| value.to_owned())
                .expect("form attribute")
        };
        let port: u16 = attribute("action")
            .strip_prefix("http://127.0.0.1:")
            .and_then(|rest| rest.strip_suffix("/session"))
            .and_then(|p| p.parse().ok())
            .expect("loopback origin");
        let mut me = Self {
            child,
            root: root.to_path_buf(),
            port,
            cookie: String::new(),
        };
        let signed_in = me.raw(
            "POST",
            "/session",
            format!("launch={}", attribute("value")).as_bytes(),
            &[
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Origin", "null"),
            ],
        );
        assert_eq!(signed_in.0, 303, "{signed_in:?}");
        me.cookie = signed_in
            .1
            .lines()
            .find_map(|l| {
                l.to_ascii_lowercase()
                    .starts_with("set-cookie: ")
                    .then(|| &l[12..])
            })
            .and_then(|c| c.split(';').next())
            .expect("session cookie")
            .to_owned();
        me
    }
    fn raw(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        headers: &[(&str, &str)],
    ) -> (u16, String, Vec<u8>) {
        let limit = budget(BudgetId::WorkerStartup);
        let mut stream =
            TcpStream::connect_timeout(&SocketAddr::from((Ipv4Addr::LOCALHOST, self.port)), limit)
                .expect("connect");
        stream.set_read_timeout(Some(limit)).expect("timeout");
        let mut wire = format!(
            "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\nContent-Length: {}\r\n",
            self.port,
            body.len()
        );
        if !self.cookie.is_empty() {
            wire.push_str(&format!("Cookie: {}\r\n", self.cookie));
        }
        for (key, value) in headers {
            wire.push_str(&format!("{key}: {value}\r\n"));
        }
        wire.push_str("\r\n");
        stream.write_all(wire.as_bytes()).expect("write head");
        stream.write_all(body).expect("write body");
        let mut reply = Vec::new();
        stream.read_to_end(&mut reply).expect("read reply");
        let split = reply
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .expect("reply head");
        let head = String::from_utf8_lossy(&reply[..split]).into_owned();
        let status = head
            .split(' ')
            .nth(1)
            .and_then(|s| s.parse().ok())
            .expect("status");
        (status, head, reply[split + 4..].to_vec())
    }
    fn get(&self, path: &str) -> (u16, Value) {
        let (status, _, body) = self.raw("GET", path, &[], &[]);
        (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
    }
    fn post(&self, path: &str, body: &Value) -> (u16, Value) {
        let origin = format!("http://127.0.0.1:{}", self.port);
        let (status, _, reply) = self.raw(
            "POST",
            path,
            body.to_string().as_bytes(),
            &[
                ("Content-Type", "application/json"),
                ("Origin", &origin),
                ("X-Mycelium-Action", "1"),
            ],
        );
        (
            status,
            serde_json::from_slice(&reply).unwrap_or(Value::Null),
        )
    }
    fn pid(&self) -> Pid {
        Pid::from_raw(i32::try_from(self.child.id()).expect("pid")).expect("nonzero pid")
    }
    fn status_view(&self) -> Value {
        let (status, s) = self.get("/api/status");
        assert_eq!(status, 200);
        json!({
            "acquisition": s["acquisition"], "acquisition_policy_error": s["acquisition_policy_error"],
            "resource_observation": s["resource_observation"], "running": s["running"],
            "membership_state": s["authority"]["membership_state"],
        })
    }
    /// The status `owner_run` block once its state is `state` (or the patience ran out).
    fn wait_owner_run(&self, state: &str) -> Value {
        self.wait_run_matching(|run| run["state"] == state)
    }
    fn wait_run_matching(&self, ready: impl Fn(&Value) -> bool) -> Value {
        let deadline = Instant::now() + patience();
        loop {
            let (status, s) = self.get("/api/status");
            assert_eq!(status, 200);
            if ready(&s["owner_run"]) || Instant::now() > deadline {
                return s["owner_run"].clone();
            }
            std::thread::sleep(poll_every());
        }
    }
    fn wait_state(&self, states: &[&str]) -> Value {
        let deadline = Instant::now() + patience();
        loop {
            let view = self.status_view();
            if states.iter().any(|s| view["acquisition"]["state"] == *s)
                || Instant::now() > deadline
            {
                return view;
            }
            std::thread::sleep(poll_every());
        }
    }
}
impl Drop for Instance {
    fn drop(&mut self) {
        kill_process(self.pid(), Signal::TERM).unwrap_or_default();
        let deadline = Instant::now() + patience();
        while self.child.try_wait().ok().flatten().is_none() && Instant::now() < deadline {
            std::thread::sleep(poll_every());
        }
        if self.child.try_wait().ok().flatten().is_none() {
            kill_process(self.pid(), Signal::KILL).unwrap_or_default();
            self.child.wait().unwrap_or_default();
        }
        assert!(
            test_kill_process(self.pid()).is_err(),
            "orphan {}",
            self.child.id()
        );
        fs::remove_dir_all(&self.root).unwrap_or_default();
        fs::remove_file(self.root.with_extension("stderr")).unwrap_or_default();
    }
}

#[test]
fn model_download_and_cancel_on_the_rust_headless_equal_the_executed_oracle() {
    build();
    let oracle: Value = serde_json::from_str(FIXTURE).unwrap();
    let canonical = fs::canonicalize(std::env::temp_dir()).unwrap();
    let base = canonical.join(format!("l10-model-routes-{}", std::process::id()));
    fs::create_dir_all(&base).unwrap();
    fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
    // The observation the oracle's own helper measured in its sandbox (`resource_observation` of the recording).
    let observed = oracle["steps"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["label"] == "after_download")
        .unwrap()["status_view"]["resource_observation"]
        .clone();
    let probe = base.join("probe.sh");
    fs::write(&probe, format!("#!/bin/sh\nprintf '%s' '{observed}'\n")).unwrap();
    fs::set_permissions(&probe, fs::Permissions::from_mode(0o700)).unwrap();
    let me = Instance::launch(&base.join("home"), &probe);
    let grant = json!({"download_acknowledged": true, "max_download_bytes": 2 * GB, "storage_limit_bytes": 4 * GB});
    let policy = json!({"peer_visibility": true, "power": "battery_allowed", "storage_bytes": 4 * GB,
        "network": "metered_allowed", "background": false, "diagnostics": false, "thermal": "stop_on_serious"});
    let mut compared = 0;
    for step in oracle["steps"].as_array().unwrap() {
        let label = step["label"].as_str().unwrap();
        if step.get("status_view").is_some() {
            // The recorded status views after a download are compared once the attempt settled.
            let view = if label.starts_with("after_download") {
                me.wait_state(&["failed", "prepared", "cancelled"])
            } else {
                std::thread::sleep(if label == "settled" {
                    budget(BudgetId::OwnerPoll) * 12
                } else {
                    Duration::ZERO
                });
                me.status_view()
            };
            assert_eq!(view, step["status_view"], "status view at {label}");
            compared += 1;
            continue;
        }
        let path = step["path"].as_str().unwrap();
        let request = match label {
            "create" => json!({"consent": policy}),
            l if l.starts_with("download") => json!({"consent": grant}),
            _ => step["request"].clone(),
        };
        let request = if step["request"].is_null() {
            json!({})
        } else if label.starts_with("grant_") || label.starts_with("shape_") {
            step["request"].clone()
        } else {
            request
        };
        let (mut status, mut body) = me.post(path, &request);
        // The helper is bounded to 2.5 s and fails closed (`resource_thermal_policy_violation`, as the oracle's
        // does); a loaded host can miss that for a trivial script. Only the downloads that the oracle accepted
        // are retried, and only on that refusal: every other answer is compared as it came.
        let accepted = step["status"] == 200 && label.contains("download");
        for _ in 0..5 {
            if !(accepted && status == 400 && body["error"] == "resource_thermal_policy_violation")
            {
                break;
            }
            std::thread::sleep(budget(BudgetId::OwnerPoll) * 8);
            (status, body) = me.post(path, &request);
        }
        assert_eq!(
            status,
            step["status"].as_u64().unwrap() as u16,
            "{label}: {body}"
        );
        match label {
            // Racy by nature: the answer is the status document at the instant the worker had started.
            "download_valid" | "download_again_after_failure" | "download_after_restart_valid" => {
                let state = body["state"].as_str().unwrap_or("");
                assert!(
                    ["starting", "resolving_metadata", "failed"].contains(&state),
                    "{label}: {body}"
                );
                assert_eq!(body["model"], step["body"]["model"], "{label}");
            }
            "create" | "start" => {}
            "cancel_after_failure" | "cancel_fresh" => assert_eq!(body, step["body"], "{label}"),
            _ => assert_eq!(body, step["body"], "{label}"),
        }
        compared += 1;
    }
    assert_eq!(compared, oracle["steps"].as_array().unwrap().len());
    // The model snapshot directory is the oracle's: one per run model beside the state.
    let listing: Vec<String> = {
        let mut names: Vec<String> = fs::read_dir(&me.root)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with("model-source"))
            .collect();
        names.sort();
        names
    };
    assert_eq!(listing, ["model-source-7ae557604adf"]);
    drop(me);
    fs::remove_dir_all(&base).unwrap_or_default();
}

// ---- download to `prepared` through the running process, from a loopback repository ----
#[path = "support/tiny_repository.rs"]
mod tiny_repository;
use tiny_repository::{serve_repository, tiny_snapshot};
#[test]
/// Proves: B-RUN-08 (local contributor and descriptor reporting through the real host), B-MODEL-09.
/// The valid sidecar seam supplies an owned executable FILE, never starts it: this is descriptor
/// disclosure/admission, NOT native hello or a two-Mac run. FX-SLICE1-steps-r12/no_remote_members
/// records the next refusal (`two_macs_required`); FX-L10-LOCAL-AGENT records missing-sidecar detail.
fn a_model_download_reaches_prepared_through_the_running_process_and_a_bad_snapshot_is_refused() {
    build();
    let oracle: Value = serde_json::from_str(FIXTURE).unwrap();
    let canonical = fs::canonicalize(std::env::temp_dir()).unwrap();
    let base = canonical.join(format!("l10-model-prepared-{}", std::process::id()));
    fs::create_dir_all(&base).unwrap();
    fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
    let observed = oracle["steps"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["label"] == "after_download")
        .unwrap()["status_view"]["resource_observation"]
        .clone();
    let probe = base.join("probe.sh");
    fs::write(&probe, format!("#!/bin/sh\nprintf '%s' '{observed}'\n")).unwrap();
    fs::set_permissions(&probe, fs::Permissions::from_mode(0o700)).unwrap();
    let grant = json!({"consent": {"download_acknowledged": true, "max_download_bytes": 2 * GB, "storage_limit_bytes": 4 * GB}});
    let policy = json!({"consent": {"peer_visibility": true, "power": "battery_allowed", "storage_bytes": 4 * GB,
        "network": "metered_allowed", "background": false, "diagnostics": false, "thermal": "stop_on_serious"}});
    let executable_file = exe_dir().join("examples/headless");
    let missing_file = base.join("sidecar-not-installed");
    for (name, broken, sidecar) in [
        ("good", false, executable_file.as_path()),
        ("missing_sidecar", false, missing_file.as_path()),
        ("bad_layer_count", true, executable_file.as_path()),
    ] {
        let (mut files, _) = tiny_snapshot();
        if broken {
            // 25 layers declared, 24 present: the manifest compile refuses what the byte checks accepted.
            let text = String::from_utf8(files[0].1.clone())
                .unwrap()
                .replace("\"num_hidden_layers\": 24", "\"num_hidden_layers\": 25");
            files[0].1 = text.into_bytes();
        }
        let weights = files[1].1.clone();
        let (port, stop, server) = serve_repository(files);
        let me = Instance::launch_with_sidecar(
            &base.join(format!("home-{name}")),
            &probe,
            Some(&format!("http://127.0.0.1:{port}")),
            Some(sidecar),
        );
        assert_eq!(me.post("/api/create", &policy).0, 200);
        let started = {
            let mut tries = 0;
            loop {
                let (status, body) = me.post("/api/model/download", &grant);
                if status == 200 || tries > 5 {
                    break (status, body);
                }
                tries += 1;
                std::thread::sleep(budget(BudgetId::OwnerPoll) * 8);
            }
        };
        assert_eq!(started.0, 200, "{name}: {}", started.1);
        let view = me.wait_state(&["failed", "prepared", "cancelled"]);
        let acquisition = &view["acquisition"];
        if broken {
            assert_eq!(acquisition["state"], "failed", "{name}: {view}");
            assert_eq!(
                acquisition["error_code"], "native_manifest_compile_failed",
                "{name}: {view}"
            );
        } else {
            assert_eq!(acquisition["state"], "prepared", "{name}: {view}");
            assert_eq!(acquisition["error_code"], Value::Null);
            let total = (weights.len() + acquisition_config_len(&me)) as u64;
            assert_eq!(acquisition["downloaded_bytes"].as_u64(), Some(total));
        }
        // The real HTTP start must now pass contributor and reach descriptors, not NP-L10-contributor.
        let (code, started) = me.post("/api/demo/start", &json!({}));
        assert_eq!(code, 200, "{name}: {started}");
        assert_eq!(started["state"], "running", "{name}");
        assert_eq!(started["reason"], "start", "{name}");
        let missing = name == "missing_sidecar";
        let run = if missing {
            me.wait_run_matching(|run| {
                run["step"] == "descriptors"
                    && run["participants"][0]["detail"] == "native_sidecar_installation_invalid"
            })
        } else {
            me.wait_owner_run("failed")
        };
        assert_eq!(
            run["protocol"], "mycelium.a13_owner_run.v1",
            "{name}: {run}"
        );
        if broken {
            assert_eq!(run["failed_step"], "model", "{name}: {run}");
            assert_eq!(
                run["error_code"], "owner_model_not_prepared",
                "{name}: {run}"
            );
        } else {
            assert_eq!(run["steps"][1]["state"], "done", "{name}: {run}");
            assert_eq!(
                run["participants"].as_array().unwrap().len(),
                1,
                "{name}: {run}"
            );
            assert_eq!(run["participants"][0]["label"], "This Mac (owner)");
            if missing {
                assert_eq!(run["state"], "running", "{name}: {run}");
                assert_eq!(run["step"], "descriptors", "{name}: {run}");
                assert_eq!(run["participants"][0]["state"], "error");
                assert_eq!(
                    run["participants"][0]["detail"],
                    "native_sidecar_installation_invalid"
                );
            } else {
                assert_eq!(run["state"], "failed", "{name}: {run}");
                assert_eq!(run["failed_step"], "descriptors", "{name}: {run}");
                assert_eq!(run["error_code"], "two_macs_required", "{name}: {run}");
                assert_eq!(run["participants"][0]["state"], "reported", "{name}: {run}");
                assert_eq!(
                    run["steps"][2]["detail"], "1 of 1 Macs reported",
                    "{name}: {run}"
                );
            }
            assert_eq!(run["steps"][0]["state"], "done", "{name}: {run}");
            assert_eq!(
                run["steps"][0]["detail"],
                "Qwen/Qwen2.5-0.5B-Instruct \u{b7} whole snapshot per Mac",
                "{name}: {run}"
            );
        }
        // A second Start is refused only while a run is active; this one has ended.
        let (code, stopped) = me.post("/api/demo/stop", &json!({}));
        assert_eq!(code, 200, "{name}: {stopped}");
        if missing {
            assert!(
                matches!(stopped["state"].as_str(), Some("stopping" | "stopped")),
                "{name}: {stopped}"
            );
            assert_eq!(me.wait_owner_run("stopped")["state"], "stopped");
        } else {
            assert_eq!(stopped["state"], "failed", "{name}: {stopped}");
        }
        println!(
            "HOST-BOUNDARY {name}: step={} state={} code={} participant={}",
            run["step"], run["state"], run["error_code"], run["participants"][0]["state"]
        );
        drop(me);
        stop.store(true, std::sync::atomic::Ordering::SeqCst);
        server.join().unwrap();
    }
    fs::remove_dir_all(&base).unwrap_or_default();
}
fn acquisition_config_len(_: &Instance) -> usize {
    tiny_snapshot().0[0].1.len()
}
