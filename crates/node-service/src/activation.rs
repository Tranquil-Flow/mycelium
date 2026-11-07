// SPDX-License-Identifier: AGPL-3.0-only
//! f4e366e9 `mycelium_router/payloads.py` MYAC v1; no new tensor protocol.
//! L8's supported slice is F32, rank three, BSH. Other valid wire dtypes refuse.
use protocol::{ContractError, router_wire};
use stage_contract::{Activation, ByteOrder, DType, Layout};
fn invalid() -> ContractError {
    ContractError("invalid_activation_payload")
}
fn length(shape: &[u32; 3]) -> Result<usize, ContractError> {
    shape.iter().try_fold(4usize, |n, &d| {
        if d == 0 {
            return Err(invalid());
        }
        n.checked_mul(d as usize)
            .filter(|n| *n <= router_wire::MAX_PAYLOAD_BYTES)
            .ok_or_else(invalid)
    })
}
pub fn encode(a: &Activation) -> Result<Vec<u8>, ContractError> {
    if a.dtype != DType::F32
        || a.order != ByteOrder::Little
        || a.layout != Layout::BatchSequenceHidden
        || length(&a.shape)? != a.bytes.len()
    {
        return Err(invalid());
    }
    let mut out = b"MYAC\x01\x03\x03\x01".to_vec();
    out.extend((a.bytes.len() as u64).to_be_bytes());
    for d in a.shape {
        out.extend(d.to_be_bytes());
    }
    out.extend(&a.bytes);
    Ok(out)
}
pub fn decode(raw: &[u8]) -> Result<Activation, ContractError> {
    if raw.get(..8) != Some(b"MYAC\x01\x03\x03\x01") || raw.len() < 28 {
        return Err(invalid());
    }
    let declared = u64::from_be_bytes(raw[8..16].try_into().map_err(|_| invalid())?);
    let mut shape = [0; 3];
    for (i, d) in shape.iter_mut().enumerate() {
        *d = u32::from_be_bytes(
            raw[16 + i * 4..20 + i * 4]
                .try_into()
                .map_err(|_| invalid())?,
        );
    }
    let bytes = length(&shape)?;
    if declared != bytes as u64 || raw.len() != 28 + bytes {
        return Err(invalid());
    }
    Ok(Activation {
        shape,
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes: raw[28..].to_vec(),
    })
}
/// Bind the exact expected hop including request/path/topology and token position.
pub fn frame(header: &serde_json::Value, a: &Activation) -> Result<Vec<u8>, ContractError> {
    router_wire::encode_frame("HopHeader", header, &encode(a)?)
}
pub fn unframe(raw: &[u8], expected: &serde_json::Value) -> Result<Activation, ContractError> {
    let frame = router_wire::decode_frame(raw)?;
    let normalized =
        router_wire::decode_frame(&router_wire::encode_frame("HopHeader", expected, &[])?)?;
    if frame.message_type() != "HopHeader" || frame.body() != normalized.body() {
        return Err(ContractError("activation_generation_binding"));
    }
    decode(frame.payload())
}
