// SPDX-License-Identifier: AGPL-3.0-only
//! Native parity of `mobile-rust-reference` against the `pixel-stdlib` executor
//! (`mycelium_mobile/pixel_stage.py`), on a fixture it produced.

use mycelium_mobile_core::runtime::Gpt2BlockStage;
use serde_json::Value;

fn fixture() -> Value {
    let path = format!("{}/tests/fixtures/gpt2_stage.json", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn rows(v: &Value) -> Vec<Vec<f64>> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|r| r.as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect())
        .collect()
}

#[test]
fn reference_block_matches_pixel_stdlib_bit_for_bit_at_f32_and_within_1e_12_at_f64() {
    let f = fixture();
    let mut stage = Gpt2BlockStage::from_document(&f["pack"]).unwrap();
    assert_eq!(stage.pack_digest(), f["pack"]["pack_digest"].as_str().unwrap());
    let out = stage.execute("req-1", &rows(&f["hidden"])).unwrap();
    let expected = rows(&f["output"]);
    let mut max_err = 0.0f64;
    for (a, b) in out.iter().flatten().zip(expected.iter().flatten()) {
        max_err = max_err.max((a - b).abs());
        // The activation crosses the hop as MYAC float32: identical there.
        assert_eq!(*a as f32, *b as f32);
    }
    assert!(max_err <= 1e-12, "max abs error {max_err}");
}

#[test]
fn replay_and_pack_integrity_fail_closed() {
    let f = fixture();
    let hidden = rows(&f["hidden"]);
    let mut stage = Gpt2BlockStage::from_document(&f["pack"]).unwrap();
    let first = stage.execute("req-1", &hidden).unwrap();
    assert_eq!(stage.execute("req-1", &hidden).unwrap(), first);
    assert_eq!(stage.request_count, 1);
    let mut changed = hidden.clone();
    changed[0][0] += 1.0;
    assert_eq!(stage.execute("req-1", &changed).unwrap_err().0, "request_replay_conflict");
    assert_eq!(stage.execute("req-2", &hidden[..1].iter().map(|r| r[..3].to_vec()).collect::<Vec<_>>()).unwrap_err().0, "request_hidden_shape_invalid");

    let mut tampered = f["pack"].clone();
    let key = "transformer.h.1.ln_1.bias";
    tampered["tensors"][key][0] = Value::from(9.0);
    assert_eq!(Gpt2BlockStage::from_document(&tampered).err().unwrap().0, "stage_pack_digest_mismatch");

    let mut routed = f["pack"].clone();
    routed["route_ready"] = Value::Bool(true);
    assert_eq!(Gpt2BlockStage::from_document(&routed).err().unwrap().0, "stage_pack_route_ready_invalid");
}
