// SPDX-License-Identifier: AGPL-3.0-only
//! Additional unchanged L3 observation, real Rust HTTP and shipped asset bytes.
//! This is an asset/default observation, not a browser or complete L3 journey.
use api::{
    desktop::{Desktop, Service, Session},
    http::serve_connection,
    schema::PublicError,
    status::PublishedStatus,
};
use budgets::BudgetId;
use journey_tests::{compare, duration, http::Client};
use serde_json::{Map, Value, json};
use std::{
    net::{Ipv4Addr, TcpListener},
    sync::Arc,
    thread,
    time::Instant,
};
struct NoActions;
impl Service for NoActions {
    fn action(&self, _: &str, _: &Map<String, Value>) -> Result<Value, PublicError> {
        panic!("asset observation cannot execute an action")
    }
}
#[test]
fn unchanged_onboarding_default_observation_over_real_http() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let host = listener.local_addr().unwrap().to_string();
    let origin = format!("http://{host}");
    let app = Desktop::new(
        host,
        Arc::new(NoActions),
        Arc::new(PublishedStatus::new()),
        Session::new(
            "unused-launch".into(),
            "unused-session".into(),
            Instant::now() + duration(BudgetId::ModelSelection).unwrap(),
            duration(BudgetId::ModelSelection).unwrap(),
        )
        .unwrap(),
    );
    let job = thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        serve_connection(
            stream,
            duration(BudgetId::ModelSelection).unwrap(),
            |r| app.preflight(r),
            |r| app.handle(r),
        )
        .unwrap();
    });
    let response = Client::new(&origin)
        .unwrap()
        .request("GET", "/", b"", &[], false, BudgetId::ModelSelection)
        .unwrap();
    job.join().unwrap();
    assert_eq!(response.status, 200);
    let html = response.text().unwrap();
    // The same selected-option observation as L3 scenario.rs:148-189.
    let select = html
        .split_once("id=\"storage\"")
        .unwrap()
        .1
        .split_once("</select>")
        .unwrap()
        .0;
    let selected = select
        .split("<option ")
        .skip(1)
        .map(|o| o.split_once('>').unwrap().0)
        .find(|a| a.contains(" selected"))
        .unwrap();
    let bytes: u64 = selected
        .split_once("value=\"")
        .unwrap()
        .1
        .split('"')
        .next()
        .unwrap()
        .parse()
        .unwrap();
    let fields = json!({"fresh_storage_bytes":bytes,"ten_minute_option_present":html.contains("<option value=\"600\">10 minutes</option>"),"relay_unchecked":html.contains("id=\"create-with-relay\">")});
    let golden: Value = serde_json::from_str(include_str!(
        "../../../test-support/journeys/fixtures/python-v014.json"
    ))
    .unwrap();
    let expected = golden["observations"]
        .as_array()
        .unwrap()
        .iter()
        .find(|o| o["id"] == "onboarding_default_fields")
        .unwrap();
    let mut observed = expected.clone();
    observed["fields"] = fields;
    observed["status"] = json!(response.status);
    compare(&json!([expected]), &json!([observed])).unwrap();
    eprintln!(
        "L3_ADDITIONAL_OBSERVATION=onboarding_default_fields; REAL_HTTP=true; FULL_L3=NOT_RUN; BROWSER=NOT_RUN"
    );
}
