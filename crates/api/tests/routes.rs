// SPDX-License-Identifier: AGPL-3.0-only
//! Round-8 routes against the EXECUTED oracle (`L10-evidence/r8-probe-routes.py` ->
//! `fixtures/oracle_routes.json`): `DemoApp.action` / `_action` / `_become_target` /
//! `_target_join_action` run on a recording stand-in for `/api/join/{request,window,
//! window/poll,window/cancel}`, `/api/pair/{request,approve,accept,window/*}` + the owner role
//! gate, `/api/serving/status`, and the handler's `last-action-error.private.json`.
use api::{
    actions::{Action, Backend, Control},
    desktop::Service,
    persistent::{Host, JoinLink, PersistentBackend},
    preferences::dumps_sorted,
    schema::PublicError,
    wire::{Json, Text},
};
use serde_json::{Map, Value, json};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

#[derive(Default)]
struct State {
    target: bool,
    closed: bool,
    running: bool,
    swarm_created: bool,
    membership: Option<String>,
    memory: Option<u64>,
    log: Vec<Value>,
    standing_error: Option<String>,
    adaptive: Vec<bool>,
    script: Value,
    request: Value,
    accepted: Value,
    binding: Value,
    seed: Option<String>,
    generation: Value,
}
#[derive(Clone, Default)]
struct RecHost(Arc<Mutex<State>>);
impl RecHost {
    fn with<R>(&self, f: impl FnOnce(&mut State) -> R) -> R {
        f(&mut self.0.lock().unwrap())
    }
}
impl Host for RecHost {
    fn closed(&self) -> bool {
        self.with(|s| s.closed)
    }
    fn owner(&self) -> bool {
        self.with(|s| !s.target)
    }
    fn environment(&self, _: &str) -> Option<String> {
        None
    }
    fn device_memory_bytes(&self) -> Option<u64> {
        self.with(|s| s.memory)
    }
    fn run_active(&self) -> bool {
        false
    }
    fn acquisition_active(&self) -> bool {
        false
    }
    fn owner_run_active(&self) -> bool {
        false
    }
    fn now_seconds(&self) -> f64 {
        1_761_700_000.125
    }
    fn swarm_created(&self) -> bool {
        self.with(|s| s.swarm_created)
    }
    fn membership_state(&self) -> Option<String> {
        self.with(|s| s.membership.clone())
    }
    fn running(&self) -> bool {
        self.with(|s| s.running)
    }
    fn stop_authority(&mut self) {
        self.with(|s| {
            s.running = false;
            s.log.push(json!("authority_stop"));
        });
    }
    fn adopt_target_role(&mut self) -> Result<(), PublicError> {
        self.with(|s| {
            s.target = true;
            s.log.push(json!("target_application"));
        });
        Ok(())
    }
    fn start_authority(&mut self) -> Result<(), PublicError> {
        self.with(|s| {
            s.running = true;
            s.log.push(json!("authority_start"));
        });
        Ok(())
    }
    fn reopen_acquisition_if_needed(&mut self) {
        self.with(|s| s.log.push(json!("open_acquisition")));
    }
    fn pair_request(&mut self) -> Result<Json, PublicError> {
        self.with(|s| {
            s.log.push(json!("pair_request"));
            Ok(if s.request.is_object() {
                Json::from(&s.request)
            } else {
                Json::object([("request", Json::str("pr"))])
            })
        })
    }
    fn join_submit(
        &mut self,
        link: &JoinLink,
        _request: &Value,
        quota: &Value,
    ) -> Result<(Value, Vec<Value>), PublicError> {
        self.with(|s| {
            s.log.push(json!(["client", link.origin]));
            s.log.push(json!(["measure", "join"]));
            s.log
                .push(json!(["submit", link.window_id, link.code, quota]));
            Ok((s.script["submit"].clone(), Vec::new()))
        })
    }
    fn join_poll(&mut self, state: &Map<String, Value>) -> Result<Value, PublicError> {
        self.with(|s| {
            s.log.push(json!(["client", state["origin"]]));
            s.log
                .push(json!(["poll", state["window_id"], state["code"]]));
            match &s.script["poll"] {
                Value::String(code) => Err(PublicError::new(
                    409,
                    Box::leak(code.clone().into_boxed_str()),
                )),
                other => Ok(other.clone()),
            }
        })
    }
    fn join_accept(
        &mut self,
        handoff: &Value,
        pin: Option<&str>,
    ) -> Result<api::persistent::JoinAccepted, PublicError> {
        self.with(|s| {
            if pin.is_some_and(|p| {
                Some(p)
                    != s.seed.as_deref().or(Some(
                        "sha256:abababababababababababababababababababababababababababababababab",
                    ))
            }) {
                return Err(PublicError::new(400, "join_seed_pin_mismatch"));
            }
            s.log.push(json!(["accept", handoff]));
            s.log.push(json!(["auto_report", "join"]));
            s.running = true;
            let membership = if s.accepted.is_object() {
                s.accepted
                    .get("membership_state")
                    .cloned()
                    .unwrap_or(json!("current"))
            } else {
                json!("current")
            };
            Ok(api::persistent::JoinAccepted {
                membership_state: membership,
                generation: s.generation.clone(),
                binding: s.binding.as_object().map(|b| Value::Object(b.clone())),
                report: json!({"auto": "join"}),
            })
        })
    }
    fn apply_first_consent_choice(&mut self, enabled: bool) {
        self.with(|s| s.adaptive.push(enabled));
    }
    fn quit(&mut self) -> Result<Json, PublicError> {
        self.with(|s| s.log.push(json!("quit")));
        Ok(
            api::jobj! {"stopping" => true, "identity_retained" => true, "evidence_retained" => true},
        )
    }
    fn standing_bind_failed(&mut self, code: &'static str) {
        self.with(|s| s.standing_error = Some(code.to_owned()));
    }
    fn pair_approve(
        &mut self,
        request: &Value,
        ttl: &Value,
        quota: &Value,
    ) -> Result<Json, PublicError> {
        self.with(|s| s.log.push(json!(["approve", request, ttl, quota])));
        Ok(Json::object([("handoff", Json::str("h"))]))
    }
    fn pair_accept(&mut self, handoff: &Value) -> Result<Json, PublicError> {
        self.with(|s| s.log.push(json!(["accept", handoff])));
        Ok(Json::object([("accepted", Json::Bool(true))]))
    }
    fn parse_join_text(&self, text: &Value, _now: f64) -> Result<JoinLink, PublicError> {
        if text == "abc-def ABCD-EFGH-JKMN" {
            Ok(JoinLink {
                origin: "https://abc-def.trycloudflare.com".into(),
                window_id: "ABCD".into(),
                code: "EFGHJKMN".into(),
                seed_key_digest: None,
            })
        } else {
            Err(PublicError::new(409, "join_code_invalid"))
        }
    }
}
struct Inner;
impl Backend for Inner {
    fn prepare(&mut self, _: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        Ok(())
    }
    fn execute(&mut self, _: Action, _: &Map<String, Value>) -> Result<Value, PublicError> {
        Err(PublicError::new(404, "inner_must_not_serve_this"))
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
}
fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("l10-routes-{tag}-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::create_dir_all(&dir).unwrap();
    dir
}
fn body(v: &Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}
fn read(dir: &Path, name: &str) -> Option<String> {
    fs::read_to_string(dir.join(name)).ok()
}
fn oracle() -> Value {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/oracle_routes.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    fixture
}
const POLICY: &str = r#"{"background": false, "diagnostics": false, "network": "unmetered_only", "peer_visibility": true, "power": "ac_only", "storage_bytes": 1000000000, "thermal": "stop_on_serious"}"#;

#[test]
fn every_oracle_case_matches_status_files_log_and_bytes() {
    let oracle = oracle();
    let cases = oracle["cases"].as_array().unwrap();
    assert!(cases.len() >= 60);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = root(label);
        for (name, raw) in case["files"].as_object().unwrap() {
            let file = dir.join(name);
            fs::write(&file, raw.as_str().unwrap()).unwrap();
            fs::set_permissions(&file, fs::Permissions::from_mode(0o600)).unwrap();
        }
        if case["pre"] == "granted" {
            let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":1_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
            let caps = json!({"storage_bytes":1_000_000_000u64,"max_memory_bytes":4u64*1024*1024*1024,"max_wall_seconds":900,"max_commands":50});
            let grant = json!({"acknowledged":true,"disclosure_digest":api::preferences::standing_consent::DISCLOSURE_DIGEST,"caps":caps});
            api::preferences::standing_consent::grant(&dir, &grant, &policy, 1_761_600_000.5, None)
                .unwrap();
        }
        let host = RecHost::default();
        let opts = &case["opts"];
        host.with(|s| {
            s.target = opts["role"] == "target";
            s.closed = opts["closed"] == true;
            s.running = opts["running"] == true;
            s.memory = opts["memory"].as_u64();
            let status = &case["status"];
            s.swarm_created = status["swarm_created"] == true;
            s.membership = status["membership_state"].as_str().map(str::to_owned);
            s.script = case["script"].clone();
            s.request = case["request"].clone();
            s.accepted = case["accepted"].clone();
            s.binding = case["binding"].clone();
            s.seed = case["seed"].as_str().map(str::to_owned);
            s.generation = case["generation"].clone();
        });
        let control = Control::new(PersistentBackend::new(dir.clone(), host.clone(), Inner));
        let got = control.action_json(case["path"].as_str().unwrap(), &body(&case["body"]));
        let mut problems = Vec::new();
        match (&got, case.get("ok")) {
            (Ok(json), Some(_)) => {
                let bytes = String::from_utf8(json.dumps(Text::Ascii)).unwrap();
                if bytes != case["wire"].as_str().unwrap() {
                    problems.push(format!("bytes {bytes} != {}", case["wire"]));
                }
            }
            (Err(e), None) => {
                let want = &case["err"];
                if (u64::from(e.status), e.code)
                    != (
                        want["status"].as_u64().unwrap(),
                        want["code"].as_str().unwrap(),
                    )
                {
                    problems.push(format!("error {e} != {want}"));
                }
            }
            (other, _) => problems.push(format!("outcome {other:?} vs oracle {case}")),
        }
        let log = host.with(|s| s.log.clone());
        if Value::Array(log.clone()) != case["log"] {
            problems.push(format!("log {} != {}", Value::Array(log), case["log"]));
        }
        let role = if host.with(|s| s.target) {
            "target"
        } else {
            "owner"
        };
        if role != case["role_after"] {
            problems.push(format!("role {role}"));
        }
        if case["standing_error"].as_str() != host.with(|s| s.standing_error.clone()).as_deref() {
            problems.push(format!(
                "standing_error {:?}",
                host.with(|s| s.standing_error.clone())
            ));
        }
        for name in [
            "resource-policy.json",
            "installation-role.json",
            "join-window.json",
            "standing-consent.json",
        ] {
            let want = case[format!("file:{name}")].as_str().map(str::to_owned);
            if read(&dir, name) != want {
                problems.push(format!("file {name}: {:?} != {want:?}", read(&dir, name)));
            }
        }
        // `adaptive-host-policy.json` is the host's effect (adaptive_host); the api must call it.
        let wrote_policy = case["modes"].get("adaptive-host-policy.json").is_some();
        if wrote_policy == host.with(|s| s.adaptive.clone()).is_empty() {
            problems.push(format!(
                "adaptive effect {:?}",
                host.with(|s| s.adaptive.clone())
            ));
        }
        for (name, mode) in case["modes"].as_object().unwrap() {
            if name == "adaptive-host-policy.json" {
                continue;
            }
            let actual = fs::metadata(dir.join(name))
                .map(|m| format!("0o{:o}", m.permissions().mode() & 0o7777));
            if actual.as_deref().ok() != mode.as_str() {
                problems.push(format!("mode {name}: {actual:?} != {mode}"));
            }
        }
        // last-action-error.private.json exists exactly for the oracle's non-DemoError failures.
        match (
            case.get("last_action_error"),
            read(&dir, "last-action-error.private.json"),
        ) {
            (Some(want), Some(raw)) => {
                // The recorded identity is the oracle's own exception class and module (ruling
                // 2): the whole record, key order and bytes included, must be the oracle's.
                let record: Value = serde_json::from_str(&raw).unwrap();
                if &record != want {
                    problems.push(format!("last-action-error {record} != {want}"));
                }
                if raw != dumps_sorted(want) {
                    problems.push(format!("last-action-error bytes {raw}"));
                }
                if fs::metadata(dir.join("last-action-error.private.json"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o7777
                    != 0o600
                {
                    problems.push("last-action-error mode".into());
                }
            }
            (None, None) => {}
            (want, got) => problems.push(format!(
                "last-action-error presence: oracle {want:?} rust {got:?}"
            )),
        }
        if !problems.is_empty() {
            failures.push(format!("{label}: {}", problems.join(" | ")));
        }
        fs::remove_dir_all(dir).unwrap();
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.len(),
        failures
            .iter()
            .take(12)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
    let _ = POLICY;
}

#[test]
fn quit_is_closed_gated_shape_checked_and_answers_the_released_bytes() {
    let routes: Value =
        serde_json::from_str(include_str!("fixtures/oracle_wire_routes.json")).unwrap();
    let want = routes["routes"]["fixed"]["quit"].as_str().unwrap();
    let dir = root("quit");
    let host = RecHost::default();
    let control = Control::new(PersistentBackend::new(dir.clone(), host.clone(), Inner));
    // app.py:1681-1684: `if body: invalid_request`, after the closed check.
    let refused = control
        .action_json("/api/quit", &body(&json!({"x": 1})))
        .unwrap_err();
    assert_eq!((refused.status, refused.code), (400, "invalid_request"));
    host.with(|s| s.closed = true);
    let closed = control
        .action_json("/api/quit", &body(&json!({})))
        .unwrap_err();
    assert_eq!((closed.status, closed.code), (400, "application_closed"));
    host.with(|s| s.closed = false);
    let got = control.action_json("/api/quit", &body(&json!({}))).unwrap();
    assert_eq!(String::from_utf8(got.dumps(Text::Ascii)).unwrap(), want);
    assert_eq!(host.with(|s| s.log.clone()), vec![json!("quit")]);
    fs::remove_dir_all(dir).unwrap();
}
