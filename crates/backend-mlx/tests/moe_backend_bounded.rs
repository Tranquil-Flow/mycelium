// SPDX-License-Identifier: AGPL-3.0-only
//! PREPARED / NOT RUN in r9. Exact two-layer packed StageBackend follow-on;
//! not the 40-layer chain, 64-token qualification, real transport or a loader double.
#![cfg(feature = "metal")]
use backend_mlx::{Context, MlxBackend, moe_admission, payload_id};
use sha2::{Digest, Sha256};
use stage_contract::*;
use std::{path::PathBuf, rc::Rc};
mod common;

#[test]
#[ignore = "requires fresh parent-approved bounded native window; r9 CPU-only"]
fn bounded_moe_factory_two_layers() {
    assert_eq!(
        std::env::var("MYCELIUM_L8_BOUNDED_NATIVE_APPROVED").as_deref(),
        Ok("1")
    );
    common::quiet();
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/moe-decoder-packed.json")).unwrap();
    let npy = PathBuf::from(
        std::env::var("MYCELIUM_MOE_DECODER_ARRAYS").expect("sealed packed decoder NPY directory"),
    );
    let checkpoint = Artifacts {
        checkpoint: common::checkpoint_root(),
    };
    let limits = Limits {
        weight_bytes: 3 << 30,
        state_bytes: 64 << 20,
        scratch_bytes: 2 << 30,
        max_tokens: 5,
    };
    let d = moe_admission::descriptor(0..2, false, false, NumericsProfile::MlxPackedInt8).unwrap();
    let ctx = Context::new(6 << 30, 128 << 20).unwrap();
    let mut factory = MlxBackend::new(Rc::clone(&ctx));
    let mut worker = factory.prepare(d.clone(), &checkpoint, limits, 1).unwrap();
    assert!(
        worker
            .loaded_names()
            .iter()
            .all(|name| name.starts_with("model.language_model.layers.0.")
                || name.starts_with("model.language_model.layers.1."))
    );
    let binding = Binding {
        request: [1; 32],
        assignment: [2; 32],
        load_generation: 1,
        descriptor: d,
    };
    let mut session = worker.new_session(binding.clone(), limits).unwrap();
    for (index, position) in [0, 3, 4, 0].into_iter().enumerate() {
        if index == 3 {
            worker.reset(&mut session).unwrap();
            assert!(worker.state_snapshot(&session).unwrap().is_empty());
        }
        let expected = &fixture["arrays"][format!("input-{index}")];
        let data = common::npy_f32(&npy.join(format!("input-{index}.npy")), expected);
        let rows = expected["shape"][1].as_u64().unwrap() as u32;
        let input = Input::Hidden(Activation {
            shape: [1, rows, 2048],
            dtype: DType::F32,
            order: ByteOrder::Little,
            layout: Layout::BatchSequenceHidden,
            bytes: data.iter().flat_map(|v| v.to_le_bytes()).collect(),
        });
        let step = Step {
            binding: binding.clone(),
            state_generation: session.state_generation(),
            position,
            payload: payload_id(&input),
            input,
        };
        let mut wrong = step.clone();
        wrong.binding.assignment[0] ^= 1;
        assert_eq!(
            worker.prefill(&mut session, wrong).unwrap_err().code,
            ErrorCode::Binding
        );
        let output = if position == 0 {
            worker.prefill(&mut session, step.clone())
        } else {
            worker.decode(&mut session, step.clone())
        }
        .unwrap();
        assert_eq!(
            format!("{:x}", Sha256::digest(&output.hidden.bytes)),
            fixture["arrays"][format!("layer-1-step-{index}-output")]["payload_sha256"]
                .as_str()
                .unwrap()
        );
        assert_eq!(output.committed_position, position + rows);
        assert!(output.logits.is_none());
        assert_eq!(
            worker.decode(&mut session, step).unwrap_err().code,
            ErrorCode::Position
        );
        for snapshot in worker.state_snapshot(&session).unwrap() {
            let slot = match snapshot.kind {
                "conv" => 0,
                "recurrent" => 1,
                other => panic!("unexpected state {other}"),
            };
            let expected =
                &fixture["arrays"][format!("layer-{}-step-{index}-state-{slot}", snapshot.layer)];
            assert_eq!(serde_json::json!(snapshot.shape), expected["shape"]);
            assert_eq!(
                format!("{:x}", Sha256::digest(&snapshot.bytes)),
                expected["payload_sha256"].as_str().unwrap()
            );
        }
    }
    assert_eq!(
        worker.kv_snapshot(&session).unwrap_err().code,
        ErrorCode::Unsupported
    );
    worker.cancel(&mut session).unwrap();
    worker.release_session(session).unwrap();
    let successor = worker.new_session(binding, limits).unwrap();
    assert_eq!(successor.position(), 0);
    assert!(worker.state_snapshot(&successor).unwrap().is_empty());
    worker.release_session(successor).unwrap();
    worker.close().unwrap();
    assert_eq!(worker.synchronize().unwrap_err().code, ErrorCode::Closed);
    drop(worker);
    drop(factory);
    ctx.synchronize().unwrap();
    ctx.clear_cache().unwrap();
    assert_eq!(ctx.memory_stats().unwrap().0, 0);
    ctx.close().unwrap();
}
