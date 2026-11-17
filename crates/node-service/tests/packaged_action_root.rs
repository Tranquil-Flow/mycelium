// SPDX-License-Identifier: AGPL-3.0-only
//! S1 root A: the packaged executable's `POST /api/*` goes through
//! `api::persistent::PersistentBackend<PackagedHost, NodeService>`, not through the node
//! service's `slice_action_unsupported`. Its first consumers are v0.1.4's preference actions
//! (app.py:1356-1361 @ f4e366e9: fast_mode.py:53-70 `choose`, app.py:855-868
//! `_choose_run_model`, run_model.py:88-94 `save_choice`); Stop and Rebuild still reach the node
//! service; an action whose producer is not ported is the host's own refusal.
//!
//! `PackagedHost` reads the real process environment and process-global `set_var` is unsafe
//! (denied in this workspace), so the tests that depend on `MYCELIUM_TOKEN_PATH` or
//! `MYCELIUM_A13_RUN_MODEL` re-run themselves in a child of this test binary with exactly the
//! environment they need. The child records what it saw; the parent asserts.
#![cfg(feature = "desktop-shell")]
#[path = "../../api/tests/support/oracle_context.rs"]
mod oracle_context;
use api::schema::PublicError;
use budgets::{BudgetId as B, Entries};
use desktop::services::{ServiceRequest, Services};
use node_service::{
    app_host::FastModeDefault,
    desktop_host::DesktopServices,
    service::{Driver, Job, NodeService},
};
use serde_json::{Value, json};
use std::{
    num::NonZeroUsize,
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::{Path, PathBuf},
    process::Command,
    sync::{Arc, Condvar, Mutex},
    time::{Duration, Instant},
};

/// v0.1.4 executed `_choose_run_model` cases (`api` tests, `L10-evidence/r7-probe-run-model.py`).
const RUN_MODEL_ORACLE: &str = include_str!("../../api/tests/fixtures/oracle_run_model.json");
/// v0.1.4's real packaged process over loopback HTTP (`test-support/journeys`): what the
/// packaged app itself answered at each public boundary.
const PACKAGED_BOUNDARIES: &str =
    include_str!("../../../test-support/journeys/fixtures/python-v014-boundaries.json");
/// Set only in the child: the private root the parent made for it.
const CHILD_ROOT: &str = "MYCELIUM_TEST_PACKAGED_ACTION_ROOT";
/// The packaged host's environment overrides, removed from (or set in) every child.
const OVERRIDES: [&str; 2] = [
    api::persistent::TOKEN_PATH_ENV,
    api::preferences::run_model::ENV,
];

fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
struct MustNotRun;
impl Driver for MustNotRun {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        panic!("a desktop action started native work")
    }
}
/// Holds its run until released, so the node service has an active run.
type Release = Arc<(Mutex<bool>, Condvar)>;
struct Hold(Release);
impl Driver for Hold {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        let (released, changed) = &*self.0;
        let mut released = released.lock().unwrap();
        while !*released {
            released = changed.wait(released).unwrap();
        }
        Ok(())
    }
}
fn private_root() -> PathBuf {
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "packaged-action-root-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
/// The packaged composition (`desktop_host::compose` minus the native driver) over `root`.
fn compose(driver: Arc<dyn Driver>, root: &Path) -> (DesktopServices, NodeService) {
    let node = NodeService::new(
        driver,
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    let host = DesktopServices::new(node.clone(), NonZeroUsize::new(4).unwrap(), root);
    (host, node)
}
fn send(host: &DesktopServices, method: &str, path: &str, body: Option<Value>) -> (u16, Value) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    // The window's channel header: chat pages send `X-Mycelium-Chat`, actions `X-Mycelium-Action`.
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
    (reply.status, serde_json::from_slice(&reply.body).unwrap())
}
fn post(host: &DesktopServices, path: &str, body: Value) -> (u16, Value) {
    send(host, "POST", path, Some(body))
}
fn refusal(reply: &(u16, Value)) -> (u16, &str) {
    (
        reply.0,
        reply.1["error"].as_str().unwrap_or("<not a refusal>"),
    )
}
fn default_json(default: FastModeDefault) -> Value {
    json!({"value": match default.value { Ok(v) => json!({"ok": v}), Err(c) => json!({"err": c}) },
           "refreshes": default.refreshes})
}
/// `(canonical text, mode)` of a private file, `null` when absent.
fn private_file(path: &Path) -> Value {
    match std::fs::read_to_string(path) {
        Ok(text) => json!({"text": text,
            "mode": format!("{:#o}", std::fs::metadata(path).unwrap().mode() & 0o7777)}),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Value::Null,
        Err(e) => panic!("{}: {e}", path.display()),
    }
}
fn entries(root: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(root)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}
/// In the parent: run `test` again in a child whose environment holds exactly `set` of the
/// [`OVERRIDES`], over a fresh private root, and return that root and what the child observed.
/// In the child: run `observe` over the root the parent made, record it, and return `None`.
fn in_child(
    test: &str,
    set: &[(&str, &str)],
    observe: impl FnOnce(&Path) -> Value,
) -> Option<(PathBuf, Value)> {
    if let Some(root) = std::env::var_os(CHILD_ROOT) {
        let root = PathBuf::from(root);
        let seen = observe(&root);
        std::fs::write(root.with_extension("observed.json"), seen.to_string()).unwrap();
        return None;
    }
    let root = private_root();
    let mut child = Command::new(std::env::current_exe().unwrap());
    child.args([test, "--exact"]).env(CHILD_ROOT, &root);
    for name in OVERRIDES {
        child.env_remove(name);
    }
    for (name, value) in set {
        child.env(name, value);
    }
    let output = child.output().unwrap();
    assert!(
        output.status.success(),
        "child {test} failed:\n{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let seen = std::fs::read_to_string(root.with_extension("observed.json"))
        .unwrap_or_else(|e| panic!("child {test} recorded nothing ({e})"));
    Some((root, serde_json::from_str(&seen).unwrap()))
}

#[test]
fn packaged_action_root_run_model_persists_choice() {
    let oracle: Value = serde_json::from_str(RUN_MODEL_ORACLE).unwrap();
    assert_eq!(oracle["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    // Every executed case a fresh, unlocked, idle packaged owner can replay in order (the
    // corrupt-state and failed-write cases need a prepared or unwritable root).
    let cases: Vec<Value> = oracle["choose_cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| {
            c["env"].is_null()
                && c["pre"].is_null()
                && c["acquisition_active"] == false
                && c["owner_run_active"] != true
                && !matches!(c["label"].as_str(), Some("corrupt_state" | "write_failed"))
        })
        .cloned()
        .collect();
    assert_eq!(cases.len(), 14);
    let file = |root: &Path| private_file(&root.join(api::preferences::run_model::FILE));
    let Some((_, seen)) = in_child(
        "packaged_action_root_run_model_persists_choice",
        &[],
        |root| {
            let (host, _) = compose(Arc::new(MustNotRun), root);
            Value::Array(
                cases
                    .iter()
                    .map(|case| {
                        let (status, body) = post(&host, "/api/run-model", case["body"].clone());
                        json!({"status": status, "body": body, "file": file(root)})
                    })
                    .collect(),
            )
        },
    ) else {
        return;
    };
    let mut previous = Value::Null;
    for (case, seen) in cases.iter().zip(seen.as_array().unwrap()) {
        let label = case["label"].as_str().unwrap();
        if let Some(ok) = case.get("ok") {
            assert_eq!(seen["status"], 200, "{label}: {}", seen["body"]);
            assert_eq!(
                seen["file"],
                json!({"text": case["file"], "mode": case["mode"]}),
                "{label}"
            );
            // `resources` is this Mac's memory (`host_memory_bytes()`), not the oracle's.
            let strip = |v: &Value| {
                let mut v = v.clone();
                v.as_object_mut().unwrap().remove("resources");
                v
            };
            assert!(seen["body"]["resources"].is_object(), "{label}");
            // v0.2's starter and picker texts (B13, `api/contracts/divergences.json`): the
            // oracle's are checked, then moved to the product's.
            let mut ok = ok.clone();
            assert_eq!(oracle_context::at_product_picker(&mut ok), 1, "{label}");
            assert_eq!(strip(&seen["body"]), strip(&ok), "{label}");
        } else {
            assert_eq!(
                (seen["status"].clone(), seen["body"]["error"].clone()),
                (case["err"]["status"].clone(), case["err"]["code"].clone()),
                "{label}"
            );
            assert_eq!(
                seen["file"], previous,
                "{label}: a refusal wrote the choice"
            );
        }
        previous = seen["file"].clone();
    }
}

#[test]
fn packaged_action_root_fast_mode_persists_and_refreshes() {
    let Some((root, seen)) = in_child(
        "packaged_action_root_fast_mode_persists_and_refreshes",
        &[],
        |root| {
            let (host, _) = compose(Arc::new(MustNotRun), root);
            let before = host.fast_mode_default();
            // fast_mode.choose's shape check: exactly `enabled`, a real bool.
            let malformed: Vec<Value> = [
                json!({"enabled": 1}),
                json!({"enabled": "true"}),
                json!({}),
                json!({"enabled": true, "x": 1}),
                json!([true]),
                json!(true),
            ]
            .into_iter()
            .map(|body| json!(refusal(&post(&host, "/api/fast-mode", body))))
            .collect();
            let refused = host.fast_mode_default();
            let written_by_refusals = entries(root);
            let saved = post(&host, "/api/fast-mode", json!({"enabled": true}));
            json!({"before": default_json(before), "malformed": malformed,
                   "refused": default_json(refused), "written_by_refusals": written_by_refusals,
                   "saved": [saved.0, saved.1], "after": default_json(host.fast_mode_default())})
        },
    ) else {
        return;
    };
    let invalid = json!([400, "invalid_request"]);
    assert_eq!(seen["malformed"], json!(vec![invalid; 6]));
    assert_eq!(seen["written_by_refusals"], json!([]));
    assert_eq!(seen["refused"], seen["before"]);
    assert_eq!(
        seen["saved"],
        json!([200, {"enabled": true, "source": "saved", "locked": false}])
    );
    // `_atomic_private_json`: canonical JSON, 0600 (authority.py:147-170).
    assert_eq!(
        private_file(&root.join(api::preferences::fast_mode::FILE)),
        json!({"text": r#"{"enabled":true,"protocol":"mycelium.a13_fast_mode.v1"}"#, "mode": "0o600"})
    );
    // Bound once at launch (fast_mode.py:84), refreshed exactly once by the saved change.
    assert_eq!(
        seen["before"],
        json!({"value": {"ok": "off"}, "refreshes": 1})
    );
    assert_eq!(
        seen["after"],
        json!({"value": {"ok": "on"}, "refreshes": 2})
    );
}

#[test]
fn packaged_action_root_fast_mode_locked_by_environment() {
    let Some((root, seen)) = in_child(
        "packaged_action_root_fast_mode_locked_by_environment",
        &[(api::persistent::TOKEN_PATH_ENV, "on")],
        |root| {
            let (host, _) = compose(Arc::new(MustNotRun), root);
            let before = host.fast_mode_default();
            let replies: Vec<Value> = [true, false]
                .into_iter()
                .map(|enabled| {
                    json!(refusal(&post(
                        &host,
                        "/api/fast-mode",
                        json!({"enabled": enabled})
                    )))
                })
                .collect();
            json!({"before": default_json(before), "replies": replies,
                   "after": default_json(host.fast_mode_default())})
        },
    ) else {
        return;
    };
    // preferences.rs `choose_then_json`: the environment lock is a DemoError (400).
    let locked = json!([400, "fast_mode_locked_by_environment"]);
    assert_eq!(seen["replies"], json!([locked.clone(), locked]));
    assert_eq!(entries(&root), Vec::<String>::new(), "nothing is written");
    // The operator override wins at bind (fast_mode.py:24-28) and no refresh followed.
    assert_eq!(
        seen["before"],
        json!({"value": {"ok": "on"}, "refreshes": 1})
    );
    assert_eq!(seen["after"], seen["before"]);
}

#[test]
fn packaged_action_root_stop_and_rebuild_reach_node_service() {
    let root = private_root();
    let (host, node) = compose(Arc::new(MustNotRun), &root);
    assert_eq!(
        refusal(&post(&host, "/api/demo/stop", json!({"x": 1}))),
        (400, "invalid_request")
    );
    assert_eq!(
        post(&host, "/api/demo/stop", json!({})),
        (200, json!({"stopped": true}))
    );
    // The packaged probe's Stop (packaged/probe.js).
    assert_eq!(
        post(&host, "/api/serving/stop", json!({})),
        (200, json!({"stopped": true}))
    );
    assert_eq!(
        post(&host, "/api/demo/rebuild", json!({})),
        (200, json!({"generation": 2}))
    );
    // The same node service: its generation moved and its published status followed.
    assert_eq!(node.generation().unwrap(), 2);
    let status = send(&host, "GET", "/api/status", None);
    assert_eq!(status.0, 200);
    assert_eq!(status.1["owner_run"]["publisher_generation"], 2);
    assert_eq!(entries(&root), Vec::<String>::new());
    // Stop and Rebuild pass the packaged root's own gate first: once the node service is closed
    // they are v0.1.4's DemoError (app.py:1343-1345), refused before the node is asked.
    node.request_shutdown().unwrap();
    for path in ["/api/demo/stop", "/api/demo/rebuild"] {
        assert_eq!(
            refusal(&post(&host, path, json!({}))),
            (400, "application_closed"),
            "{path}"
        );
    }
    assert_eq!(node.generation().unwrap(), 2);
}

#[test]
fn packaged_action_root_unported_host_action_is_host_refusal() {
    let root = private_root();
    let (host, _) = compose(Arc::new(MustNotRun), &root);
    let refused = json!({"error": "action_not_completed"});
    // (path, body, status, answer): each unported seam answers its `persistent::Host` default.
    // `/api/create`, `/api/pair/request` and `/api/start` are the owner authority's now
    // (`api::app_host`); their answers are pinned in `tests/packaged_owner_create.rs`. Joining
    // (`/api/join/request`, `/api/join/window`) is ported too (lane PH3a: `adopt_target_role`,
    // the join seams); its answers and refusals are pinned in `tests/packaged_join.rs`.
    let cases = [
        ("/api/demo/start", json!({}), 409, refused.clone()),
        ("/api/model/cancel", json!({}), 409, refused.clone()),
        (
            "/api/local-contributor/start",
            json!({}),
            409,
            refused.clone(),
        ),
        (
            "/api/serving/status",
            json!({}),
            200,
            json!({"compilation": null, "serving": null, "route_ready": false}),
        ),
        (
            "/api/nope",
            json!({}),
            400,
            json!({"error": "unknown_action"}),
        ),
    ];
    for (path, body, status, answer) in cases {
        assert_eq!(post(&host, path, body), (status, answer), "{path}");
    }
    // The handler recorded why the last 409 happened (app.py:2008-2031).
    let record: Value = serde_json::from_slice(
        &std::fs::read(root.join("last-action-error.private.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        (&record["path"], &record["public_code"]),
        (
            &json!("/api/local-contributor/start"),
            &json!("action_not_completed")
        )
    );
    // `/api/serving/status` is v0.1.4's advanced serving block (app.py:1377-1399 @ f4e366e9),
    // not the node service's health: before any chain compile the real packaged process
    // answered exactly these fields (`no_model_no_execution_proposal`).
    let capture: Value = serde_json::from_str(PACKAGED_BOUNDARIES).unwrap();
    assert_eq!(
        capture["capture_kind"],
        "real_packaged_process_loopback_http"
    );
    let observed = capture["observations"]
        .as_array()
        .unwrap()
        .iter()
        .find(|o| o["id"] == "no_model_no_execution_proposal")
        .unwrap();
    assert_eq!(
        (&observed["method"], &observed["path"]),
        (&json!("POST"), &json!("/api/serving/status"))
    );
    let (status, answer) = post(&host, "/api/serving/status", json!({}));
    assert_eq!(json!(status), observed["status"]);
    for (pointer, value) in observed["fields"].as_object().unwrap() {
        assert_eq!(answer.pointer(pointer), Some(value), "{pointer}: {answer}");
    }
    // `/api/join/request` is ported (lane PH3a): a fresh owner becomes a target and answers its
    // signed pairing request (`_become_target` + `authority.pair_request()`, app.py:1580-1584).
    let consent = json!({
        "peer_visibility": true, "power": "ac_only",
        "storage_bytes": api::model_catalog::storage_consent_ceiling(),
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    });
    let (status, request) = post(&host, "/api/join/request", json!({"consent": consent}));
    assert_eq!(
        (status, &request["protocol"]),
        (200, &json!("mycelium.pairing_request.v1")),
        "{request}"
    );
}

/// The host's run and close facts are the node service's, not constants.
#[test]
fn packaged_action_root_preferences_follow_the_node_service_run_and_close() {
    let Some((root, seen)) = in_child(
        "packaged_action_root_preferences_follow_the_node_service_run_and_close",
        &[],
        |root| {
            let release: Release = Arc::default();
            let (host, node) = compose(Arc::new(Hold(release.clone())), root);
            let sent = post(
                &host,
                "/chat/api/send",
                json!({"prompt": "hello", "max_new_tokens": 1}),
            );
            let fast = post(&host, "/api/fast-mode", json!({"enabled": true}));
            let model = post(&host, "/api/run-model", json!({"choice": "qwen3-0.6b"}));
            let written_while_active = entries(root);
            *release.0.lock().unwrap() = true;
            release.1.notify_all();
            let deadline = Instant::now() + millis(B::WorkerStartup);
            let idle = loop {
                let reply = post(&host, "/api/fast-mode", json!({"enabled": true}));
                if reply.0 != 400 || Instant::now() >= deadline {
                    break reply;
                }
                std::thread::sleep(millis(B::RegistryPoll));
            };
            // Behind the window's gate: the node service's own shutdown fence.
            node.request_shutdown().unwrap();
            let closed = post(&host, "/api/fast-mode", json!({"enabled": false}));
            json!({"sent": sent.0, "fast": refusal(&fast), "model": refusal(&model),
                   "written_while_active": written_while_active, "idle": [idle.0, idle.1],
                   "closed": refusal(&closed)})
        },
    ) else {
        return;
    };
    assert_eq!(seen["sent"], 200);
    assert_eq!(seen["fast"], json!([400, "fast_mode_run_active"]));
    assert_eq!(seen["model"], json!([400, "owner_run_active"]));
    assert_eq!(seen["written_while_active"], json!([]));
    assert_eq!(
        seen["idle"],
        json!([200, {"enabled": true, "source": "saved", "locked": false}])
    );
    assert_eq!(seen["closed"], json!([400, "application_closed"]));
    assert_eq!(
        private_file(&root.join(api::preferences::fast_mode::FILE))["text"],
        r#"{"enabled":true,"protocol":"mycelium.a13_fast_mode.v1"}"#
    );
}
