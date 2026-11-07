// SPDX-License-Identifier: AGPL-3.0-only
//! `RemoteMacMember.renew()` against the EXECUTED oracle (FX-L10-PEER-RENEW: `L10-evidence/r13-probe-renew.py` ->
//! `fixtures/oracle_peer_renew.json`, v0.1.4 f4e366e9): the real Python member against a real loopback TLS seed,
//! with TIME injected on both sides (the member's clock is a script whose every read is counted; the seed
//! coordinator's clock is set per step). Every seed exchange was recorded; this test replays them from a loopback
//! TLS server and demands the same request BYTES (the signed heartbeat), the same answer or exception
//! (class/module/code), the same number of clock reads, the same lease, membership projection and files.
mod common;
use api::{
    member::{Binding, Session},
    peer::{
        Accepted, JoinEnv, accept_handoff, membership_binding, membership_result_live_lazy, renew,
        start,
    },
    seed::SeedOptions,
};
use common::{first_counter, lay_out, listing, script_of, secret, serve};
use protocol::identity::Identity;
use serde_json::Value;
use std::{cell::Cell, collections::HashMap, fs};

fn fixture() -> Value {
    let doc: Value = serde_json::from_str(include_str!("fixtures/oracle_peer_renew.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc
}
fn instant(value: &Value) -> f64 {
    match value.as_str() {
        Some("nan") => f64::NAN,
        _ => value.as_f64().unwrap(),
    }
}

#[test]
fn renew_equals_the_executed_oracle() {
    let doc = fixture();
    let scenarios = doc["scenarios"].as_array().unwrap();
    assert!(scenarios.len() >= 38, "{} scenarios", scenarios.len());
    let (mut failures, mut steps_run, mut renews_run) = (Vec::new(), 0usize, 0usize);
    for scenario in scenarios {
        let label = scenario["label"].as_str().unwrap();
        let signer = Identity::from_secret(
            scenario["signer_endpoint"].as_str().unwrap(),
            &secret(scenario["signer_secret"].as_str().unwrap()),
        )
        .unwrap();
        let counter = Cell::new(first_counter(scenario) - 1);
        let server = serve(scenario);
        // The Python member objects of this scenario: `started` and the live session.
        let mut members: HashMap<usize, (bool, Option<Session>)> = HashMap::new();
        for step in scenario["steps"].as_array().unwrap() {
            steps_run += 1;
            let name = step["name"].as_str().unwrap();
            let id = step["member"].as_u64().unwrap() as usize;
            let dir = lay_out(&format!("{label}-{name}"), &step["files_in"]);
            let calls = step["calls"].as_array().unwrap();
            server.script(script_of(calls));
            let options = SeedOptions {
                ca_pem: Some(scenario["ca_pem"].as_str().unwrap().as_bytes().to_vec()),
                connect_to: Some(server.address),
                server_name: None,
            };
            let script: Vec<f64> = step["script"]
                .as_array()
                .unwrap()
                .iter()
                .map(instant)
                .collect();
            let reads = Cell::new(0usize);
            let clock = || {
                let value = script[reads.get().min(script.len() - 1)];
                reads.set(reads.get() + 1);
                value
            };
            let random = |n: usize| -> String {
                if n == 8 {
                    return "00112233445566ff".to_owned();
                }
                counter.set(counter.get() + 1);
                format!("{}{:02x}", "ab".repeat(n - 1), counter.get())
            };
            let env = JoinEnv {
                signer: &signer,
                clock: &clock,
                random_hex: &random,
                seed: &options,
            };
            let want = &step["outcome"];
            let mut problems = Vec::new();
            members
                .entry(id)
                .or_insert((step["member_started_before"].as_bool().unwrap(), None));
            let mut error: Option<(String, String, String)> = None;
            let mut answer: Option<Value> = None;
            if name == "accept" {
                match accept_handoff(&dir, true, &scenario["handoff"], &env) {
                    Ok(Accepted { answer: a, session }) => {
                        answer = Some(a.to_value());
                        members.insert(id, (true, session));
                    }
                    Err(e) => {
                        let (class, module) = e.raised.identity();
                        error = Some((class.into(), module.into(), e.code.into()));
                    }
                }
            } else if name.starts_with("restart") {
                let identity_present = step["files_in"].get("identity/node.key").is_some();
                match start(&dir, &signer, identity_present, &env) {
                    Ok(done) => members.insert(id, (true, done.session)),
                    Err(failed) => {
                        let (class, module) = failed.error.raised.identity();
                        error = Some((class.into(), module.into(), failed.error.code.into()));
                        members.insert(id, (failed.started, None))
                    }
                };
            } else {
                renews_run += 1;
                let (started, session) = members.get_mut(&id).unwrap();
                match renew(&dir, *started, session.as_mut(), &env) {
                    Ok(a) => answer = Some(a.to_value()),
                    Err(e) => {
                        let (class, module) = e.raised.identity();
                        error = Some((class.into(), module.into(), e.code.into()));
                    }
                }
                if reads.get() as u64 != want["clock_reads"].as_u64().unwrap() {
                    problems.push(format!(
                        "clock reads {} != {}",
                        reads.get(),
                        want["clock_reads"]
                    ));
                }
            }
            match (&error, want.get("error")) {
                (None, None) => {
                    if name.starts_with("renew") && answer.as_ref() != want.get("answer") {
                        problems.push(format!("answer {answer:?} != {:?}", want.get("answer")));
                    }
                }
                (Some((class, module, code)), Some(w)) => {
                    // An `http.client` exception has no closed code: its text carries byte counts.
                    let raw = w["module"] == "http.client";
                    if (class.as_str(), module.as_str())
                        != (w["type"].as_str().unwrap(), w["module"].as_str().unwrap())
                        || (!raw && code.as_str() != w["code"].as_str().unwrap())
                    {
                        problems.push(format!("raised {class}/{module}/{code} != {w}"));
                    }
                }
                (got, w) => problems.push(format!("outcome {got:?} vs {w:?}")),
            }
            let (started, session) = members.get(&id).unwrap();
            if *started != want["_started"] {
                problems.push(format!("_started {started} != {}", want["_started"]));
            }
            if session.is_some() != want["has_member"] {
                problems.push(format!(
                    "live session {} != {}",
                    session.is_some(),
                    want["has_member"]
                ));
            }
            let lease = session
                .as_ref()
                .and_then(Session::lease_expires_at)
                .map(Value::from);
            if lease.clone().unwrap_or(Value::Null) != want["lease"] {
                problems.push(format!("lease {lease:?} != {}", want["lease"]));
            }
            match (
                membership_result_live_lazy(&dir, session.as_ref(), &clock),
                want.get("membership_result"),
            ) {
                (Ok(got), Some(w)) if &got.to_value() == w => {}
                (Err(e), None) => {
                    let (class, module) = e.raised.identity();
                    let w = &want["membership_result_error"];
                    if (class, module, e.code)
                        != (
                            w["type"].as_str().unwrap_or_default(),
                            w["module"].as_str().unwrap_or_default(),
                            w["code"].as_str().unwrap_or_default(),
                        )
                    {
                        problems.push(format!(
                            "membership_result error {class}/{module}/{} != {w}",
                            e.code
                        ));
                    }
                }
                (got, w) => problems.push(format!("membership_result {got:?} != {w:?}")),
            }
            if let Some(binding) = want.get("binding").filter(|b| !b.is_null()) {
                let expect = Binding {
                    swarm_id: binding["swarm_id"].as_str().unwrap().into(),
                    node_id: binding["node_id"].as_str().unwrap().into(),
                    seed_key_digest: binding["seed_key_digest"].as_str().unwrap().into(),
                };
                if membership_binding(session.as_ref()) != Some(expect) {
                    problems.push("membership_binding differs".into());
                }
            }
            if let (Some(s), Some(g)) = (session, want.get("session_generation"))
                && s.generation().map(Value::from).as_ref() != Some(g)
            {
                problems.push(format!("generation {:?} != {g}", s.generation()));
            }
            let got_files = listing(&dir);
            if got_files != step["files"] {
                for (file, w) in step["files"].as_object().unwrap() {
                    if got_files.get(file) != Some(w) {
                        problems.push(format!("  {file} differs"));
                    }
                }
                problems.push(format!(
                    "files differ: {:?}",
                    got_files.as_object().unwrap().keys().collect::<Vec<_>>()
                ));
            }
            let (seen, used_all) = server.take();
            let wanted: Vec<(String, String, String)> = calls
                .iter()
                .map(|c| {
                    (
                        c["method"].as_str().unwrap().into(),
                        c["path"].as_str().unwrap().into(),
                        c["request"].as_str().unwrap_or_default().into(),
                    )
                })
                .collect();
            if seen != wanted || !used_all {
                problems.push(format!(
                    "requests {:?} != {:?}",
                    seen.iter().map(|s| &s.1).collect::<Vec<_>>(),
                    wanted.iter().map(|s| &s.1).collect::<Vec<_>>()
                ));
                for (a, b) in seen.iter().zip(&wanted) {
                    if a != b {
                        problems.push(format!("  {} body equal: {}", a.1, a.2 == b.2));
                    }
                }
            }
            if !problems.is_empty() {
                failures.push(format!("{label}/{name}: {}", problems.join(" | ")));
            }
            fs::remove_dir_all(&dir).unwrap_or(());
        }
        server.finish();
    }
    assert!(steps_run >= 104, "{steps_run} steps");
    assert!(renews_run >= 60, "{renews_run} renew steps");
    assert!(
        failures.is_empty(),
        "{} steps differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
