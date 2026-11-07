// SPDX-License-Identifier: AGPL-3.0-only
//! `api::registry::Registry` + the thin host rules around it against the EXECUTED `RequestGatewayService`
//! (FX-L10-GATEWAY-ADMISSION: `L10-evidence/r13-probe-gateway.py` -> `fixtures/oracle_gateway_admission.json`,
//! v0.1.4 f4e366e9, Python 3.14): 15 scripted multi-request scenarios, 11 of them replayable against the pure
//! registry (the four that depend on the qualification gate - `qualification_per_submit` and the three
//! `revalidation_*` ones - and the executor-bound backend-cancel lists stay host-level facts, see
//! `round13-seams.md`). The "host" in this file is the minimal one the oracle implies:
//! a pending-slot counter taken before the registry is asked for room and released when a worker ends, the
//! request id drawn only once the registry has room, and a worker that finishes a request on a `terminal` op.
//! Every result, error code, id draw, presence of sessions, subscribe generation and replayed event list must
//! be the oracle's.
use api::registry::{AdmissionError, Config, LastEventId, Pull, Registry, Subscription};
use serde_json::{Value, json};
use std::collections::HashMap;

struct Host {
    registry: Registry,
    ids: Vec<String>,
    drawn: usize,
    pending: usize,
    max_pending: usize,
    now: f64,
    subs: HashMap<String, Subscription>,
    modes: HashMap<String, String>,
    cancelled: Vec<String>,
}

impl Host {
    fn submit(&mut self, token: Option<&str>) -> Result<String, &'static str> {
        // service.py:440-506 in order: token bound, (qualification gate: not modelled here), pending slot,
        // closed / room, THEN the id source, then validity and uniqueness of that id.
        if token.is_some_and(|t| !(32..=256).contains(&t.len())) {
            return Err("invalid_session_token");
        }
        if self.pending >= self.max_pending {
            return Err("gateway_capacity_exhausted");
        }
        let id = self.ids.get(self.drawn).cloned().unwrap_or_default();
        let mut cancelled = Vec::new();
        match self.registry.submit(&id, token, self.now, &mut cancelled) {
            Ok(()) => {
                self.drawn += 1;
                self.pending += 1;
                Ok(id)
            }
            // Refusals that precede the id source leave it untouched; the rest consumed one draw.
            Err(AdmissionError(
                code @ ("gateway_capacity_exhausted" | "gateway_closed" | "invalid_session_token"),
            )) => Err(code),
            Err(AdmissionError(code)) => {
                self.drawn += 1;
                Err(code)
            }
        }
    }

    /// The worker ends: tokens for a `fast` request, then the one terminal (`cancelled` if a cancel won
    /// while it ran); the slot is released.
    fn finish(&mut self, rid: &str) {
        let now = self.now;
        let mode = self.modes.get(rid).map_or("fast", String::as_str);
        if mode == "fast" || mode == "hold_after_tokens" {
            for (i, text) in ["a", "b"].iter().enumerate() {
                self.registry
                    .append(rid, "token", json!({"token_index": i, "text": text}), now)
                    .unwrap();
            }
        }
        let kind = if self.cancelled.iter().any(|r| r == rid) {
            "cancelled"
        } else {
            "completed"
        };
        self.registry
            .append_terminal(rid, kind, Value::Null, now)
            .unwrap();
        self.registry.mark_worker_done(rid).unwrap();
        self.pending -= 1;
    }

    fn drain(&mut self, key: &str, limit: usize) -> Option<Vec<Value>> {
        let mut sub = self.subs.remove(key)?;
        let mut events = Vec::new();
        while events.len() < limit {
            match self.registry.next_event(&mut sub).unwrap() {
                Pull::Event(e) => {
                    let get = |name: &str| e.payload.get(name).cloned().unwrap_or(Value::Null);
                    events.push(json!([
                        e.sequence,
                        e.kind,
                        get("token_index"),
                        get("text"),
                        get("code"),
                        Value::Null
                    ]));
                    self.registry.ack(&mut sub, e.sequence, self.now).unwrap();
                    if matches!(e.kind.as_str(), "completed" | "failed" | "cancelled") {
                        break;
                    }
                }
                Pull::End | Pull::Pending => break,
            }
        }
        self.subs.insert(key.to_owned(), sub);
        Some(events)
    }
}

fn value_of(r: &Result<String, &'static str>) -> (Option<String>, Option<String>) {
    match r {
        Ok(id) => (Some(id.clone()), None),
        Err(code) => (None, Some((*code).to_owned())),
    }
}

#[test]
fn registry_and_host_rules_equal_the_executed_gateway() {
    let doc: Value =
        serde_json::from_str(include_str!("fixtures/oracle_gateway_admission.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let grace = doc["grace"].as_f64().unwrap();
    let replayable = [
        "evict_terminal_oldest_first",
        "terminal_with_active_subscription_pins",
        "never_subscribed_grace",
        "abandoned_subscriber_no_grace",
        "active_subscription_pins_held",
        "duplicate_invalid_ids_and_slots",
        "slot_released_after_worker",
        "owner_tokens",
        "resume_and_eviction",
        "submit_storm_max_one",
        "close_cancels_and_refuses",
    ];
    let (mut ran, mut ops_run, mut failures) = (0usize, 0usize, Vec::new());
    for scenario in doc["scenarios"].as_array().unwrap() {
        let label = scenario["label"].as_str().unwrap();
        if !replayable.contains(&label) {
            continue;
        }
        ran += 1;
        let config = &scenario["config"];
        let sessions = config["max_sessions"].as_u64().unwrap() as usize;
        let mut host = Host {
            registry: Registry::new(Config::new(64, sessions, grace).unwrap()),
            ids: scenario["ids"]
                .as_array()
                .unwrap()
                .iter()
                .map(|i| i.as_str().unwrap().to_owned())
                .collect(),
            drawn: 0,
            pending: 0,
            max_pending: config
                .get("max_pending_requests")
                .and_then(Value::as_u64)
                .unwrap_or(64) as usize,
            now: 0.0,
            subs: HashMap::new(),
            modes: scenario["modes"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
                .collect(),
            cancelled: Vec::new(),
        };
        let mut tracked: Vec<String> = Vec::new();
        for (index, op) in scenario["ops"].as_array().unwrap().iter().enumerate() {
            ops_run += 1;
            let kind = op["op"].as_str().unwrap();
            let mut problems = Vec::new();
            let token = op.get("token").and_then(Value::as_str);
            match kind {
                "submit" => {
                    let got = host.submit(token);
                    if let Ok(id) = &got {
                        tracked.push(id.clone());
                    }
                    let (id, code) = value_of(&got);
                    let want_id = op.get("result").and_then(Value::as_str).map(str::to_owned);
                    let want_code = op
                        .get("error")
                        .map(|e| e["code"].as_str().unwrap().to_owned());
                    if (id, code) != (want_id, want_code) {
                        problems.push(format!(
                            "submit {got:?} != {} / {}",
                            op["result"], op["error"]
                        ));
                    }
                    if host.drawn as u64 != op["ids_drawn"].as_u64().unwrap() {
                        problems.push(format!("ids drawn {} != {}", host.drawn, op["ids_drawn"]));
                    }
                }
                "terminal" => host.finish(op["rid"].as_str().unwrap()),
                "release" => {}
                "set_time" => host.now = op["offset"].as_f64().unwrap(),
                "present" => {
                    for (rid, want) in op["present"].as_object().unwrap() {
                        let got = host.registry.buffered_event_count(rid).is_ok();
                        if Some(got) != want.as_bool() {
                            problems.push(format!("present {rid}: {got} != {want}"));
                        }
                    }
                }
                "subscribe" => {
                    let rid = op["rid"].as_str().unwrap();
                    let key = op["key"].as_str().unwrap().to_owned();
                    let last = match op.get("last") {
                        Some(Value::Array(a)) => Some(LastEventId::Bound(
                            a[0].as_i64().unwrap(),
                            a[1].as_i64().unwrap(),
                        )),
                        Some(Value::Number(n)) => Some(LastEventId::Cursor(n.as_i64().unwrap())),
                        _ => None,
                    };
                    let got =
                        host.registry
                            .subscribe(rid, last, token, host.now, &mut |_, _, _| Ok(true));
                    match (got, op.get("result"), op.get("error")) {
                        (Ok(sub), Some(want), None) => {
                            if json!({"publisher_generation": sub.publisher_generation}) != *want {
                                problems.push(format!(
                                    "generation {} != {want}",
                                    sub.publisher_generation
                                ));
                            }
                            host.subs.insert(key, sub);
                        }
                        (Err(AdmissionError(code)), None, Some(want)) if want["code"] == code => {}
                        (got, w, e) => problems.push(format!("subscribe {got:?} vs {w:?} / {e:?}")),
                    }
                }
                "drain" => {
                    let want = op["events"].as_array();
                    let got = host.drain(op["key"].as_str().unwrap(), want.map_or(0, Vec::len));
                    if got.map(Value::Array) != want.cloned().map(Value::Array) {
                        problems.push(format!("events differ from {}", op["events"]));
                    }
                }
                "close_subscription" => {
                    let key = op["key"].as_str().unwrap();
                    if let Some(mut sub) = host.subs.remove(key) {
                        host.registry.close_subscription(&mut sub).unwrap();
                    } else if op.get("skipped").is_none() {
                        problems.push(format!("no subscription {key}"));
                    }
                }
                "cancel" => {
                    let got = host.registry.cancel(op["rid"].as_str().unwrap(), token);
                    match (got, op.get("result"), op.get("error")) {
                        (Ok(v), Some(want), None) if want.as_bool() == Some(v) => {
                            if v {
                                host.cancelled.push(op["rid"].as_str().unwrap().to_owned());
                            }
                        }
                        (Err(AdmissionError(code)), None, Some(want)) if want["code"] == code => {}
                        (got, w, e) => problems.push(format!("cancel {got:?} vs {w:?} / {e:?}")),
                    }
                }
                "close" => {
                    // service.py:758-780: no more admissions, and every non-terminal session is cancelled.
                    host.registry.close();
                    for rid in tracked.clone() {
                        if host.registry.cancel(&rid, None) == Ok(true) {
                            host.cancelled.push(rid);
                        }
                    }
                }
                // facts about other layers (qualification source, binding fields): nothing to drive here
                "source" | "binding_field" | "note" => {}
                other => panic!("unmodelled op {other}"),
            }
            if !problems.is_empty() {
                failures.push(format!(
                    "{label}#{index} ({kind}): {}",
                    problems.join(" | ")
                ));
            }
        }
    }
    assert_eq!(ran, replayable.len(), "scenarios replayed");
    assert!(ops_run >= 100, "{ops_run} ops");
    assert!(
        failures.is_empty(),
        "{} ops differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
