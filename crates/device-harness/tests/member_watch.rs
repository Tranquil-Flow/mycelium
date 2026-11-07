// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Fault, FaultKind, Host, Result,
    keeper::Alert,
    member_watch::*,
    observe::{Mailbox, Metrics, Observer},
};
use serde_json::{Value, json};
use std::collections::BTreeSet;
fn expected() -> BTreeSet<String> {
    ["fixture-a".into(), "fixture-b".into()].into()
}
fn status(age: Value) -> Value {
    json!({"authority":{"members":[{"node_id":"fixture-a","clock":{"age_seconds":300,"fresh":true}},
    {"node_id":"fixture-b","clock":{"age_seconds":age,"fresh":true}}]}})
}
#[test]
fn actual_authority_member_clock_shape_boundary_and_missing_data_are_observed() {
    assert!(
        inspect(&status(json!(300)), &expected(), Span::from_millis(300_000))
            .unwrap()
            .is_empty()
    );
    let a = inspect(
        &status(json!(300.001)),
        &expected(),
        Span::from_millis(300_000),
    )
    .unwrap();
    assert_eq!(
        a,
        vec![Alert {
            code: "member_silent",
            peer: Some("fixture-b".into())
        }]
    );
    for age in [
        Value::Null,
        json!(-1),
        json!("1565"),
        json!(true),
        json!(1e100),
    ] {
        assert_eq!(
            inspect(&status(age), &expected(), Span::from_millis(300_000)).unwrap()[0].code,
            "member_clock_unobserved"
        );
    }
    assert_eq!(
        inspect(&json!({}), &expected(), Span::from_millis(300_000))
            .unwrap()
            .len(),
        2
    );
    let mut duplicate = status(json!(0));
    duplicate["authority"]["members"][1]["node_id"] = json!("fixture-a");
    assert_eq!(
        inspect(&duplicate, &expected(), Span::from_millis(300_000))
            .unwrap_err()
            .code,
        "member_clock_duplicate"
    );
    assert!(
        inspect(
            &status(json!(0)),
            &BTreeSet::new(),
            Span::from_millis(300_000)
        )
        .is_err()
    );
    assert!(inspect(&status(json!(0)), &expected(), Span::ZERO).is_err());
}
struct Source;
impl Observer for Source {
    fn status(&mut self, host: Host) -> Result<(u16, Value)> {
        assert_eq!(host, Host::Owner);
        Ok((200, status(json!(1565))))
    }
    fn metrics(&mut self, _: Host) -> Result<Metrics> {
        Err(Fault::new(
            "fixture_metrics_unavailable",
            FaultKind::Observation,
        ))
    }
    fn mailbox(&mut self, _: Host) -> Result<Mailbox> {
        Err(Fault::new(
            "fixture_mailbox_unavailable",
            FaultKind::Observation,
        ))
    }
}
#[derive(Default)]
struct Sink(Vec<Alert>);
impl Alerts for Sink {
    fn alert(&mut self, a: Alert) -> Result<()> {
        self.0.push(a);
        Ok(())
    }
}
#[test]
fn device_observer_emits_silence_alert_without_mutating_status_or_member() {
    let mut o = Watching {
        inner: Source,
        alerts: Sink::default(),
        expected: expected(),
        silence: Span::from_millis(300_000),
    };
    let (http, value) = o.status(Host::Owner).unwrap();
    assert_eq!(http, 200);
    assert_eq!(value, status(json!(1565)));
    assert_eq!(
        o.alerts.0,
        vec![Alert {
            code: "member_silent",
            peer: Some("fixture-b".into())
        }]
    );
    assert_eq!(
        o.metrics(Host::Member).unwrap_err().code,
        "fixture_metrics_unavailable"
    );
    assert_eq!(
        o.mailbox(Host::Member).unwrap_err().code,
        "fixture_mailbox_unavailable"
    );
}
