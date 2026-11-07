// SPDX-License-Identifier: AGPL-3.0-only
//! `run_exchange.RunExchange` (the owner side's in-memory state: announcement prepare / commit with the idle
//! refresh fence, per-node extras, model-source hint, the member-report inbox, `validate_report`) against
//! EXECUTED v0.1.4: probe `r19-probe-run-exchange.py` (FX-L10-RUN-EXCHANGE: 11 scenarios, 122 ops, run twice
//! identical, the real class on a stub mailbox). The recorded key ORDER of every announcement is compared
//! (the wire document is signed over it). The signed-control routes (`/managed-control/demo-run/*`) are a
//! separate seam (SE-L10-EXCHANGE-ROUTES).
use api::run_exchange::{Announce, ExchangeError, RunExchange, validate_report};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};

const FIXTURE: &str = include_str!("fixtures/oracle_run_exchange.json");

/// The owner incarnation is random per process: the probe normalised it to a token after checking its shape
/// (`owner-process-` and 32 lowercase hex digits); so does the replay.
fn normalise(value: Value) -> Value {
    match value {
        Value::String(s) if s.starts_with("owner-process-") => {
            let hex = &s["owner-process-".len()..];
            assert!(
                hex.len() == 32
                    && hex
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
                "incarnation shape: {s}"
            );
            Value::String("<incarnation>".into())
        }
        Value::Array(items) => Value::Array(items.into_iter().map(normalise).collect()),
        Value::Object(map) => {
            Value::Object(map.into_iter().map(|(k, v)| (k, normalise(v))).collect())
        }
        other => other,
    }
}
fn ok(value: impl Into<Value>) -> Value {
    json!({"ok": normalise(value.into())})
}
fn failed(error: &ExchangeError) -> Value {
    json!({"error": {"class": error.class, "text": error.text, "code": error.code}})
}
fn done(result: Result<Value, ExchangeError>) -> Value {
    result.map_or_else(|e| failed(&e), ok)
}
fn announce_of(kw: &Value) -> Announce {
    let kw = kw.as_object().expect("kwargs");
    let text = |key: &str| kw.get(key).and_then(Value::as_str).map(str::to_owned);
    let object = |key: &str| kw.get(key).filter(|v| !v.is_null()).cloned();
    Announce {
        phase: text("phase").expect("phase"),
        run_id: text("run_id"),
        deployment_id: text("deployment_id"),
        model: object("model"),
        slice_mode: kw.get("slice_mode") == Some(&Value::Bool(true)),
        manifest_digest: text("manifest_digest"),
        work_limits: object("work_limits"),
        per_node: kw
            .get("per_node")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default(),
    }
}

/// Proves (in-memory half): partial B-RUN-18 (publication is fenced: the owner's announcement is one validated
/// document swapped in atomically, an idle refresh is conditional at the swap and never overwrites a live phase
/// or an identical idle model; per-node extras, work-limit override, model-source hint only while a run is
/// announced; the member-report inbox with closed validation and the owner's `received_at`; a refused
/// announcement or node extra is `demo_announcement_invalid`, a refused report `demo_report_invalid`), partial
/// B-MODEL-12 (a different model announced in the idle phase is what the next run pins).
#[test]
fn the_run_exchange_equals_the_executed_oracle_op_by_op() {
    let fx: Value = serde_json::from_str(FIXTURE).expect("fixture");
    assert_eq!(fx["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let scenarios = fx["scenarios"].as_array().expect("scenarios");
    assert_eq!(scenarios.len(), 11);
    let mut compared = 0;
    for scenario in scenarios {
        let name = scenario["name"].as_str().unwrap();
        let clock = Arc::new(Mutex::new(100.0f64));
        let read = clock.clone();
        let exchange = RunExchange::new(Box::new(move || *read.lock().unwrap()));
        let mut prepared = HashMap::new();
        for (n, (op, want)) in scenario["ops"]
            .as_array()
            .unwrap()
            .iter()
            .zip(scenario["results"].as_array().unwrap())
            .enumerate()
        {
            let label = format!("{name} op {n}: {op}");
            let got = match op["op"].as_str().unwrap() {
                "time" => {
                    *clock.lock().unwrap() = op["t"].as_f64().unwrap();
                    Value::Null
                }
                "announce" => done(
                    exchange
                        .announce(announce_of(&op["kw"]))
                        .map(|a| a.to_value()),
                ),
                "prepare" => match exchange.prepare_announcement(announce_of(&op["kw"])) {
                    Ok(p) => {
                        let shown = json!([p.document(), p.nodes()]);
                        prepared.insert(op["id"].as_str().unwrap().to_owned(), p);
                        ok(shown)
                    }
                    Err(e) => failed(&e),
                },
                "commit" => ok(exchange.commit_announcement(
                    &prepared[op["id"].as_str().unwrap()],
                    op["idle_refresh"] == true,
                )),
                "announcement" => {
                    let a = exchange.announcement(op["node"].as_str());
                    ok(json!({"keys": a.keys(), "value": a.to_value()}))
                }
                "update_node" => done(
                    exchange
                        .update_node(
                            op["node"].as_str().unwrap(),
                            op["extra"].as_object().unwrap().clone(),
                        )
                        .map(|()| Value::Null),
                ),
                "submit_local" => done(
                    exchange
                        .submit_local(op["node"].as_str().unwrap(), &op["report"])
                        .map(|()| Value::Null),
                ),
                "reports" => ok(exchange.reports()),
                "forget" => {
                    exchange.forget_reports();
                    ok(Value::Null)
                }
                "provider" => match op["mode"].as_str().unwrap() {
                    // A non-callable provider cannot be expressed: the type system refuses it.
                    "uncallable" => want.clone(),
                    "hint" => {
                        exchange.set_model_source_provider(Some(Box::new(|| {
                            Ok(Some(json!({"url": "http://127.0.0.1:1/x", "n": 1})))
                        })));
                        ok(Value::Null)
                    }
                    "none" => {
                        exchange.set_model_source_provider(Some(Box::new(|| Ok(None))));
                        ok(Value::Null)
                    }
                    // `1 / 0` and a NaN inside the hint (`allow_nan=False`): the exchange publishes no hint.
                    "raises" | "nan" => {
                        exchange.set_model_source_provider(Some(Box::new(|| Err(()))));
                        ok(Value::Null)
                    }
                    "clear" => {
                        exchange.set_model_source_provider(None);
                        ok(Value::Null)
                    }
                    other => panic!("unknown provider mode {other}"),
                },
                "validate" => done(validate_report(&op["report"])),
                other => panic!("unknown op {other}"),
            };
            if want.is_null() {
                assert!(got.is_null(), "{label}: {got}");
            } else {
                assert_eq!(*want, got, "{label}");
            }
            compared += 1;
        }
    }
    assert_eq!(compared, 122);
}
