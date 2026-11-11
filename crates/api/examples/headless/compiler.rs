// SPDX-License-Identifier: AGPL-3.0-only
//! Source-bound compiler front end. No operator plan, native proof or Launch is
//! fabricated when the native assignment/challenge producer is unavailable.
//!
//! The compile port itself is `api::owner_compiler` (lane PH3b S3m, moved unchanged from this
//! file) over this launcher's `ChainHost` (`observation.rs`).
use super::*;
#[cfg(test)]
use api::owner_compiler::failed;
pub(super) use api::owner_compiler::{Observation, SharedCompiler};
#[cfg(test)]
use owner_run::steps::compile::{BuildPoll, CompileInputs, CompilePort};
#[cfg(test)]
use planner::chain_compile::{BuildRequest, ModelContract};
#[cfg(test)]
#[path = "compiler_tests.rs"]
mod compiler_tests;

pub(super) type Compiler = api::owner_compiler::Compiler<HeadlessRun>;

pub(super) fn status(core: &Shared) -> Json {
    api::owner_compiler::status(&HeadlessRun(core.clone()))
}

/// Public route preserves source preflight and private builtin identity. The
/// complete compiler owner remains the actual OwnerChain, not a detached runner.
pub(super) fn public_compile(
    core: &Shared,
    body: &Map<String, Value>,
) -> Result<Json, PublicError> {
    api::owner_compiler::public_compile(&HeadlessRun(core.clone()), body)
}
