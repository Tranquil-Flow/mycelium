// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_contributor` (`demo_chain.py:509-530 @ f4e366e9`): this Mac joins the run as a member.
use crate::chain::{StepEnv, StepError};
use crate::machine::OwnerRunError;

/// The host's side of the step.
pub trait ContributorPort {
    /// `with app.guard: if app.local_contributor is None: app._start_local_contributor()`, then the
    /// contributor's node id.
    fn ensure_local_contributor(&mut self) -> Result<String, StepError>;
    /// `getattr(app, 'run_exchange', None) is not None` (`_exchange()`).
    fn exchange_available(&self) -> bool;
    /// The local agent that answers the owner's announcements as this Mac's member.
    fn attach_agent(&mut self, local: &str) -> Result<(), StepError>;
}

/// Runs the step; the node id becomes the run's `local_node`.
pub fn run_contributor_step(
    env: &mut dyn StepEnv,
    port: &mut dyn ContributorPort,
) -> Result<String, StepError> {
    let local = port.ensure_local_contributor()?;
    if !port.exchange_available() {
        return Err(OwnerRunError::new("owner_exchange_unavailable").into());
    }
    port.attach_agent(&local)?;
    let head: String = local.chars().take(12).collect();
    env.progress(&format!("this Mac contributes as node {head}"));
    Ok(local)
}
