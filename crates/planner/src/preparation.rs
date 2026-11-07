// SPDX-License-Identifier: AGPL-3.0-only
//! `route_assignment.validate_preparation_assignment` (v0.1.4 `mycelium_seed/route_assignment.py:311-337`
//! @ f4e366e9), the planner-owned half of SE-L5-L7: bind owner-issued route authority to a compiled
//! deterministic assignment.
use crate::bound::validate_assignment_identity;
use crate::py::{Failure, Kind, PyResult, py_eq, req, verr};
use crate::runtime::validate_normalized_runtime;
use protocol::qwen_stage_manifest::is_signed_stage_manifest;
use serde_json::Value;

const MOBILE_BACKEND: &str = "mobile-rust-qwen2";

/// `_fail('phone_preparation_assignment_invalid')`.
pub const PREPARATION_ASSIGNMENT_INVALID: &str = "phone_preparation_assignment_invalid";

/// Why [`validate_preparation_assignment`] did not return `Ok`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PreparationError {
    /// `SeedCoordinatorError('phone_preparation_assignment_invalid')`.
    Invalid,
    /// An exception outside the function's `KeyError`/`TypeError`/`ValueError` net (kept for fidelity).
    Escaped(crate::py::Failure),
}

impl PreparationError {
    /// The refusal code a caller maps to a `SeedCoordinatorError`; `None` for an escaped exception.
    pub fn code(&self) -> Option<&'static str> {
        match self {
            Self::Invalid => Some(PREPARATION_ASSIGNMENT_INVALID),
            Self::Escaped(_) => None,
        }
    }
}

/// `_DIGEST_RE = sha256:[0-9a-f]{64}\Z` applied with `fullmatch`.
fn is_digest_text(v: &Value) -> PyResult<bool> {
    let s = v
        .as_str()
        .ok_or_else(|| Failure::other("TypeError: expected string or bytes-like object"))?;
    Ok(s.strip_prefix("sha256:").is_some_and(|h| {
        h.len() == 64 && h.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    }))
}

fn check(assignment: &Value, pack: &Value, node_id: &Value) -> PyResult<()> {
    validate_assignment_identity(assignment)?;
    validate_normalized_runtime(req(assignment, "runtime")?, Some(MOBILE_BACKEND))?;
    let m = req(pack, "message")?;
    if !is_signed_stage_manifest(pack) {
        return verr("preparation mismatch");
    }
    let range = req(assignment, "range")?;
    let names = req(m, "tensors")?
        .as_array()
        .ok_or_else(|| Failure::other("TypeError: tensors is not iterable"))?
        .iter()
        .map(|t| req(t, "name").cloned())
        .collect::<PyResult<Vec<Value>>>()?;
    let bound = req(assignment, "protocol")? == "mycelium.layer_assignment.v2"
        && py_eq(req(assignment, "node_id")?, node_id)
        && py_eq(req(assignment, "model_id")?, req(m, "model_id")?)
        && py_eq(
            req(assignment, "resolved_commit")?,
            req(m, "resolved_commit")?,
        )
        && py_eq(req(range, "start_layer")?, req(m, "start_layer")?)
        && py_eq(
            req(range, "end_layer_exclusive")?,
            req(m, "end_layer_exclusive")?,
        )
        && py_eq(
            req(assignment, "expected_tensor_keys")?,
            &Value::Array(names),
        )
        && is_digest_text(req(assignment, "manifest_digest")?)?;
    if bound {
        Ok(())
    } else {
        verr("preparation mismatch")
    }
}

/// Bind owner-issued route authority to a compiled deterministic assignment (the native manifest
/// independently binds the stage bytes; the compiled id binds the host manifest identity).
///
/// Mirrors the oracle's exception net: `KeyError`/`TypeError`/`ValueError` become
/// [`PreparationError::Invalid`]; any other exception class escapes as [`PreparationError::Escaped`].
pub fn validate_preparation_assignment(
    assignment: &Value,
    pack: &Value,
    node_id: &Value,
) -> Result<(), PreparationError> {
    match check(assignment, pack, node_id) {
        Ok(()) => Ok(()),
        Err(f) if f.kind == Kind::Value => Err(PreparationError::Invalid),
        Err(f) if f.message.starts_with("KeyError") || f.message.starts_with("TypeError") => {
            Err(PreparationError::Invalid)
        }
        Err(f) => Err(PreparationError::Escaped(f)),
    }
}
