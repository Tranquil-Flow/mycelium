// SPDX-License-Identifier: AGPL-3.0-only
//! The generalised `StageRuntime::execute(&StageCall)`: the GPT-2 reference
//! behind its adapter gives exactly what the hop carried before, and the Qwen2
//! runtime applies the router's complete-context admission rules.

use base64::Engine;
use mycelium_mobile_core::qwen2::{QWEN2_BACKEND, Qwen2Config, Qwen2Runtime, Qwen2Stage, SafetensorsBytes};
use mycelium_mobile_core::runtime::{Gpt2BlockStage, REFERENCE_BACKEND, StageCall, StagePhase, StageRuntime};
use mycelium_mobile_core::wire::Activation;
use serde_json::Value;

fn fixture(name: &str) -> Value {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

#[test]
fn gpt2_adapter_reproduces_the_legacy_hop_payload() {
    let f = fixture("gpt2_stage.json");
    let rows: Vec<Vec<f64>> = f["hidden"].as_array().unwrap().iter().map(|r| r.as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect()).collect();
    // The hop delivers f32: legacy path = MYAC f32 -> f64 rows -> execute -> f32.
    let wire_in = Activation::from_rows_f32(&rows);
    let legacy_rows = wire_in.to_rows_f64().unwrap();
    let mut legacy = Gpt2BlockStage::from_document(&f["pack"]).unwrap();
    let legacy_payload = Activation::from_rows_f32(&legacy.execute("req-1", &legacy_rows).unwrap()).encode().unwrap();

    let (sequence, hidden, flat) = wire_in.to_flat_f32().unwrap();
    let mut adapted: Box<dyn StageRuntime> = Box::new(Gpt2BlockStage::from_document(&f["pack"]).unwrap());
    assert_eq!(adapted.backend(), REFERENCE_BACKEND);
    assert_eq!(adapted.hidden_size(), hidden);
    let out = adapted.execute(&StageCall::prefill("req-1", sequence, &flat)).unwrap();
    assert_eq!(Activation::from_flat_f32(out.sequence, out.hidden_size, &out.values).encode().unwrap(), legacy_payload);
    // Its request-id replay ledger still holds behind the adapter.
    assert_eq!(adapted.execute(&StageCall::prefill("req-1", sequence, &flat)).unwrap(), out);
    assert_eq!(adapted.execute(&StageCall::prefill("req-2", sequence, &flat[..flat.len() - 1])).unwrap_err().0, "request_hidden_shape_invalid");
    assert_eq!(adapted.release_all(), 1);
}

fn qwen2_runtime() -> (Qwen2Runtime, usize, Vec<f32>) {
    let f = fixture("qwen2_synthetic.json");
    let b64 = |v: &Value| base64::engine::general_purpose::STANDARD.decode(v.as_str().unwrap()).unwrap();
    let blob = b64(&f["safetensors_b64"]);
    let config = Qwen2Config::from_hf_config(&f["config"]).unwrap();
    let stage = Qwen2Stage::load(config, 0, 2, &mut SafetensorsBytes::new(&blob).unwrap()).unwrap();
    let input = b64(&f["input_b64"]).chunks_exact(4).map(|c| f32::from_le_bytes(c.try_into().unwrap())).collect();
    (Qwen2Runtime::new("assignment-1", "stage-0-2", stage).unwrap(), f["seq"].as_u64().unwrap() as usize, input)
}

#[test]
fn qwen2_runtime_executes_complete_context_calls() {
    let (mut rt, seq, input) = qwen2_runtime();
    assert_eq!(rt.backend(), QWEN2_BACKEND);
    assert_eq!((rt.assignment_id(), rt.stage_id(), rt.hidden_size()), ("assignment-1", "stage-0-2", 64));
    let expected = rt.stage().forward(seq, &input).unwrap();
    let out = rt.execute(&StageCall::prefill("req-1", seq, &input)).unwrap();
    assert_eq!((out.sequence, out.hidden_size), (seq, 64));
    assert_eq!(out.values, expected);
    // A replay decode step: the whole context again, position = seq - 1.
    let decode = StageCall { phase: StagePhase::Decode, token_index: 0, position: seq as u64 - 1, ..StageCall::prefill("req-1", seq, &input) };
    assert_eq!(rt.execute(&decode).unwrap().values, expected);
    assert_eq!(rt.request_count, 2);
    // Both calls are in the M3a replay ledger (prefill and decode differ).
    assert_eq!(rt.release_all(), 2);
}

#[test]
fn qwen2_runtime_refuses_calls_outside_the_router_rules() {
    let (mut rt, seq, input) = qwen2_runtime();
    let base = StageCall::prefill("req-1", seq, &input);
    let cases = [
        (StageCall { request_id: "", ..base }, "request_id_invalid"),
        (StageCall { token_index: 3, ..base }, "prefill_sequence_mismatch"),
        (StageCall { position: 2, ..base }, "prefill_position_mismatch"),
        (StageCall { phase: StagePhase::RecoveryPrefill, ..base }, "recovery_prefill_sequence_mismatch"),
        (StageCall { phase: StagePhase::Decode, token_index: 0, position: 0, ..base }, "decode_position_mismatch"),
        (StageCall { phase: StagePhase::Decode, token_index: 0, position: 2, ..base }, "decode_context_position_mismatch"),
        (StageCall { phase: StagePhase::Decode, position: seq as u64 - 1, ..base }, "decode_sequence_mismatch"),
        (StageCall { sequence: seq + 1, ..base }, "request_hidden_shape_invalid"),
    ];
    for (call, code) in cases {
        assert_eq!(rt.execute(&call).unwrap_err().0, code);
    }
    assert_eq!(StagePhase::parse("PREFILL_CHUNK").unwrap_err().0, "prefill_chunk_requires_kv_continuity");
    assert_eq!(StagePhase::parse("prefill").unwrap_err().0, "stage_call_phase_invalid");
    assert_eq!(rt.request_count, 0);
}

#[test]
fn activation_flat_f32_round_trips() {
    let values: Vec<f32> = (0..6).map(|i| i as f32 * 0.5 - 1.0).collect();
    let encoded = Activation::from_flat_f32(2, 3, &values).encode().unwrap();
    let decoded = Activation::decode(&encoded).unwrap();
    assert_eq!(decoded.to_flat_f32().unwrap(), (2, 3, values.clone()));
    let rows = vec![values[..3].iter().map(|v| f64::from(*v)).collect::<Vec<_>>(), values[3..].iter().map(|v| f64::from(*v)).collect()];
    assert_eq!(Activation::from_rows_f32(&rows).encode().unwrap(), encoded);
}
