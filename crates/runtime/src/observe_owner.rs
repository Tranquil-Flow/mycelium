// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-run's observation port over the runtime's validated observation context.
use crate::observe::{ObservationContext, ObservationError, ObservationHost};
use owner_run::chain::PortError;
use owner_run::steps::observe::ObservePort;
use serde_json::Value;

/// The host's existing run/workflow rebuild operation. It must stop the previous workflow and
/// obtain target-confirmed cleanup before replacing `context` (chain_compile.py:480-546).
/// Returning `awaiting_member_cleanup` leaves the old context installed; returning
/// `awaiting_bootstrap_workers` installs the newly prepared context. Errors are never success.
pub trait ObservationRebuild: ObservationHost {
    fn rebuild_observation(
        &mut self,
        context: &mut Option<ObservationContext>,
        run_id: &str,
        deployment_id: &str,
        descriptors: &[Value],
    ) -> Result<String, PortError>;
}

/// Own one run's context on the owner-run Runner thread. `clock` is wall seconds and `token`
/// supplies a fresh 16-hex command nonce. The host owns signed sessions, mailbox and descriptors;
/// the adapter owns no progress loop, retry, fake hello or synthetic cleanup proof.
pub struct ObserveAdapter<H, C, T> {
    host: H,
    clock: C,
    token: T,
    context: Option<ObservationContext>,
}
impl<H, C, T> ObserveAdapter<H, C, T> {
    pub fn new(host: H, clock: C, token: T) -> Self {
        Self {
            host,
            clock,
            token,
            context: None,
        }
    }
    /// Compile consumes the exact context that completed native hello, not a reconstructed view.
    pub fn context(&self) -> Option<&ObservationContext> {
        self.context.as_ref()
    }
    pub fn host_mut(&mut self) -> &mut H {
        &mut self.host
    }
}
fn failed(error: ObservationError) -> PortError {
    PortError::Failed {
        code: Some(error.0),
        text: String::new(),
    }
}
impl<H: ObservationRebuild, C: FnMut() -> f64, T: FnMut() -> String> ObservePort
    for ObserveAdapter<H, C, T>
{
    fn prepare(&mut self, run: &str, deployment: &str, cards: &[Value]) -> Result<(), PortError> {
        // ChainOps.prepare_observation calls the app route: role precedes context admission.
        if !self.host.is_owner() {
            return Err(failed(ObservationError::new("owner_action_required")));
        }
        if self.context.is_some() {
            return Err(failed(ObservationError::new(
                "bootstrap_context_already_exists",
            )));
        }
        self.context = Some(
            crate::observe::prepare_observation(
                &mut self.host,
                run,
                deployment,
                &Value::Array(cards.to_vec()),
                true,
            )
            .map_err(failed)?,
        );
        Ok(())
    }
    fn rebuild(
        &mut self,
        run: &str,
        deployment: &str,
        cards: &[Value],
    ) -> Result<String, PortError> {
        self.host
            .rebuild_observation(&mut self.context, run, deployment, cards)
    }
    fn facts_nodes(&mut self) -> Vec<String> {
        self.context
            .as_ref()
            .map_or_else(Vec::new, ObservationContext::participants)
    }
    fn offers(&mut self) -> Vec<(String, String)> {
        self.context.as_ref().map_or_else(Vec::new, |c| {
            c.offers
                .iter()
                .map(|(n, o)| (n.clone(), o.offer_id.clone()))
                .collect()
        })
    }
    fn collect(&mut self) -> Result<(), PortError> {
        let context = self
            .context
            .as_mut()
            .ok_or_else(|| failed(ObservationError::new("bootstrap_context_missing")))?;
        context
            .collect(&mut self.host, &mut self.clock, &mut self.token)
            .map(|_| ())
            .map_err(failed)
    }
    fn observed_hosts(&mut self) -> Vec<Option<String>> {
        self.context
            .as_ref()
            .map_or_else(Vec::new, ObservationContext::observed_hosts)
            .into_iter()
            .map(Some)
            .collect()
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| failed(ObservationError::new("bootstrap_context_missing")))?;
        context
            .retire_bootstrap(&mut self.host)
            .map(|_| ())
            .map_err(failed)
    }
}
