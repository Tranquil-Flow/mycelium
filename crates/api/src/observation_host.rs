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
/// `layer_split.MIN_HOSTS` / `MAX_HOSTS` (v0.1.4 `layer_split.py:44-45`).
const REBUILD_MIN_HOSTS: usize = 2;
const REBUILD_MAX_HOSTS: usize = 8;
const AWAITING_BOOTSTRAP_WORKERS: &str = "awaiting_bootstrap_workers";
fn rebuild_refused(code: &str) -> PortError {
    PortError::Failed {
        code: Some(code.to_owned()),
        text: String::new(),
    }
}
/// `_rebuild_key(run_id, deployment_id, members)` without the hash: the same bound fields.
fn rebuild_key(members: &[Value]) -> Vec<Vec<Value>> {
    let mut key: Vec<Vec<Value>> = members
        .iter()
        .map(|m| {
            [
                "node_id",
                "generation",
                "incarnation",
                "verification_key_digest",
                "endpoint_id",
            ]
            .iter()
            .map(|k| m[*k].clone())
            .collect()
        })
        .collect();
    key.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
    key
}
impl<B: ObservationBackend> ObservationRebuild for VerifiedObservationHost<B> {
    /// `rebuild_with_current_members(app, acknowledged=True, ...)` (`chain_compile.py:480-546`
    /// @ f4e366e9), what the observe step of a Rollover or Rebuild run calls: keep the cards of
    /// members still current (a vanished, revoked or re-admitted member is dropped, never fatal),
    /// refuse below two before any effect, repeat idempotently for the same run and members, else
    /// release the previous observation and prepare a fresh one over the kept cards (sorted by
    /// node) through the same verified path as `prepare`.
    ///
    /// The oracle also stops serving and retires each member's pending stage offer here. In this
    /// host the chain's teardown has already done both before a successor's observe step runs
    /// (`PackagedChainPorts::teardown_with`: route work closed, then
    /// `owner_observation::Backend::teardown` retires the offers and the context ends), so no
    /// member awaits cleanup and the state is always `awaiting_bootstrap_workers`.
    fn rebuild_observation(
        &mut self,
        context: &mut Option<ObservationContext>,
        run: &str,
        deployment: &str,
        descriptors: &[Value],
    ) -> Result<String, PortError> {
        if !self.is_owner() || !self.running() {
            return Err(rebuild_refused("rebuild_owner_consent_required"));
        }
        if descriptors.len() > 4 * REBUILD_MAX_HOSTS {
            return Err(rebuild_refused("rebuild_descriptors_invalid"));
        }
        let swarm = self.swarm_id();
        let mut kept: Vec<(String, Value, Value)> = Vec::new();
        for card in descriptors {
            let Some(node) = card["statement"]["target_node_id"].as_str() else {
                continue;
            };
            if kept.iter().any(|(known, _, _)| known == node) {
                continue;
            }
            let Some(member) = self.member(node) else {
                continue;
            };
            if crate::installation_descriptor::verify_installation_descriptor(
                card,
                &member,
                &swarm,
                run,
                deployment,
                self.backend.wall_clock(),
            )
            .is_err()
            {
                continue;
            }
            kept.push((node.to_owned(), card.clone(), member));
        }
        if kept.len() > REBUILD_MAX_HOSTS {
            return Err(rebuild_refused("host_count_exceeds_limit"));
        }
        if kept.len() < REBUILD_MIN_HOSTS {
            return Err(rebuild_refused("rebuild_requires_two_current_members"));
        }
        let members: Vec<Value> = kept.iter().map(|(_, _, m)| m.clone()).collect();
        if let Some(previous) = context.as_ref().filter(|c| c.run_id == run) {
            return if previous.deployment_id == deployment
                && rebuild_key(&previous.members) == rebuild_key(&members)
            {
                Ok(AWAITING_BOOTSTRAP_WORKERS.to_owned())
            } else {
                Err(rebuild_refused("rebuild_run_id_reused"))
            };
        }
        // The next observation opens its own owner channel: the previous one is released first.
        *context = None;
        kept.sort_by(|a, b| a.0.cmp(&b.0));
        let cards = Value::Array(kept.into_iter().map(|(_, card, _)| card).collect());
        let prepared = runtime::observe::prepare_observation(self, run, deployment, &cards, true)
            .map_err(|e| rebuild_refused(&e.0))?;
        *context = Some(prepared);
        Ok(AWAITING_BOOTSTRAP_WORKERS.to_owned())
    }
}
