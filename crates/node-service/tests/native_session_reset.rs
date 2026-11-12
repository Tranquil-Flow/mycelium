// SPDX-License-Identifier: AGPL-3.0-only
//! EP0 `session_reset_v1` against the REAL native worker with the cached
//! Qwen3-0.6B checkpoint (Metal). Two owned worker processes serve the two
//! halves of the model over their private links, exactly as the native
//! compiler drives them; between requests only the stage session is reset.
//! Run explicitly: `MYCELIUM_QWEN3_SNAPSHOT=<snapshot dir with model.safetensors>`.
#![cfg(feature = "metal")]
use node_service::worker_link::Link;
use serde_json::{Value, json};
use std::{
    os::{fd::OwnedFd, unix::net::UnixStream},
    process::{Child, Command, Stdio},
    time::Instant,
};
struct Owned(Child);
impl Drop for Owned {
    fn drop(&mut self) {
        if self.0.try_wait().expect("worker status").is_none() {
            self.0.kill().expect("kill owned test worker");
        }
        self.0.wait().expect("reap owned test worker");
    }
}
fn spawn() -> (Owned, Link) {
    let (client, server) = UnixStream::pair().unwrap();
    let child = Owned(
        Command::new(env!("CARGO_BIN_EXE_mycelium-node"))
            .arg("native-worker")
            .stdin(Stdio::null())
            .stdout(Stdio::from(OwnedFd::from(server)))
            .spawn()
            .unwrap(),
    );
    let wait = budgets::BudgetId::MemberChosenWall
        .span(budgets::Entries::ZERO)
        .unwrap();
    let link = Link::new(client, std::time::Duration::from_millis(wait.millis())).unwrap();
    (child, link)
}
fn request(link: &mut Link, head: &Value, body: &[u8]) -> (Value, Vec<u8>) {
    link.send(head, body).unwrap();
    link.receive().unwrap()
}
/// Greedy token from the final stage's MYAC logits payload.
fn greedy(raw: &[u8]) -> u32 {
    assert_eq!(&raw[..8], b"MYAC\x01\x03\x03\x01");
    let width = u32::from_be_bytes(raw[24..28].try_into().unwrap()) as usize;
    let logits: Vec<f32> = raw[28..]
        .chunks_exact(4)
        .map(|b| f32::from_le_bytes(b.try_into().unwrap()))
        .collect();
    assert_eq!(logits.len(), width);
    let mut best = 0;
    for (i, v) in logits.iter().enumerate() {
        assert!(v.is_finite());
        if *v > logits[best] {
            best = i;
        }
    }
    best as u32
}

#[test]
#[ignore = "requires explicitly built Metal native worker"]
fn real_native_worker_refuses_session_reset_before_prepare() {
    let (_child, mut link) = spawn();
    let (reply, body) = request(
        &mut link,
        &json!({"command":stage_contract::native_session::COMMAND}),
        &[],
    );
    assert!(body.is_empty());
    assert_eq!(reply, json!({"error":"worker_not_prepared"}));
}

/// U2 of plans/EP-PLAN.md: 50 requests on the SAME two workers, each followed
/// by a session reset. Every request's tokens equal the pinned oracle (a reset
/// leaves a fresh session: no KV of the previous request is reused), active
/// memory after every reset is back at the post-prepare baseline (no Metal
/// residue growth), and the resident weights and worker processes are the same
/// ones at the end.
#[test]
#[ignore = "requires the Metal native worker and the cached Qwen3-0.6B snapshot"]
fn session_reset_frees_kv_and_keeps_weights() {
    let snapshot = std::env::var("MYCELIUM_QWEN3_SNAPSHOT").expect("explicit snapshot dir");
    let source = std::path::Path::new(&snapshot).join("model.safetensors");
    let oracle: Value =
        serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE).unwrap();
    let recorded = &oracle["recorded"];
    let identity = &recorded["identity"];
    let prompt: Vec<u32> =
        serde_json::from_value(recorded["workload"]["prompt_ids"].clone()).unwrap();
    let outputs = stage_contract::native_proof::CHALLENGE_OUTPUT_TOKENS;
    let expected: Vec<u32> =
        serde_json::from_value(json!(recorded["tokens"].as_array().unwrap()[..outputs])).unwrap();
    let memory =
        budgets::BudgetId::PyFinalScriptsA13OperatorSingleMacUiRehearsalMaxMemoryBytesL171C55
            .bytes()
            .unwrap()
            .get();
    let mut workers = Vec::new();
    for (start, end) in [(0u32, 14u32), (14, 28)] {
        let (child, mut link) = spawn();
        let began = Instant::now();
        let (reply, _) = request(
            &mut link,
            &json!({"command":"prepare","checkpoint":source,"start":start,"end":end,"total":28,
                "memory":memory,"tokens":prompt.len() + outputs,"id":"ab".repeat(32),"generation":1,
                "stage_image":{"model_id":"Qwen/Qwen3-0.6B","revision":identity["revision"],
                    "image_digest":identity["checkpoint_sha256"],"tensor_manifest":identity["manifest_sha256"],
                    "profile":"packed_int8","layers":{"start":start,"end":end},"total_layers":28,
                    "source_metadata":{"header_sha256":identity["header_sha256"]}}}),
            &[],
        );
        assert_eq!(reply, json!({"ok":true}));
        eprintln!("EP0 prepared [{start},{end}) in {:?}", began.elapsed());
        workers.push((child, link));
    }
    let proof = |link: &mut Link| {
        let (reply, _) = request(
            link,
            &json!({"command":stage_contract::native_proof::COMMAND,
                "protocol":stage_contract::native_proof::REQUEST_PROTOCOL,
                "nonce":"00112233445566778899aabbccddeeff"}),
            &[],
        );
        (
            reply["proof"]["weights_digest"].clone(),
            reply["proof"]["worker_pid"].clone(),
        )
    };
    let before: Vec<_> = workers.iter_mut().map(|(_, l)| proof(l)).collect();
    let reset = |link: &mut Link| {
        let began = Instant::now();
        let (reply, body) = request(
            link,
            &json!({"command":stage_contract::native_session::COMMAND}),
            &[],
        );
        assert!(body.is_empty());
        assert_eq!(reply["ok"], true, "{reply}");
        let session = reply["session"].clone();
        assert_eq!(
            session["protocol"],
            stage_contract::native_session::REPLY_PROTOCOL
        );
        (session, began.elapsed().as_secs_f64() * 1000.0)
    };
    // The first reset right after prepare: the baseline every later reset must meet.
    let baseline: Vec<Value> = workers.iter_mut().map(|(_, l)| reset(l).0).collect();
    eprintln!("EP0 baseline sessions {baseline:?}");
    let mut reset_ms = Vec::new();
    for round in 0..50 {
        let began = Instant::now();
        let mut input = prompt.clone();
        let mut tokens = Vec::new();
        for _ in 0..outputs {
            let mut payload = Vec::new();
            for (i, (_, link)) in workers.iter_mut().enumerate() {
                let head = if i == 0 {
                    json!({"command":"step","tokens":input})
                } else {
                    json!({"command":"step"})
                };
                let (reply, body) = request(link, &head, &payload);
                assert_eq!(reply, json!({"ok":true}), "round {round}");
                payload = body;
            }
            let token = greedy(&payload);
            tokens.push(token);
            input = vec![token];
        }
        assert_eq!(
            tokens, expected,
            "round {round}: a reset left request state behind"
        );
        let request_ms = began.elapsed().as_secs_f64() * 1000.0;
        for (i, (_, link)) in workers.iter_mut().enumerate() {
            let (session, ms) = reset(link);
            assert_eq!(
                session["active_bytes"], baseline[i]["active_bytes"],
                "round {round} stage {i}: active memory after reset {session} differs from the post-prepare baseline {}",
                baseline[i]
            );
            assert_eq!(session["baseline_bytes"], baseline[i]["baseline_bytes"]);
            assert_eq!(
                session["state_generation"].as_u64(),
                baseline[i]["state_generation"]
                    .as_u64()
                    .map(|g| g + round + 1)
            );
            reset_ms.push(ms);
        }
        if round % 10 == 0 {
            eprintln!("EP0 round {round}: request {request_ms:.1} ms, tokens {tokens:?}");
        }
    }
    let after: Vec<_> = workers.iter_mut().map(|(_, l)| proof(l)).collect();
    assert_eq!(before, after, "weights or worker processes changed");
    reset_ms.sort_by(f64::total_cmp);
    eprintln!(
        "EP0 resets n={} min {:.2} ms median {:.2} ms max {:.2} ms",
        reset_ms.len(),
        reset_ms[0],
        reset_ms[reset_ms.len() / 2],
        reset_ms[reset_ms.len() - 1]
    );
    for (i, (_, link)) in workers.iter_mut().enumerate() {
        let (reply, _) = request(link, &json!({"command":"close"}), &[]);
        assert_eq!(reply["stats"]["active_bytes"], 0, "stage {i}: {reply}");
        assert_eq!(reply["stats"]["context_closed"], true);
    }
}
