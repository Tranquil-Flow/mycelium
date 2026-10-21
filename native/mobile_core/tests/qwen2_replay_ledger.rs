// SPDX-License-Identifier: AGPL-3.0-only
//! M3a: the Qwen2 runtime's bounded replay ledger. A call is identified by
//! `request:path:attempt:phase:token` (the stage holds one hop, so this is the
//! router idempotency key without the hop index) and fingerprinted over its
//! position, sequence and exact f32 input. A retry returns the cached output
//! without recomputing; the same identity with another input is refused; the
//! ledger is bounded (count and bytes, oldest evicted) and released per path
//! or entirely when the node fences (drain, new generation).

use base64::Engine;
use mycelium_mobile_core::qwen2::{MAX_QWEN2_REPLAY_BYTES, MAX_QWEN2_REPLAY_RESULTS, Qwen2Config, Qwen2Runtime, Qwen2Stage, SafetensorsBytes};
use mycelium_mobile_core::runtime::{StageCall, StagePhase, StageRuntime};
use serde_json::Value;

fn runtime() -> (Qwen2Runtime, usize, Vec<f32>) {
    let path = format!("{}/tests/fixtures/qwen2_synthetic.json", env!("CARGO_MANIFEST_DIR"));
    let f: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let b64 = |v: &Value| base64::engine::general_purpose::STANDARD.decode(v.as_str().unwrap()).unwrap();
    let blob = b64(&f["safetensors_b64"]);
    let config = Qwen2Config::from_hf_config(&f["config"]).unwrap();
    let stage = Qwen2Stage::load(config, 0, 2, &mut SafetensorsBytes::new(&blob).unwrap()).unwrap();
    let input = b64(&f["input_b64"]).chunks_exact(4).map(|c| f32::from_le_bytes(c.try_into().unwrap())).collect();
    (Qwen2Runtime::new("assignment-1", "stage-0-2", stage).unwrap(), f["seq"].as_u64().unwrap() as usize, input)
}

fn call<'a>(request: &'a str, path: &'a str, attempt: u64, seq: usize, hidden: &'a [f32]) -> StageCall<'a> {
    StageCall { path_id: path, path_attempt: attempt, ..StageCall::prefill(request, seq, hidden) }
}

#[test]
fn verified_provenance_and_operation_counters_come_from_actual_stage() {
    let f: Value = serde_json::from_str(include_str!("fixtures/qwen2_synthetic.json")).unwrap();
    let blob = base64::engine::general_purpose::STANDARD.decode(f["safetensors_b64"].as_str().unwrap()).unwrap();
    let load = || Qwen2Stage::load(Qwen2Config::from_hf_config(&f["config"]).unwrap(),0,2,&mut SafetensorsBytes::new(&blob).unwrap()).unwrap();
    let stage = load();
    let digest = mycelium_mobile_core::stage_manifest::golden_digest(&stage,42,2).unwrap();
    let mut golden = mycelium_mobile_core::stage_manifest::Golden { seed:42, seq:2, output_digest:digest.clone() };
    let mut rt = Qwen2Runtime::new_verified("actual-assignment","actual-stage",stage,&golden).unwrap();
    assert_eq!(rt.golden_output_digest(),Some(digest.as_str()));
    assert_eq!(rt.applied_operation_count(),Some(0));
    let (_,seq,input)=runtime();
    rt.execute(&call("r","p",0,seq,&input)).unwrap();
    rt.execute(&call("r","p",0,seq,&input)).unwrap();
    assert_eq!(rt.applied_operation_count(),Some(1));
    assert_eq!(rt.replay_result_count(),Some(1));
    rt.release_path("r","p");
    assert_eq!(rt.replay_result_count(),Some(0));
    golden.output_digest = format!("sha256:{}","0".repeat(64));
    assert!(Qwen2Runtime::new_verified("a","s",load(),&golden).is_err());
    assert!(Qwen2Runtime::new("a","s",load()).unwrap().golden_output_digest().is_none());
}

#[test]
fn held_real_kernel_cancellation_leaves_no_partial_replay() {
    // Barriers hold *inside* each production kernel, never a replacement runtime.
    for unit in ["linear_dot", "attention_score", "attention_value", "rms_norm", "rope", "silu", "residual"] {
        let (mut rt, seq, input) = runtime();
        let (entered_tx, entered_rx) = std::sync::mpsc::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let mut held = false;
            let result = rt.execute_cooperative(&call("held", "p", 0, seq, &input), &mut |name| {
                if name == unit && !held {
                    held = true;
                    entered_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                    return Err(mycelium_mobile_core::runtime::RuntimeError("request_cancelled_or_expired".into()));
                }
                Ok(())
            });
            (rt, result)
        });
        let entered = entered_rx.recv_timeout(std::time::Duration::from_secs(2));
        if entered.is_ok() { release_tx.send(()).unwrap(); }
        let (mut rt, result) = worker.join().unwrap();
        assert!(entered.is_ok(), "missing cooperative checkpoint: {unit}");
        assert_eq!(result.unwrap_err().0, "request_cancelled_or_expired");
        assert_eq!((rt.request_count, rt.ledger_len(), rt.ledger_bytes()), (0, 0, 0));
        let (_, seq, input) = runtime();
        assert!(rt.execute(&call("next", "p", 0, seq, &input)).is_ok(), "weights survive interruption");
    }
}

#[test]
fn a_retry_returns_the_cached_output_without_recomputing() {
    let (mut rt, seq, input) = runtime();
    let first = rt.execute(&call("req-1", "path-a", 0, seq, &input)).unwrap();
    let again = rt.execute(&call("req-1", "path-a", 0, seq, &input)).unwrap();
    assert_eq!(first, again);
    assert_eq!(rt.request_count, 1, "the retry did not recompute");
    assert_eq!(rt.ledger_len(), 1);
    // A decode step of the same request is another call.
    let decode = StageCall { phase: StagePhase::Decode, token_index: 0, position: seq as u64 - 1, ..call("req-1", "path-a", 0, seq, &input) };
    rt.execute(&decode).unwrap();
    assert_eq!((rt.request_count, rt.ledger_len()), (2, 2));
    // A new path attempt (A6 replay) is a new identity and recomputes.
    rt.execute(&call("req-1", "path-a", 1, seq, &input)).unwrap();
    assert_eq!(rt.request_count, 3);
}

#[test]
fn the_same_identity_with_another_input_is_refused_and_not_computed() {
    let (mut rt, seq, input) = runtime();
    let out = rt.execute(&call("req-1", "path-a", 0, seq, &input)).unwrap();
    let mut other = input.clone();
    other[0] += 1.0;
    assert_eq!(rt.execute(&call("req-1", "path-a", 0, seq, &other)).unwrap_err().0, "request_replay_conflict");
    // -0.0 vs 0.0 differ in bits: the fingerprint is over exact bytes.
    let mut signed = input.clone();
    let zero = signed.iter().position(|v| *v == 0.0);
    if let Some(i) = zero {
        signed[i] = -signed[i];
        assert_eq!(rt.execute(&call("req-1", "path-a", 0, seq, &signed)).unwrap_err().0, "request_replay_conflict");
    }
    assert_eq!(rt.request_count, 1);
    assert_eq!(rt.execute(&call("req-1", "path-a", 0, seq, &input)).unwrap(), out);
}

#[test]
fn refused_calls_are_not_recorded() {
    let (mut rt, seq, input) = runtime();
    let bad = StageCall { position: 2, ..call("req-1", "path-a", 0, seq, &input) };
    assert_eq!(rt.execute(&bad).unwrap_err().0, "prefill_position_mismatch");
    assert_eq!(rt.ledger_len(), 0);
    rt.execute(&call("req-1", "path-a", 0, seq, &input)).unwrap();
    assert_eq!(rt.request_count, 1);
}

#[test]
fn release_by_path_and_release_all_fence_the_ledger() {
    let (mut rt, seq, input) = runtime();
    rt.execute(&call("req-1", "path-a", 0, seq, &input)).unwrap();
    rt.execute(&call("req-1", "path-b", 0, seq, &input)).unwrap();
    rt.execute(&call("req-2", "path-a", 0, seq, &input)).unwrap();
    assert_eq!(rt.release_path("req-1", "path-a"), 1);
    assert_eq!(rt.ledger_len(), 2);
    // A released call recomputes (deterministically the same output).
    rt.execute(&call("req-1", "path-a", 0, seq, &input)).unwrap();
    assert_eq!(rt.request_count, 4);
    assert_eq!(rt.release_all(), 3);
    assert_eq!((rt.ledger_len(), rt.ledger_bytes()), (0, 0));
}

#[test]
fn the_ledger_is_bounded_by_count_and_evicts_the_oldest() {
    let (mut rt, seq, input) = runtime();
    let ids: Vec<String> = (0..=MAX_QWEN2_REPLAY_RESULTS).map(|i| format!("req-{i}")).collect();
    for id in &ids {
        rt.execute(&call(id, "path-a", 0, seq, &input)).unwrap();
    }
    assert_eq!(rt.ledger_len(), MAX_QWEN2_REPLAY_RESULTS);
    assert!(rt.ledger_bytes() <= MAX_QWEN2_REPLAY_BYTES);
    let before = rt.request_count;
    rt.execute(&call(&ids[0], "path-a", 0, seq, &input)).unwrap(); // evicted: recomputed
    assert_eq!(rt.request_count, before + 1);
    rt.execute(&call(&ids[MAX_QWEN2_REPLAY_RESULTS], "path-a", 0, seq, &input)).unwrap(); // newest: cached
    assert_eq!(rt.request_count, before + 1);
}

#[test]
fn the_ledger_is_bounded_by_bytes() {
    let (mut rt, seq, input) = runtime();
    let per_entry = seq * 64 * 4;
    rt.set_ledger_byte_cap(per_entry * 3);
    for i in 0..5 {
        rt.execute(&call(&format!("req-{i}"), "path-a", 0, seq, &input)).unwrap();
    }
    assert_eq!(rt.ledger_len(), 3);
    assert_eq!(rt.ledger_bytes(), per_entry * 3);
    // An output larger than the whole cap is returned but never kept.
    rt.set_ledger_byte_cap(per_entry - 1);
    assert_eq!(rt.ledger_len(), 0);
    rt.execute(&call("req-big", "path-a", 0, seq, &input)).unwrap();
    assert_eq!(rt.ledger_len(), 0);
}

#[test]
fn the_trait_object_releases_by_path() {
    let (rt, seq, input) = runtime();
    let mut boxed: Box<dyn StageRuntime> = Box::new(rt);
    boxed.execute(&call("req-1", "path-a", 0, seq, &input)).unwrap();
    boxed.execute(&call("req-1", "path-b", 0, seq, &input)).unwrap();
    assert_eq!(boxed.release_path("req-1", "path-b"), 1);
    assert_eq!(boxed.release_all(), 1);
}

#[test]
fn ids_containing_the_separator_never_share_or_release_each_others_results() {
    let (mut rt, seq, input) = runtime();
    rt.execute(&call("r:x", "p", 0, seq, &input)).unwrap();
    rt.execute(&call("r", "x:p", 0, seq, &input)).unwrap();
    assert_eq!(rt.request_count, 2, "the second call ran instead of reusing the first's result");
    assert_eq!(rt.release_path("r", "x"), 0, "no path is released by a prefix of another's ids");
    assert_eq!(rt.release_path("r", "x:p"), 1);
    assert_eq!(rt.ledger_len(), 1);
}
