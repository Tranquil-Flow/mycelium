// SPDX-License-Identifier: AGPL-3.0-only
//! Refusal status/headers/body bytes equal what the pinned v0.1.4 seed and managed-control
//! origin handlers sent (recorded by `round6-http-refusal-probe.py`, which executes those
//! handlers on loopback with fake coordinator/mailbox raising the oracle's exception types).
use protocol::http_refusal as h;
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("../fixtures/round6-http-refusal-vectors.json")).unwrap()
}
fn expect(got: &h::Refusal, want: &Value, what: &str) {
    assert_eq!(u64::from(got.status), want[0].as_u64().unwrap(), "{what}");
    assert_eq!(
        String::from_utf8(got.body.clone()).unwrap(),
        want[1].as_str().unwrap(),
        "{what}"
    );
}
fn template(v: &Value, surface: &str, len: usize, drop_server: bool) -> Vec<(String, String)> {
    v["header_templates"][surface]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| {
            (
                p[0].as_str().unwrap().to_owned(),
                p[1].as_str().unwrap().replace("<len>", &len.to_string()),
            )
        })
        .filter(|(k, _)| !(drop_server && k == "Server"))
        .collect()
}

#[test]
fn managed_control_origin_refusals_match_the_executed_handler() {
    let v = vectors();
    let codes = v["codes"].as_array().unwrap();
    assert!(codes.len() > 200);
    for code in codes {
        let code = code.as_str().unwrap();
        expect(&h::managed_control_origin(code), &v["origin"][code], code);
    }
    expect(
        &h::managed_control_origin_internal_error(),
        &v["origin_internal"],
        "internal",
    );
    let sample = h::managed_control_origin("managed_control_signature_invalid");
    let want = template(&v, "origin", sample.body.len(), true);
    let got: Vec<(String, String)> = h::managed_control_origin_headers(sample.body.len())
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect();
    // The stdlib `Server: BaseHTTP/0.6 Python/..` banner is a Python-stack artefact, not contract.
    assert_eq!(got, want);
}

#[test]
fn seed_refusals_match_the_executed_handler_on_every_route_and_exception_type() {
    let v = vectors();
    let codes = v["codes"].as_array().unwrap();
    for class in ["membership", "invite", "coordinator"] {
        for code in codes {
            let code = code.as_str().unwrap();
            let what = format!("{class} {code}");
            expect(
                &h::seed_join(code),
                &v["seed"][class]["/seed/join"][code],
                &what,
            );
            expect(
                &h::seed_message(code),
                &v["seed"][class]["/seed/resume"][code],
                &what,
            );
            expect(
                &h::seed_message(code),
                &v["seed"][class]["/seed/message"][code],
                &what,
            );
        }
    }
    for route in ["/seed/join", "/seed/resume", "/seed/message"] {
        expect(
            &h::seed_internal_error(),
            &v["seed"]["unlisted"][route],
            route,
        );
    }
    let sample = h::seed_message("membership_signature_invalid");
    let want = template(&v, "seed", sample.body.len(), false);
    let got: Vec<(String, String)> = h::seed_headers(sample.body.len())
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect();
    assert_eq!(got, want);
}
