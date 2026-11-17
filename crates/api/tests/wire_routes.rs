// SPDX-License-Identifier: AGPL-3.0-only
//! Integrator ruling (a), route level: the bytes of every ported desktop action response equal
//! the bytes v0.1.4's handler sends (`json.dumps(value, separators=(',',':'))`, insertion key
//! order, `\uXXXX` escapes). Expected bytes are produced by EXECUTING the oracle's own
//! functions and serialising their result objects as `send_value` does
//! (`L10-evidence/r8-probe-wire-routes.py` -> `fixtures/oracle_wire_routes.json`).
#[path = "support/oracle_context.rs"]
mod oracle_context;
use api::{
    actions::{Action, Backend, Control},
    desktop::Service,
    persistent::{Host, PersistentBackend},
    preferences::{fast_mode, run_model, standing_consent as sc},
    schema::PublicError,
    wire::{Json, Text},
};
use oracle_context::{at_product_context_bytes, at_product_picker_bytes};
use serde_json::{Map, Value, json};
use std::{fs, path::PathBuf};

#[derive(Clone, Default)]
struct FakeHost {
    memory: Option<u64>,
    env: Option<String>,
}
impl Host for FakeHost {
    fn closed(&self) -> bool {
        false
    }
    fn owner(&self) -> bool {
        true
    }
    fn environment(&self, name: &str) -> Option<String> {
        (name == run_model::ENV).then(|| self.env.clone()).flatten()
    }
    fn device_memory_bytes(&self) -> Option<u64> {
        self.memory
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
}
struct Inner;
impl Backend for Inner {
    fn prepare(&mut self, _: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        Ok(())
    }
    fn execute(&mut self, _: Action, _: &Map<String, Value>) -> Result<Value, PublicError> {
        Ok(json!({"inner": true}))
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
}
fn routes() -> Value {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_wire_routes.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    fixture["routes"].clone()
}
fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("l10-wire-{tag}-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::create_dir_all(&dir).unwrap();
    dir
}
fn body(v: &Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}
fn bytes(j: &Json) -> String {
    String::from_utf8(j.dumps(Text::Ascii)).unwrap()
}
fn app(dir: &std::path::Path, host: FakeHost) -> Control<PersistentBackend<FakeHost, Inner>> {
    Control::new(PersistentBackend::new(dir.to_path_buf(), host, Inner))
}

#[test]
fn run_model_documents_are_byte_identical_over_state_env_memory() {
    let routes = routes();
    let want = routes["run_model_document"].as_object().unwrap();
    assert_eq!(want.len(), 60);
    let memory = |label: &str| match label {
        "none" => None,
        "16gib" => Some(16u64 << 30),
        other => panic!("{other}"),
    };
    for (key, expected) in want {
        let parts: Vec<&str> = key.split('|').collect();
        let dir = root("doc");
        if parts[0] != "None" {
            run_model::save_choice(&dir, parts[0]).unwrap();
        }
        let env = (parts[1] != "None").then_some(parts[1]);
        let got = run_model::document_json(&dir, env, memory(parts[2])).unwrap();
        // v0.2's starter and picker texts (B13), then the serving context: the oracle side of
        // each is checked by the transform before it moves.
        let expected =
            at_product_context_bytes(&at_product_picker_bytes(expected.as_str().unwrap()));
        assert_eq!(bytes(&got), expected, "{key}");
        fs::remove_dir_all(dir).unwrap();
    }
}
#[test]
fn run_model_choose_bodies_are_byte_identical() {
    let routes = routes();
    let oracle: Value =
        serde_json::from_str(include_str!("fixtures/oracle_run_model.json")).unwrap();
    for case in oracle["choose_cases"].as_array().unwrap() {
        let label = case["label"].as_str().unwrap();
        let Some(expected) = routes["run_model_choose"].get(label) else {
            continue;
        };
        let dir = root("choose");
        if let Some(pre) = case["pre"].as_str() {
            run_model::save_choice(&dir, pre).unwrap();
        }
        let host = FakeHost {
            memory: case["device_memory_bytes"].as_u64(),
            env: case["env"].as_str().map(str::to_owned),
        };
        let got = app(&dir, host)
            .action_json("/api/run-model", &body(&case["body"]))
            .unwrap();
        let expected =
            at_product_context_bytes(&at_product_picker_bytes(expected.as_str().unwrap()));
        assert_eq!(bytes(&got), expected, "{label}");
        fs::remove_dir_all(dir).unwrap();
    }
    assert_eq!(routes["run_model_choose"].as_object().unwrap().len(), 5);
}
#[test]
fn fast_mode_bodies_are_byte_identical() {
    let routes = routes();
    for (label, enabled) in [("choose_true", true), ("choose_false", false)] {
        let dir = root("fast");
        let got = app(&dir, FakeHost::default())
            .action_json("/api/fast-mode", &body(&json!({"enabled": enabled})))
            .unwrap();
        assert_eq!(
            bytes(&got),
            routes["fast_mode"][label].as_str().unwrap(),
            "{label}"
        );
        fs::remove_dir_all(dir).unwrap();
    }
    let dir = root("fast-default");
    assert_eq!(
        bytes(&fast_mode::document_json(&dir, None).unwrap()),
        routes["fast_mode"]["default"].as_str().unwrap()
    );
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn standing_withdraw_bodies_are_byte_identical() {
    let routes = routes();
    let oracle: Value = serde_json::from_str(include_str!("fixtures/oracle_consent.json")).unwrap();
    let policy = oracle["consent_valid"][0]["consent"].clone();
    let caps = json!({"storage_bytes":1_000_000_000,"max_memory_bytes":4u64*1024*1024*1024,"max_wall_seconds":900,"max_commands":50});
    let grant = json!({"acknowledged":true,"disclosure_digest":sc::DISCLOSURE_DIGEST,"caps":caps});
    let mut seen = 0;
    for case in oracle["withdraw_cases"].as_array().unwrap() {
        let label = case["label"].as_str().unwrap();
        let Some(expected) = routes["withdraw"].get(label) else {
            continue;
        };
        let dir = root("withdraw");
        if let Some(pre) = case["pre"].as_str() {
            sc::grant(&dir, &grant, &policy, 1_761_600_000.5, None).unwrap();
            if pre == "withdrawn" {
                sc::withdraw(&dir, 1_761_600_001.0).unwrap();
            }
        }
        let got = app(&dir, FakeHost::default())
            .action_json("/api/consent/standing/withdraw", &body(&case["body"]))
            .unwrap();
        let expected = at_product_context_bytes(expected.as_str().unwrap());
        assert_eq!(bytes(&got), expected, "{label}");
        seen += 1;
        fs::remove_dir_all(dir).unwrap();
    }
    assert_eq!(seen, 4);
}
#[test]
fn pair_preview_projection_keeps_the_source_key_order() {
    use api::pairing::preview_json;
    // Key order of app.py:1676 `{key: value[key] for key in (node_id, identity_key_digest,
    // peer_class, expires_at)}` - a request that fails the signature check proves nothing, so
    // the projection itself is exercised through the public builder.
    let routes = routes();
    let want = routes["pair_preview"]["body"].as_str().unwrap();
    let input = &routes["pair_preview"]["input"];
    let projected = api::pairing::project_preview(input);
    assert_eq!(bytes(&projected), want);
    let _ = preview_json; // the full signed path is covered in tests/pairing.rs
}
#[test]
fn ordered_responses_keep_insertion_order_through_the_signed_wrapper() {
    use api::pairing::SignedBackend;
    let dir = root("signed");
    let backend = SignedBackend::new(
        PersistentBackend::new(dir.clone(), FakeHost::default(), Inner),
        || 1000.0,
    );
    let control = Control::new(backend);
    let got = control
        .action_json("/api/fast-mode", &body(&json!({"enabled": true})))
        .unwrap();
    assert_eq!(got.keys(), ["enabled", "source", "locked"]);
    fs::remove_dir_all(dir).unwrap();
}

mod http {
    use super::*;
    use api::{
        desktop::{Desktop, Session},
        http::Request,
        schema::{STATUS_FIELDS, Snapshot},
        status::PublishedStatus,
    };
    use std::{sync::Arc, time::Instant};

    fn desktop(
        dir: &std::path::Path,
        status: Arc<PublishedStatus>,
    ) -> Desktop<Control<PersistentBackend<FakeHost, Inner>>> {
        Desktop::new(
            "127.0.0.1:12345".into(),
            Arc::new(app(
                dir,
                FakeHost {
                    memory: Some(16 << 30),
                    env: None,
                },
            )),
            status,
            Session::new(
                "launch-canary".into(),
                "session-canary".into(),
                Instant::now() + journey_tests::duration(budgets::BudgetId::WorkerStartup).unwrap(),
                journey_tests::duration(budgets::BudgetId::WorkerStartup).unwrap(),
            )
            .unwrap(),
        )
    }
    fn request(method: &str, path: &str, body: &[u8]) -> Request {
        Request {
            method: method.into(),
            target: path.into(),
            headers: vec![
                ("host".into(), "127.0.0.1:12345".into()),
                ("cookie".into(), "a13session=session-canary".into()),
                ("origin".into(), "http://127.0.0.1:12345".into()),
                ("x-mycelium-action".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
                ("content-length".into(), body.len().to_string()),
            ],
            body: body.into(),
        }
    }
    fn sign_in(d: &Desktop<Control<PersistentBackend<FakeHost, Inner>>>) {
        let mut r = request("POST", "/session", b"launch=launch-canary");
        r.headers.retain(|(k, _)| k != "content-type");
        r.headers.push((
            "content-type".into(),
            "application/x-www-form-urlencoded".into(),
        ));
        assert_eq!(d.handle(r).unwrap().status, 303);
    }
    #[test]
    fn run_model_post_over_the_desktop_is_the_released_app_bytes() {
        let routes = routes();
        let dir = root("http-run-model");
        let d = desktop(&dir, Arc::new(PublishedStatus::new()));
        sign_in(&d);
        let reply = d
            .handle(request(
                "POST",
                "/api/run-model",
                br#"{"choice":"qwen3-8b"}"#,
            ))
            .unwrap();
        assert_eq!(reply.status, 200);
        let want = routes["run_model_choose"]["save_qwen3_8b"]
            .as_str()
            .unwrap();
        assert!(
            want.contains("\\u2014"),
            "the picker labels carry an em dash"
        );
        assert_eq!(
            String::from_utf8(reply.body).unwrap(),
            super::at_product_context_bytes(&super::at_product_picker_bytes(want))
        );
        fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn refusals_and_status_follow_the_same_encoding() {
        let routes = routes();
        let dir = root("http-status");
        let status = Arc::new(PublishedStatus::new());
        let d = desktop(&dir, status.clone());
        sign_in(&d);
        let reply = d
            .handle(request("POST", "/api/run-model", br#"{"choice":"gpt"}"#))
            .unwrap();
        assert_eq!(reply.status, 400);
        assert_eq!(reply.body, br#"{"error":"run_model_choice_invalid"}"#);
        let owner_only = d
            .handle(request("POST", "/api/create", br#"{"consent":1}"#))
            .unwrap();
        assert_eq!(
            String::from_utf8(owner_only.body).unwrap(),
            r#"{"error":"consent_required"}"#
        );
        let _ = &routes;
        // Host-ordered status: top level in the order given, `status_snapshot` appended last,
        // non-ASCII escaped.
        let mut fields: Vec<(String, Json)> = STATUS_FIELDS
            .iter()
            .rev()
            .map(|k| ((*k).to_owned(), Json::Null))
            .collect();
        for (key, value) in &mut fields {
            match key.as_str() {
                "installation_role" => *value = Json::str("owner"),
                "running" | "test_seams_active" => *value = Json::Bool(false),
                "label" => *value = Json::str("Mycelium \u{2014} demo"),
                _ => {}
            }
        }
        status
            .publish(1, Snapshot::parse_json(Json::Obj(fields)).unwrap())
            .unwrap();
        let reply = d.handle(request("GET", "/api/status", b"")).unwrap();
        let text = String::from_utf8(reply.body).unwrap();
        assert!(
            text.contains("\"label\":\"Mycelium \\u2014 demo\""),
            "{text}"
        );
        let reversed: Vec<&str> = STATUS_FIELDS.iter().rev().copied().collect();
        let positions: Vec<usize> = reversed
            .iter()
            .map(|k| text.find(&format!("\"{k}\":")).unwrap())
            .collect();
        assert!(
            positions.windows(2).all(|w| w[0] < w[1]),
            "host order kept: {text}"
        );
        assert!(
            text.ends_with("\"status_snapshot\":{\"age_seconds\":0.0,\"refresh_failed\":false}}")
                || text.contains("\"status_snapshot\":{\"age_seconds\":"),
            "{text}"
        );
        let tail = text.rsplit_once("\"status_snapshot\"").unwrap().1;
        assert!(
            tail.starts_with(":{\"age_seconds\":") && tail.ends_with("\"refresh_failed\":false}}"),
            "{text}"
        );
        fs::remove_dir_all(dir).unwrap();
    }
}
