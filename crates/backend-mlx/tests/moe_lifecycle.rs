// SPDX-License-Identifier: AGPL-3.0-only
//! Tests the production worker's pure control plane, not a numerical substitute.
use backend_mlx::{
    lifecycle::{Lifecycle, payload_id},
    moe_admission,
};
use stage_contract::*;
fn limits() -> Limits {
    Limits {
        weight_bytes: 6 << 30,
        state_bytes: 1 << 30,
        scratch_bytes: 6 << 30,
        max_tokens: 8,
    }
}
fn setup() -> (Lifecycle, Binding) {
    let d = moe_admission::descriptor(0..1, false, false, NumericsProfile::MlxPackedInt8).unwrap();
    let b = Binding {
        request: [1; 32],
        assignment: [2; 32],
        load_generation: 7,
        descriptor: d.clone(),
    };
    (Lifecycle::new(d, limits(), 7).unwrap(), b)
}
fn step(b: &Binding, generation: u64, position: u32, rows: u32) -> Step {
    let input = Input::Hidden(Activation {
        shape: [1, rows, 2048],
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes: vec![0; rows as usize * 2048 * 4],
    });
    Step {
        binding: b.clone(),
        state_generation: generation,
        position,
        payload: payload_id(&input),
        input,
    }
}
#[test]
fn cursor_commit_reset_and_closed_lifetime() {
    let (mut worker, b) = setup();
    let mut s = worker.new_session(b.clone(), limits()).unwrap();
    let checked = worker
        .check_step(&s, &step(&b, s.state_generation(), 0, 3), true)
        .unwrap();
    assert_eq!(s.position(), 0); // validation is not publication
    worker.commit(&mut s, checked).unwrap();
    assert_eq!(s.position(), 3);
    let old = s.state_generation();
    worker.reset(&mut s).unwrap();
    assert_eq!(s.position(), 0);
    assert_ne!(s.state_generation(), old);
    assert_eq!(
        worker
            .check_step(&s, &step(&b, old, 0, 1), true)
            .unwrap_err()
            .code,
        ErrorCode::Binding
    );
    assert_eq!(worker.close().unwrap_err().code, ErrorCode::Cleanup);
    worker.cancel(&mut s).unwrap();
    assert_eq!(
        worker
            .check_step(&s, &step(&b, s.state_generation(), 0, 1), true)
            .unwrap_err()
            .code,
        ErrorCode::Cancelled
    );
    worker.release(s).unwrap();
    worker.close().unwrap();
    assert_eq!(
        worker.new_session(b, limits()).unwrap_err().code,
        ErrorCode::Closed
    );
}
#[test]
fn wrong_binding_origin_and_prepared_commit_refuse() {
    let (mut w, b) = setup();
    let (mut other, _) = setup();
    let mut s = w.new_session(b.clone(), limits()).unwrap();
    assert_eq!(
        w.new_session(b.clone(), limits()).unwrap_err().code,
        ErrorCode::ResourceCap
    );
    for field in 0..8 {
        let mut op = step(&b, s.state_generation(), 0, 1);
        match field {
            0 => op.binding.request[0] ^= 1,
            1 => op.binding.assignment[0] ^= 1,
            2 => op.binding.load_generation += 1,
            3 => op.binding.descriptor.checkpoint[0] ^= 1,
            4 => op.binding.descriptor.quantizer.push('x'),
            5 => op.binding.descriptor.layers = 1..2,
            6 => op.state_generation += 1,
            _ => op.payload[0] ^= 1,
        }
        assert_eq!(
            w.check_step(&s, &op, true).unwrap_err().code,
            ErrorCode::Binding
        );
        assert_eq!(s.position(), 0);
    }
    let op = step(&b, s.state_generation(), 0, 1);
    assert_eq!(
        other.check_step(&s, &op, true).unwrap_err().code,
        ErrorCode::Binding
    );
    assert_eq!(other.reset(&mut s).unwrap_err().code, ErrorCode::Binding);
    let prepared = w.check_step(&s, &op, true).unwrap();
    w.reset(&mut s).unwrap();
    assert_eq!(
        w.commit(&mut s, prepared).unwrap_err().code,
        ErrorCode::Binding
    );
    w.release(s).unwrap();
    let mut wrong = b.clone();
    wrong.load_generation += 1;
    assert_eq!(
        w.new_session(wrong, limits()).unwrap_err().code,
        ErrorCode::Binding
    );
    let mut wrong = b;
    wrong.assignment = [0; 32];
    assert_eq!(
        w.new_session(wrong, limits()).unwrap_err().code,
        ErrorCode::Binding
    );
}
#[test]
fn shape_value_position_profile_and_limits_refuse_without_commit() {
    let (mut w, b) = setup();
    let mut s = w.new_session(b.clone(), limits()).unwrap();
    assert_eq!(
        w.check_step(&s, &step(&b, 0, 1, 1), true).unwrap_err().code,
        ErrorCode::Position
    );
    assert_eq!(
        w.check_step(&s, &step(&b, 0, 0, 1), false)
            .unwrap_err()
            .code,
        ErrorCode::Position
    );
    for field in 0..6 {
        let mut op = step(&b, 0, 0, 1);
        let Input::Hidden(a) = &mut op.input else {
            unreachable!()
        };
        match field {
            0 => a.shape[2] = 1024,
            1 => a.shape[0] = 2,
            2 => a.order = ByteOrder::Big,
            3 => a.dtype = DType::Bf16,
            4 => a.bytes[..4].copy_from_slice(&f32::NAN.to_le_bytes()),
            _ => {
                a.bytes.pop();
            }
        }
        op.payload = payload_id(&op.input);
        assert_eq!(
            w.check_step(&s, &op, true).unwrap_err().code,
            ErrorCode::MalformedTensor
        );
    }
    assert_eq!(
        w.check_step(&s, &step(&b, 0, 0, 9), true).unwrap_err().code,
        ErrorCode::ResourceCap
    );
    let next = w.check_step(&s, &step(&b, 0, 0, 3), true).unwrap();
    w.commit(&mut s, next).unwrap();
    assert_eq!(
        w.check_step(&s, &step(&b, 0, 3, 2), false)
            .unwrap_err()
            .code,
        ErrorCode::MalformedTensor
    );
    assert_eq!(
        w.check_step(&s, &step(&b, 0, 0, 1), true).unwrap_err().code,
        ErrorCode::Position
    );
    assert_eq!(s.position(), 3);
}

/// Proves: B-NUM-05 (partial: real worker cursor, not native KV publication).
#[test]
fn duplicate_decode_and_foreign_prepared_commit_do_not_advance() {
    let (mut w, b) = setup();
    let (mut other, ob) = setup();
    let mut s = w.new_session(b.clone(), limits()).unwrap();
    let os = other.new_session(ob.clone(), limits()).unwrap();
    let foreign = other
        .check_step(&os, &step(&ob, os.state_generation(), 0, 1), true)
        .unwrap();
    assert_eq!(
        w.commit(&mut s, foreign).unwrap_err().code,
        ErrorCode::Binding
    );
    let first = w
        .check_step(&s, &step(&b, s.state_generation(), 0, 1), true)
        .unwrap();
    w.commit(&mut s, first).unwrap();
    let op = step(&b, s.state_generation(), 1, 1);
    let next = w.check_step(&s, &op, false).unwrap();
    let duplicate = w.check_step(&s, &op, false).unwrap();
    w.commit(&mut s, next).unwrap();
    assert_eq!(
        w.commit(&mut s, duplicate).unwrap_err().code,
        ErrorCode::Binding
    );
    assert_eq!(
        w.check_step(&s, &op, false).unwrap_err().code,
        ErrorCode::Position
    );
    assert_eq!(s.position(), 2);
    assert_eq!(
        other.release_check(&s).unwrap_err().code,
        ErrorCode::Binding
    );
    w.release(s).unwrap();
    other.release(os).unwrap();
    w.close().unwrap();
    other.close().unwrap();
}
#[test]
fn dense_and_moe_share_entry_limits_and_generation_binding() {
    use backend_mlx::admission::{qwen2_descriptor, qwen3_descriptor, qwen35_descriptor};
    let descriptors = [
        qwen2_descriptor(0..1, true, false).unwrap(),
        qwen3_descriptor(0..1, true, false, NumericsProfile::MlxPackedInt8).unwrap(),
        qwen35_descriptor(0..1, true, false, NumericsProfile::MlxPackedInt8).unwrap(),
        moe_admission::descriptor(0..1, true, false, NumericsProfile::MlxPackedInt8).unwrap(),
    ];
    for d in descriptors {
        assert!(matches!(
            Lifecycle::new(d.clone(), limits(), 0),
            Err(Error {
                code: ErrorCode::Binding,
                ..
            })
        ));
        let mut w = Lifecycle::new(d.clone(), limits(), 7).unwrap();
        let b = Binding {
            request: [1; 32],
            assignment: [2; 32],
            load_generation: 7,
            descriptor: d,
        };
        for field in 0..4 {
            let mut cap = limits();
            match field {
                0 => cap.weight_bytes += 1,
                1 => cap.state_bytes += 1,
                2 => cap.scratch_bytes += 1,
                _ => cap.max_tokens += 1,
            };
            assert_eq!(
                w.new_session(b.clone(), cap).unwrap_err().phase,
                "session-limits"
            );
        }
        let mut s = w.new_session(b.clone(), limits()).unwrap();
        let invalid = step(&b, 0, 0, 1);
        assert_eq!(
            w.check_step(&s, &invalid, true).unwrap_err().code,
            ErrorCode::MalformedTensor
        );
        for ids in [vec![], vec![u32::MAX]] {
            let input = Input::Tokens(ids);
            let op = Step {
                binding: b.clone(),
                state_generation: 0,
                position: 0,
                payload: payload_id(&input),
                input,
            };
            assert_eq!(
                w.check_step(&s, &op, true).unwrap_err().code,
                ErrorCode::MalformedTensor
            );
        }
        let input = Input::Tokens(vec![1, 2, 3]);
        let op = Step {
            binding: b.clone(),
            state_generation: 0,
            position: 0,
            payload: payload_id(&input),
            input,
        };
        let checked = w.check_step(&s, &op, true).unwrap();
        w.commit(&mut s, checked).unwrap();
        assert_eq!(s.position(), 3);
        w.release(s).unwrap();
        let next = w.new_session(b, limits()).unwrap();
        assert_eq!(next.position(), 0);
        assert_eq!(next.state_generation(), 1);
        w.release(next).unwrap();
        w.close().unwrap();
    }
}
