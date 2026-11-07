// SPDX-License-Identifier: AGPL-3.0-only
use node_service::activation::{decode, encode};
use stage_contract::{Activation, ByteOrder, DType, Layout};
#[test]
fn native_activation_uses_existing_myac_v1_not_a_new_float_protocol() {
    // Independent expected bytes from f4e366e9 mycelium_router/payloads.py:
    // >4sBBBBQ, followed by rank big-endian u32 dimensions, LE elements.
    let expected = vec![
        77, 89, 65, 67, 1, 3, 3, 1, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 2, 0,
        0, 128, 63, 0, 0, 0, 192,
    ];
    let a = Activation {
        shape: [1, 1, 2],
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes: vec![0, 0, 128, 63, 0, 0, 0, 192],
    };
    assert_eq!(encode(&a).unwrap(), expected);
    let b = decode(&expected).unwrap();
    assert_eq!(b.shape, a.shape);
    assert_eq!(b.bytes, a.bytes);
}
#[test]
fn malformed_activation_never_reaches_native() {
    let a = Activation {
        shape: [1, 1, 2],
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes: vec![0; 4],
    };
    assert!(encode(&a).is_err());
    assert!(decode(b"MYAC").is_err());
}
