// SPDX-License-Identifier: AGPL-3.0-only
//! Exact-run CLIENT factory over the existing journal and host authority.
use super::*;
use crate::observe::{CloseFailed, HelloSession, HelloSessions};
pub struct ManagedSessionFactory<F> {
    owner: Arc<SharedManagedOwner<F>>,
    run: String,
    deployment: String,
    bindings: BTreeMap<String, Value>,
}
impl<F, H> ManagedSessionFactory<F>
where
    F: Fn() -> Result<H>,
    H: SessionHost,
{
    pub fn new(
        owner: Arc<SharedManagedOwner<F>>,
        run: &str,
        deployment: &str,
        members: &[Value],
    ) -> SessionResult<Self> {
        let bad = OwnerError("managed_session_factory_members_invalid");
        let mut bindings = BTreeMap::new();
        for member in members {
            let binding =
                binding_from_member(member, &json!(run), &json!(deployment)).map_err(|_| bad)?;
            let node = binding["target_node_id"].as_str().ok_or(bad)?.to_owned();
            if bindings.insert(node, binding).is_some() {
                return Err(bad.into());
            }
        }
        if bindings.is_empty() {
            return Err(bad.into());
        }
        Ok(Self {
            owner,
            run: run.to_owned(),
            deployment: deployment.to_owned(),
            bindings,
        })
    }
    pub fn open(
        &self,
        argv: &[String],
        node: &str,
        run: &str,
        deployment: &str,
        timeout_seconds: f64,
    ) -> SessionResult<ManagedNodeSession<F>> {
        if !argv.is_empty()
            || !self.bindings.contains_key(node)
            || run != self.run
            || deployment != self.deployment
            || !timeout_seconds.is_finite()
            || timeout_seconds <= 0.0
        {
            return Err(OwnerError("managed_session_factory_scope_mismatch").into());
        }
        let bad = OwnerError("managed_session_factory_member_not_current");
        let ceiling = span(B::PyMyceliumA13MacosControlSessionMaxSessionTimeoutSecondsL124)?;
        let millis = (timeout_seconds.min(ceiling.millis() as f64 / 1000.0) * 1000.0).ceil() as u64;
        let session = self
            .owner
            .session(node, run, deployment, Span::from_millis(millis))
            .map_err(|_| bad)?;
        if session.state.lock().map_err(|_| bad)?.binding != self.bindings[node] {
            return Err(bad.into());
        }
        Ok(session)
    }
    /// Fresh binding equality only; intentionally not lease/peer/owner-open admission.
    pub fn binding_current(&self, node: &str) -> bool {
        let current = (|| -> Result<Value> {
            let mut host = (self.owner.factory)()?;
            let member = host.current_member(node)?;
            binding_from_member(&member, &json!(self.run), &json!(self.deployment))
        })();
        current.is_ok_and(|binding| self.bindings.get(node) == Some(&binding))
    }
}
impl<F, H> HelloSessions for ManagedSessionFactory<F>
where
    F: Fn() -> Result<H> + Send + Sync,
    H: SessionHost,
{
    fn run_id(&self) -> &str {
        &self.run
    }
    fn deployment_id(&self) -> &str {
        &self.deployment
    }
    fn bound_nodes(&self) -> std::collections::BTreeSet<String> {
        self.bindings.keys().cloned().collect()
    }
    fn open(
        &mut self,
        node: &str,
        timeout_seconds: f64,
    ) -> std::result::Result<Box<dyn HelloSession + '_>, Option<String>> {
        ManagedSessionFactory::open(
            self,
            &[],
            node,
            &self.run,
            &self.deployment,
            timeout_seconds,
        )
        .map(|session| Box::new(session) as Box<dyn HelloSession>)
        .map_err(|error| Some(error.0))
    }
    fn binding_current(&mut self, node: &str) -> bool {
        ManagedSessionFactory::binding_current(self, node)
    }
}
impl<F, H> HelloSession for ManagedNodeSession<F>
where
    F: Fn() -> Result<H>,
    H: SessionHost,
{
    fn send_hello(&mut self, id: &str) -> std::result::Result<Value, Option<String>> {
        self.send(id, "hello", json!({})).map_err(|e| Some(e.0))
    }
    fn close(&mut self) -> std::result::Result<(), CloseFailed> {
        ManagedNodeSession::close(self).map_err(|_| CloseFailed)
    }
    fn cleanup_error(&self) -> bool {
        ManagedNodeSession::cleanup_error(self).is_some()
    }
    fn stop_response_ok(&self) -> Option<bool> {
        self.stop_response().map(|r| r["ok"] == true)
    }
}
