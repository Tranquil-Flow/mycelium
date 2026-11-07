// SPDX-License-Identifier: AGPL-3.0-only
//! Reviewed qualification path: pooling is allowed only across attempts named by a
//! hash-bound trial declaration; exactness is derived from the sample kind, never from
//! the declaration or from sample count.
use latency_profiles::device::sha256_hex;
use latency_profiles::qualify::{Policy, qualify_device};
use serde_json::{Value, json};
use std::{fs, path::PathBuf};

const SRC: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";
const EXE: &str = "8fff41823d357949ffa55c00b3fb8dfb0b4cc541f66343a72f21e3d8dd89186f";

struct World {
    root: PathBuf,
}
fn hosts(owner: f64, laptop: f64) -> Value {
    json!({"owner":{"load":[owner,owner,owner]},"laptop":{"load":[laptop,laptop,laptop]}})
}
fn chat(at: f64, total: f64, done: bool, with_load: bool) -> Value {
    let mut v = json!({"event":"chat","at_epoch":at,"done":done,"terminal":if done {"completed"} else {"failed"},
        "error":if done {Value::Null} else {json!("chat_timeout")},"n_tokens":32,"ttft_s":2.0,"total_s":total,
        "decode_tok_s":14.0,"measurement_boundary":"client token-chunk polling at 250ms; not native decode throughput"});
    if with_load {
        v["hosts"] = hosts(6.0, 2.0);
        v["hosts_after"] = hosts(6.5, 2.1);
    }
    v
}
fn admission(host: &str, os: &str) -> Value {
    json!({"event":"host_admission","host":host,"at_epoch":1.0,"summary":{"os_version":os,"load":[1.0,1.0,1.0]}})
}
impl World {
    fn new(name: &str) -> Self {
        let root = std::env::temp_dir().join(format!("fx-qual-{}-{name}", std::process::id()));
        if root.exists() {
            fs::remove_dir_all(&root).unwrap();
        }
        fs::create_dir_all(&root).unwrap();
        Self { root }
    }
    fn attempt(&self, name: &str, events: &[Value]) {
        self.attempt_with(name, events, SRC, "a9-route");
    }
    fn attempt_with(&self, name: &str, events: &[Value], src: &str, model: &str) {
        let dir = self.root.join(name);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("RESULT.json"), json!({"source_sha":src,"exe_sha256":EXE,"member_join":{"run_model":{"selected":model}}}).to_string()).unwrap();
        let mut text = String::new();
        for e in events {
            text.push_str(&e.to_string());
            text.push('\n');
        }
        fs::write(dir.join("events.jsonl"), text).unwrap();
    }
    fn chats(&self, name: &str, n: usize, first_total: f64) {
        let mut ev = vec![admission("owner", "26.5.1"), admission("laptop", "15.6.1")];
        for i in 0..n {
            ev.push(chat(
                10.0 + i as f64,
                first_total + i as f64 * 0.01,
                true,
                true,
            ));
        }
        self.attempt(name, &ev);
    }
    fn declaration(&self, names: &[&str]) -> Value {
        let attempts: Vec<Value> = names
            .iter()
            .map(|n| {
                let d = self.root.join(n);
                json!({"name":n,
                "result_sha256":sha256_hex(&fs::read(d.join("RESULT.json")).unwrap()).unwrap(),
                "events_sha256":sha256_hex(&fs::read(d.join("events.jsonl")).unwrap()).unwrap()})
            })
            .collect();
        json!({"schema_version":1,"trial_id":"t1","reviewed_by":"owner-review","review_note":"same build/model/devices",
            "independence_basis":"separate windows, fixed 16 s spacing; declared, not statistically tested",
            "comparable":{"source_sha":SRC,"exe_sha256":EXE,"model":"a9-route","method":"v014-cycle-driver",
              "devices":{"owner":{"hardware":"Mac16,8","hardware_basis":"owner attestation","macos":"26.5.1"},
                         "laptop":{"hardware":"Mac16,1","hardware_basis":"owner attestation","macos":"15.6.1"}}},
            "attempts":attempts})
    }
    fn run(&self, decl: &Value, policy: Policy) -> Result<Value, String> {
        let bytes = decl.to_string().into_bytes();
        let path = self.root.join("trial-declaration.json");
        fs::write(&path, &bytes).unwrap();
        self.run_bound(&path, &sha256_hex(&bytes).unwrap(), policy)
    }
    fn run_bound(
        &self,
        path: &std::path::Path,
        digest: &str,
        policy: Policy,
    ) -> Result<Value, String> {
        qualify_device(&self.root, path, digest, policy)
            .map(|q| serde_json::to_value(q).unwrap())
            .map_err(|e| e.to_string())
    }
}
impl Drop for World {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).expect("owned fixture cleanup");
    }
}
fn cell<'a>(q: &'a Value, phase: &str) -> &'a Value {
    q["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["phase"] == phase && c["variant"].is_null())
        .unwrap_or_else(|| panic!("no cell {phase}"))
}

#[test]
fn declared_attempts_pool_to_exact_loaded_p99_that_no_single_attempt_reaches() {
    let w = World::new("pool");
    for (i, n) in ["attempt1", "attempt2", "attempt3"].iter().enumerate() {
        w.chats(n, 40, 1.0 + i as f64 * 0.5);
    }
    let q = w
        .run(
            &w.declaration(&["attempt1", "attempt2", "attempt3"]),
            Policy::default(),
        )
        .unwrap();
    let c = cell(&q, "chat_total");
    assert_eq!(c["n_valid_exact_load_known"], 120);
    assert_eq!(c["status"], "EMPIRICAL_P99");
    // nearest rank: 119th of 120 sorted values (attempt3 spans 2.0..2.39)
    assert!((c["p99"].as_f64().unwrap() - 2380.0).abs() < 1e-6);
    assert_eq!(c["device"]["owner"]["hardware"], "Mac16,8");
    assert_eq!(c["device"]["laptop"]["macos"], "15.6.1");
    assert_eq!(c["build"], "f4e366e9");
    assert_eq!(c["model"], "a9-route");
    // The pre-existing per-attempt importer path is unchanged: it still never emits a p99.
    let old = latency_profiles::device::import_device(&w.root).unwrap();
    assert!(old.profiles.iter().all(|p| p.statistics.p99.is_none()));
}

#[test]
fn undeclared_attempts_are_never_pooled() {
    let w = World::new("undeclared");
    w.chats("attempt1", 60, 1.0);
    w.chats("attempt2", 60, 1.0);
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    let c = cell(&q, "chat_total");
    assert_eq!(c["n_valid_exact_load_known"], 60);
    assert_eq!(c["status"], "BLOCKED");
    assert!(c["blocked_reasons"].to_string().contains("fewer_than_100"));
    assert_eq!(q["attempts_unpooled"], json!(["attempt2"]));
}

#[test]
fn declaration_is_hash_bound_and_attempt_bytes_are_bound_to_it() {
    let w = World::new("hash");
    w.chats("attempt1", 5, 1.0);
    let decl = w.declaration(&["attempt1"]);
    let path = w.root.join("trial-declaration.json");
    fs::write(&path, decl.to_string()).unwrap();
    assert_eq!(
        w.run_bound(&path, &"0".repeat(64), Policy::default())
            .unwrap_err(),
        "declaration_digest_mismatch"
    );
    let ok = sha256_hex(&fs::read(&path).unwrap()).unwrap();
    assert!(w.run_bound(&path, &ok, Policy::default()).is_ok());
    // Edit an attempt after the declaration was reviewed.
    let mut text = fs::read_to_string(w.root.join("attempt1/events.jsonl")).unwrap();
    text.push_str(&chat(99.0, 0.5, true, true).to_string());
    fs::write(w.root.join("attempt1/events.jsonl"), text).unwrap();
    assert_eq!(
        w.run_bound(&path, &ok, Policy::default()).unwrap_err(),
        "declared_attempt_digest_mismatch:attempt1:events.jsonl"
    );
}

#[test]
fn comparability_violations_reject_the_whole_declaration() {
    let w = World::new("compare");
    w.chats("attempt1", 5, 1.0);
    let mut ev = vec![admission("owner", "26.5.1"), admission("laptop", "15.6.1")];
    ev.push(chat(10.0, 1.0, true, true));
    w.attempt_with("attempt2", &ev, &"c".repeat(40), "a9-route");
    assert_eq!(
        w.run(&w.declaration(&["attempt1", "attempt2"]), Policy::default())
            .unwrap_err(),
        "declared_comparability_violated:attempt2:source_sha"
    );
    w.attempt_with("attempt2", &ev, SRC, "other-model");
    assert_eq!(
        w.run(&w.declaration(&["attempt1", "attempt2"]), Policy::default())
            .unwrap_err(),
        "declared_comparability_violated:attempt2:model"
    );
    let mut os = vec![admission("owner", "26.5.1"), admission("laptop", "15.7")];
    os.push(chat(10.0, 1.0, true, true));
    w.attempt("attempt2", &os);
    assert_eq!(
        w.run(&w.declaration(&["attempt1", "attempt2"]), Policy::default())
            .unwrap_err(),
        "declared_comparability_violated:attempt2:laptop_macos"
    );
    w.attempt("attempt2", &[chat(10.0, 1.0, true, true)]);
    assert_eq!(
        w.run(&w.declaration(&["attempt1", "attempt2"]), Policy::default())
            .unwrap_err(),
        "declared_comparability_violated:attempt2:owner_macos_unrecorded"
    );
}

#[test]
fn declaration_needs_hardware_review_and_rejects_exactness_overrides() {
    let w = World::new("decl");
    w.chats("attempt1", 5, 1.0);
    let mut d = w.declaration(&["attempt1"]);
    d["comparable"]["devices"]["owner"]["hardware"] = json!("");
    assert_eq!(
        w.run(&d, Policy::default()).unwrap_err(),
        "declaration_hardware_missing:owner"
    );
    let mut d = w.declaration(&["attempt1"]);
    d["reviewed_by"] = json!("");
    assert_eq!(
        w.run(&d, Policy::default()).unwrap_err(),
        "declaration_review_missing"
    );
    let mut d = w.declaration(&["attempt1"]);
    d["exact_phases"] = json!(["ttft", "decode"]);
    assert_eq!(
        w.run(&d, Policy::default()).unwrap_err(),
        "declaration_invalid"
    );
    let mut d = w.declaration(&["attempt1"]);
    d["attempts"] = json!([]);
    assert_eq!(
        w.run(&d, Policy::default()).unwrap_err(),
        "declaration_no_attempts"
    );
}

#[test]
fn recorded_hardware_must_agree_with_the_declaration_and_sets_provenance() {
    let w = World::new("hw");
    let mut a = admission("owner", "26.5.1");
    a["summary"]["hardware_model"] = json!("Mac16,8");
    w.attempt(
        "attempt1",
        &[
            a,
            admission("laptop", "15.6.1"),
            chat(10.0, 1.0, true, true),
        ],
    );
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    let c = cell(&q, "chat_total");
    assert_eq!(
        c["device"]["owner"]["hardware_provenance"],
        "recorded_and_declared"
    );
    assert_eq!(
        c["device"]["laptop"]["hardware_provenance"],
        "declared_only"
    );
    let mut b = admission("owner", "26.5.1");
    b["summary"]["hardware_model"] = json!("Mac14,5");
    w.attempt(
        "attempt1",
        &[
            b,
            admission("laptop", "15.6.1"),
            chat(10.0, 1.0, true, true),
        ],
    );
    assert_eq!(
        w.run(&w.declaration(&["attempt1"]), Policy::default())
            .unwrap_err(),
        "declared_comparability_violated:attempt1:owner_hardware"
    );
}

#[test]
fn polled_boundaries_stay_inexact_even_with_many_loaded_samples() {
    let w = World::new("polled");
    w.chats("attempt1", 130, 1.0);
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    for phase in ["ttft", "decode"] {
        let c = cell(&q, phase);
        assert_eq!(c["n_observed"], 130);
        assert_eq!(c["exact"], false);
        assert!(c["p99"].is_null(), "{phase}");
        assert_eq!(c["status"], "BLOCKED");
        assert!(
            c["blocked_reasons"]
                .to_string()
                .contains("polled_boundary_not_exact")
        );
        assert!(c["descriptive_p99"].is_number());
    }
}

#[test]
fn strict_event_level_policy_also_blocks_client_observed_elapsed() {
    let w = World::new("strict");
    w.chats("attempt1", 130, 1.0);
    let d = w.declaration(&["attempt1"]);
    assert_eq!(
        cell(&w.run(&d, Policy::default()).unwrap(), "chat_total")["status"],
        "EMPIRICAL_P99"
    );
    let q = w
        .run(
            &d,
            Policy {
                event_level_only: true,
            },
        )
        .unwrap();
    let c = cell(&q, "chat_total");
    assert_eq!(c["status"], "BLOCKED");
    assert!(
        c["blocked_reasons"]
            .to_string()
            .contains("client_observed_elapsed_not_event_level")
    );
    assert!(c["p99"].is_null());
}

#[test]
fn failed_and_load_unknown_samples_never_count_toward_n() {
    let w = World::new("validity");
    let mut ev = vec![admission("owner", "26.5.1"), admission("laptop", "15.6.1")];
    for i in 0..99 {
        ev.push(chat(10.0 + i as f64, 1.0 + i as f64 * 0.01, true, true));
    }
    ev.push(chat(200.0, 5.0, true, false)); // completed, load not recorded
    ev.push(chat(201.0, 0.02, false, true)); // failed chat is not a latency sample
    w.attempt("attempt1", &ev);
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    let c = cell(&q, "chat_total");
    assert_eq!(c["n_valid_exact_load_known"], 99);
    assert_eq!(c["n_load_unknown"], 1);
    assert_eq!(c["n_failed"], 1);
    assert_eq!(c["status"], "BLOCKED");
    assert!(c["p99"].is_null());
    // one more valid sample reaches the rule
    let mut ev2 = ev.clone();
    ev2.push(chat(300.0, 1.5, true, true));
    w.attempt("attempt1", &ev2);
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    assert_eq!(cell(&q, "chat_total")["status"], "EMPIRICAL_P99");
}

fn probe(i: usize, exact: bool) -> Value {
    json!({"event":"relay_http_probe","at_epoch":10.0 + i as f64,"total_s":0.08,"ok":true,"http_status":200,
        "relay_rtt_exact":exact,"hosts":hosts(6.0, 2.0),
        "method":if exact {"carrier round trip"} else {"public HTTPS GET /chat elapsed; not carrier RTT"}})
}

#[test]
fn https_probes_are_auxiliary_and_relay_rtt_stays_blocked_without_carrier_samples() {
    let w = World::new("relay");
    let mut ev = vec![admission("owner", "26.5.1"), admission("laptop", "15.6.1")];
    for i in 0..120 {
        ev.push(probe(i, false));
    }
    w.attempt("attempt1", &ev);
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    let rtt = cell(&q, "relay_rtt");
    assert_eq!(rtt["n_observed"], 0);
    assert_eq!(rtt["status"], "BLOCKED");
    assert!(
        rtt["blocked_reasons"]
            .to_string()
            .contains("no_carrier_rtt_samples")
    );
    let aux = cell(&q, "relay_https_probe");
    assert_eq!(aux["n_observed"], 120);
    assert_eq!(aux["required_phase"], false);
    assert_eq!(aux["status"], "BLOCKED");
    assert!(
        aux["blocked_reasons"]
            .to_string()
            .contains("not_a_required_phase")
    );
}

#[test]
fn explicit_carrier_rtt_probes_feed_relay_rtt_and_can_qualify() {
    let w = World::new("carrier");
    let mut ev = vec![admission("owner", "26.5.1"), admission("laptop", "15.6.1")];
    for i in 0..100 {
        ev.push(probe(i, true));
    }
    w.attempt("attempt1", &ev);
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    let c = cell(&q, "relay_rtt");
    assert_eq!(c["status"], "EMPIRICAL_P99");
    assert!((c["p99"].as_f64().unwrap() - 80.0).abs() < 1e-9);
}

#[test]
fn close_and_unported_phases_are_explicitly_blocked_never_silently_absent() {
    let w = World::new("blocked");
    w.chats("attempt1", 3, 1.0);
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    let close = cell(&q, "close");
    assert_eq!(close["n_observed"], 0);
    assert_eq!(close["status"], "BLOCKED");
    assert!(
        close["blocked_reasons"]
            .to_string()
            .contains("no_close_samples")
    );
    for phase in ["rollover", "restart"] {
        assert!(
            cell(&q, phase)["blocked_reasons"]
                .to_string()
                .contains("no_ingestion_producer"),
            "{phase}"
        );
    }
}

#[test]
fn stop_observations_are_polled_and_never_exact_while_run_phases_use_interval_load() {
    let w = World::new("stop");
    let mut ev = vec![admission("owner", "26.5.1"), admission("laptop", "15.6.1")];
    for i in 0..110 {
        ev.push(json!({"event":"stop_observation","at_epoch":10.0 + i as f64,"state":"stopped","seconds_after_stop":3.5,
            "cleanup_unproven":null,"hosts":hosts(6.0, 2.0),"exact":false,"poll_interval_s":1}));
    }
    w.attempt("attempt1", &ev);
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    let c = cell(&q, "stop");
    assert_eq!(c["n_observed"], 110);
    assert_eq!(c["exact"], false);
    assert!(c["p99"].is_null());
    assert_eq!(c["descriptive_p99"].as_f64().unwrap(), 3500.0);
}

fn progress(at: f64, run: &str, state: &str, step: &str, extra: Value, with_hosts: bool) -> Value {
    let mut o = json!({"state":state,"step":step,"run_id":run});
    for (k, v) in extra.as_object().unwrap() {
        o[k] = v.clone();
    }
    let mut r = json!({"event":"run_progress","at_epoch":at,"owner_run":o});
    if with_hosts {
        r["hosts"] = hosts(7.0, 2.0);
    }
    r
}

#[test]
fn run_phases_are_polled_or_reported_and_cache_hits_are_not_cold_compiles() {
    let w = World::new("runs");
    let mut ev = vec![admission("owner", "26.5.1"), admission("laptop", "15.6.1")];
    ev[0]["at_epoch"] = json!(100.0);
    ev.push(
        json!({"event":"owner_action","at_epoch":101.0,"path":"/api/demo/start","http_status":200}),
    );
    ev.push(progress(
        102.0,
        "r1",
        "starting",
        "staging",
        json!({"started_at":101.5,"build_seconds":6.0,"compilation_cache_hit":true}),
        true,
    ));
    ev.push(progress(
        103.0,
        "r1",
        "starting",
        "publish",
        json!({}),
        true,
    ));
    ev.push(progress(
        110.0,
        "r1",
        "starting",
        "targets",
        json!({}),
        true,
    ));
    ev.push(progress(160.0, "r1", "serving", "serving", json!({}), true));
    // second run: load never recorded inside its interval
    ev.push(
        json!({"event":"owner_action","at_epoch":200.0,"path":"/api/demo/start","http_status":200}),
    );
    ev.push(progress(
        201.0,
        "r2",
        "starting",
        "publish",
        json!({}),
        false,
    ));
    ev.push(progress(
        205.0,
        "r2",
        "serving",
        "serving",
        json!({}),
        false,
    ));
    w.attempt("attempt1", &ev);
    let q = w
        .run(&w.declaration(&["attempt1"]), Policy::default())
        .unwrap();
    let find = |phase: &str, variant: Option<&str>| {
        q["cells"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["phase"] == phase && c["variant"] == json!(variant))
            .unwrap()
            .clone()
    };
    let hit = find("compile", Some("cache_hit"));
    assert_eq!(hit["n_observed"], 1);
    assert_eq!(hit["descriptive_p99"], 6000.0);
    assert!(
        hit["blocked_reasons"]
            .to_string()
            .contains("cache_hit_not_cold_compile")
    );
    assert_eq!(find("compile", Some("cold_or_unknown"))["n_observed"], 0);
    let publish = find("publish", None);
    assert_eq!(publish["n_observed"], 2);
    assert_eq!(publish["n_load_unknown"], 1);
    assert_eq!(publish["descriptive_p99"], 7000.0);
    let start = find("start", None);
    assert_eq!(start["n_observed"], 2);
    assert_eq!(start["descriptive_p99"], 59000.0);
    assert_eq!(start["exact"], false);
}
