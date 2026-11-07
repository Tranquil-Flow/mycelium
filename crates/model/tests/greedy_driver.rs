// SPDX-License-Identifier: AGPL-3.0-only
//! Greedy driver semantics on a synthetic chain (no model weights): selected tokens are fed back
//! (no teacher forcing), positions advance by the rows just run, per-step hashes are of the f32
//! payload, caps/stop ids/refusals are exact.
use model::{
    greedy::{Chain, ChainOutput, StepRecord, first_divergence, generate, hex, sha256_f32},
    moe_decoder::{MAX_ROWS, VOCAB},
};
use stage_contract::{ErrorCode, Result};

/// Next token = (last fed id * 7 + position) % 1000 + 1; logits have a unique maximum there.
struct Toy {
    calls: Vec<(Vec<u32>, i32)>,
    fail_at: Option<usize>,
    resets: usize,
}
impl Toy {
    fn new() -> Self {
        Self {
            calls: vec![],
            fail_at: None,
            resets: 0,
        }
    }
    fn next(ids: &[u32], position: i32) -> u32 {
        (ids[ids.len() - 1] * 7 + position as u32) % 1000 + 1
    }
}
impl Chain for Toy {
    fn step(&mut self, ids: &[u32], position: i32) -> Result<ChainOutput> {
        if self.fail_at == Some(self.calls.len()) {
            return Err(stage_contract::Error::new(ErrorCode::Native, "toy"));
        }
        self.calls.push((ids.to_vec(), position));
        let mut logits = vec![0.0_f32; VOCAB as usize];
        let want = Self::next(ids, position);
        logits[want as usize] = 5.0;
        logits[(want as usize + 1) % VOCAB as usize] = 5.0; // tie: lowest index must win
        logits[0] = -1.0 / (position as f32 + 1.0); // varies per step so hashes differ
        Ok(ChainOutput {
            logits,
            hidden_sha256: Some(sha256_f32(&[position as f32])),
        })
    }
    fn reset(&mut self) {
        self.resets += 1;
    }
}

#[test]
fn selected_token_is_fed_back_and_positions_advance_by_rows() {
    let mut toy = Toy::new();
    let g = generate(&mut toy, &[11, 12, 13], 6, &[], 64).unwrap();
    assert_eq!(toy.resets, 1);
    // independent expectation from the toy's closed form
    let mut want = vec![];
    let mut position = 0;
    let mut feed = vec![11_u32, 12, 13];
    for _ in 0..6 {
        let t = Toy::next(&feed, position);
        want.push(t);
        position += feed.len() as i32;
        feed = vec![t];
    }
    assert_eq!(g.tokens(), want);
    let positions: Vec<i32> = g.steps.iter().map(|s| s.position).collect();
    assert_eq!(positions, [0, 3, 4, 5, 6, 7]);
    let rows: Vec<usize> = g.steps.iter().map(|s| s.rows).collect();
    assert_eq!(rows, [3, 1, 1, 1, 1, 1]);
    // what the chain actually received: prompt once, then ONLY its own previous selections
    assert_eq!(toy.calls[0].0, vec![11, 12, 13]);
    for (i, (ids, _)) in toy.calls.iter().enumerate().skip(1) {
        assert_eq!(ids, &vec![want[i - 1]]);
    }
    assert_eq!(g.stopped_on, None);
}
#[test]
fn tie_selects_the_lowest_index_and_hash_is_of_the_f32_payload() {
    let mut toy = Toy::new();
    let g = generate(&mut toy, &[1], 1, &[], 8).unwrap();
    assert_eq!(g.steps[0].token, Toy::next(&[1], 0));
    // Independent expectations: SHA-256 of the little-endian f32 payload computed with Python
    // hashlib/struct (`struct.pack('<4f', 1.0, -2.5, 0.0, 0.1)`; and `'<248320f'` of zeros with
    // [8]=[9]=5.0 and [0]=-1.0, which is the toy's step-0 logits for prompt [1]).
    assert_eq!(
        hex(&sha256_f32(&[1.0, -2.5, 0.0, 0.1])),
        "e33bcfabe2eae1adeae0afd49d8e995634aaef244541dff35fb885166dede1e0"
    );
    assert_eq!(
        hex(&g.steps[0].logits_sha256),
        "a97b1701de1e4e3cf9a4354592a4686e19adf62cfa3eed3dd3571ffa1e7dc635"
    );
    assert_eq!(
        hex(&sha256_f32(&[])),
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
}
#[test]
fn stop_id_ends_generation_and_is_recorded() {
    let mut toy = Toy::new();
    let first = Toy::next(&[5], 0);
    let g = generate(&mut toy, &[5], 10, &[first], 64).unwrap();
    assert_eq!(g.steps.len(), 1);
    assert_eq!(g.stopped_on, Some(first));
}
#[test]
fn caps_and_prompt_shapes_are_refused_before_any_step() {
    let mut toy = Toy::new();
    let code = |r: Result<_>| r.err().unwrap().code;
    assert_eq!(
        code(generate(&mut toy, &[], 4, &[], 64)),
        ErrorCode::MalformedTensor
    );
    assert_eq!(
        code(generate(&mut toy, &[1], 0, &[], 64)),
        ErrorCode::MalformedTensor
    );
    let long = vec![1_u32; MAX_ROWS as usize + 1];
    assert_eq!(
        code(generate(&mut toy, &long, 2, &[], 4096)),
        ErrorCode::MalformedTensor
    );
    // prompt 3 + 63 decodes = 66 positions; cap 65 refuses, 66 accepts
    assert_eq!(
        code(generate(&mut toy, &[1, 2, 3], 64, &[], 65)),
        ErrorCode::ResourceCap
    );
    assert!(toy.calls.is_empty());
    assert_eq!(
        generate(&mut toy, &[1, 2, 3], 64, &[], 66)
            .unwrap()
            .steps
            .len(),
        64
    );
}
#[test]
fn a_failing_step_propagates_with_the_steps_already_run_unrecorded() {
    let mut toy = Toy::new();
    toy.fail_at = Some(2);
    let error = generate(&mut toy, &[1], 5, &[], 16).unwrap_err();
    assert_eq!(error.code, ErrorCode::Native);
    assert_eq!(toy.calls.len(), 2);
}
#[test]
fn non_finite_or_wrong_width_logits_are_refused() {
    struct Bad(Vec<f32>);
    impl Chain for Bad {
        fn step(&mut self, _: &[u32], _: i32) -> Result<ChainOutput> {
            Ok(ChainOutput {
                logits: self.0.clone(),
                hidden_sha256: None,
            })
        }
        fn reset(&mut self) {}
    }
    let mut nan = vec![0.0_f32; VOCAB as usize];
    nan[3] = f32::NAN;
    assert!(generate(&mut Bad(nan), &[1], 1, &[], 4).is_err());
    assert!(generate(&mut Bad(vec![0.0; 10]), &[1], 1, &[], 4).is_err());
}
#[test]
fn first_divergence_names_the_first_differing_step() {
    let mut toy = Toy::new();
    let a = generate(&mut toy, &[9, 8], 8, &[], 32).unwrap().steps;
    assert_eq!(first_divergence(&a, &a), None);
    let mut b: Vec<StepRecord> = a.clone();
    b[5].logits_sha256[0] ^= 1;
    assert_eq!(first_divergence(&a, &b), Some(5));
    let mut c = a.clone();
    c[2].token += 1;
    assert_eq!(first_divergence(&a, &c), Some(2));
    assert_eq!(first_divergence(&a, &a[..7]), Some(7));
    let mut d = a.clone();
    d[1].hidden_sha256 = Some([7; 32]);
    assert_eq!(first_divergence(&a, &d), Some(1));
}
