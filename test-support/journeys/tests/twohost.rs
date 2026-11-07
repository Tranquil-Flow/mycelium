// SPDX-License-Identifier: AGPL-3.0-only
//! Two-host scenario data validation (static) and engine behaviour over a fake loopback server.
use budgets::BudgetId;
use journey_tests::duration;
use journey_tests::twohost::{
    EndpointInstance, Endpoints, Options, Runner, Scenarios, compare_golden, load_scenarios, plan,
    stabilize, subst, validate,
};
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    thread,
    time::Duration,
};

fn shipped() -> Scenarios {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/twohost-scenarios.json");
    load_scenarios(&path).expect("shipped scenarios parse")
}

#[test]
fn shipped_scenarios_have_no_validation_problems() {
    let problems = validate(&shipped(), None);
    assert!(problems.is_empty(), "{problems:#?}");
}

#[test]
fn shipped_executed_references_resolve_when_evidence_is_present() {
    // The probes live in the program evidence directory, not in this repository.
    let Some(dir) = std::env::var_os("L3C_EVIDENCE_DIR").map(PathBuf::from) else {
        return;
    };
    let problems = validate(&shipped(), Some(&dir));
    assert!(problems.is_empty(), "{problems:#?}");
}

#[test]
fn shipped_data_covers_every_window_claim_and_every_incident() {
    let data = shipped();
    let covered: BTreeSet<_> = data
        .scenarios
        .iter()
        .flat_map(|s| s.claims.iter().cloned())
        .collect();
    for claim in ["CL-L3-05", "CL-L3-06", "CL-L3-07", "CL-L3-08", "CL-L3-09"] {
        assert!(covered.contains(claim), "{claim}");
    }
    let incidents: BTreeSet<_> = data
        .scenarios
        .iter()
        .filter_map(|s| s.incident.clone())
        .collect();
    assert_eq!(
        incidents,
        ["F14", "F15", "F15b", "F3", "F5"]
            .map(String::from)
            .into_iter()
            .collect()
    );
    // Each incident has a RED session on a historical executable and the certified GREEN session.
    let reds: BTreeSet<_> = data
        .sessions
        .iter()
        .filter(|s| s.expect == "red")
        .map(|s| s.id.clone())
        .collect();
    assert_eq!(
        reds,
        ["red-f14", "red-f15", "red-f15b", "red-f3-f5"]
            .map(String::from)
            .into_iter()
            .collect()
    );
}

fn mutate(edit: impl FnOnce(&mut Value)) -> Vec<String> {
    let mut value = shipped_json();
    edit(&mut value);
    let data: Scenarios = serde_json::from_value(value).expect("mutated data still parses");
    validate(&data, None)
}
fn shipped_json() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/twohost-scenarios.json");
    serde_json::from_slice(&std::fs::read(path).expect("read")).expect("json")
}

fn first_step<'a>(value: &'a mut Value, scenario: &str) -> &'a mut Value {
    let list = value["scenarios"].as_array_mut().expect("scenarios");
    let s = list
        .iter_mut()
        .find(|s| s["id"] == scenario)
        .expect("scenario");
    &mut s["steps"][0]
}

#[test]
fn validator_rejects_missing_source() {
    let p = mutate(|v| first_step(v, "joined-bringup")["source"] = json!(" "));
    assert!(p.iter().any(|x| x.contains("missing_source")), "{p:?}");
}
#[test]
fn validator_rejects_unknown_op_who_and_method() {
    let p = mutate(|v| {
        let s = first_step(v, "joined-bringup");
        s["op"] = json!("teleport");
        s["who"] = json!("stranger");
        let r = first_step(v, "chat-sse-cancel");
        r["op"] = json!("http");
        r["method"] = json!("PUT");
    });
    for needle in ["unknown_op", "unknown_who", "bad_method"] {
        assert!(p.iter().any(|x| x.contains(needle)), "{needle} in {p:?}");
    }
}
#[test]
fn validator_rejects_duplicate_step_ids_and_bad_provenance() {
    let p = mutate(|v| {
        let id = first_step(v, "joined-bringup")["id"].clone();
        first_step(v, "fresh-4gb-default")["id"] = id;
        first_step(v, "member-leave")["provenance"] = json!("GUESS");
    });
    assert!(p.iter().any(|x| x.contains("duplicate_id")), "{p:?}");
    assert!(p.iter().any(|x| x.contains("bad_provenance")), "{p:?}");
}
#[test]
fn validator_rejects_undefined_template_variable() {
    let p = mutate(|v| first_step(v, "joined-bringup")["body"] = json!({"x":"{{never_defined}}"}));
    assert!(
        p.iter()
            .any(|x| x.contains("undefined_variable:never_defined")),
        "{p:?}"
    );
}
#[test]
fn validator_rejects_variable_used_before_it_is_captured() {
    // join_link is captured by owner_open_window; using it in the first step is a forward reference.
    let p = mutate(|v| first_step(v, "relay-window")["body"] = json!({"x":"{{join_link}}"}));
    assert!(
        p.iter().any(|x| x.contains("undefined_variable:join_link")),
        "{p:?}"
    );
}
#[test]
fn validator_rejects_budget_outside_the_allowed_registry_list() {
    let p = mutate(|v| first_step(v, "joined-bringup")["budget"] = json!("TwoSeconds"));
    assert!(
        p.iter()
            .any(|x| x.contains("budget_not_allowed:TwoSeconds")),
        "{p:?}"
    );
}
#[test]
fn validator_rejects_poll_without_until_and_sleep_without_budget() {
    let p = mutate(|v| {
        let s = first_step(v, "joined-bringup");
        s["op"] = json!("poll");
        s.as_object_mut().expect("object").remove("until");
        let f = first_step(v, "f15b-owner-local-deadline");
        f.as_object_mut().expect("object").remove("budget");
    });
    assert!(p.iter().any(|x| x.contains("poll_without_until")), "{p:?}");
    assert!(
        p.iter().any(|x| x.contains("sleep_needs_budget_and_times")),
        "{p:?}"
    );
}
#[test]
fn validator_rejects_session_that_runs_a_dependent_before_its_dependency() {
    let p = mutate(|v| {
        let sessions = v["sessions"].as_array_mut().expect("sessions");
        let s = sessions
            .iter_mut()
            .find(|s| s["id"] == "certified-joined")
            .expect("session");
        s["scenarios"].as_array_mut().expect("list").swap(0, 1);
    });
    assert!(
        p.iter().any(|x| x.contains("dependency_not_earlier")),
        "{p:?}"
    );
}
#[test]
fn validator_rejects_unknown_claim_and_uncovered_claim() {
    let p = mutate(|v| {
        v["scenarios"][0]["claims"] = json!(["CL-L9-99"]);
        v["claims"]
            .as_array_mut()
            .expect("claims")
            .push(json!("CL-L3-77"));
    });
    assert!(
        p.iter().any(|x| x.contains("unknown_claim:CL-L9-99")),
        "{p:?}"
    );
    assert!(
        p.iter().any(|x| x.contains("claim:CL-L3-77:no_scenario")),
        "{p:?}"
    );
}
#[test]
fn validator_rejects_unknown_fields_anywhere() {
    let mut value = shipped_json();
    first_step(&mut value, "joined-bringup")["surprise"] = json!(1);
    assert!(serde_json::from_value::<Scenarios>(value).is_err());
}
#[test]
fn validator_rejects_override_that_is_not_a_default() {
    let p = mutate(|v| {
        let s = v["scenarios"]
            .as_array_mut()
            .expect("list")
            .iter_mut()
            .find(|s| s["id"] == "f14-placement")
            .expect("f14");
        s["overrides"] = json!({"nonexistent":1});
    });
    assert!(
        p.iter().any(|x| x.contains("override_not_a_default")),
        "{p:?}"
    );
}

#[test]
fn validator_rejects_session_override_that_is_not_a_default() {
    let p = mutate(|v| {
        let sessions = v["sessions"].as_array_mut().expect("sessions");
        sessions[0]["overrides"] = json!({"made_up":1});
    });
    assert!(
        p.iter()
            .any(|x| x.contains("override_not_a_default:made_up")),
        "{p:?}"
    );
}

#[test]
fn subst_replaces_whole_values_keeping_type_and_embeds_scalars() {
    let mut vars = Map::new();
    vars.insert("n".into(), json!(2_000_000_000u64));
    vars.insert("s".into(), json!("abc"));
    assert_eq!(
        subst(&json!("{{n}}"), &vars).expect("ok"),
        json!(2_000_000_000u64)
    );
    assert_eq!(
        subst(&json!({"k":["x-{{s}}-{{n}}"]}), &vars).expect("ok"),
        json!({"k":["x-abc-2000000000"]})
    );
    assert!(subst(&json!("{{missing}}"), &vars).is_err());
    assert!(subst(&json!("a{{"), &vars).is_err());
}

#[test]
fn plan_marks_capability_gaps_and_relay_mode_branches() {
    let data = shipped();
    let none = BTreeSet::new();
    let p = plan(&data, "certified-joined", &none, "external").expect("plan");
    let rows = p["steps"].as_array().expect("rows");
    let status = |id: &str| {
        rows.iter().find(|r| r["step"] == id).expect("row")["status"]
            .as_str()
            .expect("s")
            .to_owned()
    };
    assert_eq!(status("owner_create_with_quick_relay"), "SKIP_WHEN");
    assert_eq!(status("relay_external_start"), "RUN");
    assert_eq!(
        status("member_join_with_consent"),
        "NOT_RUN:capability_missing:public_relay"
    );
    assert_eq!(
        status("owner_demo_start"),
        "NOT_RUN:capability_missing:public_relay"
    );
    let caps: BTreeSet<String> = ["public_relay", "model", "directory", "stable_origin"]
        .map(String::from)
        .into();
    let p = plan(&data, "certified-joined", &caps, "quick").expect("plan");
    let rows = p["steps"].as_array().expect("rows");
    let get = |id: &str| {
        rows.iter().find(|r| r["step"] == id).expect("row")["status"]
            .as_str()
            .expect("s")
            .to_owned()
    };
    assert_eq!(get("relay_external_start"), "SKIP_WHEN");
    assert_eq!(get("owner_create_with_quick_relay"), "RUN");
}

// --------------------------------------------------------------------------------------------
// Engine over a fake loopback server
// --------------------------------------------------------------------------------------------
type Handler = dyn Fn(&str, &str, &str) -> (u16, Vec<(&'static str, String)>, String) + Send + Sync;
struct Fake {
    origin: String,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Fake {
    fn start(handler: Arc<Handler>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().expect("addr").port();
        listener.set_nonblocking(true).expect("nonblocking");
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let thread = thread::spawn(move || {
            while !flag.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => serve(stream, &*handler),
                    Err(_) => thread::yield_now(),
                }
            }
        });
        Self {
            origin: format!("http://127.0.0.1:{port}"),
            stop,
            thread: Some(thread),
        }
    }
}
impl Drop for Fake {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(t) = self.thread.take() {
            t.join().expect("join fake");
        }
    }
}
fn serve(mut stream: TcpStream, handler: &Handler) {
    stream.set_nonblocking(false).expect("blocking");
    stream
        .set_read_timeout(Some(duration(BudgetId::ModelSelection).expect("budget")))
        .expect("timeout");
    let mut data = Vec::new();
    let mut buf = [0u8; 4096];
    let (head_end, length) = loop {
        let n = stream.read(&mut buf).expect("read");
        data.extend_from_slice(&buf[..n]);
        if let Some(pos) = data.windows(4).position(|w| w == b"\r\n\r\n") {
            let head = String::from_utf8_lossy(&data[..pos]).to_lowercase();
            let length = head
                .lines()
                .find_map(|l| {
                    l.strip_prefix("content-length:")
                        .map(|v| v.trim().parse::<usize>().expect("len"))
                })
                .unwrap_or(0);
            break (pos + 4, length);
        }
        assert!(n > 0, "closed early");
    };
    while data.len() < head_end + length {
        let n = stream.read(&mut buf).expect("read body");
        data.extend_from_slice(&buf[..n]);
    }
    let head = String::from_utf8_lossy(&data[..head_end]).into_owned();
    let mut first = head.lines().next().expect("line").split_whitespace();
    let (method, path) = (
        first.next().expect("m").to_owned(),
        first.next().expect("p").to_owned(),
    );
    let body = String::from_utf8_lossy(&data[head_end..head_end + length]).into_owned();
    let (status, headers, reply) = handler(&method, &path, &body);
    let mut wire = format!(
        "HTTP/1.1 {status} X\r\nContent-Length: {}\r\nConnection: close\r\n",
        reply.len()
    );
    for (k, v) in headers {
        wire.push_str(&format!("{k}: {v}\r\n"));
    }
    wire.push_str("\r\n");
    wire.push_str(&reply);
    record_reply_write(stream.write_all(wire.as_bytes())).expect("fake response write");
}

fn record_reply_write(result: std::io::Result<()>) -> std::io::Result<()> {
    match result {
        // A read-timeout probe can close before its deliberately late reply.
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::BrokenPipe | std::io::ErrorKind::ConnectionReset
            ) =>
        {
            eprintln!("fake_response_peer_closed: {:?}: {error}", error.kind());
            Ok(())
        }
        other => other,
    }
}

#[test]
fn fake_response_write_reports_peer_close_and_propagates_other_failures() {
    use std::io::{Error, ErrorKind};
    record_reply_write(Ok(())).expect("successful write");
    for kind in [ErrorKind::BrokenPipe, ErrorKind::ConnectionReset] {
        record_reply_write(Err(Error::from(kind))).expect("recorded peer closure");
    }
    for kind in [
        ErrorKind::PermissionDenied,
        ErrorKind::WriteZero,
        ErrorKind::Other,
    ] {
        let error = record_reply_write(Err(Error::from(kind))).expect_err("must propagate");
        assert_eq!(error.kind(), kind);
    }
}

fn session_cookie() -> (u16, Vec<(&'static str, String)>, String) {
    (
        303,
        vec![(
            "Set-Cookie",
            "a13session=t; HttpOnly; SameSite=Strict; Path=/".into(),
        )],
        String::new(),
    )
}
fn json_reply(value: Value) -> (u16, Vec<(&'static str, String)>, String) {
    (
        200,
        vec![("Content-Type", "application/json".into())],
        value.to_string(),
    )
}

fn data_with(steps: Value, session_expect: &str, requires: Value) -> Scenarios {
    serde_json::from_value(json!({
        "schema":"mycelium.journeys.twohost.v1","oracle":{},"claims":["CL-L3-05"],
        "budgets_allowed":["WorkerStartup","ModelSelection","OwnerPoll","Compile"],
        "targets":{"t":{"label":"t","sha256":null,"path":null}},
        "defaults":{"storage":2000000000u64},
        "scenarios":[{"id":"s","title":"t","claims":["CL-L3-05"],"steps":steps,"requires":requires}],
        "sessions":[{"id":"sess","target":"t","expect":session_expect,"scenarios":["s"]}]
    }))
    .expect("engine data")
}
fn endpoints(origin: &str, control: BTreeMap<String, Vec<String>>) -> Endpoints {
    let mut instances = BTreeMap::new();
    instances.insert(
        "owner".to_owned(),
        EndpointInstance {
            origin: origin.to_owned(),
            launch: Some("abc_DEF-1".into()),
        },
    );
    Endpoints {
        schema: journey_tests::twohost::ENDPOINTS_SCHEMA.into(),
        target: None,
        instances,
        control,
        vars: Map::new(),
    }
}
fn step(id: &str, extra: Value) -> Value {
    let mut base = json!({"id":id,"who":"owner","op":"http","method":"GET","path":"/api/status","source":"x:1 @ f4e366e9","provenance":"SOURCE"});
    for (k, v) in extra.as_object().expect("object") {
        base[k] = v.clone();
    }
    base
}

#[test]
fn engine_runs_capture_template_poll_observe_and_records_timing() {
    let polls = Arc::new(AtomicUsize::new(0));
    let seen_body = Arc::new(Mutex::new(String::new()));
    let (p, b) = (polls.clone(), seen_body.clone());
    let fake = Fake::start(Arc::new(move |m, path, body| match (m, path) {
        ("POST", "/session") => session_cookie(),
        ("POST", "/api/create") => {
            json_reply(json!({"window":{"id":"W1"},"membership_state":"current"}))
        }
        ("POST", "/api/use") => {
            *b.lock().expect("lock") = body.to_owned();
            json_reply(json!({"ok":true}))
        }
        ("GET", "/api/status") => {
            let n = p.fetch_add(1, Ordering::SeqCst);
            json_reply(
                json!({"state": if n >= 2 { "serving" } else { "warming" }, "tree":{"a":1,"b":["x",true]}}),
            )
        }
        _ => (404, vec![], "{}".into()),
    }));
    let steps = json!([
        step(
            "create",
            json!({"method":"POST","path":"/api/create","body":{"consent":{"storage_bytes":"{{storage}}"}},
            "capture":{"wid":"/window/id"},"expect":[{"ptr":"/membership_state","eq":"current"}],"observe":["/membership_state"],"timing":"start_issued"})
        ),
        step(
            "use",
            json!({"method":"POST","path":"/api/use","body":{"id":"{{wid}}","n":"{{storage}}"}})
        ),
        step(
            "serving",
            json!({"op":"poll","budget":"WorkerStartup","until":[{"ptr":"/state","eq":"serving"}],
            "observe_tree":[{"ptr":"/tree","name":"t"}],"timing":"start_to_serving"})
        ),
    ]);
    let data = data_with(steps, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "PASS", "{receipt}");
    assert_eq!(
        *seen_body.lock().expect("lock"),
        r#"{"id":"W1","n":2000000000}"#
    );
    assert!(polls.load(Ordering::SeqCst) >= 3);
    let fields = &receipt["observations"][1]["fields"];
    assert_eq!(fields["t:/a"], 1);
    assert_eq!(fields["t:/b/1"], true);
    assert_eq!(
        receipt["observations"][0]["fields"]["/membership_state"],
        "current"
    );
    assert!(receipt["timings"]["start_to_serving"].is_u64());
}

#[test]
fn engine_fails_on_wrong_expectation_and_marks_the_rest_not_run() {
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(json!({"membership_state":"absent"})),
    }));
    let steps = json!([
        step(
            "a",
            json!({"expect":[{"ptr":"/membership_state","eq":"current"}]})
        ),
        step("b", json!({})),
    ]);
    let data = data_with(steps, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "FAIL");
    assert_eq!(receipt["steps"][0]["status"], "FAIL");
    assert_eq!(receipt["steps"][1]["status"], "NOT_RUN");
    assert_eq!(receipt["steps"][1]["reason"], "aborted_after_failure");
}

#[test]
fn engine_rejects_unexpected_http_status_and_accepts_declared_refusals() {
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => (
            400,
            vec![],
            json!({"error":"consent_retry_changed"}).to_string(),
        ),
    }));
    let ok = json!([step(
        "refusal",
        json!({"status":400,"expect":[{"ptr":"/error","eq":"consent_retry_changed"}]})
    )]);
    let data = data_with(ok, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    assert_eq!(runner.run_session("sess").expect("run")["result"], "PASS");
    let bad = json!([step("unexpected", json!({}))]);
    let data = data_with(bad, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "FAIL");
    assert!(
        receipt["steps"][0]["error"]
            .as_str()
            .expect("e")
            .contains("unexpected_status:400:error=consent_retry_changed")
    );
}

#[test]
fn engine_red_session_passes_only_when_a_key_step_fails() {
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(json!({"state":"refused"})),
    }));
    let steps = json!([
        step("setup", json!({})),
        step(
            "recipe",
            json!({"key":true,"expect":[{"ptr":"/state","eq":"serving"}]})
        ),
    ]);
    let data = data_with(steps, "red", json!([]));
    let options = Options {
        expect_red: true,
        ..Options::default()
    };
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        options.clone(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "RED_AS_EXPECTED");
    assert_eq!(receipt["steps"][1]["status"], "RED");
    // The same recipe against a GREEN implementation is an UNEXPECTED_GREEN, not a pass.
    let green = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(json!({"state":"serving"})),
    }));
    let mut runner = Runner::new(&data, endpoints(&green.origin, BTreeMap::new()), options);
    assert_eq!(
        runner.run_session("sess").expect("run")["result"],
        "UNEXPECTED_GREEN"
    );
    // A non-key failure in a RED session is a real FAIL.
    let steps = json!([step(
        "setup",
        json!({"expect":[{"ptr":"/state","eq":"serving"}]})
    )]);
    let data = data_with(steps, "red", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options {
            expect_red: true,
            ..Options::default()
        },
    );
    assert_eq!(runner.run_session("sess").expect("run")["result"], "FAIL");
}

#[test]
fn engine_poll_times_out_with_a_named_deadline() {
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(json!({"state":"warming"})),
    }));
    // ModelSelection (5 s) is the shortest registered wait; the poll must give up, not hang.
    let steps = json!([step(
        "never",
        json!({"op":"poll","budget":"ModelSelection","until":[{"ptr":"/state","eq":"serving"}]})
    )]);
    let data = data_with(steps, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "FAIL");
    assert!(
        receipt["steps"][0]["error"]
            .as_str()
            .expect("e")
            .contains("poll:never:deadline_after_")
    );
}

#[test]
fn engine_skips_missing_capabilities_and_wrong_relay_mode_without_failing() {
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(json!({})),
    }));
    let steps = json!([
        step("quick_only", json!({"when":{"relay_mode":"quick"}})),
        step("needs_dir", json!({"requires":["directory"]})),
        step("plain", json!({})),
    ]);
    let data = data_with(steps, "green", json!([]));
    let options = Options {
        relay_mode: "external".into(),
        ..Options::default()
    };
    let mut runner = Runner::new(&data, endpoints(&fake.origin, BTreeMap::new()), options);
    let receipt = runner.run_session("sess").expect("run");
    let rows = receipt["steps"].as_array().expect("rows");
    assert!(
        rows.iter().all(|r| r["id"] != "quick_only"),
        "wrong-mode branch is not reported at all"
    );
    assert_eq!(
        rows.iter().find(|r| r["id"] == "needs_dir").expect("row")["status"],
        "NOT_RUN"
    );
    assert_eq!(
        rows.iter().find(|r| r["id"] == "plain").expect("row")["status"],
        "PASS"
    );
    assert_eq!(
        receipt["result"], "PARTIAL",
        "a run with NOT_RUN steps is never a PASS"
    );
}

#[test]
fn engine_reads_finite_sse_and_sends_delete() {
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        ("DELETE", "/cancel") => (202, vec![], String::new()),
        ("GET", "/events") => (
            200,
            vec![("Content-Type", "text/event-stream".into())],
            "event: token\ndata: {\"t\":\"a\"}\n\nevent: cancelled\ndata: {}\n\n".into(),
        ),
        _ => (404, vec![], "{}".into()),
    }));
    let steps = json!([
        step(
            "cancel",
            json!({"method":"DELETE","path":"/cancel","status_any":[200,202,204]})
        ),
        step(
            "sse",
            json!({"op":"sse","path":"/events","sse_expect":{"last_event_in":["cancelled","completed"],"min_events":2},"observe":[]})
        ),
    ]);
    let data = data_with(steps, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "PASS", "{receipt}");
    assert_eq!(
        receipt["observations"][0]["fields"]["last_event"],
        "cancelled"
    );
}

#[test]
fn engine_sse_that_ends_on_a_token_is_not_a_pass() {
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => (
            200,
            vec![("Content-Type", "text/event-stream".into())],
            "event: token\ndata: {}\n\n".into(),
        ),
    }));
    let steps = json!([step(
        "sse",
        json!({"op":"sse","path":"/events","sse_expect":{"last_event_in":["cancelled"],"min_events":1}})
    )]);
    let data = data_with(steps, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    assert_eq!(runner.run_session("sess").expect("run")["result"], "FAIL");
}

#[test]
fn engine_assert_latency_uses_recorded_poll_latencies() {
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(json!({"state":"serving"})),
    }));
    let steps = json!([
        step(
            "work",
            json!({"op":"poll","budget":"WorkerStartup","until":[{"ptr":"/state","eq":"serving"}]})
        ),
        step(
            "fast",
            json!({"op":"assert_latency","of_step":"work","per_poll_budget":"OwnerPoll"})
        ),
    ]);
    let data = data_with(steps, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    assert_eq!(runner.run_session("sess").expect("run")["result"], "PASS");
    // A server slower than the 500 ms OwnerPoll discriminator fails the same assertion.
    let slow = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => {
            thread::sleep(duration(BudgetId::OwnerPoll).expect("budget") * 2);
            json_reply(json!({"state":"serving"}))
        }
    }));
    let mut runner = Runner::new(
        &data,
        endpoints(&slow.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "FAIL");
    assert!(
        receipt["steps"][1]["error"]
            .as_str()
            .expect("e")
            .contains("status_poll_exceeded")
    );
}

#[test]
fn engine_control_replaces_an_instance_and_missing_control_is_not_run() {
    let first = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(json!({"which":"first"})),
    }));
    let second = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(json!({"which":"second"})),
    }));
    let reply = json!({"instance":"owner","origin":second.origin,"launch":"zzz_1"}).to_string();
    let mut control = BTreeMap::new();
    control.insert(
        "restart_owner".to_owned(),
        // The runner appends `<who> <path>`; $0 is the reply, so the last stdout line is the JSON.
        vec![
            "/bin/sh".to_owned(),
            "-c".to_owned(),
            "printf '%s\\n' \"$0\"".to_owned(),
            reply,
        ],
    );
    let steps = json!([
        step("before", json!({"expect":[{"ptr":"/which","eq":"first"}]})),
        step("restart", json!({"op":"control","action":"restart_owner"})),
        step("after", json!({"expect":[{"ptr":"/which","eq":"second"}]})),
        step("absent", json!({"op":"control","action":"no_such_action"})),
    ]);
    let data = data_with(steps, "green", json!([]));
    let mut runner = Runner::new(&data, endpoints(&first.origin, control), Options::default());
    let receipt = runner.run_session("sess").expect("run");
    let rows = receipt["steps"].as_array().expect("rows");
    assert_eq!(rows[2]["status"], "PASS", "{receipt}");
    assert_eq!(rows[3]["status"], "NOT_RUN");
    assert_eq!(rows[3]["reason"], "control_unavailable:no_such_action");
}

#[test]
fn golden_stabilize_drops_fields_that_differ_between_two_python_runs_and_compare_is_strict() {
    let run_a = json!({"session":"s","observations":[
        {"id":"x","scenario":"s","fields":{"/state":"joined","/at":1,"/n":3}},
        {"id":"y","scenario":"s","fields":{"/ok":true}}]});
    let run_b = json!({"session":"s","observations":[
        {"id":"x","scenario":"s","fields":{"/state":"joined","/at":2,"/n":3}},
        {"id":"y","scenario":"s","fields":{"/ok":true}}]});
    let golden = stabilize(&run_a, &run_b).expect("stabilize");
    assert_eq!(golden["unstable"], json!([{"id":"x","field":"/at"}]));
    assert_eq!(
        golden["observations"][0]["fields"],
        json!({"/state":"joined","/n":3})
    );
    // A third run reproduces the stable fields: pass (the unstable field is ignored).
    let run_c = json!({"observations":[
        {"id":"x","scenario":"s","fields":{"/state":"joined","/at":99,"/n":3}},
        {"id":"y","scenario":"s","fields":{"/ok":true}}]});
    compare_golden(&golden, &run_c).expect("same behaviour");
    // Changed stable value, extra field, missing observation and reordering are all rejected.
    for mutate in [
        |v: &mut Value| v["observations"][0]["fields"]["/state"] = json!("left"),
        |v: &mut Value| v["observations"][0]["fields"]["/extra"] = json!(1),
        |v: &mut Value| {
            v["observations"].as_array_mut().expect("a").pop();
        },
        |v: &mut Value| v["observations"].as_array_mut().expect("a").reverse(),
    ] {
        let mut changed = run_c.clone();
        mutate(&mut changed);
        assert!(compare_golden(&golden, &changed).is_err());
    }
    // Different observation order between the two recording runs cannot be stabilized.
    let mut swapped = run_b.clone();
    swapped["observations"].as_array_mut().expect("a").reverse();
    assert!(stabilize(&run_a, &swapped).is_err());
}

#[test]
fn engine_applies_session_overrides_to_every_scenario() {
    // Window 2026-10-28: session overrides were validated but never applied, so the "2 GB owner" sessions ran with 4 GB.
    let seen = Arc::new(Mutex::new(String::new()));
    let s = seen.clone();
    let fake = Fake::start(Arc::new(move |m, p, body| match (m, p) {
        ("POST", "/session") => session_cookie(),
        ("POST", "/api/use") => {
            *s.lock().expect("lock") = body.to_owned();
            json_reply(json!({"ok":true}))
        }
        _ => (404, vec![], "{}".into()),
    }));
    let mut data = data_with(
        json!([step(
            "use",
            json!({"method":"POST","path":"/api/use","body":{"n":"{{storage}}"}})
        )]),
        "green",
        json!([]),
    );
    data.sessions[0]
        .overrides
        .insert("storage".into(), json!(1234));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "PASS", "{receipt}");
    assert_eq!(*seen.lock().expect("lock"), r#"{"n":1234}"#);
}

#[test]
fn engine_poll_fails_at_once_on_a_terminal_state_and_names_it() {
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(json!({"owner_run":{"state":"failed","error_code":"two_macs_required"}})),
    }));
    let steps = json!([step(
        "serving",
        json!({"op":"poll","budget":"WorkerStartup","until":[{"ptr":"/owner_run/state","eq":"serving"}],
        "fail_when":[{"ptr":"/owner_run/state","in":["failed","failing"]}],
        "trace":["/owner_run/state","/owner_run/error_code"]})
    )]);
    let data = data_with(steps, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let started = std::time::Instant::now();
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "FAIL");
    let error = receipt["steps"][0]["error"].as_str().expect("error");
    assert!(
        error.starts_with("poll:serving:terminal:/owner_run/state=failed:after_1_polls:"),
        "{error}"
    );
    assert!(
        error.contains("/owner_run/error_code=two_macs_required"),
        "{error}"
    );
    assert!(
        started.elapsed() < duration(BudgetId::WorkerStartup).expect("budget"),
        "the poll must not run to its budget"
    );
}

#[test]
fn engine_captures_several_set_cookie_lines_as_one_cookie_header() {
    let seen = Arc::new(Mutex::new(String::new()));
    let s = seen.clone();
    let fake = Fake::start(Arc::new(move |m, p, body| match (m, p) {
        ("POST", "/session") => session_cookie(),
        ("GET", "/api/v1/bootstrap") => (
            200,
            vec![
                ("Content-Type", "application/json".into()),
                ("Set-Cookie", "gw=one; Path=/; HttpOnly".into()),
                ("Set-Cookie", "csrf=two; Path=/".into()),
                ("Vary", "Origin".into()),
                ("Vary", "Cookie".into()),
            ],
            "{}".into(),
        ),
        ("POST", "/api/echo") => {
            *s.lock().expect("lock") = body.to_owned();
            json_reply(json!({}))
        }
        _ => (404, vec![], "{}".into()),
    }));
    let steps = json!([
        step(
            "boot",
            json!({"path":"/api/v1/bootstrap","capture_header":{"jar":"set-cookie"}})
        ),
        step(
            "echo",
            json!({"method":"POST","path":"/api/echo","body":{"cookie":"{{jar}}"}})
        ),
    ]);
    let data = data_with(steps, "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "PASS", "{receipt}");
    assert_eq!(
        *seen.lock().expect("lock"),
        r#"{"cookie":"gw=one; csrf=two"}"#
    );
}

#[test]
fn engine_poll_records_a_read_timeout_as_latency_when_the_step_declares_it() {
    // Window 2026-10-28: a v0.1.3 owner blocked /api/status past the client read budget. That is the F5 evidence
    // (latency > OwnerPoll), not a failure of the non-key poll that observed it.
    let calls = Arc::new(AtomicUsize::new(0));
    let c = calls.clone();
    let fake = Fake::start(Arc::new(move |m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => {
            if c.fetch_add(1, Ordering::SeqCst) == 0 {
                thread::sleep(
                    duration(BudgetId::ModelSelection).expect("budget")
                        + duration(BudgetId::JourneyReadTimeoutOvershoot).expect("budget"),
                );
            }
            json_reply(json!({"state":"serving"}))
        }
    }));
    let polled = |extra: Value| {
        let mut poll =
            json!({"op":"poll","budget":"WorkerStartup","until":[{"ptr":"/state","eq":"serving"}]});
        for (k, v) in extra.as_object().expect("object") {
            poll[k] = v.clone();
        }
        json!([
            step("work", poll),
            step(
                "fast",
                json!({"op":"assert_latency","of_step":"work","per_poll_budget":"OwnerPoll","key":true})
            ),
        ])
    };
    // Declared: the poll passes, the latency assertion sees the stalled read and fails (RED for a historical build).
    let data = data_with(
        polled(json!({"timeout_is_latency":true})),
        "green",
        json!([]),
    );
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    let rows = receipt["steps"].as_array().expect("rows");
    assert_eq!(rows[0]["status"], "PASS", "{receipt}");
    assert_eq!(rows[1]["status"], "FAIL", "{receipt}");
    assert!(
        rows[1]["error"]
            .as_str()
            .expect("e")
            .contains("status_poll_exceeded"),
        "{receipt}"
    );
    assert!(
        runner.timings["work_status_poll_max_ms"] >= 5_000,
        "{receipt}"
    );
    // Not declared: the same stall still fails the step (a read timeout is never silently swallowed).
    calls.store(0, Ordering::SeqCst);
    let data = data_with(polled(json!({})), "green", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["steps"][0]["status"], "FAIL", "{receipt}");
    assert!(
        receipt["steps"][0]["error"]
            .as_str()
            .expect("e")
            .starts_with("http:"),
        "{receipt}"
    );
}

#[test]
fn engine_red_tolerant_poll_records_a_terminal_state_only_in_a_red_session() {
    // F14 on a historical build: the owner run fails at placement. The poll that sees it is not the key step.
    let fake = Fake::start(Arc::new(|m, p, _| match (m, p) {
        ("POST", "/session") => session_cookie(),
        _ => json_reply(
            json!({"owner_run":{"state":"failed","error_code":"member_storage_cap_below_share"}}),
        ),
    }));
    let steps = json!([
        step(
            "serving",
            json!({"op":"poll","budget":"WorkerStartup","red_tolerant":true,"until":[{"ptr":"/owner_run/state","eq":"serving"}],
            "fail_when":[{"ptr":"/owner_run/state","in":["failed","failing"]}]})
        ),
        step(
            "fits",
            json!({"key":true,"expect":[{"ptr":"/owner_run/state","eq":"serving"}]})
        ),
    ]);
    let data = data_with(steps, "red", json!([]));
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, BTreeMap::new()),
        Options {
            expect_red: true,
            ..Options::default()
        },
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "RED_AS_EXPECTED", "{receipt}");
    assert_eq!(receipt["steps"][0]["status"], "PASS", "{receipt}");
    assert_eq!(receipt["steps"][1]["status"], "RED", "{receipt}");
    // The same data in a green session is a failure at the poll.
    let steps = data.scenarios[0].steps.clone();
    let mut green = data_with(json!([]), "green", json!([]));
    green.scenarios[0].steps = steps;
    let mut runner = Runner::new(
        &green,
        endpoints(&fake.origin, BTreeMap::new()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "FAIL", "{receipt}");
    assert_eq!(receipt["steps"][0]["status"], "FAIL", "{receipt}");
}

#[test]
fn engine_signs_in_again_when_the_session_is_rejected_or_too_old() {
    // app.py:1995 @ f4e366e9: a browser session lasts 3600 s. The orchestrator's `resign` control makes a new launch value.
    let sessions = Arc::new(AtomicUsize::new(0));
    let s = sessions.clone();
    let fake = Fake::start(Arc::new(move |m, p, _| match (m, p) {
        ("POST", "/session") => {
            s.fetch_add(1, Ordering::SeqCst);
            session_cookie()
        }
        _ => {
            if s.load(Ordering::SeqCst) < 2 {
                (
                    401,
                    vec![],
                    json!({"error":"open_from_native_launcher"}).to_string(),
                )
            } else {
                json_reply(json!({"state":"serving"}))
            }
        }
    }));
    let reply = json!({"instance":"owner","origin":fake.origin,"launch":"fresh_1"}).to_string();
    let mut control = BTreeMap::new();
    control.insert(
        "resign".to_owned(),
        vec![
            "/bin/sh".to_owned(),
            "-c".to_owned(),
            "printf '%s\\n' \"$0\"".to_owned(),
            reply,
        ],
    );
    let data = data_with(
        json!([step(
            "status",
            json!({"expect":[{"ptr":"/state","eq":"serving"}]})
        )]),
        "green",
        json!([]),
    );
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, control.clone()),
        Options::default(),
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "PASS", "{receipt}");
    assert_eq!(
        sessions.load(Ordering::SeqCst),
        2,
        "one rejected session, one new sign-in"
    );
    // Proactive: a session older than the limit is renewed before the request is sent.
    sessions.store(5, Ordering::SeqCst);
    let mut runner = Runner::new(
        &data,
        endpoints(&fake.origin, control),
        Options {
            resign_after: Some(Duration::ZERO),
            ..Options::default()
        },
    );
    let receipt = runner.run_session("sess").expect("run");
    assert_eq!(receipt["result"], "PASS", "{receipt}");
    assert!(
        sessions.load(Ordering::SeqCst) >= 7,
        "the old session was renewed before use"
    );
}
