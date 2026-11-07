// SPDX-License-Identifier: AGPL-3.0-only
//! Borrowed managed serving lifecycle (workflow.py:165-351 @ f4e366e9).
//! No worker, launch authority, cached readiness or owning host is manufactured.
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[cfg(test)]
#[path = "managed_workflow_tests.rs"]
mod tests;

const VERIFIED_TOKEN_LIMIT: budgets::BudgetId =
    budgets::BudgetId::PyManagedWorkflowVerifiedChallengeTokenLimitL206C66;

// Private metadata only: no token value, proof body, prompt, or external sink.
struct Challenge {
    request: String,
    limit: u64,
    count: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkflowError {
    pub code: String,
    pub exception_class: String,
}
impl WorkflowError {
    pub fn new(code: impl Into<String>, exception_class: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            exception_class: exception_class.into(),
        }
    }
    pub fn refused(code: &str) -> Self {
        Self::new(code, "ValueError")
    }
}
pub type Result<T> = std::result::Result<T, WorkflowError>;

/// The host validates genuine prepared/model/resource approval and rereads its
/// operator plan binding. This is not permission to accept an absent launch.
#[derive(Clone, Debug)]
pub struct PreparedLaunch {
    pub model: Value,
    pub resource_proposal: Value,
    pub operator_plan: std::path::PathBuf,
    pub operator_plan_digest: String,
    pub physical_host_ids: Vec<String>,
}
impl PreparedLaunch {
    /// Source local approval guard only, not a signature or proof of human consent.
    /// Inputs are the integral, JSON-representable prepared-source contract.
    pub fn validate_approval(&self, approval: &Value) -> Result<()> {
        if !approval.is_object() {
            return Err(WorkflowError::refused("model_execution_approval_required"));
        }
        let same_model = [
            "model_id",
            "resolved_commit",
            "source_representation",
            "runtime_dtype",
            "runtime_quantization",
        ]
        .into_iter()
        .all(|key| self.model[key].as_str().is_some() && approval[key] == self.model[key]);
        let exact = [
            ("max_execution_download_bytes", "execution_download_bytes"),
            ("run_count", "run_count"),
        ]
        .into_iter()
        .all(|(a, p)| {
            self.resource_proposal[p].as_u64().is_some()
                && approval[a].as_u64() == self.resource_proposal[p].as_u64()
        });
        let enough = [
            ("max_additional_disk_bytes", "additional_disk_bytes"),
            ("max_combined_rss_bytes", "max_combined_rss_bytes"),
            ("max_wall_seconds", "max_wall_seconds"),
        ]
        .into_iter()
        .all(|(a, p)| {
            approval[a]
                .as_u64()
                .zip(self.resource_proposal[p].as_u64())
                .is_some_and(|(a, p)| a >= p)
        });
        if !same_model || !exact || !enough {
            return Err(WorkflowError::refused("model_execution_approval_mismatch"));
        }
        // Preserve short-circuit order: do not read plan bytes on a declared
        // digest/host mismatch, and do not substitute canonicalized JSON bytes.
        if approval["operator_plan_digest"] != self.operator_plan_digest
            || approval["physical_host_ids"] != json!(self.physical_host_ids)
            || plan_digest(&self.operator_plan)? != self.operator_plan_digest
        {
            return Err(WorkflowError::refused("managed_workflow_approval_mismatch"));
        }
        Ok(())
    }
}
#[cfg(unix)]
fn plan_digest(path: &std::path::Path) -> Result<String> {
    use std::io::Read;
    use std::os::unix::fs::OpenOptionsExt;
    let invalid = || WorkflowError::refused("operator_plan_invalid");
    let cap = budgets::BudgetId::PyMyceliumA13MacosServingMaxJsonBytesL360
        .bytes()
        .map_err(|_| invalid())?
        .get();
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)
        .map_err(|_| invalid())?;
    let metadata = file.metadata().map_err(|_| invalid())?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > cap {
        return Err(invalid());
    }
    let mut bytes = Vec::new();
    file.take(cap + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| invalid())?;
    if bytes.len() as u64 > cap {
        return Err(invalid());
    }
    Ok(protocol::canonical::sha256_prefixed(&bytes))
}
#[cfg(not(unix))]
fn plan_digest(_path: &std::path::Path) -> Result<String> {
    Err(WorkflowError::refused("operator_plan_platform_unavailable"))
}
/// Protocol port-range validation followed by a real, immediately closed IPv4
/// loopback bind probe; no listener/service is retained and no peer is contacted.
pub fn validate_dynamic_port(port: u16) -> Result<()> {
    // serving.py:358-359,886-898: protocol identifiers, not time/resource budgets.
    if [8791, 8876, 8877, 4350, 4351, 4352].contains(&port) {
        return Err(WorkflowError::refused("port_not_available_for_a13"));
    }
    if port < 49_152 {
        return Err(WorkflowError::refused("dynamic_loopback_port_required"));
    }
    let probe = std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port))
        .map_err(|_| WorkflowError::refused("dynamic_loopback_port_unavailable"))?;
    drop(probe);
    Ok(())
}
pub trait Launch {
    fn prepared_binding(&self) -> Result<&PreparedLaunch> {
        Err(WorkflowError::refused(
            "managed_workflow_launch_unavailable",
        ))
    }
    fn validate_approval(&self, approval: &Value) -> Result<()> {
        self.prepared_binding()?.validate_approval(approval)
    }
    fn validate_port(&self, port: u16) -> Result<()> {
        validate_dynamic_port(port)
    }
    fn run(
        &mut self,
        approval: &Value,
        port: u16,
        stop: &WorkflowView,
        hooks: &mut LaunchHooks<'_>,
    ) -> Result<Value>;
}
/// Actual compiled controller; prepare must install/use the published staging
/// adapter and preserve target consent. Publication alone is not preparation.
#[derive(Clone, Debug)]
pub struct Publication {
    pub transport: String,
    pub binding: Value,
    pub archive: Vec<u8>,
    pub preposition: Option<Value>,
}
pub trait Controller {
    /// Complete current-member/archive/preposition input batch, after the real
    /// controller transfer validation; no model bytes may be inferred here.
    fn publications(&mut self) -> Result<Vec<Publication>> {
        Err(WorkflowError::refused("managed_workflow_plan_unavailable"))
    }
    fn run_identity(&self) -> Result<(String, String)>;
    fn prepare(
        &mut self,
        authority: &mut dyn Authority,
        offers: &[Value],
        stop: &WorkflowView,
    ) -> Result<()>;
}
/// Existing authenticated mailbox adapter. Validate the COMPLETE archive/member/
/// preposition batch before publishing. Cleanup means REQUEST, never an ACK.
pub trait Authority {
    fn publish(
        &mut self,
        controller: &mut dyn Controller,
        run: &str,
        deployment: &str,
        _stop: &WorkflowView,
    ) -> Result<Vec<Value>> {
        let batch = controller.publications()?;
        if batch.is_empty() || batch.iter().any(|p| p.transport != "managed_https") {
            return Err(WorkflowError::refused("managed_workflow_plan_invalid"));
        }
        let cap = budgets::BudgetId::PyFinalMyceliumA13MacosWorkflowMaxArchiveBytesL87C48
            .bytes()
            .map_err(|_| WorkflowError::refused("managed_workflow_policy_unavailable"))?
            .get();
        // Validate every archive before the first durable mailbox write, as in
        // publish_controller_offers:86-121. The mailbox independently verifies
        // binding/preposition authority on each publication.
        if batch
            .iter()
            .any(|p| p.archive.is_empty() || p.archive.len() as u64 > cap)
        {
            return Err(WorkflowError::refused(
                "managed_workflow_archive_budget_exceeded",
            ));
        }
        batch
            .iter()
            .map(|p| self.publish_stage(p, run, deployment))
            .collect()
    }
    fn publish_stage(
        &mut self,
        _input: &Publication,
        _run: &str,
        _deployment: &str,
    ) -> Result<Value> {
        Err(WorkflowError::refused(
            "managed_workflow_publication_unavailable",
        ))
    }
    fn request_cleanup(&mut self, offer: &Value) -> Result<()>;
}
pub trait Activation {
    fn wait(&mut self, interval: Duration) -> Result<bool>;
    fn wall_time(&self) -> f64;
}
pub struct WorkflowPorts<'a> {
    pub authority: &'a mut dyn Authority,
    pub controller: &'a mut dyn Controller,
    pub launch: &'a mut dyn Launch,
}
struct State {
    stop: bool,
    phase: &'static str,
    error: Option<String>,
    startup: Option<Value>,
    identity: Option<(String, String, u64)>,
    challenge: Option<Challenge>,
    cleanup: Vec<Value>,
}
#[derive(Clone)]
pub struct WorkflowView {
    inner: Arc<Mutex<State>>,
}
impl WorkflowView {
    /// Linearizes against startup commitment. A view never owns a worker/join.
    pub fn stop(&self) {
        let mut s = self.inner.lock().unwrap();
        s.stop = true;
        s.challenge = None;
    }
    pub fn is_stopped(&self) -> bool {
        self.inner.lock().unwrap().stop
    }
    pub fn status(&self) -> Value {
        let s = self.inner.lock().unwrap();
        let mut result = json!({"state":s.phase,"error_code":s.error,"startup":s.startup,"challenge_progress":null,"readiness_source":"registry_api"});
        if s.phase == "starting"
            && !s.stop
            && let Some(challenge) = &s.challenge
            && challenge.count > 0
        {
            result["challenge_progress"] = json!({"verified_token_count":challenge.count});
        }
        if !s.cleanup.is_empty() {
            result["cleanup_errors"] = json!(s.cleanup);
        }
        result
    }
}
pub struct ManagedServingWorkflow {
    view: WorkflowView,
}
impl Default for ManagedServingWorkflow {
    fn default() -> Self {
        Self::new()
    }
}
impl ManagedServingWorkflow {
    pub fn new() -> Self {
        Self {
            view: WorkflowView {
                inner: Arc::new(Mutex::new(State {
                    stop: false,
                    phase: "prepared",
                    error: None,
                    startup: None,
                    identity: None,
                    challenge: None,
                    cleanup: vec![],
                })),
            },
        }
    }
    pub fn view(&self) -> WorkflowView {
        self.view.clone()
    }
    /// Embedded shutdown allowances only, NOT an IO/lock bound. The owning host
    /// supplies its actual controller peer count and caps by remaining wall time.
    pub fn close_wait(&self, peers: budgets::Entries) -> Result<Duration> {
        budgets::BudgetId::ConfiguredCloseWaitSum
            .span(peers)
            .map(|s| Duration::from_millis(s.millis()))
            .map_err(|_| WorkflowError::refused("managed_workflow_policy_unavailable"))
    }
    pub fn bind_progress_identity(&self, run: &str, deployment: &str, epoch: u64) -> Result<()> {
        if run.is_empty() || deployment.is_empty() {
            return Err(WorkflowError::refused("managed_progress_identity_invalid"));
        }
        let mut s = self.view.inner.lock().unwrap();
        if s.phase != "prepared" || s.identity.is_some() {
            return Err(WorkflowError::refused("managed_progress_identity_reused"));
        }
        s.identity = Some((run.into(), deployment.into(), epoch));
        Ok(())
    }
    /// Blocking, borrowing collaborators only until return. The caller owns the
    /// worker and genuine launch; this component owns only its short state lock.
    pub fn run(
        &self,
        ports: WorkflowPorts<'_>,
        approval: &Value,
        port: u16,
        mut activation: Option<&mut dyn Activation>,
        on_started: Option<&mut dyn FnMut(Value) -> Result<()>>,
    ) -> Result<Value> {
        // Source preflight failures have zero publication/cleanup/state effects.
        ports.launch.validate_approval(approval)?;
        ports.launch.validate_port(port)?;
        {
            let mut s = self.view.inner.lock().unwrap();
            if s.phase != "prepared" || s.stop {
                return Err(WorkflowError::refused("managed_workflow_not_startable"));
            }
            s.phase = "publishing";
        }
        let mut offers = Vec::new();
        let result = (|| {
            let (run, deployment) = ports.controller.run_identity()?;
            {
                let s = self.view.inner.lock().unwrap();
                if s.identity
                    .as_ref()
                    .is_some_and(|(r, d, _)| r != &run || d != &deployment)
                {
                    return Err(WorkflowError::refused("managed_progress_identity_changed"));
                }
            }
            offers = ports
                .authority
                .publish(ports.controller, &run, &deployment, &self.view)?;
            self.view.inner.lock().unwrap().phase = "staging";
            ports
                .controller
                .prepare(ports.authority, &offers, &self.view)?;
            if let Some(a) = activation.as_mut() {
                self.view.inner.lock().unwrap().phase = "awaiting_activation";
                let expires = offers
                    .iter()
                    .map(|o| {
                        o["expires_at"]
                            .as_f64()
                            .ok_or_else(|| WorkflowError::refused("managed_workflow_offer_invalid"))
                    })
                    .collect::<Result<Vec<_>>>()?
                    .into_iter()
                    .reduce(f64::min)
                    .ok_or_else(|| WorkflowError::refused("managed_workflow_offer_invalid"))?;
                let interval=Duration::from_millis(budgets::BudgetId::PyMyceliumA13MacosWorkflowManagedservingworkflowRunActivationEventWaitL323.span(budgets::Entries::ZERO).map_err(|_|WorkflowError::refused("managed_workflow_policy_unavailable"))?.millis());
                while !a.wait(interval)? {
                    if self.view.is_stopped() {
                        return Err(WorkflowError::refused("managed_workflow_stopped"));
                    }
                    if a.wall_time() >= expires {
                        return Err(WorkflowError::refused(
                            "managed_workflow_activation_expired",
                        ));
                    }
                }
                if a.wall_time() >= expires {
                    return Err(WorkflowError::refused(
                        "managed_workflow_activation_expired",
                    ));
                }
            }
            let token_limit = VERIFIED_TOKEN_LIMIT
                .count()
                .map_err(|_| WorkflowError::refused("managed_workflow_policy_unavailable"))?;
            {
                let mut s = self.view.inner.lock().unwrap();
                if s.stop {
                    return Err(WorkflowError::refused("managed_workflow_stopped"));
                }
                s.phase = "starting";
            }
            let mut hooks = LaunchHooks {
                view: self.view.clone(),
                deployment,
                token_limit,
                callback: on_started,
            };
            let value = ports.launch.run(approval, port, &self.view, &mut hooks)?;
            let mut s = self.view.inner.lock().unwrap();
            if s.phase != "failed" {
                s.phase = "stopped";
            }
            s.challenge = None;
            Ok(value)
        })();
        if let Err(error) = &result {
            let mut s = self.view.inner.lock().unwrap();
            s.challenge = None;
            if s.phase != "failed" {
                s.phase = "failed";
                s.error = Some(public_code(&error.code, 80, "managed_workflow_failed"));
            }
        }
        // Source finally: every request attempted, primary outcome retained.
        for offer in &offers {
            if let Err(error) = ports.authority.request_cleanup(offer) {
                self.record_cleanup_exception("workflow_cleanup_request_failed", &error);
            }
        }
        result
    }
    pub fn record_cleanup_exception(&self, code: &'static str, error: &WorkflowError) {
        let class = &error.exception_class;
        let class = if class.len() <= 80
            && !class.is_empty()
            && class
                .bytes()
                .enumerate()
                .all(|(i, c)| c == b'_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit()))
        {
            class.as_str()
        } else {
            "Exception"
        };
        let mut s = self.view.inner.lock().unwrap();
        if let Some(row) = s
            .cleanup
            .iter_mut()
            .find(|r| r["code"] == code && r["exception_class"] == class)
        {
            row["count"] = json!(row["count"].as_u64().unwrap().saturating_add(1));
        } else {
            s.cleanup
                .push(json!({"code":code,"exception_class":class,"count":1}));
        }
    }
}
fn public_code(code: &str, length: usize, fallback: &str) -> String {
    if !code.is_empty()
        && code.len() <= length
        && code
            .bytes()
            .enumerate()
            .all(|(i, c)| c.is_ascii_lowercase() || (i > 0 && (c == b'_' || c.is_ascii_digit())))
    {
        code.into()
    } else {
        fallback.into()
    }
}
/// Constructed only during this workflow's borrowed Launch::run call. No public
/// constructor or view injection route exists; callbacks cannot outlive the run.
/// The source's post-return callback capture has correspondence-only coverage:
/// Rust deliberately refuses that escape rather than exposing a test bypass.
///
/// ```compile_fail
/// use runtime::managed_workflow::*;
/// use serde_json::Value;
/// struct Escaping { saved: Option<&'static mut LaunchHooks<'static>> }
/// impl Launch for Escaping {
///     fn run(&mut self, _: &Value, _: u16, _: &WorkflowView,
///            hooks: &mut LaunchHooks<'_>) -> Result<Value> {
///         self.saved = Some(hooks);
///         Ok(Value::Null)
///     }
/// }
/// ```
///
/// ```compile_fail
/// use runtime::managed_workflow::*;
/// fn injected(view: WorkflowView) -> LaunchHooks<'static> {
///     LaunchHooks { view, deployment: "dep".into(), callback: None,
///         token_limit: budgets::Entries::new(4096) }
/// }
/// ```
pub struct LaunchHooks<'a> {
    view: WorkflowView,
    deployment: String,
    token_limit: budgets::Entries,
    callback: Option<&'a mut dyn FnMut(Value) -> Result<()>>,
}
impl LaunchHooks<'_> {
    /// Private startup progress from the launch's verified-route sink only.
    /// This is not a proof verifier or native readiness announcement. The caller
    /// must supply verified indices, never raw generation events. JSON numeric
    /// inputs preserve Python's exact-int rules and its distinct begin equality.
    pub fn verified_challenge_event(
        &self,
        deployment: &str,
        request: &str,
        kind: &str,
        index: &Value,
        limit: &Value,
    ) {
        let mut s = self.view.inner.lock().unwrap();
        if s.stop
            || s.phase != "starting"
            || s.identity.as_ref().is_none_or(|(_, d, _)| d != deployment)
            || request.len() != 40
            || !request.strip_prefix("startup-").is_some_and(|suffix| {
                suffix
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            })
        {
            return;
        }
        // as_u64 refuses bool, float, negative and out-of-domain JSON numbers.
        let Some(limit) = limit
            .as_u64()
            .filter(|n| *n > 0 && *n <= self.token_limit.get())
        else {
            return;
        };
        // Source index == -1 accepts representable -1.0 for begin. Do not apply
        // the token's type(index) is int guard before this branch.
        if kind == "begin" && index.as_f64() == Some(-1.0) {
            if s.challenge.is_none() {
                s.challenge = Some(Challenge {
                    request: request.into(),
                    limit,
                    count: 0,
                });
            }
            return;
        }
        if let Some(challenge) = &mut s.challenge
            && kind == "token"
            && request == challenge.request
            && limit == challenge.limit
            && index.as_u64() == Some(challenge.count)
            && challenge.count < limit
        {
            challenge.count += 1;
        }
    }
    pub fn started(&mut self, document: Value) -> Result<()> {
        {
            let mut s = self.view.inner.lock().unwrap();
            if s.stop || s.phase != "starting" {
                return Ok(());
            }
            s.startup = Some(document.clone());
            s.phase = "api_running";
        }
        if let Some(f) = &mut self.callback {
            f(document)
        } else {
            Ok(())
        }
    }
    pub fn startup_failure(&self, deployment: &str, code: &str) {
        let mut s = self.view.inner.lock().unwrap();
        if s.phase != "starting" || s.stop || deployment != self.deployment {
            return;
        }
        s.phase = "failed";
        s.error = Some(public_code(code, 64, "startup_route_unavailable"));
        s.challenge = None;
    }
}
