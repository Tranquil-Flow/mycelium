// SPDX-License-Identifier: AGPL-3.0-only
//! Offline Qwen placement and immutable assignment compilation.
use serde_json::Value;
use sha2::{Digest, Sha256};
pub mod admission;
pub mod bound;
pub mod chain_compile;
pub mod compile;
pub mod control_adapter;
pub mod discovery;
pub mod layer_planner;
mod messages;
pub mod placement;
pub mod preparation;
pub mod profile;
pub mod py;
mod ratio;
pub mod rollover;
pub mod routes;
pub mod runtime;
pub mod source_plan;
pub mod staged;
pub use placement::plan;
pub use py::py_repr;
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub code: &'static str,
    pub node_id: Option<String>,
    /// Oracle-exact refusal text where the refusal mirrors a v0.1.4 exception message
    /// (`compile::compile_assignments`); `None` for planner-owned codes.
    pub detail: Option<Box<str>>,
}
impl Error {
    pub fn new(code: &'static str) -> Self {
        Self {
            code,
            node_id: None,
            detail: None,
        }
    }
    pub fn node(code: &'static str, node: &str) -> Self {
        Self {
            code,
            node_id: Some(node.into()),
            detail: None,
        }
    }
    fn overflow() -> Self {
        Self::new("planner_arithmetic_overflow")
    }
    fn is_resource(&self) -> bool {
        matches!(
            self.code,
            "member_cannot_hold_share"
                | "member_memory_cap_below_share"
                | "member_storage_cap_below_share"
                | "member_memory_insufficient_for_share"
                | "member_disk_insufficient_for_share"
        )
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}:{:?}", self.code, self.node_id)?;
        match &self.detail {
            Some(detail) => write!(f, ": {detail}"),
            None => Ok(()),
        }
    }
}
impl std::error::Error for Error {}
pub type Result<T> = std::result::Result<T, Error>;
fn add(a: u64, b: u64) -> Result<u64> {
    a.checked_add(b).ok_or_else(Error::overflow)
}
fn mul(a: u64, b: u64) -> Result<u64> {
    a.checked_mul(b).ok_or_else(Error::overflow)
}
/// Delegate signed/compiler identity serialization to the shared L5 implementation.
pub fn canonical(v: &Value, ascii: bool) -> Result<String> {
    let bytes = if ascii {
        protocol::canonical::to_ascii_bytes(v)
    } else {
        protocol::canonical::to_bytes(v)
    }
    .map_err(|_| Error::new("canonical_number_invalid"))?;
    String::from_utf8(bytes).map_err(|_| Error::new("canonical_string_invalid"))
}
pub fn digest(v: &Value, ascii: bool) -> Result<String> {
    Ok(format!(
        "sha256:{:x}",
        Sha256::digest(canonical(v, ascii)?.as_bytes())
    ))
}
