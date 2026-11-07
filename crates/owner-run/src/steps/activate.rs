// SPDX-License-Identifier: AGPL-3.0-only
//! Activation schedules the existing workflow's startup challenge; it is not
//! evidence that any native worker started or the route qualified.
use crate::chain::{PortError, StepEnv, StepError};
use crate::state::RunState;
use serde_json::Value;

/// Non-owning access to the host's existing activation operation. The host must
/// enforce scheduling consent, exact proposal and awaiting-activation identity.
/// This borrow transfers neither the workflow nor its cleanup obligations.
pub trait ActivationPort {
    fn activate(&mut self, proposal: &Value) -> Result<(), PortError>;
}

/// `demo_chain.py:980-985 @ f4e366e9`: proposal access and the host call
/// share the broad error boundary; private collaborator advice never escapes.
pub fn run_activate_step(
    env: &mut dyn StepEnv,
    port: &mut dyn ActivationPort,
    state: &RunState,
) -> Result<(), StepError> {
    let proposal = state
        .proposal
        .as_ref()
        .ok_or_else(|| crate::machine::OwnerRunError::new("activation_failed"))?;
    port.activate(proposal)
        .map_err(|error| crate::machine::OwnerRunError::new(&error.code_or("activation_failed")))?;
    env.progress("qualifying the route (startup challenge)");
    Ok(())
}
