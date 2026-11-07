// SPDX-License-Identifier: AGPL-3.0-only
//! The chat edge's v0.1.4 rules (`mycelium_a13_macos/chat.py:39-44,285-313` and the gateway codec
//! `mycelium_live/codec.py:159-173`, f4e366e9), pure functions. Expected values are EXECUTED oracle
//! output: FX-L11-chat-edge (`fixtures/chat-edge-oracle.json`, probe `chat_edge_probe.py`) and its
//! casefold/whitespace tables (`fixtures/chat-edge-casefold-oracle.json`, `casefold_ascii_probe.py`).
use node_service::chat_edge::{self, RateWindow};
use serde_json::Value;

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/chat-edge-oracle.json")).unwrap()
}
fn tables() -> Value {
    serde_json::from_str(include_str!("fixtures/chat-edge-casefold-oracle.json")).unwrap()
}
/// What the L10 chat boundary hands the service: an empty body is `{}`.
fn parsed(raw: &str) -> Value {
    serde_json::from_str(if raw.is_empty() { "{}" } else { raw }).unwrap()
}

#[test]
fn every_executed_send_case_gets_the_oracles_status_code_and_effective_request() {
    let fixture = fixture();
    let cases = fixture["send_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 37);
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let outcome = chat_edge::plan(&parsed(case["raw"].as_str().unwrap()));
        let status = case["status"].as_u64().unwrap();
        if status == 200 {
            let plan = outcome.unwrap_or_else(|e| panic!("{name}: refused {e:?}"));
            let seen = &case["client_saw"][0];
            assert_eq!(
                plan.max_new_tokens, case["body"]["max_new_tokens"],
                "{name}"
            );
            assert_eq!(plan.max_new_tokens, seen["max_new_tokens"], "{name}");
            assert_eq!(plan.clamped, case["body"]["clamped"], "{name}");
            assert_eq!(plan.prompt, seen["prompt"].as_str().unwrap(), "{name}");
        } else {
            let error = outcome.err().unwrap_or_else(|| panic!("{name}: accepted"));
            assert_eq!(u64::from(error.status), status, "{name}");
            assert_eq!(
                error.code,
                case["body"]["error"].as_str().unwrap(),
                "{name}"
            );
            assert!(case["client_saw"].as_array().unwrap().is_empty(), "{name}");
        }
    }
}

#[test]
fn the_limits_are_the_oracles_constants_and_state_document() {
    let fixture = fixture();
    let constants = &fixture["limits_constants"];
    assert_eq!(chat_edge::MAX_PROMPT_CHARS, constants["MAX_PROMPT_CHARS"]);
    assert_eq!(
        chat_edge::MAX_NEW_TOKENS_CAP,
        constants["MAX_NEW_TOKENS_CAP"]
    );
    assert_eq!(
        chat_edge::DEFAULT_NEW_TOKENS,
        constants["DEFAULT_NEW_TOKENS"]
    );
    assert_eq!(
        chat_edge::PER_CLIENT_PER_MINUTE,
        constants["PER_CLIENT_PER_MINUTE"]
    );
    assert_eq!(chat_edge::GLOBAL_PER_MINUTE, constants["GLOBAL_PER_MINUTE"]);
    assert_eq!(chat_edge::limits_json(), fixture["state_limits"]);
}

#[test]
fn policy_response_equals_the_executed_codec_for_every_probed_prompt() {
    let fixture = fixture();
    let policy = fixture["policy"].as_array().unwrap();
    assert_eq!(policy.len(), 25);
    for case in policy {
        let prompt = case["prompt"].as_str().unwrap();
        assert_eq!(
            chat_edge::policy_response(prompt),
            case["response"].as_str(),
            "{prompt:?}"
        );
    }
    assert_eq!(
        fixture["policy_stream"]["emit_token"][1],
        chat_edge::POLICY_REFUSAL
    );
}

#[test]
fn casefold_agrees_with_pythons_on_every_scalar_that_can_reach_an_ascii_marker() {
    let tables = tables();
    let listed = tables["ascii_folds"].as_array().unwrap();
    assert_eq!(listed.len(), 19);
    let mut known = std::collections::BTreeSet::new();
    for row in listed {
        let c = char::from_u32(row[0].as_u64().unwrap().try_into().unwrap()).unwrap();
        assert_eq!(chat_edge::fold(&c.to_string()), row[1].as_str().unwrap());
        known.insert(c);
    }
    for c in ('\0'..=char::MAX).filter(|c| !known.contains(c)) {
        let folded = chat_edge::fold(&c.to_string());
        if c.is_ascii() {
            assert_eq!(folded, c.to_ascii_lowercase().to_string());
        } else {
            assert!(
                !folded.chars().any(|f| f.is_ascii_alphabetic()),
                "{c:?} folds to {folded:?}, which the oracle's casefold does not"
            );
        }
    }
}

#[test]
fn blank_means_pythons_isspace_not_rusts_white_space() {
    let tables = tables();
    let python: std::collections::BTreeSet<u32> = tables["isspace"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| u32::try_from(v.as_u64().unwrap()).unwrap())
        .collect();
    assert_eq!(python.len(), 29);
    for c in '\0'..=char::MAX {
        assert_eq!(
            chat_edge::is_py_space(c),
            python.contains(&(c as u32)),
            "{:#x}",
            c as u32
        );
    }
}

fn status_of(window: &mut RateWindow, client: &str, now: f64) -> (u16, Option<&'static str>) {
    match window.admit(client, now) {
        Ok(()) => (200, None),
        Err(e) => (e.status, Some(e.code)),
    }
}

#[test]
fn the_rate_window_replays_the_executed_sequence() {
    let fixture = fixture();
    let rate = fixture["rate"].as_array().unwrap();
    let mut window = RateWindow::default();
    // The oracle's steps: six sends at t+0, one at t+59.9, one at t+60.0 (the first four expire).
    let times = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 59.9, 60.0];
    assert_eq!(rate.len(), times.len());
    for (step, now) in rate.iter().zip(times) {
        let (status, code) = status_of(&mut window, "local", 1000.0 + now);
        assert_eq!(
            u64::from(status),
            step["status"].as_u64().unwrap(),
            "{}",
            step["step"]
        );
        assert_eq!(code, step["body"]["error"].as_str(), "{}", step["step"]);
    }
}

#[test]
fn only_accepted_requests_are_charged_and_a_refusal_costs_nothing() {
    let fixture = fixture();
    let mut window = RateWindow::default();
    // The oracle: six invalid sends, then five valid -> the fifth valid one is limited. Invalid
    // requests never reach the window (the edge validates first), so only the valid ones count.
    let valid = fixture["rate_charged_only_when_accepted"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|s| s["step"].as_str().unwrap().starts_with("valid"))
        .collect::<Vec<_>>();
    assert_eq!(valid.len(), 5);
    for step in valid {
        let (status, code) = status_of(&mut window, "local", 0.0);
        assert_eq!(u64::from(status), step["status"].as_u64().unwrap());
        assert_eq!(code, step["body"]["error"].as_str());
    }
}

#[test]
fn the_swarm_wide_limit_replays_the_executed_sequence() {
    let fixture = fixture();
    let swarm = fixture["swarm"].as_array().unwrap();
    assert_eq!(swarm.len(), 32);
    let mut window = RateWindow::default();
    for step in swarm {
        let client = format!("client{}", step["client"]);
        let (status, code) = status_of(&mut window, &client, 0.0);
        assert_eq!(
            u64::from(status),
            step["status"].as_u64().unwrap(),
            "{client}"
        );
        assert_eq!(code, step["error"].as_str(), "{client}");
    }
    // After the window the swarm budget is back (the oracle accepted client0 at t+60).
    assert_eq!(status_of(&mut window, "client0", 60.0).0, 200);
}
