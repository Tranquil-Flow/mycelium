// SPDX-License-Identifier: AGPL-3.0-only
//! Lane TEN: the hermetic ten-row gate. Two PACKAGED hosts in this one test process, each the
//! shipped app's own composition (`packaged::core_with_run`, the function `core_with_native_program`
//! calls) over its own private state root, driven through the window's callbacks as the page drives
//! them. The joined Mac enrols through the real join path (relay window, claim, approve, poll).
//!
//! Native program (lane BND): the PACKAGED `mycelium` executable, for every role (the compiler's and
//! every stage's `native-worker`, the route's `tokenize`). Its worker roles pass the shipped worker
//! admission (`desktop::package::admit_worker`), which needs a bundle around the executable, so
//! the gate stages one (`support/staged_bundle.rs`: the executable named by
//! `MYCELIUM_TEN_PACKAGED_EXECUTABLE` as `Contents/MacOS/mycelium` plus exactly the pinned payload,
//! copied from `MYCELIUM_PINNED_PAYLOAD_APP`) and takes the program's identity through the shipped
//! startup path (`admit_bundle` + `packaged::admitted_native_program`). Both hosts get that one
//! program as `RunConfig::native_program`, as `core_with_native_program` gives it. The gate REFUSES
//! to run with `MYCELIUM_COMPILER_NATIVE_BINARY` / `_SHA256` set: no stage worker may come from the
//! development environment (REPORT-laneTEN W-D2 is gone). `mycelium-node` is not used.
//!
//! EVERY substitution (REVIEW-laneTEN-astra F5):
//!
//! * host identity: the run's OBSERVE PORT is the named
//!   [`observe_test_double::HostIdentityTestDouble`] (decision U4 / ruling 14: one Mac cannot give
//!   two host identities; it wraps the real observation backend and rewrites only each hello
//!   answer's `host_id` / `boot_id`);
//! * the model repository's ORIGIN: a loopback server over this Mac's cached Hugging Face snapshot
//!   (`support/cached_repository.rs`); the acquisition downloads, hashes and verifies every byte;
//! * the relay: the external provider on a loopback proxy (no `cloudflared`; the relay client lookup
//!   answers none), as every packaged pair test; the seed's control plane therefore runs on
//!   loopback, while the WORK transport (member control, shard source, route carrier) is the app
//!   default, bound to this Mac's private LAN address (`WorkNetwork::ThisMac`);
//! * the power / thermal / network observer: both hosts get `macs::Permissive` (always external
//!   power, nominal thermal state, unmetered network), never this Mac's real readings;
//! * the descriptor sidecar: `support/packaged_repository.rs::sidecar_file`, a generated
//!   `#!/bin/sh` / `exit 1` stub named in the descriptors, never a real sidecar launch;
//! * the bootstrap (hello) program: installed into each host's private state straight from the
//!   document `MYCELIUM_BOOTSTRAP_TEST_CONFIG` names (`admit_hello_program`), not from a bundle;
//! * entry: both hosts are composed by calling `core_with_run` and driven through the core's
//!   callbacks, BELOW the shipped entry: no bundle admission of the window process, no launch
//!   location check, no installer, no window, page or JavaScript;
//! * the bundle around the native program is staged here (above): not installed under
//!   `programs/<version>`, not signed, no `Info.plist`; the payload bytes are the pinned ones
//!   (admission checks them) but come from another app's copy;
//! * one Mac: both hosts, both stage workers and both state roots share this Mac, its GPU, disk
//!   and LAN interface (the "LAN" path never leaves the Mac).
//!
//! Real: the packaged executable's worker and tokenizer roles on Metal with the cached
//! Qwen3-0.6B, the compiler and its challenge, the work transport on the LAN address, the startup
//! challenge, the chat service and the run's route.
//!
//! Run (one model test at a time; see REPORT-laneBND.md for the exact command):
//! `cargo build --offline --release -p node-service --features packaged --bin mycelium`, then
//! `MYCELIUM_TEN_PACKAGED_EXECUTABLE=target/release/mycelium MYCELIUM_PINNED_PAYLOAD_APP=<app>
//!  cargo test --offline -p node-service --features "packaged desktop-shell" --test packaged_ten_rows
//!  ten_rows_with_the_real_model_on_the_lan -- --exact --ignored --nocapture --test-threads 1`
#![cfg(feature = "desktop-shell")]
#[path = "support/cached_repository.rs"]
mod cached;
#[path = "support/packaged_macs.rs"]
mod macs;
#[path = "support/host_identity_double.rs"]
mod observe_test_double;
#[path = "support/packaged_repository.rs"]
mod repository;
#[path = "support/staged_bundle.rs"]
mod staged;
use api::m16_route::Codec as _;
use budgets::{BudgetId as B, Entries, Span};
use desktop::{
    channel::{ORIGIN, Request},
    lifecycle::{Cleanup, Poll},
};
use macs::{
    Mac, Owner, PUBLIC, Permissive, approve_and_join, consent, free_port, listening, millis,
    private,
};
use node_service::{
    packaged::{self, RunConfig},
    packaged_network::{JoinSeams, NetworkConfig},
};
use serde_json::{Value, json};
use std::{
    io::Write as _,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

/// The recorded reference prompt (the packed oracle's 35 `prompt_ids`, the same text the joined
/// route tests send: `crates/api/tests/support/joined_route_process.rs:30`).
const PROMPT: &str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\nExplain how rain forms and why plants need it. Give a detailed answer.<|im_end|>\n<|im_start|>assistant\n";
/// Tokens the gate's chat asks for (the joined route tests compare 32 with the reference).
const CHAT_TOKENS: u64 = 32;
/// The member's consent: the disk and memory this Mac offers, and the worker wall.
const STORAGE: u64 = 8_000_000_000;
const MEMORY: u64 = 8_589_934_592;
/// The joined Mac's chosen worker wall: 600 s unless `MYCELIUM_TEN_MEMBER_WALL` says otherwise (the
/// member's own choice on its join page). The owner's own contributor grants itself 3600 s
/// (`owner_ports::grant_self_consent`: the standing consent's maximum since REPORT-laneTEN D4,
/// `815fa5d0`), at least the proposal's wall the compile gives the owner's local worker
/// (`chain_compile::admit`, 1800 s), so a longer member wall no longer refuses row 6.
fn wall_seconds() -> u64 {
    std::env::var("MYCELIUM_TEN_MEMBER_WALL")
        .ok()
        .map_or(600, |v| {
            v.parse().expect("MYCELIUM_TEN_MEMBER_WALL in seconds")
        })
}

fn oracle() -> Value {
    serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE).unwrap()
}
/// `row` with `key` set to `value`.
fn with(mut row: Value, key: &str, value: Value) -> Value {
    row[key] = value;
    row
}
fn ms(since: Instant) -> u64 {
    since.elapsed().as_millis() as u64
}

// ---------------------------------------------------------------- evidence -----------------

/// The run directory (`MYCELIUM_TEN_RUN_DIR`): every line the gate writes lands here at once.
#[derive(Clone)]
struct Evidence {
    dir: PathBuf,
    began: Instant,
    rows: Arc<Mutex<Vec<Value>>>,
}
impl Evidence {
    fn open() -> Self {
        let dir = PathBuf::from(
            std::env::var_os("MYCELIUM_TEN_RUN_DIR")
                .expect("MYCELIUM_TEN_RUN_DIR names this run's evidence directory"),
        );
        std::fs::create_dir_all(&dir).unwrap();
        Self {
            dir,
            began: Instant::now(),
            rows: Arc::default(),
        }
    }
    fn append(&self, file: &str, line: &Value) {
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.dir.join(file))
            .unwrap();
        writeln!(f, "{line}").unwrap();
    }
    fn note(&self, what: &str, value: Value) {
        let line = json!({"t_ms": ms(self.began), "what": what, "value": value});
        println!("TEN {line}");
        self.append("events.jsonl", &line);
    }
    /// One table row (row 1-10, chat, Stop, second run, Quit), rewritten as a whole each time.
    fn row(&self, row: Value) {
        println!("TEN ROW {row}");
        let mut rows = self.rows.lock().unwrap();
        rows.push(row);
        let mut table = String::from(
            "| # | row | result | wall ms | page status line |\n|---|---|---|---|---|\n",
        );
        for (i, r) in rows.iter().enumerate() {
            table.push_str(&format!(
                "| {} | {} | {} | {} | {} |\n",
                i + 1,
                r["row"].as_str().unwrap_or_default(),
                r["result"].as_str().unwrap_or_default(),
                r["wall_ms"],
                r["status_line"].to_string().replace('|', "/")
            ));
        }
        std::fs::write(self.dir.join("rows.md"), table).unwrap();
        std::fs::write(
            self.dir.join("rows.json"),
            serde_json::to_vec_pretty(&*rows).unwrap(),
        )
        .unwrap();
    }
}

// ------------------------------------------------------------- processes -------------------

/// `(pid, ppid, rss_kib, command)` of every descendant of this test process.
fn descendants() -> Vec<(u32, u32, u64, String)> {
    let out = std::process::Command::new("/bin/ps")
        .args(["-A", "-o", "pid=,ppid=,rss=,command="])
        .output()
        .unwrap();
    let rows: Vec<(u32, u32, u64, String)> = String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|l| {
            let mut it = l.split_whitespace();
            let pid = it.next()?.parse().ok()?;
            let ppid = it.next()?.parse().ok()?;
            let rss = it.next()?.parse().ok()?;
            Some((pid, ppid, rss, it.collect::<Vec<_>>().join(" ")))
        })
        .filter(|r: &(u32, u32, u64, String)| !r.3.starts_with("/bin/ps "))
        .collect();
    let mut ours = vec![std::process::id()];
    let mut found = Vec::new();
    loop {
        let more: Vec<_> = rows
            .iter()
            .filter(|r| ours.contains(&r.1) && !ours.contains(&r.0))
            .cloned()
            .collect();
        if more.is_empty() {
            return found;
        }
        ours.extend(more.iter().map(|r| r.0));
        found.extend(more);
    }
}
fn alive(pid: u32) -> bool {
    std::process::Command::new("/bin/kill")
        .args(["-0", &pid.to_string()])
        .output()
        .is_ok_and(|o| o.status.success())
}
/// Kills every descendant still alive when the gate unwinds (a failed row must not leave a
/// native worker holding the GPU); by pid, recorded before the kill.
struct Reaper(Evidence);
impl Drop for Reaper {
    fn drop(&mut self) {
        let left = descendants();
        if !left.is_empty() {
            self.0.note(
                "reaper_killed",
                json!(
                    left.iter()
                        .map(|r| json!({"pid":r.0,"command":r.3}))
                        .collect::<Vec<_>>()
                ),
            );
        }
        for (pid, ..) in left {
            std::process::Command::new("/bin/kill")
                .args(["-KILL", &pid.to_string()])
                .status()
                .unwrap_or_default();
        }
    }
}
/// This process's sockets (`lsof -i`), for the work transport's address.
fn sockets() -> Vec<String> {
    let out = std::process::Command::new("/usr/sbin/lsof")
        .args(["-nP", "-a", "-i", "-p", &std::process::id().to_string()])
        .output()
        .unwrap();
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .skip(1)
        .map(str::to_owned)
        .collect()
}
fn lan_ip() -> String {
    let out = std::process::Command::new("/usr/sbin/ipconfig")
        .args(["getifaddr", "en0"])
        .output()
        .unwrap();
    let ip = String::from_utf8_lossy(&out.stdout).trim().to_owned();
    let parsed: std::net::Ipv4Addr = ip.parse().expect("en0 has an IPv4 address");
    assert!(parsed.is_private(), "en0 is not a private address: {ip}");
    if let Ok(expected) = std::env::var("MYCELIUM_LAN_IP") {
        assert_eq!(ip, expected, "MYCELIUM_LAN_IP");
    }
    ip
}

// ------------------------------------------------------------------- hosts -----------------

fn launch(
    state: &Path,
    repository: &cached::CachedRepository,
    network: NetworkConfig,
    run: RunConfig,
) -> Mac {
    let (core, mut cleanup) = packaged::core_with_run(
        state,
        repository.transport(),
        Arc::new(Permissive),
        network,
        run,
    )
    .unwrap();
    let origin = Instant::now();
    cleanup.bind_clock(origin).unwrap();
    ORIGINS.lock().unwrap().push((state.to_owned(), origin));
    Mac {
        core,
        cleanup,
        state: state.to_owned(),
    }
}
/// Each host's Quit clock origin (bound at launch, as the shell binds it at window start).
static ORIGINS: Mutex<Vec<(PathBuf, Instant)>> = Mutex::new(Vec::new());
/// The window's Quit: `begin` with the Quit allowance counted from NOW on the clock bound at launch
/// (`ServiceCleanup::begin` adds its span to that origin), then poll until it settles.
fn quit(mut mac: Mac) -> Result<Poll, desktop::lifecycle::CleanupError> {
    let origin = ORIGINS
        .lock()
        .unwrap()
        .iter()
        .find(|(state, _)| *state == mac.state)
        .map(|(_, origin)| *origin)
        .expect("launched here");
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let began = Instant::now();
    mac.cleanup.begin(Span::from_millis(
        origin.elapsed().as_millis() as u64 + allowance.millis(),
    ))?;
    loop {
        match mac.cleanup.poll() {
            Ok(Poll::Pending) => {
                assert!(
                    began.elapsed()
                        < Duration::from_millis(allowance.millis()) + millis(B::OwnerPoll),
                    "the packaged core did not quit within its allowance"
                );
                std::thread::sleep(millis(B::OwnerPoll) / 5);
            }
            other => return other,
        }
    }
}
fn private_dir(base: &Path, name: &str) -> PathBuf {
    let state = base.join(name);
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    state
}
/// The app's own network composition (work transport: this Mac's private LAN address), with the
/// relay client lookup finding nothing (`provider: external` only; never a `cloudflared`).
fn app_network() -> NetworkConfig {
    let production = NetworkConfig::default().hooks;
    NetworkConfig {
        hooks: Arc::new(move || {
            let mut hooks = production();
            hooks.binary = Arc::new(|| None);
            hooks
        }),
        ..NetworkConfig::default()
    }
}
/// The page's run-model picker: Qwen3-0.6B (ruling 28: the only two-Mac model).
fn choose_qwen3(mac: &Mac) {
    let chosen = mac.post("/api/run-model", json!({"choice": "qwen3-0.6b"}));
    assert_eq!(chosen.0, 200, "run-model: {}", chosen.1);
}
/// `/api/model/download` of the cached snapshot until this Mac's acquisition prepared it.
fn prepare_model(mac: &Mac, ev: &Evidence, who: &str) {
    let began = Instant::now();
    let grant = json!({"consent": {"download_acknowledged": true,
        "max_download_bytes": 4_000_000_000u64, "storage_limit_bytes": STORAGE}});
    let started = mac.post("/api/model/download", grant);
    assert_eq!(started.0, 200, "download: {}", started.1);
    let source = format!("model-source-{}", &cached::COMMIT[..12]);
    let deadline = Instant::now() + millis(B::VerifyStall);
    let status = loop {
        let s: Value = std::fs::read(mac.state.join(&source).join("status.json"))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or(Value::Null);
        if matches!(
            s["state"].as_str(),
            Some("prepared" | "failed" | "cancelled")
        ) {
            break s;
        }
        assert!(Instant::now() < deadline, "{who} model: {s}");
        std::thread::sleep(millis(B::OwnerPoll));
    };
    ev.note(&format!("{who}_model_prepared"), json!({"ms": ms(began), "status": status,
        "page": mac.status()["model_status"], "policy_error": mac.status()["acquisition_policy_error"]}));
    assert_eq!(status["state"], "prepared", "{who}: {status}");
}
fn admit_hello_program(mac: &Mac) {
    let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG")
        .expect("MYCELIUM_BOOTSTRAP_TEST_CONFIG names the admitted hello helper");
    let document: Value = serde_json::from_slice(&std::fs::read(config).unwrap()).unwrap();
    api::preferences::private_state::store(
        &private(&mac.state).join("bootstrap-program.json"),
        &document,
        api::preferences::private_state::private_json_limit().unwrap(),
    )
    .unwrap();
}
fn join_body(member: &Mac, link: &str) -> Value {
    let digest = member.status()["join_flow"]["standing_disclosure"]["digest"].clone();
    json!({
        "consent": consent(STORAGE),
        "standing_consent": {"acknowledged": true, "disclosure_digest": digest,
            "caps": {"storage_bytes": STORAGE, "max_memory_bytes": MEMORY,
                     "max_wall_seconds": wall_seconds(), "max_commands": 5000}},
        "join": link,
    })
}
/// The packaged owner (relay on a loopback proxy, swarm, Qwen3-0.6B prepared, open code) and a
/// packaged member joined to it through the real join path with its own prepared model.
fn pair(
    base: &Path,
    repository: &cached::CachedRepository,
    run: RunConfig,
    program: &api::native_compiler::NativeProgram,
    ev: &Evidence,
) -> (Owner, Mac) {
    let sidecar = repository::sidecar_file(base);
    let state = private_dir(base, "owner");
    let before = listening();
    let mac = launch(&state, repository, app_network(), run);
    let proxy = free_port();
    let started = mac.post(
        "/api/network/start",
        json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
               "public_origin": PUBLIC, "proxy_port": proxy}),
    );
    assert_eq!(started.0, 200, "network start: {}", started.1);
    let created = mac.post(
        "/api/create",
        json!({"consent": consent(api::model_catalog::storage_consent_ceiling())}),
    );
    assert_eq!(created.0, 200, "create: {}", created.1);
    choose_qwen3(&mac);
    prepare_model(&mac, ev, "owner");
    admit_hello_program(&mac);
    let opened = mac.post("/api/pair/window/open", json!({"ttl_seconds": 600}));
    assert_eq!(opened.1["state"], "open", "{}", opened.1);
    let seed: Vec<u16> = listening()
        .into_iter()
        .filter(|p| *p != proxy && !before.contains(p))
        .collect();
    assert_eq!(seed.len(), 1, "one owner seed listener: {seed:?}");
    let ca = std::fs::read(private(&state).join("seed-rs/tls/ca.pem")).unwrap();
    let seams = JoinSeams {
        join_base: Some(format!("http://127.0.0.1:{proxy}")),
        member_seed: api::seed::SeedOptions {
            ca_pem: Some(ca),
            connect_to: Some(format!("127.0.0.1:{}", seed[0]).parse().unwrap()),
            server_name: Some("127.0.0.1".into()),
        },
    };
    let owner = Owner {
        link: opened.1["join_link"].as_str().unwrap().to_owned(),
        window_id: opened.1["window_id"].clone(),
        mac,
        proxy,
        seams,
    };
    let member = launch(
        &private_dir(base, "member"),
        repository,
        NetworkConfig {
            seams: owner.seams.clone(),
            sidecar: Some(sidecar),
            ..app_network()
        },
        // The same app on the joined Mac: its stage worker is the program its own startup
        // admitted (`core_with_native_program`), never the development environment.
        RunConfig {
            native_program: Some(program.clone()),
            ..RunConfig::default()
        },
    );
    choose_qwen3(&member);
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    approve_and_join(&owner, &member);
    // No download is asked of the joined Mac: its own agent starts the announced model's
    // acquisition at the descriptors step (`_ensure_whole_model`, demo_target.py:543-612;
    // `member_agent::whole_model`), as the shipped app does.
    admit_hello_program(&member);
    (owner, member)
}

// -------------------------------------------------------------------- rows -----------------

/// Each row's registered budget (budgets::table, demo_chain.py:259-264 @ f5bfb709), the limit of
/// the gate's wait for that row once it is running.
fn row_budget(step: &str) -> Duration {
    match step {
        "model" => millis(B::ModelSelection) + millis(B::VerifyStall),
        "contributor" => millis(B::Descriptors),
        "descriptors" => millis(B::Descriptors) + millis(B::DescriptorMargin),
        "observe" => millis(B::InitialCleanup) * 2 + millis(B::Bootstrap) + millis(B::Collect),
        "compile" => millis(B::Compile),
        "slices" => millis(B::Slices),
        "publish" => millis(B::Publish),
        "targets" => millis(B::Targets),
        "activate" => millis(B::Targets),
        "serving" => millis(B::Serving),
        other => panic!("unknown step {other}"),
    }
}
/// The status line the page shows for a step (run.js: label, then the step's detail).
fn status_line(step: &Value) -> Value {
    json!({"label": step["label"], "state": step["state"], "detail": step["detail"]})
}
/// Start pressed: wait on the page's status for each of the ten rows to reach its done state in
/// order, one row's registered budget at a time. Ok(()) once the run reads `serving`; Err(the
/// failure line) at the first row that failed or overran its budget.
fn ten_rows(owner: &Mac, member: &Mac, ev: &Evidence, run: &str) -> Result<Value, String> {
    let (code, started) = owner.post("/api/demo/start", json!({}));
    ev.note(
        &format!("{run}_start"),
        json!({"code": code, "answer": started}),
    );
    if code != 200 {
        return Err(format!("start refused {code}: {started}"));
    }
    let began = Instant::now();
    let mut done = 0usize;
    let mut row_began = Instant::now();
    let mut last = Value::Null;
    let mut last_member = Value::Null;
    loop {
        let status = owner.status();
        let state = status["owner_run"].clone();
        if state != last {
            ev.append(
                &format!("{run}-owner-status.jsonl"),
                &json!({"t_ms": ms(began), "owner_run": state}),
            );
            last = state.clone();
        }
        let member_status = member.status();
        let card = json!({"demo_target": member_status["demo_target"],
            "acquisition": member_status["acquisition"], "leave": member_status["leave"],
            "renewal_error": member_status["renewal_error"]});
        if card != last_member {
            ev.append(
                &format!("{run}-member-status.jsonl"),
                &json!({"t_ms": ms(began), "member": member_status}),
            );
            last_member = card;
        }
        let steps = state["steps"].as_array().cloned().unwrap_or_default();
        let finished_in_order = steps.iter().take_while(|s| s["state"] == "done").count();
        if steps[finished_in_order..]
            .iter()
            .any(|s| s["state"] == "done")
        {
            return Err(format!("row order broken: {state}"));
        }
        // Rows that finished between two polls of the page share this poll's wall.
        let in_this_poll = finished_in_order.saturating_sub(done);
        while done < finished_in_order {
            ev.row(json!({"row": format!("{run} row {} {}", done + 1, steps[done]["name"].as_str().unwrap_or("?")),
                "result": "passed", "wall_ms": row_began.elapsed().as_millis() as u64,
                "since_start_ms": ms(began), "status_line": status_line(&steps[done]),
                "rows_seen_done_in_one_poll": in_this_poll}));
            done += 1;
            row_began = Instant::now();
            // Once the run's work is composed (row 6), and again before serving (row 9), the
            // page's chat must still refuse: no route is offered before the run serves.
            if done == 6 || done == 9 {
                let (code, answer) = chat_call(
                    owner,
                    "POST",
                    "/chat/api/send",
                    &json!({"prompt": PROMPT, "max_new_tokens": 4}),
                );
                ev.note(
                    &format!("{run}_chat_before_serving_row{done}"),
                    json!({"code": code, "answer": answer}),
                );
                // The run may reach serving between the status read and this call: only a 200
                // while a fresh status still is not serving is a route offered too early.
                if code == 200 && owner.status()["owner_run"]["state"] != "serving" {
                    return Err(format!(
                        "{run}: the chat route was offered before the run served (after row {done}): {answer}"
                    ));
                }
            }
        }
        match state["state"].as_str() {
            Some("serving") if done >= 9 => {
                if done == 9 {
                    ev.row(json!({"row": format!("{run} row 10 serving"), "result": "passed",
                        "wall_ms": row_began.elapsed().as_millis() as u64, "since_start_ms": ms(began),
                        "status_line": status_line(&steps[9]), "run_state": "serving"}));
                }
                ev.note(
                    &format!("{run}_serving"),
                    json!({"total_ms": ms(began), "owner_run": state}),
                );
                return Ok(state);
            }
            Some("failed" | "stopped") => {
                let at = steps.get(done).cloned().unwrap_or(Value::Null);
                ev.row(json!({"row": format!("{run} row {} {}", done + 1, at["name"].as_str().unwrap_or("?")),
                    "result": "failed", "wall_ms": row_began.elapsed().as_millis() as u64,
                    "since_start_ms": ms(began), "status_line": status_line(&at),
                    "error_code": state["error_code"], "failed_step": state["failed_step"],
                    "cleanup_errors": state["cleanup_errors"]}));
                return Err(format!(
                    "run {run} ended {} at row {} ({}): error_code={} failed_step={} detail={}",
                    state["state"],
                    done + 1,
                    at["name"],
                    state["error_code"],
                    state["failed_step"],
                    at["detail"]
                ));
            }
            _ => {}
        }
        if let Some(step) = steps.get(done).and_then(|s| s["name"].as_str())
            && row_began.elapsed() > row_budget(step)
        {
            ev.row(json!({"row": format!("{run} row {} {step}", done + 1), "result": "failed",
                "wall_ms": row_began.elapsed().as_millis() as u64, "status_line": status_line(&steps[done]),
                "error_code": "gate_row_budget_exceeded"}));
            return Err(format!(
                "row {} ({step}) overran its registered budget: {state}",
                done + 1
            ));
        }
        // The page's own refresh: poll the status again one fifth of an owner poll later.
        std::thread::sleep(millis(B::OwnerPoll) / 5);
    }
}

// -------------------------------------------------------------------- chat -----------------

fn chat_call(mac: &Mac, method: &str, path: &str, body: &Value) -> (u16, Value) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    mac.core.submit(
        Request {
            window: "main".into(),
            method: method.into(),
            uri: format!("{ORIGIN}{path}"),
            headers: vec![
                ("x-mycelium-chat".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(body).unwrap(),
        },
        Box::new(move |reply| tx.send(reply).unwrap_or_default()),
    );
    let reply = rx.recv_timeout(millis(B::WorkerStartup) * 2).unwrap();
    (
        reply.status,
        serde_json::from_slice(&reply.body).unwrap_or(Value::Null),
    )
}
/// One page chat: send, then the events loop (chat.js) until done. The pieces as the page shows
/// them, with each piece's arrival time from the send.
fn chat(mac: &Mac, ev: &Evidence, label: &str, tokens: u64) -> (Value, Vec<(String, u64)>) {
    let (_, state) = chat_call(mac, "POST", "/chat/api/state", &json!({}));
    ev.note(&format!("{label}_chat_state_before"), state);
    let posted = Instant::now();
    let (status, sent) = chat_call(
        mac,
        "POST",
        "/chat/api/send",
        &json!({"prompt": PROMPT, "max_new_tokens": tokens}),
    );
    ev.note(
        &format!("{label}_chat_send"),
        json!({"status": status, "answer": sent}),
    );
    if status != 200 {
        return (json!({"send_status": status, "send": sent}), Vec::new());
    }
    let mut pieces = Vec::new();
    let mut after = json!(0);
    // chat's own deadlines: 30 s to the first token, 10 s per token (inherited by the route).
    let end = posted
        + millis(B::WorkerStartup)
        + Duration::from_secs(10) * tokens as u32
        + millis(B::WorkerStartup);
    loop {
        let (status, page) = chat_call(
            mac,
            "POST",
            "/chat/api/events",
            &json!({"request_id": sent["request_id"], "after": after}),
        );
        if status != 200 {
            return (json!({"events_status": status, "events": page}), pieces);
        }
        for t in page["tokens"].as_array().into_iter().flatten() {
            pieces.push((t.as_str().unwrap_or_default().to_owned(), ms(posted)));
        }
        after = page["next"].clone();
        if page["done"] == true {
            return (page, pieces);
        }
        if Instant::now() > end {
            return (json!({"gate": "chat_never_ended", "last": page}), pieces);
        }
        std::thread::sleep(millis(B::OwnerPoll) / 5);
    }
}
/// The reference's first `n` tokens as the page shows them: one decoded piece per token, by the
/// product tokenizer helper over the cached tokenizer.
fn reference_pieces(codec: &api::joined_owner::codec::NativeCodec, n: usize) -> Vec<String> {
    let reference: Vec<u32> =
        serde_json::from_value(oracle()["recorded"]["tokens"].clone()).unwrap();
    reference[..n]
        .iter()
        .map(|t| codec.decode(*t).unwrap())
        .collect()
}
fn chat_row(
    mac: &Mac,
    ev: &Evidence,
    label: &str,
    codec: &api::joined_owner::codec::NativeCodec,
) -> Result<(), String> {
    let (last, pieces) = chat(mac, ev, label, CHAT_TOKENS);
    let got: Vec<String> = pieces.iter().map(|p| p.0.clone()).collect();
    let expected = reference_pieces(codec, CHAT_TOKENS as usize);
    let ttft = pieces.first().map(|p| p.1);
    let row = json!({"row": format!("{label} chat"), "wall_ms": pieces.last().map(|p| p.1),
        "ttft_ms": ttft, "pieces": got.len(), "status_line": {"terminal": last["terminal"], "error": last["error"]},
        "last": last});
    if last["terminal"] == "completed" && got == expected {
        ev.row(with(row, "result", json!("passed")));
        return Ok(());
    }
    ev.note(
        &format!("{label}_chat_mismatch"),
        json!({"got": got, "expected": expected}),
    );
    ev.row(with(row, "result", json!("failed")));
    // A second row, not a substitute: the longest chat the route's stages admit (their KV context
    // is the challenge's prompt + 4, REPORT-laneTEN D6), compared token for token as above.
    let (small, small_pieces) = chat(mac, ev, &format!("{label}_within_capacity"), 4);
    let small_got: Vec<String> = small_pieces.iter().map(|p| p.0.clone()).collect();
    let small_expected = reference_pieces(codec, 4);
    let passed = small["terminal"] == "completed" && small_got == small_expected;
    ev.row(
        json!({"row": format!("{label} chat within the stages' 39-token context (4 tokens)"),
        "result": if passed { "passed" } else { "failed" },
        "wall_ms": small_pieces.last().map(|p| p.1), "ttft_ms": small_pieces.first().map(|p| p.1),
        "pieces": small_got, "expected": small_expected,
        "status_line": {"terminal": small["terminal"], "error": small["error"]}}),
    );
    Err(format!(
        "chat {label}: terminal={} error={} pieces={}; within the 39-token context: {}",
        last["terminal"],
        last["error"],
        got.len(),
        if passed {
            "equal to the reference"
        } else {
            "FAILED"
        }
    ))
}

/// Lane CAP measurement (opt-in, `MYCELIUM_TEN_MEASURE_CHATS=<n>`; not a gate row): after the
/// run's chat row, one 128-token answer, then 256-token answers on the reference prompt until one
/// does not complete or `n` were sent. Per chat: time to the first piece, mean time per later
/// piece, the page's terminal and error, both workers' resident memory after it. A send the page
/// rate-limits (4 per minute per client) is retried after a pause and counted.
fn measure_chats(mac: &Mac, ev: &Evidence, run: &str, binary: &Path) {
    let Some(n) = std::env::var("MYCELIUM_TEN_MEASURE_CHATS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
    else {
        return;
    };
    let began = Instant::now();
    for i in 0..n {
        let tokens = if i == 0 { 128 } else { 256 };
        let label = format!("{run}_measure_{i}_{tokens}");
        let mut limited = 0;
        let (last, pieces) = loop {
            let (last, pieces) = chat(mac, ev, &label, tokens);
            if last["send_status"] == 429 && limited < 8 {
                limited += 1;
                std::thread::sleep(Duration::from_secs(16));
                continue;
            }
            break (last, pieces);
        };
        let ttft = pieces.first().map(|p| p.1);
        let per_token = (pieces.len() > 1)
            .then(|| (pieces.last().unwrap().1 - pieces[0].1) as f64 / (pieces.len() - 1) as f64);
        let completed = last["terminal"] == "completed";
        ev.note(
            "measure_chat",
            json!({"label": label, "asked": tokens, "pieces": pieces.len(), "ttft_ms": ttft,
                "mean_ms_per_later_piece": per_token, "wall_ms": pieces.last().map(|p| p.1),
                "terminal": last["terminal"], "error": last["error"], "rate_limited_retries": limited,
                "since_measure_start_ms": ms(began), "workers_after": workers(binary),
                "text": pieces.iter().map(|p| p.0.as_str()).collect::<String>(), "last": last}),
        );
        if !completed {
            let (_, state) = chat_call(mac, "POST", "/chat/api/state", &json!({}));
            ev.note(
                "measure_end",
                json!({"after_chats": i, "page_state": state, "owner_run": mac.status()["owner_run"]}),
            );
            return;
        }
    }
    ev.note(
        "measure_end",
        json!({"after_chats": n, "all_completed": true}),
    );
}

// ---------------------------------------------------------------- the gate -----------------

/// The native workers of a serving run: this process's descendants running the native program's
/// `native-worker` role, with their resident memory.
fn workers(binary: &Path) -> Vec<(u32, u64)> {
    let name = binary.to_string_lossy().into_owned();
    descendants()
        .into_iter()
        .filter(|r| r.3.contains(&name) && r.3.contains("native-worker"))
        .map(|r| (r.0, r.2))
        .collect()
}
/// Lane CHATS item 4 (ledger row 106 a): while the run serves, the owner's page can reach the
/// chat. run.js shows "Open the chat on this Mac" (and the room's link) only from the status's
/// `chat` block (v0.1.4 `chat.chat_status`, chat.py:521-529); the chat row below calls the chat
/// API directly, so it passed on rv39 while the page said "The chat opens when the route is
/// serving." The owner's block must say available with a `local_url`; a joined Mac has none.
fn chat_link_row(owner: &Mac, member: &Mac, ev: &Evidence, run: &str) -> Result<(), String> {
    let block = owner.status()["chat"].clone();
    let joined = member.status()["chat"].clone();
    ev.note(
        &format!("{run}_status_chat"),
        json!({"owner": block, "member": joined}),
    );
    let ok = block["available"] == true
        && block["local_url"]
            .as_str()
            .is_some_and(|url| url.ends_with("/chat"))
        && joined.is_null();
    ev.row(
        json!({"row": format!("{run} chat link"), "result": if ok { "passed" } else { "failed" },
        "wall_ms": null, "status_line": block}),
    );
    if ok {
        Ok(())
    } else {
        Err(format!(
            "{run}: the serving owner's status does not offer the chat: {block} (member: {joined})"
        ))
    }
}
fn stop_row(
    owner: &Owner,
    member: &Mac,
    ev: &Evidence,
    run: &str,
    binary: &Path,
    lan: &str,
) -> Result<(), String> {
    let pids = workers(binary);
    let began = Instant::now();
    let (code, answer) = owner.mac.post("/api/demo/stop", json!({}));
    ev.note(
        &format!("{run}_stop_pressed"),
        json!({"code": code, "answer": answer, "workers": pids}),
    );
    let allowance = Duration::from_millis(
        B::EffectiveStopAllowance
            .wall_span(Span::from_millis(wall_seconds() * 1000), Entries::new(2))
            .unwrap()
            .millis(),
    );
    let state = loop {
        let state = owner.mac.status()["owner_run"].clone();
        if matches!(state["state"].as_str(), Some("stopped" | "failed")) {
            break state;
        }
        if began.elapsed() > allowance {
            break json!({"gate": "stop_allowance_exceeded", "last": state});
        }
        std::thread::sleep(millis(B::OwnerPoll) / 5);
    };
    let stopped_ms = ms(began);
    let (_, page) = chat_call(&owner.mac, "POST", "/chat/api/state", &json!({}));
    let (send_code, send) = chat_call(
        &owner.mac,
        "POST",
        "/chat/api/send",
        &json!({"prompt": PROMPT, "max_new_tokens": 4}),
    );
    // Workers and transport: gone within the router's close once the run reads stopped.
    let close = Instant::now() + millis(B::RouterClose) + millis(B::CancelAndCleanup);
    let (left, lan_sockets) = loop {
        let left: Vec<u32> = pids.iter().map(|p| p.0).filter(|p| alive(*p)).collect();
        let lan_sockets: Vec<String> = sockets().into_iter().filter(|s| s.contains(lan)).collect();
        if (left.is_empty() && lan_sockets.is_empty()) || Instant::now() > close {
            break (left, lan_sockets);
        }
        std::thread::sleep(millis(B::OwnerPoll) / 5);
    };
    let row = json!({"row": format!("{run} Stop"), "wall_ms": stopped_ms, "status_line": {"run": state["state"],
        "error_code": state["error_code"], "chat_state": page, "send_after_stop": [send_code, send]},
        "workers_left": left, "lan_sockets_left": lan_sockets, "workers_gone_ms": ms(began),
        "member_card": member.status()["member_work"]});
    let ok = state["state"] == "stopped"
        && state["error_code"].is_null()
        && left.is_empty()
        && lan_sockets.is_empty()
        && !pids.is_empty()
        && send_code != 200;
    ev.row(with(
        row,
        "result",
        json!(if ok { "passed" } else { "failed" }),
    ));
    if ok {
        Ok(())
    } else {
        Err(format!(
            "Stop {run}: {state} left={left:?} lan={lan_sockets:?} page={page}"
        ))
    }
}

#[test]
#[ignore = "real Metal + cached Qwen3-0.6B + this Mac's private LAN address; MYCELIUM_TEN_PACKAGED_EXECUTABLE, MYCELIUM_PINNED_PAYLOAD_APP, MYCELIUM_TEN_RUN_DIR, MYCELIUM_BOOTSTRAP_TEST_CONFIG"]
fn ten_rows_with_the_real_model_on_the_lan() {
    let ev = Evidence::open();
    let _reaper = Reaper(ev.clone());
    // No stage worker may come from the development environment (REPORT-laneTEN W-D2).
    for name in [
        "MYCELIUM_COMPILER_NATIVE_BINARY",
        "MYCELIUM_COMPILER_NATIVE_SHA256",
    ] {
        assert!(
            std::env::var_os(name).is_none(),
            "{name} is set: the gate runs the admitted program only"
        );
    }
    let lan = lan_ip();
    let base = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "ten-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&base)
        .unwrap();
    ev.note("state_root", json!(base));
    // The packaged executable in a bundle staged around it, admitted as the shipped startup does.
    let packaged = PathBuf::from(std::env::var_os("MYCELIUM_TEN_PACKAGED_EXECUTABLE").expect(
        "MYCELIUM_TEN_PACKAGED_EXECUTABLE = the packaged `mycelium` (--features packaged --bin mycelium)",
    ));
    let binary = staged::stage(&packaged, &staged::payload_app(), &base);
    let program = staged::admitted_program(&binary);
    assert_eq!(program.binary, binary);
    ev.note(
        "setup",
        json!({"lan": lan, "packaged_executable": packaged, "staged_executable": binary,
        "payload_app": staged::payload_app(), "native_program": {"binary": program.binary,
        "sha256": program.expected.sha256, "bytes": program.expected.bytes},
        "compiler_native_env": [std::env::var("MYCELIUM_COMPILER_NATIVE_BINARY").ok(),
            std::env::var("MYCELIUM_COMPILER_NATIVE_SHA256").ok()],
        "pid": std::process::id()}),
    );
    // The product tokenizer helper (the packaged executable's `tokenize` role) over the cached
    // tokenizer: the reference prompt's 35 ids and the decode of the reference tokens (ledger row 46).
    let tokenizer = cached::snapshot_dir().join("tokenizer.json");
    let codec = api::joined_owner::codec::NativeCodec::start(&program.binary, &tokenizer).unwrap();
    let prompt_ids = codec.encode(PROMPT).unwrap();
    assert_eq!(
        json!(prompt_ids),
        oracle()["recorded"]["workload"]["prompt_ids"],
        "the reference prompt's ids"
    );
    ev.note(
        "tokenizer",
        json!({"prompt_ids": prompt_ids.len(), "challenge_decoded": reference_pieces(&codec, 4)}),
    );

    let repository = cached::CachedRepository::start();
    let (owner, member) = pair(
        &base,
        &repository,
        RunConfig {
            sidecar: Some(repository::sidecar_file(&base)),
            native_program: Some(program.clone()),
            observe_test_double: Some(Arc::new(observe_test_double::HostIdentityTestDouble::over)),
            ..RunConfig::default()
        },
        &program,
        &ev,
    );
    ev.note(
        "paired",
        json!({"served_bytes": repository.served_bytes(), "sockets": sockets()}),
    );

    let mut failures = Vec::new();
    for run in ["run1", "run2"] {
        match ten_rows(&owner.mac, &member, &ev, run) {
            Ok(_) => {
                let at_serving = workers(&binary);
                ev.note(&format!("{run}_workers_at_serving"), json!({"workers": at_serving,
                    "lan_sockets": sockets().into_iter().filter(|s| s.contains(&lan)).collect::<Vec<_>>(),
                    "loopback_udp": sockets().into_iter().filter(|s| s.contains("UDP") && s.contains("127.0.0.1")).collect::<Vec<_>>()}));
                if let Err(e) = chat_link_row(&owner.mac, &member, &ev, run) {
                    failures.push(e);
                }
                if let Err(e) = chat_row(&owner.mac, &ev, run, &codec) {
                    failures.push(e);
                } else if run == "run1" {
                    measure_chats(&owner.mac, &ev, run, &binary);
                }
                if let Err(e) = stop_row(&owner, &member, &ev, run, &binary, &lan) {
                    failures.push(e);
                }
            }
            Err(e) => {
                ev.note(&format!("{run}_stopped_at"), json!(e));
                failures.push(e);
                // The run is over; Stop anyway so the next Start begins from a stopped run.
                let (code, answer) = owner.mac.post("/api/demo/stop", json!({}));
                ev.note(
                    &format!("{run}_stop_after_failure"),
                    json!({"code": code, "answer": answer}),
                );
                if run == "run1"
                    && std::env::var_os("MYCELIUM_TEN_SECOND_RUN_AFTER_FAILURE").is_none()
                {
                    ev.row(json!({"row": "run2", "result": "not reached", "wall_ms": null, "status_line": null}));
                    break;
                }
            }
        }
    }
    // Quit both hosts (owner first: its Quit stops its run), then no worker is left, by pid.
    let began = Instant::now();
    // The gate's own tokenizer helper (not the app's) ends first.
    drop(codec);
    let before = descendants();
    let owner_quit = quit(owner.mac);
    let member_quit = quit(member);
    let left: Vec<_> = before
        .iter()
        .filter(|r| alive(r.0))
        .map(|r| json!({"pid": r.0, "command": r.3}))
        .collect();
    let after = descendants();
    let ok = owner_quit == Ok(Poll::Verified)
        && member_quit == Ok(Poll::Verified)
        && left.is_empty()
        && after.is_empty();
    ev.row(
        json!({"row": "Quit", "result": if ok {"passed"} else {"failed"}, "wall_ms": ms(began),
        "status_line": {"owner": format!("{owner_quit:?}"), "member": format!("{member_quit:?}")},
        "children_before": before.len(), "left": left, "descendants_after": after.len()}),
    );
    if !ok {
        failures.push(format!(
            "Quit: owner={owner_quit:?} member={member_quit:?} left={left:?}"
        ));
    }
    drop(repository);
    // A failed gate keeps both state roots for the report; a passed one removes them.
    if failures.is_empty() {
        std::fs::remove_dir_all(&base).unwrap_or_default();
    }
    ev.note("end", json!({"failures": failures}));
    assert!(failures.is_empty(), "{failures:#?}");
}
