// SPDX-License-Identifier: AGPL-3.0-only
//! API-owned descriptor authentication for L9's ObservationHost.
//! The backend supplies the existing seed/mailbox/session collaborators, never synthetic hello.
use owner_run::chain::PortError;
use runtime::observe::{
    HelloSessions, ObservationContext, ObservationError, ObservationHost, Offer,
};
use runtime::observe_owner::ObservationRebuild;
use serde_json::Value;

/// Live host effects. No default implementation may turn an absent collaborator into success.
pub trait ObservationBackend: Send {
    fn is_owner(&self) -> bool;
    fn running(&self) -> bool;
    fn prepared_manifest_digest(&mut self) -> Result<String, ObservationError>;
    fn swarm_id(&self) -> String;
    fn member(&self, node: &str) -> Option<Value>;
    fn wall_clock(&self) -> f64;
    fn session_factory(
        &mut self,
        run: &str,
        deployment: &str,
        members: &[Value],
    ) -> Result<Box<dyn HelloSessions>, ObservationError>;
    fn publish_offer(
        &mut self,
        member: &Value,
        run: &str,
        deployment: &str,
        archive: &[u8],
    ) -> Result<Offer, ObservationError>;
    fn retire_offer(&mut self, offer: &Offer) -> Result<Value, ObservationError>;
    fn request_stage_cleanup(&mut self, offer: &Offer) -> Result<(), ObservationError>;
    fn rebuild_observation(
        &mut self,
        context: &mut Option<ObservationContext>,
        run: &str,
        deployment: &str,
        descriptors: &[Value],
    ) -> Result<String, PortError>;
}

pub struct VerifiedObservationHost<B> {
    pub backend: B,
}
impl<B: ObservationBackend> ObservationHost for VerifiedObservationHost<B> {
    fn is_owner(&self) -> bool {
        self.backend.is_owner()
    }
    fn running(&self) -> bool {
        self.backend.running()
    }
    fn prepared_manifest_digest(&mut self) -> Result<String, ObservationError> {
        self.backend.prepared_manifest_digest()
    }
    fn swarm_id(&self) -> String {
        self.backend.swarm_id()
    }
    fn member(&self, node: &str) -> Option<Value> {
        self.backend.member(node)
    }
    fn verify_descriptor(
        &mut self,
        card: &Value,
        member: &Value,
        swarm: &str,
        run: &str,
        deployment: &str,
    ) -> Result<Value, ObservationError> {
        let facts = crate::installation_descriptor::verify_installation_descriptor(
            card,
            member,
            swarm,
            run,
            deployment,
            self.backend.wall_clock(),
        )
        .map_err(|e| ObservationError::new(&e.code))?;
        Ok(facts)
    }
    fn session_factory(
        &mut self,
        run: &str,
        deployment: &str,
        members: &[Value],
    ) -> Result<Box<dyn HelloSessions>, ObservationError> {
        self.backend.session_factory(run, deployment, members)
    }
    fn publish_offer(
        &mut self,
        member: &Value,
        run: &str,
        deployment: &str,
        archive: &[u8],
    ) -> Result<Offer, ObservationError> {
        self.backend.publish_offer(member, run, deployment, archive)
    }
    fn retire_offer(&mut self, offer: &Offer) -> Result<Value, ObservationError> {
        self.backend.retire_offer(offer)
    }
    fn request_stage_cleanup(&mut self, offer: &Offer) -> Result<(), ObservationError> {
        self.backend.request_stage_cleanup(offer)
    }
}
impl<B: ObservationBackend> ObservationRebuild for VerifiedObservationHost<B> {
    fn rebuild_observation(
        &mut self,
        context: &mut Option<ObservationContext>,
        run: &str,
        deployment: &str,
        descriptors: &[Value],
    ) -> Result<String, PortError> {
        self.backend
            .rebuild_observation(context, run, deployment, descriptors)
    }
}
