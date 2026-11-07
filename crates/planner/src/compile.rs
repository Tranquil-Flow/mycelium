// SPDX-License-Identifier: AGPL-3.0-only
//! Metadata-only port of layer_assignment.py. No assignment is route-ready.
use crate::py::{Failure, Kind};
use crate::{Error, Result, bound};
use serde_json::Value;
/// Assignment UUIDv5 identity: delegated to the shared L5 implementation
/// (`protocol::assignment::id_for`) so the identity calculation is not forked.
pub fn assignment_id(assignment: &Value) -> Result<String> {
    protocol::assignment::id_for(assignment).map_err(|message| {
        if message.starts_with("assignment identity missing field") {
            Error::new("assignment_identity_field_missing")
        } else if message.contains("deployment_id") {
            Error::new("assignment_deployment_id_invalid")
        } else {
            Error::new("canonical_number_invalid")
        }
    })
}
pub fn validate_assignment(a: &Value) -> Result<()> {
    if a["assignment_id"] != assignment_id(a)? || a["route_ready"] != false {
        return Err(Error::new("assignment_identity_mismatch"));
    }
    Ok(())
}
/// Map a v0.1.4 refusal onto the stable planner code callers match on; the oracle's exact
/// message is always preserved in `Error::detail`.
fn refusal(failure: Failure) -> Error {
    let m = failure.message.as_str();
    let code = if failure.kind != Kind::Value {
        "assignment_input_invalid"
    } else if m == "manifest digest mismatch" {
        "manifest_digest_mismatch"
    } else if m.starts_with("route model ") {
        "assignment_model_mismatch"
    } else if m.starts_with("deployment_id ") {
        "assignment_deployment_id_invalid"
    } else if m.contains("cache root") {
        "assignment_cache_root_invalid"
    } else if m.contains("runtime") {
        "assignment_runtime_invalid"
    } else if m.contains("layer range")
        || m.contains("layer gap")
        || m.contains("overlap")
        || m.contains("contiguous")
        || m.contains("cover")
    {
        "assignment_range_invalid"
    } else if m.contains("route") {
        "assignment_route_invalid"
    } else {
        "assignment_input_invalid"
    };
    let mut error = Error::new(code);
    error.detail = Some(failure.message.into_boxed_str());
    error
}
/// v0.1.4 `layer_assignment.compile_layer_assignments` on one JSON object
/// `{manifest, route_plan, deployment_id, deployment_epoch, cache_roots, runtime_by_node,
/// control_plane_binding?}`. It accepts exactly what the oracle accepts (legacy `route_plan.v1`,
/// every spelling `uuid.UUID` takes, the GPT-2 runtime, ...) and refuses exactly what it refuses:
/// this delegates to the oracle-vectored port in [`bound`] instead of a second, stricter
/// compiler. A missing field is a refusal, as the oracle's missing keyword argument is.
pub fn compile_assignments(input: &Value) -> Result<Value> {
    let field = |name: &str| input.get(name).unwrap_or(&Value::Null);
    if [
        "manifest",
        "route_plan",
        "deployment_id",
        "deployment_epoch",
        "cache_roots",
        "runtime_by_node",
    ]
    .iter()
    .any(|k| input.get(*k).is_none())
    {
        let mut error = Error::new("assignment_input_invalid");
        error.detail = Some("TypeError: missing required argument".into());
        return Err(error);
    }
    bound::compile_layer_assignments(
        field("route_plan"),
        field("manifest"),
        field("deployment_id"),
        field("deployment_epoch"),
        field("cache_roots"),
        field("runtime_by_node"),
        input.get("control_plane_binding"),
    )
    .map(Value::Array)
    .map_err(refusal)
}
