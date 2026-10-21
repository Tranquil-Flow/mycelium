// SPDX-License-Identifier: AGPL-3.0-only
//! Hand-written Safetensors header parser (no I/O, wasm32-compatible).
//!
//! Mirrors the fail-closed checks of `runtime_loader._validate_safetensors_header`:
//! duplicate names refused, only `dtype`/`shape`/`data_offsets` per entry,
//! byte length = element count x dtype size, no overlap, no gap, no trailing
//! data. Tensor bytes are then read by the caller at
//! `data_offset + start .. data_offset + end`.

use std::collections::BTreeMap;
use std::fmt;

use serde::de::{self, Deserializer, MapAccess, Visitor};
use serde_json::Value;

/// Bound on the header JSON; Qwen2.5-0.5B's is ~30 KB.
pub const MAX_HEADER_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct SafetensorsError(pub &'static str);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TensorEntry {
    pub dtype: String,
    pub shape: Vec<usize>,
    pub start: u64,
    pub end: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Header {
    pub tensors: BTreeMap<String, TensorEntry>,
    /// Absolute file offset of the first data byte (8 + header length).
    pub data_offset: u64,
}

impl Header {
    /// Absolute byte range of one tensor in the file.
    pub fn range(&self, name: &str) -> Option<(u64, u64)> {
        self.tensors.get(name).map(|e| (self.data_offset + e.start, self.data_offset + e.end))
    }
}

fn dtype_bytes(dtype: &str) -> Option<u64> {
    Some(match dtype {
        "BOOL" | "U8" | "I8" | "F8_E4M3" | "F8_E5M2" => 1,
        "BF16" | "F16" | "I16" | "U16" => 2,
        "F32" | "I32" | "U32" => 4,
        "F64" | "I64" | "U64" => 8,
        _ => return None,
    })
}

/// The 8-byte little-endian header length prefix.
pub fn header_length(prefix: &[u8], file_size: u64) -> Result<u64, SafetensorsError> {
    let bytes: [u8; 8] = prefix.get(..8).and_then(|b| b.try_into().ok()).ok_or(SafetensorsError("safetensors_header_truncated"))?;
    let n = u64::from_le_bytes(bytes);
    if n == 0 || n > MAX_HEADER_BYTES || file_size < 8 || n > file_size - 8 {
        return Err(SafetensorsError("safetensors_header_length_invalid"));
    }
    Ok(n)
}

/// A JSON object whose keys must be unique (serde_json keeps the last one).
struct UniqueObject(Vec<(String, Value)>);

impl<'de> serde::Deserialize<'de> for UniqueObject {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = UniqueObject;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("a JSON object")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<UniqueObject, A::Error> {
                let mut seen = std::collections::BTreeSet::new();
                let mut out = Vec::new();
                while let Some((k, v)) = map.next_entry::<String, Value>()? {
                    if !seen.insert(k.clone()) {
                        return Err(de::Error::custom("duplicate"));
                    }
                    out.push((k, v));
                }
                Ok(UniqueObject(out))
            }
        }
        d.deserialize_map(V)
    }
}

fn uint(v: &Value) -> Option<u64> {
    if v.is_u64() { v.as_u64() } else { None }
}

/// Parse and validate the header JSON (`header_bytes` = the `n` bytes after
/// the prefix) of a file of `file_size` bytes.
pub fn parse_header(header_bytes: &[u8], file_size: u64) -> Result<Header, SafetensorsError> {
    let n = header_bytes.len() as u64;
    if n == 0 || n > MAX_HEADER_BYTES || file_size < 8 + n {
        return Err(SafetensorsError("safetensors_header_length_invalid"));
    }
    let object: UniqueObject = serde_json::from_slice(header_bytes).map_err(|_| SafetensorsError("safetensors_header_invalid"))?;
    let data_size = file_size - 8 - n;
    let mut tensors = BTreeMap::new();
    for (name, entry) in object.0 {
        if name == "__metadata__" {
            let ok = entry.as_object().is_some_and(|m| m.values().all(Value::is_string));
            if !ok {
                return Err(SafetensorsError("safetensors_metadata_invalid"));
            }
            continue;
        }
        let map = entry.as_object().ok_or(SafetensorsError("safetensors_entry_invalid"))?;
        if name.is_empty() || map.len() != 3 || !["dtype", "shape", "data_offsets"].iter().all(|k| map.contains_key(*k)) {
            return Err(SafetensorsError("safetensors_entry_invalid"));
        }
        let dtype = map["dtype"].as_str().ok_or(SafetensorsError("safetensors_dtype_invalid"))?;
        let item = dtype_bytes(dtype).ok_or(SafetensorsError("safetensors_dtype_invalid"))?;
        let shape: Vec<u64> = map["shape"]
            .as_array()
            .and_then(|a| a.iter().map(uint).collect())
            .ok_or(SafetensorsError("safetensors_shape_invalid"))?;
        let offsets: Vec<u64> = map["data_offsets"]
            .as_array()
            .filter(|a| a.len() == 2)
            .and_then(|a| a.iter().map(uint).collect())
            .ok_or(SafetensorsError("safetensors_offsets_invalid"))?;
        let (start, end) = (offsets[0], offsets[1]);
        if end < start || end > data_size {
            return Err(SafetensorsError("safetensors_offsets_invalid"));
        }
        let elements = shape.iter().try_fold(1u64, |a, d| a.checked_mul(*d)).ok_or(SafetensorsError("safetensors_shape_invalid"))?;
        if elements.checked_mul(item) != Some(end - start) {
            return Err(SafetensorsError("safetensors_length_mismatch"));
        }
        let shape = shape.iter().map(|d| usize::try_from(*d)).collect::<Result<Vec<_>, _>>().map_err(|_| SafetensorsError("safetensors_shape_invalid"))?;
        tensors.insert(name, TensorEntry { dtype: dtype.to_owned(), shape, start, end });
    }
    if tensors.is_empty() {
        return Err(SafetensorsError("safetensors_empty"));
    }
    let mut intervals: Vec<(u64, u64)> = tensors.values().map(|e| (e.start, e.end)).collect();
    intervals.sort_unstable();
    let mut cursor = 0u64;
    for (start, end) in intervals {
        if start < cursor {
            return Err(SafetensorsError("safetensors_overlap"));
        }
        if start > cursor {
            return Err(SafetensorsError("safetensors_gap"));
        }
        cursor = cursor.max(end);
    }
    if cursor != data_size {
        return Err(SafetensorsError("safetensors_trailing_data"));
    }
    Ok(Header { tensors, data_offset: 8 + n })
}

/// Parse a complete in-memory file.
pub fn parse_file(bytes: &[u8]) -> Result<Header, SafetensorsError> {
    let n = header_length(bytes, bytes.len() as u64)?;
    parse_header(&bytes[8..8 + n as usize], bytes.len() as u64)
}

/// A Safetensors file on disk: the header is parsed once, tensor bytes are
/// read on demand by absolute range (nothing else is loaded).
pub struct SafetensorsFile {
    file: std::fs::File,
    pub header: Header,
}

impl SafetensorsFile {
    pub fn open(path: &std::path::Path) -> Result<Self, SafetensorsError> {
        use std::io::Read;
        let mut file = std::fs::File::open(path).map_err(|_| SafetensorsError("safetensors_unreadable"))?;
        let size = file.metadata().map_err(|_| SafetensorsError("safetensors_unreadable"))?.len();
        let mut prefix = [0u8; 8];
        file.read_exact(&mut prefix).map_err(|_| SafetensorsError("safetensors_header_truncated"))?;
        let n = header_length(&prefix, size)?;
        let mut header = vec![0u8; n as usize];
        file.read_exact(&mut header).map_err(|_| SafetensorsError("safetensors_header_truncated"))?;
        Ok(Self { file, header: parse_header(&header, size)? })
    }

    /// The raw bytes of one tensor.
    pub fn read(&mut self, name: &str) -> Result<Vec<u8>, SafetensorsError> {
        use std::io::{Read, Seek, SeekFrom};
        let (start, end) = self.header.range(name).ok_or(SafetensorsError("safetensors_tensor_missing"))?;
        let mut out = vec![0u8; (end - start) as usize];
        self.file.seek(SeekFrom::Start(start)).map_err(|_| SafetensorsError("safetensors_unreadable"))?;
        self.file.read_exact(&mut out).map_err(|_| SafetensorsError("safetensors_unreadable"))?;
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn file(header: &str, data: usize) -> Vec<u8> {
        let mut h = header.as_bytes().to_vec();
        while !h.len().is_multiple_of(8) {
            h.push(b' ');
        }
        let mut out = (h.len() as u64).to_le_bytes().to_vec();
        out.extend(h);
        out.extend(vec![0u8; data]);
        out
    }

    #[test]
    fn parses_a_canonical_file() {
        let f = file(r#"{"__metadata__":{"format":"pt"},"a":{"dtype":"BF16","shape":[2,3],"data_offsets":[0,12]},"b":{"dtype":"F32","shape":[1],"data_offsets":[12,16]}}"#, 16);
        let h = parse_file(&f).unwrap();
        assert_eq!(h.tensors["a"].shape, vec![2, 3]);
        assert_eq!(h.range("b"), Some((h.data_offset + 12, h.data_offset + 16)));
    }

    #[test]
    fn refuses_malformed_headers() {
        let cases = [
            (r#"{"a":{"dtype":"BF16","shape":[2],"data_offsets":[0,4]},"a":{"dtype":"BF16","shape":[2],"data_offsets":[0,4]}}"#, 4, "safetensors_header_invalid"),
            (r#"{"a":{"dtype":"BF16","shape":[3],"data_offsets":[0,4]}}"#, 4, "safetensors_length_mismatch"),
            (r#"{"a":{"dtype":"BF16","shape":[2],"data_offsets":[0,4]}}"#, 6, "safetensors_trailing_data"),
            (r#"{"a":{"dtype":"BF16","shape":[2],"data_offsets":[2,6]}}"#, 6, "safetensors_gap"),
            (r#"{"a":{"dtype":"BF16","shape":[2],"data_offsets":[0,4]},"b":{"dtype":"BF16","shape":[2],"data_offsets":[2,6]}}"#, 6, "safetensors_overlap"),
            (r#"{"a":{"dtype":"Q4","shape":[2],"data_offsets":[0,4]}}"#, 4, "safetensors_dtype_invalid"),
            (r#"{"a":{"dtype":"BF16","shape":[2],"data_offsets":[0,4],"x":1}}"#, 4, "safetensors_entry_invalid"),
            (r#"{"a":{"dtype":"BF16","shape":[-2],"data_offsets":[0,4]}}"#, 4, "safetensors_shape_invalid"),
            (r#"{"a":{"dtype":"BF16","shape":[2],"data_offsets":[0,40]}}"#, 4, "safetensors_offsets_invalid"),
            (r#"{}"#, 0, "safetensors_empty"),
        ];
        for (header, data, code) in cases {
            assert_eq!(parse_file(&file(header, data)).unwrap_err().0, code, "{header}");
        }
        assert_eq!(parse_file(&[1, 0]).unwrap_err().0, "safetensors_header_truncated");
        let mut huge = file(r#"{"a":{"dtype":"BF16","shape":[2],"data_offsets":[0,4]}}"#, 4);
        huge[..8].copy_from_slice(&u64::MAX.to_le_bytes());
        assert_eq!(parse_file(&huge).unwrap_err().0, "safetensors_header_length_invalid");
    }
}
