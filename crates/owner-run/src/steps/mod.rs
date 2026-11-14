// SPDX-License-Identifier: AGPL-3.0-only
//! The chain's steps (`demo_chain.py AppDemoChain._step_*`), each over ports a host implements with
//! its real collaborators. Steps not ported yet are listed in `runs/v2/v02/OWNER-RUN.md`.
pub mod activate;
pub mod compile;
pub mod contributor;
pub mod descriptors;
pub mod exchange;
pub mod model;
pub mod observe;
pub mod progress_wait;
pub mod publish;
pub mod serving;
pub mod slices;
pub mod targets;

use crate::chain::StepError;
use crate::machine::OwnerRunError;

/// The closed code of a step that is not ported yet. v0.1.4 never produces it (checked against every
/// executed fixture), so it cannot be mistaken for a ported outcome.
pub const NOT_PORTED_CODE: &str = "owner_run_step_not_ported";

/// Steps still missing: `(step, difference tag, seam that must close first)`. Remove a row when its
/// step is ported; `OWNER-RUN.md` section 2 holds the v0.1.4 lines and the round that ports it.
pub const NOT_PORTED: [(&str, &str, &str); 1] =
    [("serving", "NP-SLICE1-serving", "SE-L9-RUN-SESSION")];

/// How a not-yet-ported step ends: a closed refusal naming the step and its seam. Never a success.
pub fn not_ported(step: &str) -> StepError {
    // Preserve exact legacy host refusals after the global dispatch advances.
    let fallback = match step {
        "slices" => ("NP-SLICE1-slices", "SE-L8-SLICE-FETCH"),
        "publish" => ("NP-SLICE1-publish", "SE-L10-EXCHANGE"),
        "activate" => ("NP-SLICE1-activate", "SE-L9-RUN-SESSION"),
        "targets" => ("NP-SLICE1-targets", "SE-L9-RUN-SESSION"),
        _ => ("NP-SLICE1-unknown", "SE-SLICE1-UNKNOWN"),
    };
    let (tag, seam) = NOT_PORTED
        .iter()
        .find(|(name, _, _)| *name == step)
        .map_or(fallback, |(_, tag, seam)| (*tag, *seam));
    let message = format!(
        "The {step} step is not ported to the Rust run chain yet ({tag}, waiting for seam {seam}); the previous release runs it."
    );
    OwnerRunError::with(NOT_PORTED_CODE, Some(&message), None).into()
}
