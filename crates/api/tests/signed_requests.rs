// SPDX-License-Identifier: AGPL-3.0-only
//! Real L5 signatures -> L9 session -> L10 HTTP service. No inference or remote
//! processes: clock/currentness, tokenizer and local-residue effects are controlled.
use api::{
    requests::Service,
    signed_requests::{Admission, Authority, SignedService},
};
use protocol::{ContractError, token::*};
use serde_json::{Value, json};
use std::sync::Arc;
struct Current;
impl Authority for Current {
    fn observe(&self) -> Result<(f64, bool), ContractError> {
        Ok((10.0, true))
    }
}
fn fixture() -> (SignedService, Value, Value) {
    named_fixture("DECODING-2")
}
fn named_fixture(name: &str) -> (SignedService, Value, Value) {
    let (input, receipt) = admission(name);
    let submission = input.submission.clone();
    (
        SignedService::new(input, Arc::new(Current)).unwrap(),
        submission,
        receipt,
    )
}
fn admission(name: &str) -> (Admission, Value) {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-token-vectors.json"
    ))
    .unwrap();
    let row = rows.iter().find(|r| r["name"] == name).unwrap();
    let c = &row["context"]["grant_context"];
    let grant = GrantRegistry::new(1)
        .unwrap()
        .admit(
            &row["context"]["grant"],
            &GrantExpectation {
                scope: &c["expected_scope"],
                owner_key: &c["owner_key"],
                max_new_tokens: c["max_new_tokens"].as_u64().unwrap(),
                stop_token_ids: &c["stop_token_ids"],
                window: GrantWindow {
                    now_unix_ms: c["now_unix_ms"].as_u64().unwrap(),
                    now_monotonic_s: c["now_monotonic_s"].as_f64().unwrap(),
                    process_deadline_monotonic_s: c["process_deadline_monotonic_s"]
                        .as_f64()
                        .unwrap(),
                    lease_deadline_monotonic_s: c["lease_deadline_monotonic_s"].as_f64().unwrap(),
                    clock_uncertainty_ms: c["clock_uncertainty_ms"].as_u64().unwrap(),
                },
            },
        )
        .unwrap();
    let scope = &c["expected_scope"];
    let mut subject = scope["control"].clone();
    subject["request_id"] = scope["request_id"].clone();
    subject["cancellation_generation"] = json!(1);
    let submission = json!({"protocol":"mycelium.request_gateway.v2","prompt":"private input", "max_new_tokens":4,"workload_profile_id":"interactive-1","qos_class":"interactive", "qualification": {
        "qualification_id":"q-1","qualification_digest":scope["control"]["qualification_digest"],"deployment_id":scope["control"]["deployment_id"],"deployment_epoch":scope["control"]["deployment_epoch"],"topology_version":scope["control"]["topology_generation"],"model_id":"model-1","resolved_commit":"commit-1","manifest_digest":scope["model_digest"],"path_manifest_digest":scope["control"]["path_digest"],"stage_load_proof_digests":[]}});
    (
        Admission {
            submission,
            grant,
            entry_key: row["context"]["entry_key"].clone(),
            cleanup_subject: subject,
            bearer_token: "bearer-fixture".into(),
            owner_token: "s".repeat(32),
        },
        row["envelope"].clone(),
    )
}
#[test]
fn v1_has_no_lifecycle_and_cancel_wins_only_before_terminal_settlement() {
    use router::cleanup::Terminal;
    let (mut input, receipt) = admission("COMPLETED-2");
    input.submission["protocol"] = json!("mycelium.request_gateway.v1");
    input
        .submission
        .as_object_mut()
        .unwrap()
        .remove("workload_profile_id");
    input
        .submission
        .as_object_mut()
        .unwrap()
        .remove("qos_class");
    let submission = input.submission.clone();
    let s = SignedService::new(input, Arc::new(Current)).unwrap();
    let a = s.submit(submission).unwrap();
    s.start(|| Ok(())).unwrap();
    s.accept_receipt(receipt, |_| Ok(()), |_| Ok("text".into()))
        .unwrap();
    s.native_result(Ok(())).unwrap();
    assert!(s.cancel(&a.request_id, Some(&a.session_token)).unwrap());
    assert_eq!(s.terminal().unwrap(), None);
    s.settle(cleanup(), Terminal::Completed, clean_now).unwrap();
    assert_eq!(s.terminal().unwrap(), Some(Terminal::Cancelled));
    let mut stream = s
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    for (seq, kind) in ["accepted", "token", "token", "cancelled"]
        .iter()
        .enumerate()
    {
        let e = stream.next().unwrap().unwrap();
        assert_eq!(e["type"], *kind);
        assert!(e.get("token_id").is_none());
        stream.ack(seq as u64).unwrap();
    }
    assert!(stream.next().unwrap().is_none());
    stream.close().unwrap();
}
#[test]
fn constructor_and_admission_reject_bad_owner_and_stale_authority() {
    for token in ["short".to_owned(), "é".repeat(32), "x".repeat(257)] {
        let (mut input, _) = admission("DECODING-2");
        input.owner_token = token;
        assert_eq!(
            SignedService::new(input, Arc::new(Current))
                .err()
                .unwrap()
                .code,
            "invalid_session_token"
        );
    }
    struct Observation(f64, bool);
    impl Authority for Observation {
        fn observe(&self) -> Result<(f64, bool), ContractError> {
            Ok((self.0, self.1))
        }
    }
    for authority in [
        Observation(10.0, false),
        Observation(90.0, true),
        Observation(f64::NAN, true),
    ] {
        let (input, _) = admission("DECODING-2");
        let submission = input.submission.clone();
        let service = SignedService::new(input, Arc::new(authority)).unwrap();
        assert_eq!(
            service.submit(submission).err().unwrap().code,
            "token_path_grant_stale"
        );
    }
}
fn cleanup() -> router::cleanup::CleanupProof {
    use budgets::{BudgetId, ClockDomain, Entries, MonoInstant};
    use router::cleanup::{CleanupPeer, CleanupRound};
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-token-vectors.json"
    ))
    .unwrap();
    let mut subject = rows[0]["context"]["expected_scope"]["control"].clone();
    subject["request_id"] = json!("request-1");
    subject["cancellation_generation"] = json!(1);
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-cancel-vectors.json"
    ))
    .unwrap();
    let mut observation = rows[0]["value"].clone();
    for (k, v) in subject.as_object().unwrap() {
        observation["details"]["request_cleanup"][k] = v.clone();
    }
    let id = protocol::identity::Identity::from_secret("endpoint-phone", &[9; 32]).unwrap();
    let signed = json!({"ok":true,"result":{"signature":id.sign(&observation).unwrap(),"verification_key":rows[0]["context"]["key"],"observation":observation}});
    let c = &rows[0]["context"];
    let peer = CleanupPeer {
        node: c["node_id"].as_str().unwrap().into(),
        host: c["host_id"].as_str().unwrap().into(),
        process: c["process_id"].clone(),
        run: c["run_id"].as_str().unwrap().into(),
        deployment: c["deployment_id"].as_str().unwrap().into(),
        endpoint: c["endpoint_id"].as_str().unwrap().into(),
        key: c["key"].clone(),
        generation: json!(5),
    };
    let now = MonoInstant::new(ClockDomain(1), 0);
    let mut round = CleanupRound::new(
        subject,
        vec![peer.clone()],
        now.checked_add(BudgetId::CancelAndCleanup.span(Entries::ZERO).unwrap())
            .unwrap(),
    )
    .unwrap();
    round.accept(&peer.node, &signed, || (now, true)).unwrap();
    round.finish(|| Ok(()), || (now, true)).unwrap()
}
fn clean_now() -> (budgets::MonoInstant, bool) {
    (budgets::MonoInstant::new(budgets::ClockDomain(1), 0), true)
}
#[test]
fn cleanup_proved_completion_is_irreversible_and_ordered_after_tokens() {
    use router::cleanup::Terminal;
    let (service, submission, receipt) = named_fixture("COMPLETED-2");
    let a = service.submit(submission).unwrap();
    service.start(|| Ok(())).unwrap();
    service
        .accept_receipt(receipt, |_| Ok(()), |id| Ok(format!("token-{id}")))
        .unwrap();
    assert_eq!(service.terminal().unwrap(), None);
    service.native_result(Ok(())).unwrap();
    assert_eq!(service.terminal().unwrap(), None);
    service
        .settle(cleanup(), Terminal::Completed, clean_now)
        .unwrap();
    assert!(
        !service
            .cancel(&a.request_id, Some(&a.session_token))
            .unwrap()
    );
    assert_eq!(service.terminal().unwrap(), Some(Terminal::Completed));
    let mut stream = service
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    for (i, kind) in [
        "accepted",
        "lifecycle",
        "lifecycle",
        "lifecycle",
        "lifecycle",
        "lifecycle",
        "token",
        "token",
        "lifecycle",
        "completed",
    ]
    .iter()
    .enumerate()
    {
        let event = stream.next().unwrap().unwrap();
        assert_eq!(event["type"], *kind);
        assert_eq!(event["sequence"], i);
        stream.ack(i as u64).unwrap();
    }
    assert!(stream.next().unwrap().is_none());
    stream.close().unwrap();
}
/// CL-L10-06: service.py:578-586,1024-1063 and asgi.py:372-387 @ f4e366e9.
/// Existing L10 contracts above already require Completed after late cancel and
/// a terminal HTTP reply. Hold the actual terminal unacknowledged so every wire
/// field can be compared before/after DELETE without a generation-changing rebind.
#[test]
fn cancel_after_completed_terminal_preserves_identity_status_and_wire_bytes() {
    use api::{
        http::{Reply, Request, Response},
        requests::Gateway,
    };
    use router::cleanup::Terminal;

    let (service, submission, receipt) = named_fixture("COMPLETED-2");
    let accepted = service.submit(submission).unwrap();
    service.start(|| Ok(())).unwrap();
    service
        .accept_receipt(receipt, |_| Ok(()), |id| Ok(format!("token-{id}")))
        .unwrap();
    service.native_result(Ok(())).unwrap();
    service
        .settle(cleanup(), Terminal::Completed, clean_now)
        .unwrap();
    assert_eq!(service.terminal().unwrap(), Some(Terminal::Completed));

    let mut stream = service
        .subscribe(&accepted.request_id, None, Some(&accepted.session_token))
        .unwrap();
    let terminal = loop {
        let event = stream.next().unwrap().expect("recorded completion");
        assert_eq!(event["request_id"], accepted.request_id);
        if event["type"] == "completed" {
            break event;
        }
        assert_ne!(event["type"], "cancelled");
        assert_ne!(event["type"], "failed");
        stream.ack(event["sequence"].as_u64().unwrap()).unwrap();
    };
    let terminal_bytes = Reply::json(200, &terminal).unwrap().body;
    let gateway = Gateway::new(service.clone());
    let cancel = || {
        let response = gateway
            .handle(Request {
                method: "DELETE".into(),
                target: format!("/v1/inference/{}", accepted.request_id),
                headers: vec![
                    ("authorization".into(), "Bearer bearer-fixture".into()),
                    ("x-mycelium-session".into(), accepted.session_token.clone()),
                ],
                body: Vec::new(),
            })
            .unwrap();
        let Response::Buffered(reply) = response else {
            panic!("DELETE must return a buffered terminal status");
        };
        assert_eq!(reply.status, 200);
        assert_eq!(
            serde_json::from_slice::<Value>(&reply.body).unwrap(),
            json!({"request_id": accepted.request_id, "status": "terminal"})
        );
        assert_eq!(service.terminal().unwrap(), Some(Terminal::Completed));
        reply
    };

    let first = cancel();
    let after = stream
        .next()
        .unwrap()
        .expect("same unacknowledged terminal");
    // Complete document equality includes protocol, request identity, generation,
    // sequence, outcome and any future summary fields; no invented summary schema.
    assert_eq!(after, terminal);
    assert_eq!(Reply::json(200, &after).unwrap().body, terminal_bytes);
    stream.ack(terminal["sequence"].as_u64().unwrap()).unwrap();
    assert!(stream.next().unwrap().is_none());

    // A repeated late cancellation after terminal delivery is equally inert.
    let repeated = cancel();
    assert_eq!(repeated.status, first.status);
    assert_eq!(repeated.headers, first.headers);
    assert_eq!(repeated.body, first.body);
    assert!(stream.next().unwrap().is_none());
    assert!(
        !service
            .cancel(&accepted.request_id, Some(&accepted.session_token))
            .unwrap()
    );
    stream.close().unwrap();
}

#[test]
fn failed_terminal_uses_oracle_generic_code_not_backend_text() {
    let (service, submission, _) = fixture();
    let a = service.submit(submission).unwrap();
    service.start(|| Ok(())).unwrap();
    service
        .native_result(Err(ContractError("private_backend_message")))
        .unwrap();
    service
        .settle(cleanup(), router::cleanup::Terminal::Failed, clean_now)
        .unwrap();
    let mut stream = service
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    for (seq, phase) in [
        None,
        Some("admission"),
        Some("queue"),
        Some("prefill"),
        Some("completion"),
    ]
    .iter()
    .enumerate()
    {
        let event = stream.next().unwrap().unwrap();
        assert_eq!(event["phase"].as_str(), *phase);
        stream.ack(seq as u64).unwrap();
    }
    assert_eq!(stream.next().unwrap().unwrap()["code"], "backend_failed");
    stream.close().unwrap();
}
#[test]
fn failed_decode_cannot_become_completed_after_cleanup() {
    let (service, submission, receipt) = named_fixture("COMPLETED-2");
    service.submit(submission).unwrap();
    service.start(|| Ok(())).unwrap();
    assert!(
        service
            .accept_receipt(
                receipt,
                |_| Ok(()),
                |_| Err(ContractError("invalid_backend_token"))
            )
            .is_err()
    );
    service.native_result(Ok(())).unwrap();
    assert_eq!(
        service
            .settle(cleanup(), router::cleanup::Terminal::Completed, clean_now)
            .unwrap_err()
            .code,
        "invalid_backend_token"
    );
    assert_eq!(service.terminal().unwrap(), None);
}
#[test]
fn qualification_mismatch_codes_preserve_oracle_precedence() {
    let (service, submission, _) = fixture();
    for (field, value, code) in [
        ("deployment_epoch", json!(3), "deployment_epoch_changed"),
        ("topology_version", json!(9), "path_changed"),
        ("qualification_id", json!("other"), "stale_qualification"),
    ] {
        let mut bad = submission.clone();
        bad["qualification"][field] = value;
        assert_eq!(service.submit(bad).err().unwrap().code, code);
    }
}
#[test]
fn real_http_signed_submission_sse_and_late_cancel() {
    use api::{
        http::serve_response_connection,
        requests::{Gateway, MAX_REQUEST_BODY},
    };
    use budgets::BudgetId;
    use journey_tests::duration;
    use std::{
        io::{Read, Write},
        net::{Ipv4Addr, TcpListener, TcpStream},
        thread,
    };
    let (service, submission, receipt) = named_fixture("COMPLETED-2");
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let addr = listener.local_addr().unwrap();
    let gateway = Gateway::new(service.clone());
    let job = thread::spawn(move || {
        for _ in 0..3 {
            let (s, _) = listener.accept().unwrap();
            serve_response_connection(
                s,
                duration(BudgetId::ModelSelection).unwrap(),
                MAX_REQUEST_BODY,
                |r| gateway.preflight(r),
                |r| gateway.handle(r),
            )
            .unwrap();
        }
    });
    let exchange = |method: &str, path: &str, body: &str, owner: Option<&str>| {
        let mut socket = TcpStream::connect(addr).unwrap();
        socket
            .set_read_timeout(Some(duration(BudgetId::ModelSelection).unwrap()))
            .unwrap();
        let auth = owner
            .map(|t| format!("X-Mycelium-Session: {t}\r\n"))
            .unwrap_or_default();
        write!(socket,"{method} {path} HTTP/1.1\r\nHost: {addr}\r\nAuthorization: Bearer bearer-fixture\r\n{auth}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
        let mut raw = String::new();
        socket.read_to_string(&mut raw).unwrap();
        raw
    };
    let raw = exchange("POST", "/v1/inference", &submission.to_string(), None);
    assert!(raw.starts_with("HTTP/1.1 202"));
    let accepted: Value = serde_json::from_str(raw.split_once("\r\n\r\n").unwrap().1).unwrap();
    let owner = accepted["session_token"].as_str().unwrap();
    service.start(|| Ok(())).unwrap();
    service
        .accept_receipt(receipt, |_| Ok(()), |id| Ok(format!("token-{id}")))
        .unwrap();
    service.native_result(Ok(())).unwrap();
    service
        .settle(cleanup(), router::cleanup::Terminal::Completed, clean_now)
        .unwrap();
    let raw = exchange("GET", "/v1/inference/request-1/events", "", Some(owner));
    assert!(raw.starts_with("HTTP/1.1 200"));
    let events: Vec<Value> = raw
        .lines()
        .filter_map(|s| s.strip_prefix("data: "))
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(
        events
            .iter()
            .filter(|e| e["type"] == "lifecycle")
            .map(|e| e["phase"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec![
            "admission",
            "queue",
            "prefill",
            "first_token",
            "decode",
            "completion"
        ],
        "request_gateway/service.py:786-802,943-945,1032 @ f4e366e9"
    );
    assert_eq!(
        events
            .iter()
            .map(|e| e["type"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec![
            "accepted",
            "lifecycle",
            "lifecycle",
            "lifecycle",
            "lifecycle",
            "lifecycle",
            "token",
            "token",
            "lifecycle",
            "completed"
        ]
    );
    for (i, e) in events.iter().enumerate() {
        assert_eq!(e["sequence"], i);
        assert_eq!(e["publisher_generation"], 3);
    }
    assert!(!raw.contains("private input"));
    assert!(!raw.contains(owner));
    let raw = exchange("DELETE", "/v1/inference/request-1", "", Some(owner));
    assert!(raw.starts_with("HTTP/1.1 200"));
    assert_eq!(
        serde_json::from_str::<Value>(raw.split_once("\r\n\r\n").unwrap().1).unwrap()["status"],
        "terminal"
    );
    assert_eq!(
        service.terminal().unwrap(),
        Some(router::cleanup::Terminal::Completed)
    );
    job.join().unwrap();
}
#[test]
fn signed_receipts_reach_public_tokens_only_after_verification() {
    let (service, submission, receipt) = fixture();
    let accepted = service.submit(submission).unwrap();
    service.start(|| Ok(())).unwrap();
    let mut stream = service
        .subscribe(&accepted.request_id, None, Some(&accepted.session_token))
        .unwrap();
    let event = stream.next().unwrap().unwrap();
    assert_eq!(event["type"], "accepted");
    stream.ack(0).unwrap();
    let mut forged = receipt.clone();
    forged["statement"]["token_ids"] = json!([42]);
    assert!(
        service
            .accept_receipt(forged, |_| Ok(()), |_| panic!("forged token decoded"))
            .is_err()
    );
    // Invalid signature poisons the L9 inbox, so use a fresh admitted execution.
    stream.close().unwrap();
    let (service, submission, receipt) = fixture();
    let a = service.submit(submission).unwrap();
    service.start(|| Ok(())).unwrap();
    let mut stream = service
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    stream.next().unwrap();
    stream.ack(0).unwrap();
    service
        .accept_receipt(receipt, |_| Ok(()), |id| Ok(format!("token-{id}")))
        .unwrap();
    for (seq, phase) in ["admission", "queue", "prefill", "first_token", "decode"]
        .iter()
        .enumerate()
    {
        let event = stream.next().unwrap().unwrap();
        assert_eq!(event["phase"], *phase);
        stream.ack(seq as u64 + 1).unwrap();
    }
    for (sequence, token) in [(6, 10), (7, 11)] {
        let event = stream.next().unwrap().unwrap();
        assert_eq!(
            event,
            json!({"protocol":"mycelium.request_event.v2","request_id":"request-1","publisher_generation":3,"sequence":sequence,"type":"token","token_index":sequence-6,"token_id":token,"text":format!("token-{token}")})
        );
        assert!(!event.to_string().contains("private input"));
        stream.ack(sequence).unwrap();
    }
    stream.close().unwrap();
}
#[test]
fn admission_owner_and_cancel_refusals_match_source_codes() {
    let (service, submission, _) = fixture();
    let mut wrong = submission.clone();
    wrong["prompt"] = json!("changed");
    assert_eq!(
        service.submit(wrong).err().unwrap().code,
        "qualification_mismatch"
    );
    let a = service.submit(submission.clone()).unwrap();
    assert_eq!(
        service.submit(submission).err().unwrap().code,
        "gateway_capacity_exhausted"
    );
    assert_eq!(
        service.cancel("missing", None).unwrap_err().code,
        "unknown_request"
    );
    assert_eq!(
        service.cancel(&a.request_id, None).unwrap_err().code,
        "session_owner_mismatch"
    );
    assert!(
        service
            .cancel(&a.request_id, Some(&a.session_token))
            .unwrap()
    );
    assert!(
        !service
            .cancel(&a.request_id, Some(&a.session_token))
            .unwrap()
    );
    assert_eq!(
        service
            .start(|| panic!("cancelled before dispatch"))
            .unwrap_err()
            .code,
        "request_cancelled"
    );
}
#[test]
fn subscription_ownership_and_resume_do_not_relabel_signed_generation() {
    // Round 6: the registry owns the session generation. The fixture grant carries generation 3,
    // so the session starts at 2 (first stream = 3); a cursor naming any other generation is
    // stale. `(2, 0)` is the session's own generation and is ACCEPTED by the oracle (see
    // `pre_first_stream_resume_matches_the_oracle_generation_table`, executed-oracle vector), so
    // the old `(2, 0)` stale probe is replaced by `(1, 0)`, which the oracle refuses.
    let (service, submission, _) = fixture();
    let a = service.submit(submission).unwrap();
    assert_eq!(
        service
            .subscribe(&a.request_id, Some((1, 0)), Some(&a.session_token))
            .err()
            .unwrap()
            .code,
        "stale_publisher_generation"
    );
    let mut first = service
        .subscribe(&a.request_id, None, Some(&a.session_token))
        .unwrap();
    assert_eq!(
        service
            .subscribe(&a.request_id, None, Some(&a.session_token))
            .err()
            .unwrap()
            .code,
        "stream_already_attached"
    );
    first.next().unwrap();
    first.ack(0).unwrap();
    first.close().unwrap();
    // L9 immutable grants cannot authorize the oracle's generation increment.
    assert_eq!(
        service
            .subscribe(&a.request_id, Some((3, 0)), Some(&a.session_token))
            .err()
            .unwrap()
            .code,
        "publisher_generation_sync_failed"
    );
}

/// The fixture grant carries publisher generation 3, so the session sits at generation 2 until
/// its first stream (a fresh v0.1.4 session is at 0 with grant generation 1; the oracle cannot
/// present a fresh session at 3). The table below is the EXECUTED oracle's
/// (`RequestGatewayService.subscribe`, service.py:500-575, via `L10-evidence/r7-probe-generation.py`)
/// for a session at generation 2 with no stream attached: a resume cursor naming generation 2
/// is accepted and binds generation 3 through `update_publisher_generation(2, 3)`; every other
/// generation is `stale_publisher_generation` before any handoff.
#[test]
fn pre_first_stream_resume_matches_the_oracle_generation_table() {
    let oracle: Value =
        serde_json::from_str(include_str!("fixtures/oracle_generation.json")).unwrap();
    assert_eq!(oracle["session_generation"], 2);
    assert_eq!(oracle["next_stream_generation"], 3);
    let mut accepted = 0;
    for (key, expect) in oracle["probe"].as_object().unwrap() {
        let (generation, sequence) = key.split_once(',').unwrap();
        let cursor = (
            generation.parse::<u64>().unwrap(),
            sequence.parse::<u64>().unwrap(),
        );
        let (service, submission, _) = fixture();
        let a = service.submit(submission).unwrap();
        let result = service.subscribe(&a.request_id, Some(cursor), Some(&a.session_token));
        match expect["result"].as_str().unwrap() {
            "ok" => {
                let mut stream = result.unwrap_or_else(|e| panic!("{key}: {}", e.code));
                service.start(|| Ok(())).unwrap();
                let mut generations = Vec::new();
                while let Some(event) = stream.next().unwrap() {
                    let sequence = event["sequence"].as_u64().unwrap();
                    generations.push(event["publisher_generation"].as_u64().unwrap());
                    stream.ack(sequence).unwrap();
                    if sequence >= 3 {
                        break;
                    }
                }
                assert!(!generations.is_empty());
                assert!(
                    generations
                        .iter()
                        .all(|g| *g == expect["stream_generation"]),
                    "{key}: {generations:?}"
                );
                accepted += 1;
            }
            code => assert_eq!(result.err().unwrap().code, code, "{key}"),
        }
    }
    assert_eq!(accepted, 1);
}

mod registry_wiring {
    use super::*;
    use api::signed_requests::GenerationSync;
    use std::sync::Mutex;
    type Rule = Box<dyn Fn(u64, u64) -> Result<bool, ContractError> + Send + Sync>;
    struct Handoff {
        seen: Mutex<Vec<(u64, u64)>>,
        rule: Rule,
    }
    impl GenerationSync for Handoff {
        fn advance(&self, _: &str, expected: u64, new: u64) -> Result<bool, ContractError> {
            self.seen.lock().unwrap().push((expected, new));
            (self.rule)(expected, new)
        }
    }
    fn handoff(rule: Rule) -> Arc<Handoff> {
        Arc::new(Handoff {
            seen: Mutex::new(Vec::new()),
            rule,
        })
    }
    fn drain(stream: &mut Box<dyn api::requests::Subscription>, up_to: u64) -> Vec<Value> {
        let mut out = Vec::new();
        while let Some(event) = stream.next().unwrap() {
            let sequence = event["sequence"].as_u64().unwrap();
            stream.ack(sequence).unwrap();
            out.push(event);
            if sequence >= up_to {
                break;
            }
        }
        out
    }
    #[test]
    fn reconnect_rebinds_through_the_authenticated_handoff_and_replays_unacknowledged_events() {
        let (service, submission, _) = fixture();
        let hook = handoff(Box::new(|_: u64, _: u64| Ok(true)));
        let service = service.with_generation_sync(hook.clone());
        let a = service.submit(submission).unwrap();
        let mut first = service
            .subscribe(&a.request_id, None, Some(&a.session_token))
            .unwrap();
        let accepted = first.next().unwrap().unwrap();
        assert_eq!(
            (
                accepted["type"].as_str(),
                accepted["publisher_generation"].as_u64()
            ),
            (Some("accepted"), Some(3))
        );
        first.ack(0).unwrap();
        first.close().unwrap();
        service.start(|| Ok(())).unwrap(); // admission, queue, prefill = sequences 1..=3
        let mut second = service
            .subscribe(&a.request_id, Some((3, 0)), Some(&a.session_token))
            .unwrap();
        let events = drain(&mut second, 3);
        assert_eq!(
            events
                .iter()
                .map(|e| e["sequence"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            [1, 2, 3]
        );
        assert!(events.iter().all(|e| e["publisher_generation"] == 4));
        assert_eq!(*hook.seen.lock().unwrap(), [(2, 3), (3, 4)]);
        // the old generation can no longer resume
        second.close().unwrap();
        assert_eq!(
            service
                .subscribe(&a.request_id, Some((3, 3)), Some(&a.session_token))
                .err()
                .unwrap()
                .code,
            "stale_publisher_generation"
        );
    }
    #[test]
    fn a_failed_handoff_detaches_and_cancels_the_request() {
        for rule in [
            Box::new(|expected: u64, _: u64| Ok(expected == 2)) as Rule,
            Box::new(|expected: u64, _: u64| {
                if expected == 2 {
                    Ok(true)
                } else {
                    Err(ContractError("handoff_exploded"))
                }
            }),
        ] {
            let (service, submission, _) = fixture();
            let service = service.with_generation_sync(handoff(rule));
            let a = service.submit(submission).unwrap();
            let mut first = service
                .subscribe(&a.request_id, None, Some(&a.session_token))
                .unwrap();
            first.close().unwrap();
            assert_eq!(
                service
                    .subscribe(&a.request_id, Some((3, 0)), Some(&a.session_token))
                    .err()
                    .unwrap()
                    .code,
                "publisher_generation_sync_failed"
            );
            // service.py:569-575: _cancel_session ran; the stream is detached again
            assert!(
                !service
                    .cancel(&a.request_id, Some(&a.session_token))
                    .unwrap()
            );
            assert_eq!(
                service.start(|| panic!("cancelled")).unwrap_err().code,
                "request_cancelled"
            );
            assert!(
                service
                    .subscribe(&a.request_id, None, Some(&a.session_token))
                    .is_err(),
                "the handoff still refuses: a cancelled request opens no stream"
            );
        }
    }
    #[test]
    fn the_immutable_grant_default_refuses_any_second_stream_and_cancels() {
        let (service, submission, _) = fixture();
        let a = service.submit(submission).unwrap();
        let mut first = service
            .subscribe(&a.request_id, None, Some(&a.session_token))
            .unwrap();
        first.close().unwrap();
        assert_eq!(
            service
                .subscribe(&a.request_id, Some((3, 0)), Some(&a.session_token))
                .err()
                .unwrap()
                .code,
            "publisher_generation_sync_failed"
        );
        assert_eq!(
            service.start(|| panic!("cancelled")).unwrap_err().code,
            "request_cancelled"
        );
    }
    #[test]
    fn replay_history_is_bounded_by_the_registry_and_never_skips() {
        // Acknowledged history is trimmed; resuming before the replay boundary is an explicit
        // expiry (service.py:518-540), not a silent skip.
        let (service, submission, _) = fixture();
        let service = service.with_generation_sync(handoff(Box::new(|_: u64, _: u64| Ok(true))));
        let a = service.submit(submission).unwrap();
        let mut first = service
            .subscribe(&a.request_id, None, Some(&a.session_token))
            .unwrap();
        service.start(|| Ok(())).unwrap();
        let events = drain(&mut first, 3);
        assert_eq!(events.len(), 4);
        first.close().unwrap();
        assert_eq!(
            service
                .subscribe(&a.request_id, Some((3, 9)), Some(&a.session_token))
                .err()
                .unwrap()
                .code,
            "invalid_last_event_id"
        );
        // cursor 3 is the last delivered sequence: the resume is accepted and owes nothing
        service
            .subscribe(&a.request_id, Some((3, 3)), Some(&a.session_token))
            .expect("resume at the last acknowledged sequence");
    }
}
