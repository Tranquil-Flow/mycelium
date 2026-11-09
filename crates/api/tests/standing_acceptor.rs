// SPDX-License-Identifier: AGPL-3.0-only
//! A joined Mac's automatic work acceptance under its standing consent, against EXECUTED v0.1.4
//! (FX-B18-STANDING-ACCEPTOR: probe `probe_standing_acceptor.py` runs the REAL
//! `standing_consent.StandingAcceptor` with its real `standing_limits` / `check_work_preview` /
//! `standing_admission` over a REAL granted+bound standing-consent file, a scripted pending offer,
//! scripted preview/accept/report collaborators and a scripted clock; two executions byte-identical).
//! Compared per tick: the outcome, `status().last`, and every collaborator call with its limits/policy.
use api::preferences::standing_consent::{self, AcceptFailure, StandingAcceptor, WorkAcceptance};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::{Arc, Mutex},
};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_standing_acceptor.json"))
        .expect("fixture parses")
}

#[derive(Default)]
struct Member {
    t: f64,
    offer: Option<Value>,
    preview: Value,
    preview_raise: Option<AcceptFailure>,
    accept_raise: Option<AcceptFailure>,
    report_raise: Option<AcceptFailure>,
    node: String,
    calls: Vec<Value>,
}
impl WorkAcceptance for Member {
    fn pending_offer(&mut self) -> Result<Option<Value>, AcceptFailure> {
        Ok(self.offer.clone())
    }
    fn report(&mut self, purpose: &str, offer: &Value) -> Result<(), AcceptFailure> {
        self.calls
            .push(json!(["report", purpose, offer["offer_id"]]));
        self.report_raise.clone().map_or(Ok(()), Err)
    }
    fn preview(
        &mut self,
        offer: &Value,
        limits: &Value,
        policy: &Value,
    ) -> Result<Value, AcceptFailure> {
        self.calls
            .push(json!(["preview", offer["offer_id"], limits, policy]));
        if let Some(failure) = self.preview_raise.clone() {
            return Err(failure);
        }
        let mut doc = json!({"protocol": "mycelium.a13.work_preview.v1", "route_ready": false, "limits": limits,
            "consent_digest": format!("sha256:{}", "66".repeat(32)), "node_id": self.node,
            "resource_policy": policy});
        for (key, value) in self.preview.as_object().into_iter().flatten() {
            if key == "limits" {
                for (k, v) in value.as_object().expect("limits") {
                    doc["limits"][k] = v.clone();
                }
            } else {
                doc[key] = value.clone();
            }
        }
        Ok(doc)
    }
    fn accept(
        &mut self,
        preview: &Value,
        limits: &Value,
        _policy: &Value,
    ) -> Result<(), AcceptFailure> {
        self.calls
            .push(json!(["accept", preview["node_id"], limits]));
        self.accept_raise.clone().map_or(Ok(()), Err)
    }
    fn clock(&mut self) -> f64 {
        self.t
    }
}

fn failure(value: &Value) -> Option<AcceptFailure> {
    if value.is_null() {
        return None;
    }
    let text = value["code"].as_str().or(value["text"].as_str());
    Some(AcceptFailure {
        public: value["public"].as_str().map(str::to_owned),
        code: text.map(str::to_owned),
        ..AcceptFailure::default()
    })
}

fn run(fx: &Value, scenario: &Value) {
    let name = scenario["name"].as_str().expect("name");
    let root: PathBuf =
        std::env::temp_dir().join(format!("b18-acceptor-{}-{name}", std::process::id()));
    fs::remove_dir_all(&root).unwrap_or_default();
    fs::create_dir_all(&root).expect("root");
    let grant = scenario["grant"].as_str();
    if grant.is_some() {
        standing_consent::grant(
            &root,
            &fx["grant"],
            &fx["policy"],
            900.0,
            Some(16_000_000_000),
        )
        .expect("grant");
        if grant != Some("unbound") {
            standing_consent::bind(&root, &fx["binding"], 901.0).expect("bind");
        }
        if grant == Some("withdrawn") {
            standing_consent::withdraw(&root, 902.0).expect("withdraw");
        }
    }
    let required: Arc<Mutex<Option<Value>>> = Arc::default();
    let shared = required.clone();
    let mut acceptor = StandingAcceptor::new(
        root.clone(),
        Some(Box::new(move |_offer: &Value| {
            shared.lock().expect("lock").clone()
        })),
    );
    let mut member = Member {
        t: 1000.0,
        node: fx["binding"]["node_id"].as_str().expect("node").into(),
        preview: json!({}),
        ..Member::default()
    };
    let mut binding = Some(fx["binding"].clone());
    let mut violation: Option<String> = None;
    let mut ticks = scenario["log"].as_array().expect("log").iter();
    for (step, op) in scenario["ops"].as_array().expect("ops").iter().enumerate() {
        if op["op"] == "set" {
            let value = &op["value"];
            match op["key"].as_str().expect("key") {
                "offer" => member.offer = Some(value.clone()),
                "binding" => binding = (!value.is_null()).then(|| value.clone()),
                "violation" => violation = value.as_str().map(str::to_owned),
                "required" => {
                    *required.lock().expect("lock") = (!value.is_null()).then(|| value.clone())
                }
                "preview" => member.preview = value.clone(),
                "preview_raise" => member.preview_raise = failure(value),
                "accept_raise" => member.accept_raise = failure(value),
                "report_raise" => member.report_raise = failure(value),
                other => panic!("unknown key {other}"),
            }
            continue;
        }
        assert_eq!(op["op"], "tick");
        let want = ticks.next().expect("a recorded tick");
        let at = format!("{name} op {step}");
        let before = member.calls.len();
        let mut check = || violation.clone();
        let result = acceptor
            .tick(&mut member, &fx["policy"], binding.as_ref(), &mut check)
            .expect("tick");
        assert_eq!(
            result.unwrap_or(Value::Null),
            want["result"],
            "{at}: outcome"
        );
        assert_eq!(
            acceptor.status().expect("status")["last"],
            want["last"],
            "{at}: last"
        );
        assert_eq!(json!(member.calls[before..]), want["calls"], "{at}: calls");
    }
    assert!(
        ticks.next().is_none(),
        "{name}: every recorded tick is replayed"
    );
    fs::remove_dir_all(&root).unwrap_or_default();
}

/// Proves: B-JOIN-09 (a joined Mac accepts each owner work offer once, automatically, within the standing caps
/// its person granted: refusals are named and recorded, transient ones (membership, grant state, resource gates)
/// are retried, a share its caps cannot hold or an expired offer is refused before any preview, and a failed
/// preview/accept/report is recorded by its public code).
#[test]
fn the_standing_acceptor_equals_the_executed_oracle() {
    let fx = fixture();
    let scenarios = fx["scenarios"].as_array().expect("scenarios");
    assert_eq!(scenarios.len(), 10, "every recorded scenario is replayed");
    let ticks: usize = scenarios
        .iter()
        .map(|s| s["log"].as_array().map_or(0, Vec::len))
        .sum();
    assert_eq!(ticks, 42, "every recorded tick is replayed");
    for scenario in scenarios {
        run(&fx, scenario);
    }
}
