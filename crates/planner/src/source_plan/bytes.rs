// SPDX-License-Identifier: AGPL-3.0-only
//! Retained-handle byte accounting for slice_acquisition.py:94-236 @ f4e366e9.
use crate::{Error, Result, add};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{fs::File, os::unix::fs::FileExt};

pub(super) fn read(file: &File, offset: u64, length: u64) -> Result<Vec<u8>> {
    let bad = || Error::new("slice_source_truncated");
    let end = offset.checked_add(length).ok_or_else(bad)?;
    if file.metadata().map_err(|_| bad())?.len() < end {
        return Err(bad());
    }
    let n = usize::try_from(length).map_err(|_| bad())?;
    let mut bytes = Vec::new();
    bytes.try_reserve_exact(n).map_err(|_| bad())?;
    bytes.resize(n, 0);
    file.read_exact_at(&mut bytes, offset).map_err(|_| bad())?;
    Ok(bytes)
}
pub(super) fn sha(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}
/// No unbounded read_to_end and no reopened pathname. Size is an independently
/// pinned manifest/transfer size, checked before and after the stream.
pub(super) fn verify(file: &File, size: u64, digest: &str, code: &'static str) -> Result<()> {
    let bad = || Error::new(code);
    let meta = file.metadata().map_err(|_| bad())?;
    if !meta.is_file() || meta.len() != size || size == 0 {
        return Err(bad());
    }
    let mut hash = Sha256::new();
    let mut offset = 0;
    // _file_sha reads 1<<20 bytes per block, slice_acquisition.py:97.
    const HASH_CHUNK: u64 = 1 << 20;
    while offset < size {
        let n = HASH_CHUNK.min(size - offset);
        hash.update(read(file, offset, n).map_err(|_| bad())?);
        offset += n;
    }
    if format!("sha256:{:x}", hash.finalize()) != digest
        || file.metadata().map_err(|_| bad())?.len() != size
    {
        return Err(bad());
    }
    Ok(())
}
#[derive(Clone)]
pub(super) struct Tensor {
    pub name: String,
    pub record: Value,
    pub start: u64,
    pub length: u64,
}
pub(super) struct Header {
    pub tensors: Vec<Tensor>,
    pub bytes: Vec<u8>,
}
pub(super) fn header(file: &File) -> Result<Header> {
    let bad = || Error::new("slice_safetensors_invalid");
    let raw = read(file, 0, 8).map_err(|_| bad())?;
    let len = u64::from_le_bytes(raw.try_into().map_err(|_| bad())?);
    let maximum = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxHeaderBytesL67
        .bytes()
        .map_err(|_| bad())?
        .get();
    if len < 2 || len > maximum {
        return Err(bad());
    }
    let bytes = read(file, 0, add(8, len)?).map_err(|_| bad())?;
    let value: Value = serde_json::from_slice(&bytes[8..]).map_err(|_| bad())?;
    let mut tensors = Vec::new();
    for (name, record) in value.as_object().ok_or_else(bad)? {
        if name == "__metadata__" {
            continue;
        }
        let fields = record.as_object().ok_or_else(bad)?;
        if fields.len() != 3 || !fields.contains_key("dtype") || !fields.contains_key("shape") {
            return Err(bad());
        }
        let offsets = record["data_offsets"]
            .as_array()
            .filter(|v| v.len() == 2)
            .ok_or_else(bad)?;
        let start = offsets[0].as_u64().ok_or_else(bad)?;
        let end = offsets[1].as_u64().ok_or_else(bad)?;
        tensors.push(Tensor {
            name: name.clone(),
            record: record.clone(),
            start,
            length: end.checked_sub(start).ok_or_else(bad)?,
        });
    }
    tensors.sort_by_key(|t| t.start);
    let mut previous = 0;
    for t in &tensors {
        if t.start != previous {
            return Err(bad());
        }
        previous = add(t.start, t.length)?;
    }
    if file.metadata().map_err(|_| bad())?.len() != add(bytes.len() as u64, previous)? {
        return Err(bad());
    }
    Ok(Header { tensors, bytes })
}
/// RFC 4648 standard padded encoding (not a cryptographic primitive). No new
/// dependency edge is needed for the source format's inline header field.
pub(super) fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in bytes.chunks(3) {
        let a = c[0];
        let b = c.get(1).copied().unwrap_or(0);
        let d = c.get(2).copied().unwrap_or(0);
        out.push(ALPHABET[(a >> 2) as usize] as char);
        out.push(ALPHABET[(((a & 3) << 4) | (b >> 4)) as usize] as char);
        out.push(if c.len() > 1 {
            ALPHABET[(((b & 15) << 2) | (d >> 6)) as usize] as char
        } else {
            '='
        });
        out.push(if c.len() > 2 {
            ALPHABET[(d & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}
