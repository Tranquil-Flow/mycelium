// SPDX-License-Identifier: AGPL-3.0-only
//! Pending join windows, the `/api/network/start` gates and origin refusal, against the
//! EXECUTED oracle (`L10-evidence/r9-probe-windows.py` -> `fixtures/oracle_windows.json`):
//! `DemoApp.action` -> `_owner_window_action` / `_open_pending_window` / `_start_public_relay`
//! with the real `TemporaryHttpsRelay` constructor and a sentinel instead of `start`.
use api::{
    actions::{Action, Backend, Control},
    desktop::Service,
    persistent::{Host, PersistentBackend, RelayState},
    schema::PublicError,
    windows::{PendingWindow, PendingWindows},
    wire::{Json, Text},
};
use serde_json::{Map, Value, json};
use std::sync::{Arc, Mutex};

#[derive(Default)]
struct State {
    target: bool,
    closed: bool,
    swarm: bool,
    coordinator: bool,
    relay: Option<(String, Option<String>)>,
    live: Vec<String>,
    contributor: bool,
    configured: Option<String>,
    counter: u32,
    store_log: Vec<Value>,
    store_opened: bool,
    relay_closed: bool,
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
        None
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
    fn swarm_created(&self) -> bool {
        self.with(|s| s.swarm)
    }
    fn coordinator_ready(&self) -> bool {
        self.with(|s| s.coordinator)
    }
    fn relay_state(&self) -> RelayState {
        self.with(
            |s| match s.relay.as_ref().map(|(state, _)| state.as_str()) {
                None => RelayState::Absent,
                Some("stopped") => RelayState::Stopped,
                Some(_) => RelayState::Running,
            },
        )
    }
    fn close_stopped_relay(&mut self) {
        self.with(|s| {
            s.relay = None;
            s.relay_closed = true;
        });
    }
    fn relay_origin(&self) -> Option<String> {
        self.with(|s| s.relay.as_ref().and_then(|(_, o)| o.clone()))
            .filter(|o| !o.is_empty())
    }
    fn live_pairing_windows(&self) -> usize {
        self.with(|s| {
            s.live
                .iter()
                .filter(|w| matches!(w.as_str(), "open" | "claimed" | "approved"))
                .count()
        })
    }
    fn local_contributor_running(&self) -> bool {
        self.with(|s| s.contributor)
    }
    fn configured_public_origin(&self) -> Option<String> {
        self.with(|s| s.configured.clone())
    }
    fn random_hex(&mut self, bytes: usize) -> Result<String, PublicError> {
        Ok(self.with(|s| {
            s.counter += 1;
            format!("{:06x}", s.counter)[..2 * bytes].to_owned()
        }))
    }
    fn owner_window(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        self.with(|s| match action {
            Action::PairWindowList => Ok(api::jobj! {"windows" => Json::Arr(
            s.live.iter().enumerate().map(|(i, state)| api::jobj! {
                "window_id" => format!("W{i}"), "state" => state.as_str()}).collect())}),
            Action::PairWindowRevoke => {
                s.store_log.push(json!(["revoke", body["window_id"]]));
                Ok(api::jobj! {"window_id" => &body["window_id"], "state" => "revoked"})
            }
            Action::PairWindowApprove => {
                s.store_log.push(json!([
                    "approve",
                    body["window_id"],
                    body["request_digest"]
                ]));
                Ok(Json::from(json!({"approved": body["window_id"]})))
            }
            _ => {
                s.store_opened = true;
                Ok(Json::from(json!({"opened": true})))
            }
        })
    }
}

/// Everything the persistent layer must not serve answers here; `NetworkStart` is the one
/// action it delegates after its own gates and validation (the relay start itself).
#[derive(Clone, Default)]
struct Inner(Arc<Mutex<Vec<Value>>>);
impl Backend for Inner {
    fn prepare(&mut self, _: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        Ok(())
    }
    fn execute(&mut self, action: Action, body: &Map<String, Value>) -> Result<Value, PublicError> {
        if action == Action::NetworkStart {
            self.0.lock().unwrap().push(Value::Object(body.clone()));
            return Err(PublicError::new(409, "action_not_completed"));
        }
        Err(PublicError::new(404, "inner_must_not_serve_this"))
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
}
fn oracle() -> Value {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_windows.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    fixture
}
/// The two cases whose oracle answer is an artifact of the probe's stand-in coordinator
/// (it has no `seed_url`): with a relay origin the real window store is the host's.
const STORE_CASES: [&str; 2] = ["open_relay_running", "open_relay_stopped_origin"];

#[test]
fn every_oracle_case_matches_status_pending_list_and_host_calls() {
    let oracle = oracle();
    let cases = oracle["cases"].as_array().unwrap();
    assert!(cases.len() >= 99, "{} cases", cases.len());
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = std::env::temp_dir().join(format!("l10-windows-{}-{label}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let host = RecHost::default();
        let opts = &case["opts"];
        host.with(|s| {
            s.target = opts["role"] == "target";
            s.closed = opts["closed"] == true;
            s.swarm = opts.get("swarm").is_none_or(|v| v == true);
            s.coordinator = opts.get("coordinator").is_none_or(|v| v == true);
            s.live = opts["live"]
                .as_array()
                .map(|l| l.iter().map(|v| v.as_str().unwrap().to_owned()).collect())
                .unwrap_or_default();
            s.contributor = opts["contributor"] == true;
            s.configured = opts["configured"]["public_origin"]
                .as_str()
                .map(str::to_owned);
            s.relay = case["relay"].as_array().map(|r| {
                (
                    r[0].as_str().unwrap().to_owned(),
                    r.get(1).and_then(Value::as_str).map(str::to_owned),
                )
            });
        });
        let inner = Inner::default();
        let backend = PersistentBackend::new(dir.clone(), host.clone(), inner.clone());
        let pending: PendingWindows = backend.pending_windows();
        for item in opts["pending"].as_array().into_iter().flatten() {
            pending.push(PendingWindow {
                pending_id: item["pending_id"].as_str().unwrap().to_owned(),
                ttl_seconds: u32::try_from(item["ttl_seconds"].as_u64().unwrap()).unwrap(),
            });
        }
        let control = Control::new(backend);
        let path = case["path"].as_str().unwrap();
        let body = case["body"].as_object().unwrap();
        let got = control.action_json(path, body);
        let mut problems = Vec::new();
        let reached_start = !inner.0.lock().unwrap().is_empty();
        if STORE_CASES.contains(&label) {
            if !host.with(|s| s.store_opened) {
                problems.push("the host's window store was not asked".to_owned());
            }
        } else {
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
                (other, _) => problems.push(format!("outcome {other:?} vs oracle {}", case["err"])),
            }
            if reached_start != (case["reached_start"] == true) {
                problems.push(format!("reached_start {reached_start}"));
            }
            if case["windows_log"] != Value::Array(host.with(|s| s.store_log.clone())) {
                problems.push(format!(
                    "store log {:?}",
                    host.with(|s| s.store_log.clone())
                ));
            }
        }
        let list: Value = pending.json().to_value();
        if list != case["pending_after"] {
            problems.push(format!("pending {list} != {}", case["pending_after"]));
        }
        if host.with(|s| s.relay_closed)
            != (case["relay_cleared"] == true && case["relay"].is_array())
        {
            problems.push(format!("relay cleared {}", host.with(|s| s.relay_closed)));
        }
        if !problems.is_empty() {
            failures.push(format!("{label}: {}", problems.join(" | ")));
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.len(),
        failures
            .iter()
            .take(14)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}

#[test]
fn pending_window_json_keeps_the_source_key_order() {
    let pending = PendingWindows::default();
    pending.push(PendingWindow {
        pending_id: "pending-00000a".into(),
        ttl_seconds: 30,
    });
    assert_eq!(
        String::from_utf8(pending.json().dumps(Text::Ascii)).unwrap(),
        r#"[{"pending_id":"pending-00000a","ttl_seconds":30,"state":"waiting_for_relay","note":"ready to share once the relay starts"}]"#
    );
}
