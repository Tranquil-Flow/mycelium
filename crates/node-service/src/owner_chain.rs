// SPDX-License-Identifier: AGPL-3.0-only
//! Adapters from real collaborators to `owner-run`'s step ports (SE-L10-OWNER-RUN, runs/v2/v02/OWNER-RUN.md).
//! Only collaborators that exist in the workspace are wired; the rest of the chain is listed there.
use api::{model_catalog, preferences::resource_policy, schema::PublicError};
use owner_run::chain::StepError;
use serde_json::Value;
use std::path::Path;

/// `owner_storage_requirement(model)` of a registered model (`model_resources.py:379-390`), from the
/// real catalog; `None` for a model that is not registered (`model_shape(model) is None`).
pub fn catalog_owner_storage_need(model_id: &str, resolved_commit: &str) -> Option<u64> {
    model_catalog::find(model_id, resolved_commit).map(model_catalog::owner_storage_requirement)
}

/// A failure inside a step that is not an `OwnerRunError` keeps only its class (`owner_run.py:420-422`):
/// the HTTP layer maps `DemoError` to 400 (`app.py:2011`), every other class is a builtin.
pub fn step_exception(error: PublicError) -> StepError {
    let class = if error.status == 400 {
        "DemoError"
    } else {
        error.raised.identity().0
    };
    StepError::Exception {
        class: class.to_owned(),
    }
}

/// `read_resource_policy(root).get('storage_bytes')` through the api crate's own reader (absent file
/// -> no cap; unsafe or invalid file -> the step fails with the reader's exception class).
pub fn storage_consent_cap(root: &Path) -> Result<Option<Value>, StepError> {
    match resource_policy::read(root).map_err(step_exception)? {
        Some(Value::Object(policy)) => Ok(policy.get("storage_bytes").cloned()),
        _ => Ok(None),
    }
}
