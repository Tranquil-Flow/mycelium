// SPDX-License-Identifier: AGPL-3.0-only
//! TEST DOUBLE (decision U4, PH3B-COMMON) of the run's OBSERVE PORT, never product code: the real
//! observe adapter over the packaged owner's REAL observation backend (descriptor verification
//! against the owner's seed, real bootstrap offers on the owner's managed mailbox, both Macs' real
//! hello helpers, the real hello and stop over the owner's control journal, cleanup and
//! retirement), except that each hello answer's host and boot identity is replaced by a distinct
//! one per Mac. It is not physical-host evidence.
//!
//! [`HostIdentityTestDouble::with_hook`] adds one more named substitution for a test that needs a
//! Mac's own helper to end by itself (PR-ASTRA-02): after that Mac answered hello, the test's
//! [`BeforeStop`] hook runs where the real stop would be sent; it may request the Mac's stage
//! cleanup on the owner's REAL mailbox and, when it returns true, the real stop is not sent (the
//! Mac's helper is already gone). Everything else stays real.
#![allow(dead_code)]
use api::{
    observation_host::{ObservationBackend, VerifiedObservationHost},
    owner_observation::Backend,
};
use node_service::packaged_run::{ObserveDouble, PackagedRun};
use owner_run::{chain::PortError, steps::observe::ObservePort};
use runtime::{
    observe::{
        CloseFailed, HelloSession, HelloSessions, ObservationContext, ObservationError, Offer,
    },
    observe_owner::ObserveAdapter,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

/// What a test's hook may do where a Mac's real stop would be sent, after its hello answered.
pub struct BeforeStop<'a> {
    /// The Mac whose hello session this is.
    pub node: &'a str,
    /// Request this Mac's stage cleanup on the owner's real managed mailbox (`collect` requests it
    /// again later; the request is idempotent).
    pub request_cleanup: &'a mut dyn FnMut() -> Result<(), ObservationError>,
}
/// Returns true when the test ended that Mac's helper itself: its real stop is then not sent.
pub type StopHook = Arc<dyn Fn(BeforeStop<'_>) -> bool + Send + Sync>;
/// The hook, a second handle on the same real backend (only for the cleanup request) and every
/// published offer by node.
#[derive(Clone)]
struct Hook {
    hook: StopHook,
    backend: Arc<Mutex<Backend<PackagedRun>>>,
    offers: Arc<Mutex<BTreeMap<String, Offer>>>,
}

/// The real hello sessions (the owner's managed control journal, the real hello and stop to
/// each Mac's own helper) with each answer's host and boot identity replaced by a distinct
/// one per Mac: the one machine-identity fact a single Mac cannot give.
struct DistinctHosts {
    real: Box<dyn HelloSessions>,
    nodes: Vec<String>,
    hook: Option<Hook>,
}
struct DistinctHost<'a> {
    real: Box<dyn HelloSession + 'a>,
    index: usize,
    node: String,
    hook: Option<Hook>,
}
impl HelloSession for DistinctHost<'_> {
    fn send_hello(&mut self, command: &str) -> Result<Value, Option<String>> {
        let mut response = self.real.send_hello(command)?;
        // The validator's identity shape (`host-`/`boot-` + 32 hex digits), one per Mac.
        let index = 0x00d0_b1e0_u128 + self.index as u128;
        response["result"]["host_id"] = json!(format!("host-{index:032x}"));
        response["result"]["boot_id"] = json!(format!("boot-{index:032x}"));
        Ok(response)
    }
    fn close(&mut self) -> Result<(), CloseFailed> {
        if let Some(hook) = &self.hook {
            let node = self.node.clone();
            let mut request_cleanup = || {
                let offer = hook
                    .offers
                    .lock()
                    .unwrap()
                    .get(&node)
                    .cloned()
                    .ok_or_else(|| ObservationError::new("test_offer_unknown"))?;
                hook.backend.lock().unwrap().request_stage_cleanup(&offer)
            };
            if (hook.hook)(BeforeStop {
                node: &node,
                request_cleanup: &mut request_cleanup,
            }) {
                return Ok(());
            }
        }
        self.real.close()
    }
    fn cleanup_error(&self) -> bool {
        self.real.cleanup_error()
    }
    fn stop_response_ok(&self) -> Option<bool> {
        self.real.stop_response_ok()
    }
}
impl HelloSessions for DistinctHosts {
    fn run_id(&self) -> &str {
        self.real.run_id()
    }
    fn deployment_id(&self) -> &str {
        self.real.deployment_id()
    }
    fn bound_nodes(&self) -> std::collections::BTreeSet<String> {
        self.real.bound_nodes()
    }
    fn binding_current(&mut self, node: &str) -> bool {
        self.real.binding_current(node)
    }
    fn open(
        &mut self,
        node: &str,
        timeout: f64,
    ) -> Result<Box<dyn HelloSession + '_>, Option<String>> {
        let index = self
            .nodes
            .iter()
            .position(|n| n == node)
            .ok_or(Some("managed_observation_member_not_current".to_owned()))?;
        Ok(Box::new(DistinctHost {
            real: self.real.open(node, timeout)?,
            index,
            node: node.to_owned(),
            hook: self.hook.clone(),
        }))
    }
}
/// The real backend; only the host identity in its hello answers is replaced.
struct Hellos(Backend<PackagedRun>, Option<Hook>);
impl ObservationBackend for Hellos {
    fn is_owner(&self) -> bool {
        self.0.is_owner()
    }
    fn running(&self) -> bool {
        self.0.running()
    }
    fn prepared_manifest_digest(&mut self) -> Result<String, ObservationError> {
        self.0.prepared_manifest_digest()
    }
    fn swarm_id(&self) -> String {
        self.0.swarm_id()
    }
    fn member(&self, node: &str) -> Option<Value> {
        self.0.member(node)
    }
    fn wall_clock(&self) -> f64 {
        self.0.wall_clock()
    }
    fn session_factory(
        &mut self,
        run: &str,
        deployment: &str,
        members: &[Value],
    ) -> Result<Box<dyn HelloSessions>, ObservationError> {
        Ok(Box::new(DistinctHosts {
            real: self.0.session_factory(run, deployment, members)?,
            nodes: members
                .iter()
                .filter_map(|m| m["node_id"].as_str().map(str::to_owned))
                .collect(),
            hook: self.1.clone(),
        }))
    }
    fn publish_offer(
        &mut self,
        member: &Value,
        run: &str,
        deployment: &str,
        archive: &[u8],
    ) -> Result<Offer, ObservationError> {
        let offer = self.0.publish_offer(member, run, deployment, archive)?;
        if let (Some(hook), Some(node)) = (&self.1, member["node_id"].as_str()) {
            hook.offers
                .lock()
                .unwrap()
                .insert(node.to_owned(), offer.clone());
        }
        Ok(offer)
    }
    fn retire_offer(&mut self, offer: &Offer) -> Result<Value, ObservationError> {
        self.0.retire_offer(offer)
    }
    fn request_stage_cleanup(&mut self, offer: &Offer) -> Result<(), ObservationError> {
        self.0.request_stage_cleanup(offer)
    }
    fn rebuild_observation(
        &mut self,
        context: &mut Option<ObservationContext>,
        run: &str,
        deployment: &str,
        descriptors: &[Value],
    ) -> Result<String, PortError> {
        self.0
            .rebuild_observation(context, run, deployment, descriptors)
    }
}
fn nonce() -> String {
    api::app_host::hex(&api::app_host::random(16).unwrap_or_default())
}
type Adapter = ObserveAdapter<VerifiedObservationHost<Hellos>, fn() -> f64, fn() -> String>;
/// The observe port the test gives the packaged run (`RunConfig::observe_test_double`).
pub struct HostIdentityTestDouble(Adapter);
impl HostIdentityTestDouble {
    pub fn over(backend: Backend<PackagedRun>) -> Box<dyn ObserveDouble> {
        Self::hooked(backend, None)
    }
    /// [`HostIdentityTestDouble::over`] with `hook` run where each Mac's real stop would be sent.
    pub fn with_hook(backend: Backend<PackagedRun>, hook: StopHook) -> Box<dyn ObserveDouble> {
        let side = Backend {
            core: backend.core.clone(),
            published: BTreeMap::new(),
        };
        let hook = Hook {
            hook,
            backend: Arc::new(Mutex::new(side)),
            offers: Arc::default(),
        };
        Self::hooked(backend, Some(hook))
    }
    fn hooked(backend: Backend<PackagedRun>, hook: Option<Hook>) -> Box<dyn ObserveDouble> {
        Box::new(Self(ObserveAdapter::new(
            VerifiedObservationHost {
                backend: Hellos(backend, hook),
            },
            api::app_host::now as fn() -> f64,
            nonce as fn() -> String,
        )))
    }
}
impl ObservePort for HostIdentityTestDouble {
    fn prepare(&mut self, run: &str, deployment: &str, cards: &[Value]) -> Result<(), PortError> {
        self.0.prepare(run, deployment, cards)
    }
    fn rebuild(
        &mut self,
        run: &str,
        deployment: &str,
        cards: &[Value],
    ) -> Result<String, PortError> {
        self.0.rebuild(run, deployment, cards)
    }
    fn facts_nodes(&mut self) -> Vec<String> {
        self.0.facts_nodes()
    }
    fn offers(&mut self) -> Vec<(String, String)> {
        self.0.offers()
    }
    fn collect(&mut self) -> Result<(), PortError> {
        // As the product's observe port: each attempt's outcome on the log (lane SR round 3).
        api::owner_compiler::collect_logged(&mut self.0)
    }
    fn observed_hosts(&mut self) -> Vec<Option<String>> {
        self.0.observed_hosts()
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        self.0.retire_bootstrap()
    }
}
impl ObserveDouble for HostIdentityTestDouble {
    fn context(&self) -> Option<&ObservationContext> {
        self.0.context()
    }
    fn teardown(&mut self) -> Result<(), String> {
        self.0.host_mut().backend.0.teardown()
    }
    fn end_run(&mut self) {
        self.0.end_run()
    }
}
