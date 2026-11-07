// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "metal")]
use backend_mlx::{Context, Worker, admission::qwen2_descriptor};
use stage_contract::*;
use std::{path::Path, rc::Rc};

#[test]
fn aggregate_native_cap_refuses_before_artifact_io() {
    let ctx = Context::new(1, 0).unwrap();
    let cap = Limits {
        weight_bytes: 2_000_000_000,
        state_bytes: 10_000_000,
        scratch_bytes: 1_000_000_000,
        max_tokens: 128,
    };
    let result = Worker::prepare(
        Rc::clone(&ctx),
        qwen2_descriptor(0..8, true, false).unwrap(),
        Path::new("/nonexistent-l8-aggregate-refusal"),
        cap,
        1,
    );
    assert_eq!(result.err().unwrap().code, ErrorCode::ResourceCap);
    ctx.close().unwrap();
}

#[test]
fn loaded_stage_caps_and_successor_session_generation() {
    let d = qwen2_descriptor(0..1, false, false).unwrap();
    let cap = Limits {
        weight_bytes: 100_000_000,
        state_bytes: 1_000_000,
        scratch_bytes: 100_000_000,
        max_tokens: 8,
    };
    let required = backend_mlx::admission::requirements(&d, cap)
        .unwrap()
        .total()
        .unwrap()
        .memory
        .get();
    let ctx = Context::new(required as usize, 0).unwrap();
    let path = std::env::var("MYCELIUM_QWEN2_CHECKPOINT").expect("cached model required");
    for _ in 0..2 {
        let failed = Worker::prepare(
            Rc::clone(&ctx),
            d.clone(),
            Path::new("/missing-l8-first-stage"),
            cap,
            1,
        );
        assert_eq!(failed.err().unwrap().code, ErrorCode::Artifact);
        assert_eq!(ctx.reserved_resources().unwrap().memory.get(), 0);
    }
    let mut worker = Worker::prepare(Rc::clone(&ctx), d.clone(), Path::new(&path), cap, 1).unwrap();
    assert_eq!(ctx.reserved_resources().unwrap().memory.get(), required);
    let other = Worker::prepare(
        Rc::clone(&ctx),
        d.clone(),
        Path::new("/missing-l8-second-stage"),
        cap,
        2,
    );
    assert_eq!(other.err().unwrap().code, ErrorCode::ResourceCap);
    let binding = Binding {
        request: [1; 32],
        assignment: [2; 32],
        load_generation: 1,
        descriptor: d,
    };
    let mut enlarged = cap;
    enlarged.weight_bytes += 1;
    assert_eq!(
        worker
            .new_session(binding.clone(), enlarged)
            .err()
            .unwrap()
            .code,
        ErrorCode::ResourceCap
    );
    let first = worker.new_session(binding.clone(), cap).unwrap();
    let old_generation = first.state_generation();
    assert_eq!(worker.close().unwrap_err().code, ErrorCode::Cleanup);
    worker.release_session(first).unwrap();
    let mut second = worker.new_session(binding.clone(), cap).unwrap();
    assert!(
        second.state_generation() > old_generation,
        "successor must fence old steps even for identical binding"
    );
    let input = Input::Hidden(Activation {
        shape: [1, 1, 896],
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes: vec![0; 896 * 4],
    });
    let stale = Step {
        binding,
        state_generation: old_generation,
        position: 0,
        payload: backend_mlx::payload_id(&input),
        input,
    };
    assert_eq!(
        worker.prefill(&mut second, stale).unwrap_err().code,
        ErrorCode::Binding
    );
    assert_eq!(second.position(), 0);
    worker.cancel(&mut second).unwrap();
    worker.release_session(second).unwrap();
    worker.close().unwrap();
    drop(worker);
    assert_eq!(ctx.memory_stats().unwrap().0, 0);
    ctx.close().unwrap();
}
