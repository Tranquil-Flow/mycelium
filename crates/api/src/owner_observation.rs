// SPDX-License-Identifier: AGPL-3.0-only
//! Production ownership: one CLIENT journal per seed, fresh factory per run.
//!
//! The owner's observe port (lane PH3b S3m; moved unchanged from the headless launcher,
//! `examples/headless/observation.rs`): the managed owner bound on the seed's mailbox, the owner's
//! own local target ([`target`]) and the staged bootstrap of its local contributor. A host
//! implements [`ChainHost`] over its own state; no step logic lives here.
use crate::app_host::{Authority, ManagedSlot, hex, now, random};
use crate::managed_carrier::{HostClock, LocalMailboxCarrier};
use crate::observation_host::{ObservationBackend, VerifiedObservationHost};
use crate::owner_ports::{RunFields, RunHost};
use crate::{acquisition::Acquisition, bootstrap_process::CleanupFailure};
use base64::Engine;
use budgets::{BudgetId, Entries, MonoInstant, Span};
use control_runtime::mailbox::{Limits, Mailbox, StagePolicy, StagePublication};
use protocol::identity::Identity;
use runtime::{
    managed_owner::{
        ManagedControlOwner, OwnerError, OwnerHost,
        session::{CallFence, ManagedSessionFactory, SessionHost, SharedManagedOwner},
    },
    observe::{HelloSessions, ObservationContext, ObservationError, Offer},
    observe_owner::ObserveAdapter,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::sync::{Arc, Mutex, MutexGuard, Weak};
use std::thread;
use std::time::Duration;
use transport::Clock;
pub mod target;

/// The owner's seed runtime, with this module's managed owner.
type SeedRuntime<H> = crate::app_host::SeedRuntime<Managed<H>>;

/// One acquisition of the host's state for the observation and compile ports: the authority
/// (`Deref`), the run fields and the owner's bootstrap custody beside them.
pub struct ChainView<'a, H: ChainHost> {
    pub authority: &'a mut Authority<Managed<H>>,
    pub run: &'a mut RunFields,
    /// `DemoApp.closed`.
    pub closed: bool,
    /// `app.acquisition`.
    pub acquisition: &'a Option<Acquisition>,
    /// Serializes bootstrap side effects and cleanup; never held with the state on entry.
    pub bootstrap_lifecycle: &'a Arc<Mutex<()>>,
    /// Identifies the contributor incarnation a bootstrap reservation belongs to.
    pub bootstrap_incarnation: &'a mut Arc<()>,
    /// The owner's own local target worker.
    pub bootstrap: &'a mut Option<target::Worker>,
    /// A bootstrap whose cleanup is still unproven.
    pub bootstrap_failure: &'a mut Option<CleanupFailure>,
    /// The run's compiler as the public compile route and the serving status reach it.
    pub compiler: &'a Option<Weak<Mutex<crate::owner_compiler::Compiler<H>>>>,
}
impl<H: ChainHost> std::ops::Deref for ChainView<'_, H> {
    type Target = Authority<Managed<H>>;
    fn deref(&self) -> &Self::Target {
        self.authority
    }
}
impl<H: ChainHost> std::ops::DerefMut for ChainView<'_, H> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.authority
    }
}

/// The host side of the observation (and compile) ports, beside its [`RunHost`].
pub trait ChainHost: RunHost<Managed = Managed<Self>> {
    /// A handle that never keeps the host's state alive (the ports retain only this).
    type Weak: Clone + Send + Sync + 'static;
    fn downgrade(&self) -> Self::Weak;
    fn upgrade(weak: &Self::Weak) -> Option<Self>;
    /// Run `f` on the state as one snapshot; `None` when the state's lock is poisoned.
    fn try_with<R>(&self, f: impl FnOnce(&mut ChainView<'_, Self>) -> R) -> Option<R>;
    /// The resource gate the owner's own target checks on every managed-control exchange.
    fn exchange_resource_check(&self) -> Option<&'static str>;
    /// A host's hold before the local bootstrap stage is created, kept until startup returns
    /// (the headless test launcher's loopback seam). None by default.
    fn startup_seam(&self) -> Result<Option<std::net::TcpStream>, OwnerError> {
        Ok(None)
    }
}

impl<H: ChainHost> ManagedSlot for Managed<H> {
    fn close(&self) -> Result<(), &'static str> {
        Managed::close(self).map_err(|error| error.0)
    }
}

type OResult<T> = Result<T, OwnerError>;
fn owner_error(error: control_runtime::Error) -> OwnerError {
    match error {
        control_runtime::Error::Refused(code) => OwnerError(code),
        _ => OwnerError("managed_mailbox_unavailable"),
    }
}
fn observe(error: OwnerError) -> ObservationError {
    ObservationError::new(error.0)
}
fn locked<T>(m: &Mutex<T>) -> OResult<MutexGuard<'_, T>> {
    m.lock().map_err(|_| OwnerError("managed_host_unavailable"))
}
/// `locked(core)?` over a [`ChainHost`]: the host's state as one snapshot, the same refusal when
/// its lock is poisoned.
fn viewed<H: ChainHost, R>(host: &H, f: impl FnOnce(&mut ChainView<'_, H>) -> R) -> OResult<R> {
    host.try_with(f)
        .ok_or(OwnerError("managed_host_unavailable"))
}
fn managed_time_limits() -> OResult<protocol::TimeLimits> {
    let seconds = |id: BudgetId| {
        id.span(Entries::ZERO)
            .map(|v| v.millis() as f64 / 1000.0)
            .map_err(|_| OwnerError("budget_unavailable"))
    };
    Ok(protocol::TimeLimits {
        max_ttl_seconds: seconds(BudgetId::ControlTtl)?,
        clock_skew_seconds: seconds(BudgetId::ControlClockSkew)?,
    })
}
fn nonce() -> OResult<String> {
    random(16)
        .map(|b| hex(&b))
        .map_err(|_| OwnerError("managed_control_entropy_unavailable"))
}

pub type Adapter<H> =
    ObserveAdapter<VerifiedObservationHost<Backend<H>>, fn() -> f64, fn() -> String>;
pub fn adapter<H: ChainHost>(core: H::Weak) -> Adapter<H> {
    ObserveAdapter::new(
        VerifiedObservationHost {
            backend: Backend {
                core,
                published: BTreeMap::new(),
            },
        },
        now,
        || nonce().unwrap_or_default(),
    )
}
pub struct Backend<H: ChainHost> {
    pub core: H::Weak,
    // Live effect handles only; durable truth stays in the actual mailbox.
    pub published: BTreeMap<String, Offer>,
}
/// Only weak authority references; the immutable signer is not authority.
pub struct PinnedHost<H: ChainHost> {
    core: H::Weak,
    seed: Weak<SeedRuntime<H>>,
    clock: HostClock,
    pub signer: Arc<Identity>,
}
impl<H: ChainHost> Clone for PinnedHost<H> {
    fn clone(&self) -> Self {
        Self {
            core: self.core.clone(),
            seed: self.seed.clone(),
            clock: self.clock.clone(),
            signer: self.signer.clone(),
        }
    }
}
impl<H: ChainHost> PinnedHost<H> {
    pub fn pin(&self) -> OResult<Arc<SeedRuntime<H>>> {
        let core = H::upgrade(&self.core).ok_or(OwnerError("managed_owner_closed"))?;
        let seed = self
            .seed
            .upgrade()
            .ok_or(OwnerError("managed_owner_closed"))?;
        {
            let stale = viewed(&core, |core| {
                !core.running
                    || core.target
                    || !core.seed.as_ref().is_some_and(|s| Arc::ptr_eq(s, &seed))
            })?;
            if stale {
                return Err(OwnerError("managed_owner_closed"));
            }
        }
        if *locked(&seed.managed_closed)? {
            return Err(OwnerError("managed_owner_closed"));
        }
        Ok(seed)
    }
    pub fn member(&self, node: &str) -> OResult<Value> {
        let seed = self.pin()?;
        let descriptor = seed
            .coordinator
            .with_current_member(node, |m, now| {
                if !matches!(
                    m["lifecycle_state"].as_str(),
                    Some("NEW" | "CONFIGURED" | "RUNNING")
                ) || m["lease_expires_at"].as_f64().is_none_or(|end| now >= end)
                {
                    return Err(seed::SeedError::new("managed_control_member_stale"));
                }
                Ok(m.clone())
            })
            .map_err(|_| OwnerError("managed_control_member_stale"))?;
        self.pin()?;
        Ok(descriptor)
    }
    /// Core is never held through coordinator, mailbox or transport operations.
    /// Fresh binding is synchronized only under CURRENT coordinator authority.
    fn guarded<T>(
        &self,
        node: &str,
        run: &Value,
        deployment: &Value,
        f: impl FnOnce(&mut Mailbox, &Value, f64) -> OResult<T>,
    ) -> OResult<T> {
        let seed = self.pin()?;
        // Reuse the actual RunExchange report, not a second semantic journal.
        // Descriptor crypto and all snapshot preparation precede authority guard.
        let card = seed.exchange.reports()[node]["descriptor"].clone();
        let discovery = run == "stage-discovery" && deployment == "stage-discovery";
        if !discovery
            && (card["statement"]["run_id"] != *run
                || card["statement"]["deployment_id"] != *deployment)
        {
            return Err(OwnerError("managed_control_run_binding_mismatch"));
        }
        let snapshot = self.member(node)?;
        crate::installation_descriptor::verify_installation_descriptor(
            &card,
            &snapshot,
            seed.coordinator.swarm_id(),
            card["statement"]["run_id"]
                .as_str()
                .ok_or(OwnerError("managed_control_run_invalid"))?,
            card["statement"]["deployment_id"]
                .as_str()
                .ok_or(OwnerError("managed_control_deployment_invalid"))?,
            self.wall_seconds(),
        )
        .map_err(|_| OwnerError("managed_control_descriptor_unavailable"))?;
        let expected = binding(&snapshot, run, deployment)?;
        let key = card["verification_key"].clone();
        let result = {
            let closed = locked(&seed.managed_closed)?;
            if *closed {
                return Err(OwnerError("managed_owner_closed"));
            }
            let managed = locked(&seed.managed)?
                .as_ref()
                .cloned()
                .ok_or(OwnerError("managed_owner_unavailable"))?;
            seed.coordinator
                .with_current_member(node, |current, now| {
                    let effect = (|| {
                        if !matches!(
                            current["lifecycle_state"].as_str(),
                            Some("NEW" | "CONFIGURED" | "RUNNING")
                        ) || current["lease_expires_at"]
                            .as_f64()
                            .is_none_or(|end| now >= end)
                        {
                            return Err(OwnerError("managed_control_member_stale"));
                        }
                        let binding = binding(current, run, deployment)?;
                        if binding != expected {
                            return Err(OwnerError("managed_control_member_stale"));
                        }
                        let mut mailbox = locked(&managed.mailbox)?;
                        mailbox
                            .bind_member(
                                &binding,
                                &key,
                                current["lease_expires_at"]
                                    .as_f64()
                                    .ok_or(OwnerError("managed_control_member_stale"))?,
                            )
                            .map_err(owner_error)?;
                        f(&mut mailbox, &binding, now)
                    })();
                    Ok(effect)
                })
                .map_err(|_| OwnerError("managed_control_member_stale"))??
        };
        self.pin()?;
        Ok(result)
    }
    fn raw(&self, prepared: &Value, end: MonoInstant, fence: Option<&CallFence>) -> OResult<Value> {
        let seed = self.pin()?;
        let managed = locked(&seed.managed)?
            .as_ref()
            .cloned()
            .ok_or(OwnerError("managed_owner_unavailable"))?;
        let statement = &prepared["envelope"]["statement"];
        let node = statement["target_node_id"]
            .as_str()
            .ok_or(OwnerError("managed_control_member_stale"))?;
        let expected = binding(
            &self.member(node)?,
            &statement["run_id"],
            &statement["deployment_id"],
        )?;
        if expected
            .as_object()
            .ok_or(OwnerError("managed_control_member_stale"))?
            .iter()
            .any(|(k, v)| statement[k] != *v)
        {
            return Err(OwnerError("managed_control_member_stale"));
        }
        let bytes = protocol::canonical::to_bytes(&prepared["envelope"])
            .map_err(|_| OwnerError("managed_control_json_invalid"))?;
        let reply = managed.carrier.call(&self.clock, &bytes, end, fence)?;
        self.pin()?;
        if binding(
            &self.member(node)?,
            &statement["run_id"],
            &statement["deployment_id"],
        )? != expected
        {
            return Err(OwnerError("managed_control_member_stale"));
        }
        protocol::strict::object(&reply, protocol::managed::MAX_FRAME_BYTES)
            .map_err(|_| OwnerError("managed_control_json_invalid"))
    }
}
fn binding(member: &Value, run: &Value, deployment: &Value) -> OResult<Value> {
    let b = json!({"target_node_id":member["node_id"], "target_endpoint_id":member["endpoint_id"],
        "target_key_digest":member["verification_key_digest"], "membership_generation":member["generation"],
        "member_incarnation":member["incarnation"], "run_id":run, "deployment_id":deployment});
    protocol::managed::validate_binding(&b).map_err(OwnerError::from)?;
    Ok(b)
}
type Factory<H> = Box<dyn Fn() -> OResult<PinnedHost<H>> + Send + Sync>;
/// The owner mailbox's authenticated dispatcher: exact signed bytes in, signed answer out.
type MailboxDispatch = dyn Fn(&[u8]) -> OResult<Vec<u8>> + Send + Sync;
/// Seed-owned capabilities. No Runner, Core, or temporary ports are retained.
pub struct Managed<H: ChainHost> {
    owner: Arc<SharedManagedOwner<Factory<H>>>,
    mailbox: Arc<Mutex<Mailbox>>,
    carrier: LocalMailboxCarrier,
    wake: Arc<target::Wake>,
}
impl<H: ChainHost> Managed<H> {
    pub fn close(&self) -> OResult<()> {
        self.carrier.revoke();
        // This releases the durable journal lock, NEVER claims remote Stop.
        let owner = self.owner.close();
        let carrier = self.carrier.close();
        owner.and(carrier)
    }
}
pub fn limits() -> OResult<Limits> {
    Ok(Limits {
        pending: BudgetId::PyMyceliumA13MacosManagedControlMaxPendingCommandsL106
            .count()
            .map_err(|_| OwnerError("budget_unavailable"))?,
        active_control: active_control()?,
        active_work: active_work()?,
    })
}
pub fn policy() -> OResult<StagePolicy> {
    Ok(StagePolicy {
        preposition_file_bytes: BudgetId::PyUpperGuardMyceliumA13MacosManagedOffersL58C52
            .bytes()
            .map_err(|_| OwnerError("budget_unavailable"))?,
        path_characters: path_characters()?,
    })
}
// Exact f4e366e9 semantic Count producers (L1b r25); no unrelated aliases.
fn active_control() -> OResult<Entries> {
    BudgetId::PyManagedControlMaxActiveControl
        .count()
        .map_err(|_| OwnerError("budget_unavailable"))
}
fn active_work() -> OResult<Entries> {
    BudgetId::PyManagedControlMaxActiveWork
        .count()
        .map_err(|_| OwnerError("budget_unavailable"))
}
fn path_characters() -> OResult<Entries> {
    BudgetId::PyStagePrepositionPathCharacters
        .count()
        .map_err(|_| OwnerError("budget_unavailable"))
}

impl<H: ChainHost> Backend<H> {
    pub fn teardown(&mut self) -> Result<(), String> {
        let mut first = None;
        for offer in self.published.values().cloned().collect::<Vec<_>>() {
            let result = self.request_stage_cleanup(&offer).and_then(|()| {
                if let Some(core) = H::upgrade(&self.core) {
                    target::close(&core).map_err(observe)?;
                }
                self.retire_offer(&offer)
            });
            match result {
                Ok(_) => {
                    self.published.remove(&offer.offer_id);
                }
                Err(error) => {
                    if first.is_none() {
                        first = Some(error.0);
                    }
                }
            }
        }
        first.map_or(Ok(()), Err)
    }
    pub fn host(&self) -> OResult<PinnedHost<H>> {
        let core = H::upgrade(&self.core).ok_or(OwnerError("managed_owner_closed"))?;
        let seed = {
            viewed(&core, |core| core.seed.clone())?.ok_or(OwnerError("managed_owner_closed"))?
        };
        let host = PinnedHost {
            core: self.core.clone(),
            seed: Arc::downgrade(&seed),
            clock: seed.clock.clone(),
            signer: seed.coordinator.shared_signer(),
        };
        host.pin()?;
        Ok(host)
    }
    pub fn managed(&self, host: &PinnedHost<H>) -> OResult<Arc<Managed<H>>> {
        let seed = host.pin()?;
        // This is a creation lock, not the Core or the publication lock.
        let _creation = locked(&seed.managed_init)?;
        host.pin()?;
        if let Some(managed) = locked(&seed.managed)?.as_ref() {
            return Ok(managed.clone());
        }
        fs::create_dir_all(&seed.managed_path)
            .map_err(|_| OwnerError("managed_mailbox_unavailable"))?;
        fs::set_permissions(
            &seed.managed_path,
            std::os::unix::fs::PermissionsExt::from_mode(0o700),
        )
        .map_err(|_| OwnerError("managed_mailbox_unavailable"))?;
        let sqlite_busy =
            BudgetId::PyMyceliumA13MacosManagedControlManagedcontrolmailboxConnectTimeoutL823
                .span(Entries::ZERO)
                .map_err(|_| OwnerError("budget_unavailable"))?;
        let store = control_runtime::Store::open_with_policy(
            &seed.managed_path.join("mailbox.sqlite"),
            None,
            sqlite_busy,
        )
        .map_err(owner_error)?;
        let mailbox = Arc::new(Mutex::new(
            Mailbox::new_shared(
                store,
                seed.coordinator.shared_signer(),
                seed.coordinator.swarm_id(),
                seed.coordinator.seed_node_id(),
                limits()?,
            )
            .map_err(owner_error)?
            .with_stage_policy(policy()?)
            .map_err(owner_error)?,
        ));
        let journal = ManagedControlOwner::open(
            &seed.managed_path.join("owner"),
            &host.signer,
            seed.coordinator.swarm_id(),
            seed.coordinator.seed_node_id(),
            runtime::managed_owner::Limits::registered()?,
        )?;
        let weak_host = host.clone();
        let factory: Factory<H> = Box::new(move || {
            weak_host.pin()?;
            Ok(weak_host.clone())
        });
        let owner = SharedManagedOwner::new(journal, factory);
        let wake = Arc::new(target::Wake::default());
        let dispatch_wake = wake.clone();
        let dispatch_host = host.clone();
        // One dispatcher for both carriers: this process's own target over the local carrier, a
        // joined member's target over the seed's managed-control routes (same authentication).
        let dispatch: Arc<MailboxDispatch> = Arc::new(move |raw| {
            let value = protocol::strict::object(raw, protocol::managed::MAX_FRAME_BYTES)
                .map_err(|_| OwnerError("invalid_control_call"))?;
            let statement = &value["statement"];
            let node = statement["target_node_id"]
                .as_str()
                .ok_or(OwnerError("invalid_control_call"))?;
            // Entropy and parsing happen before the coordinator transaction.
            let token = nonce()?;
            let result = dispatch_host.guarded(
                node,
                &statement["run_id"],
                &statement["deployment_id"],
                |mailbox, _, now| {
                    if statement["protocol"]
                        .as_str()
                        .is_some_and(|p| p.starts_with("mycelium.managed_stage_"))
                    {
                        mailbox.dispatch_stage(raw, || now).map_err(owner_error)
                    } else {
                        mailbox
                            .dispatch(raw, || now, || Ok(token))
                            .map_err(owner_error)
                    }
                },
            );
            if statement["protocol"] == "mycelium.managed_control.owner_command.v1" {
                dispatch_wake.notify()?;
            }
            result
        });
        let local = dispatch.clone();
        let carrier = LocalMailboxCarrier::start(move |raw| local(raw))?;
        seed.mailbox_routes
            .bind(Arc::new(move |raw: &[u8]| dispatch(raw).map_err(|e| e.0)));
        host.pin()?;
        let managed = Arc::new(Managed {
            owner,
            mailbox,
            carrier,
            wake,
        });
        {
            let closed = locked(&seed.managed_closed)?;
            if *closed {
                return Err(OwnerError("managed_owner_closed"));
            }
            *locked(&seed.managed)? = Some(managed.clone());
        }
        host.pin()?;
        Ok(managed)
    }
    fn offer_effect<T>(
        &self,
        offer: &Offer,
        f: impl FnOnce(&mut Mailbox, f64) -> OResult<T>,
    ) -> OResult<T> {
        let node = offer.raw["target_node_id"]
            .as_str()
            .ok_or(OwnerError("offer_fields_invalid"))?;
        self.host()?.guarded(
            node,
            &offer.raw["run_id"],
            &offer.raw["deployment_id"],
            |mailbox, _, now| f(mailbox, now),
        )
    }
}
impl<H: ChainHost> Clock for PinnedHost<H> {
    fn monotonic(&self) -> MonoInstant {
        self.clock.monotonic()
    }
    fn wall_seconds(&self) -> f64 {
        self.clock.wall_seconds()
    }
}
impl<H: ChainHost> OwnerHost for PinnedHost<H> {
    fn check_local(&self) -> OResult<()> {
        self.pin().map(drop)
    }
    fn signer(&self) -> &Identity {
        &self.signer
    }
    fn nonce(&mut self) -> OResult<String> {
        self.pin()?;
        nonce()
    }
    fn current_member(&mut self, node: &str) -> OResult<Value> {
        self.member(node)
    }
    fn exchange(&mut self, prepared: &Value, end: MonoInstant) -> OResult<Value> {
        self.raw(prepared, end, None)
    }
    fn wait(&mut self, allowance: Span, end: MonoInstant) -> OResult<()> {
        self.pin()?;
        let remaining = end
            .remaining(self.monotonic())
            .map_err(|_| OwnerError("managed_control_timeout"))?;
        thread::sleep(Duration::from_millis(allowance.min(remaining).millis()));
        self.pin().map(drop)
    }
}
impl<H: ChainHost> SessionHost for PinnedHost<H> {
    fn exchange_fenced(
        &mut self,
        prepared: &Value,
        end: MonoInstant,
        fence: &CallFence,
    ) -> OResult<Value> {
        fence.check()?;
        self.raw(prepared, end, Some(fence))
    }
    fn wait_fenced(&mut self, allowance: Span, end: MonoInstant, fence: &CallFence) -> OResult<()> {
        self.pin()?;
        let remaining = end
            .remaining(self.monotonic())
            .map_err(|_| OwnerError("managed_control_timeout"))?;
        fence.wait_for(allowance.min(remaining))?;
        self.pin().map(drop)
    }
}
fn read_offer<H: ChainHost>(
    host: &PinnedHost<H>,
    signer: &Identity,
    member: &Value,
    offer_id: Option<&str>,
    offset: u64,
) -> OResult<Value> {
    let seed = host.pin()?;
    let scope = binding(member, &json!("stage-discovery"), &json!("stage-discovery"))?;
    let mut statement = scope.clone();
    let now = host.wall_seconds();
    let ttl = BudgetId::PyQuantityMyceliumA13MacosManagedOffersL371C28
        .span(Entries::ZERO)
        .map_err(|_| OwnerError("budget_unavailable"))?;
    let request_id = nonce()?;
    for (k, v) in
        json!({"protocol":"mycelium.managed_stage_read.v1", "swarm_id":seed.coordinator.swarm_id(),
        "request_id":request_id, "sequence":1, "nonce":nonce()?, "issued_at":now,
        "expires_at":now+ttl.millis() as f64/1000.0, "offer_id":offer_id, "offset":offset})
        .as_object()
        .ok_or(OwnerError("invalid_control_call"))?
    {
        statement[k] = v.clone();
    }
    let envelope = json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,
        "signature":signer.sign(&statement).map_err(|_| OwnerError("managed_control_sign_failed"))?,
        "verification_key":signer.public_key_record(), "statement":statement});
    let timeout = BudgetId::PyMyceliumA13MacosManagedControlManagedcontrolclientInitTimeoutL1933
        .span(Entries::ZERO)
        .map_err(|_| OwnerError("budget_unavailable"))?;
    let end = host
        .monotonic()
        .checked_add(timeout)
        .map_err(|_| OwnerError("managed_control_timeout"))?;
    let response = host.raw(&json!({"envelope":envelope}), end, None)?;
    let body = protocol::managed::verify(
        &response,
        "mycelium.managed_stage_bytes.v1",
        &host.signer.public_key_record(),
        host.signer.endpoint_id(),
        host.wall_seconds(),
        managed_time_limits()?,
    )
    .map_err(OwnerError::from)?;
    let allowed = [
        "protocol",
        "request_id",
        "swarm_id",
        "owner_node_id",
        "owner_endpoint_id",
        "issued_at",
        "expires_at",
        "target_node_id",
        "target_endpoint_id",
        "target_key_digest",
        "membership_generation",
        "member_incarnation",
        "run_id",
        "deployment_id",
        "offer",
        "offset",
        "data",
    ];
    let object = body
        .as_object()
        .ok_or(OwnerError("offer_response_invalid"))?;
    if object.len() != allowed.len()
        || allowed.iter().any(|k| !object.contains_key(*k))
        || body["request_id"] != request_id
        || body["swarm_id"] != seed.coordinator.swarm_id()
        || body["owner_node_id"] != seed.coordinator.seed_node_id()
        || body["owner_endpoint_id"] != host.signer.endpoint_id()
        || body["offset"] != offset
        || (offer_id.is_none() && !body["data"].is_null())
        || scope
            .as_object()
            .ok_or(OwnerError("offer_response_invalid"))?
            .iter()
            .any(|(k, v)| body[k] != *v)
    {
        return Err(OwnerError("offer_response_invalid"));
    }
    if end
        .remaining(host.monotonic())
        .map_err(|_| OwnerError("managed_control_timeout"))?
        == Span::ZERO
    {
        return Err(OwnerError("managed_control_timeout"));
    }
    if binding(
        &host.member(
            member["node_id"]
                .as_str()
                .ok_or(OwnerError("managed_control_member_stale"))?,
        )?,
        &json!("stage-discovery"),
        &json!("stage-discovery"),
    )? != scope
    {
        return Err(OwnerError("managed_control_member_stale"));
    }
    Ok(body)
}

/// Exercise the real signed staging-discovery carrier before naming the absent
/// native collaborator. A receipt or offer alone MUST NOT promote the local agent.
pub fn bootstrap<H: ChainHost>(
    core: &H,
    node: &str,
    run: &str,
    deployment: &str,
    offer_id: &str,
) -> OResult<()> {
    let backend = Backend::<H> {
        core: core.downgrade(),
        published: BTreeMap::new(),
    };
    let host = backend.host()?;
    let seed = host.pin()?;
    let signer = viewed(core, |core| {
        let local = core
            .run
            .contributor
            .as_ref()
            .ok_or(OwnerError("managed_control_member_stale"))?;
        if local.node != node {
            return Err(OwnerError("managed_control_member_stale"));
        }
        Ok(local.signer.clone())
    })??;
    let member = host.member(node)?;
    if signer.verification_key_digest() != member["verification_key_digest"] {
        return Err(OwnerError("managed_control_member_stale"));
    }
    let body = read_offer(&host, &signer, &member, None, 0)?;
    let actual = &body["offer"];
    let expected = binding(&member, &json!(run), &json!(deployment))?;
    if actual["offer_id"] != offer_id
        || actual["expires_at"]
            .as_f64()
            .is_none_or(|t| t <= host.wall_seconds())
        || expected
            .as_object()
            .ok_or(OwnerError("offer_response_invalid"))?
            .iter()
            .any(|(k, v)| actual[k] != *v)
    {
        return Err(OwnerError("offer_response_invalid"));
    }
    // This model-free archive is the exact canonical source-produced input:
    // digest/scope are not permission to execute any peer-supplied program.
    let wanted = runtime::observe::bootstrap_archive(run, deployment);
    if actual.as_object().is_none_or(|o| o.len() != 11)
        || actual["archive_bytes"].as_u64() != Some(wanted.len() as u64)
        || actual["archive_digest"] != protocol::artifact_grant::range_digest(&wanted)
    {
        return Err(OwnerError("offer_archive_digest_mismatch"));
    }
    let authorize = || -> OResult<()> {
        host.pin()?;
        let demo = viewed(core, |c| c.run.demo.clone())?;
        if !demo.active() {
            return Err(OwnerError("owner_run_not_active"));
        }
        if let Some(code) = core.resource_check() {
            return Err(OwnerError(code));
        }
        let announcement = seed.exchange.announcement(Some(node)).to_value();
        if announcement["run_id"] != run
            || announcement["deployment_id"] != deployment
            || announcement["bootstrap_offer_id"] != offer_id
            || announcement["phase"] != "observe"
            || binding(&host.member(node)?, &json!(run), &json!(deployment))? != expected
        {
            return Err(OwnerError("offer_transfer_not_authorized"));
        }
        Ok(())
    };
    let chunk = BudgetId::PyMyceliumA13MacosManagedOffersChunkL31
        .bytes()
        .map_err(|_| OwnerError("budget_unavailable"))?
        .get();
    let mut archive = Vec::with_capacity(wanted.len());
    while archive.len() < wanted.len() {
        authorize()?;
        let result = read_offer(
            &host,
            &signer,
            &member,
            Some(offer_id),
            archive.len() as u64,
        )?;
        if result["offer"] != *actual {
            return Err(OwnerError("offer_changed"));
        }
        let data = base64::engine::general_purpose::STANDARD
            .decode(
                result["data"]
                    .as_str()
                    .ok_or(OwnerError("offer_response_invalid"))?,
            )
            .map_err(|_| OwnerError("offer_response_invalid"))?;
        if data.len() as u64 != chunk.min((wanted.len() - archive.len()) as u64) {
            return Err(OwnerError("offer_response_invalid"));
        }
        archive.extend(data);
    }
    authorize()?;
    if archive != wanted
        || protocol::artifact_grant::range_digest(&archive) != actual["archive_digest"]
    {
        return Err(OwnerError("offer_archive_digest_mismatch"));
    }
    if actual["expires_at"]
        .as_f64()
        .is_none_or(|t| t <= host.wall_seconds())
    {
        return Err(OwnerError("offer_expired"));
    }
    target::start(core, host, signer, expected, actual.clone(), archive)
}

/// A signed staging statement of the owner's own contributor for `node`, over the local mailbox
/// carrier (the statement a joined Mac posts to the seed's managed-control routes), verified as
/// `answer` (the member side is `crate::member_agent`'s `stage_call`).
fn own_stage_call<H: ChainHost>(
    core: &H,
    node: &str,
    protocol: &str,
    extra: Value,
    answer: &str,
) -> OResult<Value> {
    let backend = Backend::<H> {
        core: core.downgrade(),
        published: BTreeMap::new(),
    };
    let host = backend.host()?;
    let seed = host.pin()?;
    let signer = viewed(core, |c| {
        c.run
            .contributor
            .as_ref()
            .filter(|local| local.node == node)
            .map(|local| local.signer.clone())
            .ok_or(OwnerError("managed_control_member_stale"))
    })??;
    let member = host.member(node)?;
    if signer.verification_key_digest() != member["verification_key_digest"] {
        return Err(OwnerError("managed_control_member_stale"));
    }
    let scope = binding(&member, &json!("stage-discovery"), &json!("stage-discovery"))?;
    let mut statement = scope.clone();
    let now = host.wall_seconds();
    let ttl = BudgetId::PyQuantityMyceliumA13MacosManagedOffersL371C28
        .span(Entries::ZERO)
        .map_err(|_| OwnerError("budget_unavailable"))?;
    let request_id = nonce()?;
    for (k, v) in json!({"protocol": protocol, "swarm_id": seed.coordinator.swarm_id(),
        "request_id": request_id, "sequence": 1, "nonce": nonce()?, "issued_at": now,
        "expires_at": now + ttl.millis() as f64 / 1000.0})
    .as_object()
    .into_iter()
    .flatten()
    .chain(extra.as_object().into_iter().flatten())
    {
        statement[k] = v.clone();
    }
    let envelope = json!({"protocol": protocol::managed::SIGNED_ENVELOPE_PROTOCOL,
        "signature": signer.sign(&statement).map_err(|_| OwnerError("managed_control_sign_failed"))?,
        "verification_key": signer.public_key_record(), "statement": statement});
    let end = host
        .monotonic()
        .checked_add(ttl)
        .map_err(|_| OwnerError("managed_control_timeout"))?;
    let response = host.raw(&json!({"envelope": envelope}), end, None)?;
    let body = protocol::managed::verify(
        &response,
        answer,
        &host.signer.public_key_record(),
        host.signer.endpoint_id(),
        host.wall_seconds(),
        managed_time_limits()?,
    )
    .map_err(OwnerError::from)?;
    let offer = &body["offer"];
    if body["request_id"] != request_id
        || body["swarm_id"] != seed.coordinator.swarm_id()
        || (!offer.is_null()
            && scope
                .as_object()
                .ok_or(OwnerError("offer_response_invalid"))?
                .keys()
                .filter(|k| *k != "run_id" && *k != "deployment_id")
                .any(|k| offer[k] != scope[k]))
    {
        return Err(OwnerError("offer_response_invalid"));
    }
    Ok(body)
}
const CLEANUP_READ: &str = "mycelium.managed_stage_cleanup_read.v1";
const CLEANUP_ACK: &str = "mycelium.managed_stage_cleanup_ack.v1";
const CLEANUP_STATE: &str = "mycelium.managed_stage_cleanup_state.v1";

/// `actor.pending_cleanup()` of the owner's own contributor (`ManagedOfferClient.pending_cleanup`,
/// managed_offers.py:419 @ f4e366e9): the bootstrap offer whose stage cleanup the owner requested
/// for `node`, if any. An owner whose mailbox does not exist yet has published nothing.
pub fn pending_cleanup<H: ChainHost>(core: &H, node: &str) -> OResult<Option<Value>> {
    let body = match own_stage_call(
        core,
        node,
        CLEANUP_READ,
        json!({"offer_id": null}),
        CLEANUP_STATE,
    ) {
        Err(OwnerError("managed_owner_unavailable")) => return Ok(None),
        other => other?,
    };
    Ok((!body["offer"].is_null()).then(|| body["offer"].clone()))
}

/// `_cleanup_if_requested`'s `/api/control/stop` then `/api/staging/cleanup/accept`
/// (demo_target.py; `cleanup_offer`, target_app.py:125-145) for the owner's own contributor: the
/// hello worker is closed (its pump removes the stage and ACKs this cleanup itself); with nothing
/// staged here the cleanup is confirmed with nothing to remove.
pub fn cleanup<H: ChainHost>(core: &H, node: &str, request: &Value) -> OResult<()> {
    let (had_worker, root) = viewed(core, |c| {
        (
            c.bootstrap.is_some() || c.bootstrap_failure.is_some(),
            c.root.clone(),
        )
    })?;
    target::close(core)?;
    if had_worker {
        return Ok(());
    }
    let stage = root.join("local-contributor/native-bootstrap/stage");
    let ack = json!({"protocol": "mycelium.controller_remote_cleanup_ack.v1",
        "node_id": request["target_node_id"], "staging_root": stage, "removed": true});
    own_stage_call(
        core,
        node,
        CLEANUP_ACK,
        json!({"offer_id": request["offer_id"], "ack": ack}),
        CLEANUP_STATE,
    )
    .map(drop)
}

impl<H: ChainHost> ObservationBackend for Backend<H> {
    fn is_owner(&self) -> bool {
        H::upgrade(&self.core).is_some_and(|c| {
            c.try_with(|c| !c.target && c.swarm.is_some())
                .is_some_and(|owner| owner)
        })
    }
    fn running(&self) -> bool {
        self.host().is_ok()
    }
    fn swarm_id(&self) -> String {
        self.host()
            .and_then(|h| h.pin())
            .map_or(String::new(), |s| s.coordinator.swarm_id().into())
    }
    fn wall_clock(&self) -> f64 {
        now()
    }
    fn member(&self, node: &str) -> Option<Value> {
        self.host().and_then(|h| h.member(node)).ok()
    }
    fn prepared_manifest_digest(&mut self) -> Result<String, ObservationError> {
        let core =
            H::upgrade(&self.core).ok_or_else(|| ObservationError::new("managed_owner_closed"))?;
        let acquisition = viewed(&core, |c| c.acquisition.clone())
            .map_err(observe)?
            .ok_or_else(|| ObservationError::new("prepared_source_unavailable"))?;
        acquisition
            .prepared_source()
            .map(|(_, f)| f.manifest_digest)
            .map_err(|e| ObservationError::new(e.code()))
    }
    fn session_factory(
        &mut self,
        run: &str,
        deployment: &str,
        members: &[Value],
    ) -> Result<Box<dyn HelloSessions>, ObservationError> {
        let host = self.host().map_err(observe)?;
        for member in members {
            let node = member["node_id"]
                .as_str()
                .ok_or_else(|| ObservationError::new("managed_session_factory_members_invalid"))?;
            if binding(
                &host.member(node).map_err(observe)?,
                &json!(run),
                &json!(deployment),
            )
            .map_err(observe)?
                != binding(member, &json!(run), &json!(deployment)).map_err(observe)?
            {
                return Err(ObservationError::new(
                    "managed_session_factory_member_not_current",
                ));
            }
        }
        let managed = self.managed(&host).map_err(observe)?;
        let factory = ManagedSessionFactory::new(managed.owner.clone(), run, deployment, members)
            .map_err(|e| ObservationError(e.0))?;
        host.pin().map_err(observe)?;
        Ok(Box::new(factory))
    }
    fn publish_offer(
        &mut self,
        member: &Value,
        run: &str,
        deployment: &str,
        archive: &[u8],
    ) -> Result<Offer, ObservationError> {
        let host = self.host().map_err(observe)?;
        let node = member["node_id"]
            .as_str()
            .ok_or_else(|| ObservationError::new("managed_control_member_stale"))?;
        let expected = binding(member, &json!(run), &json!(deployment)).map_err(observe)?;
        let id = format!("stage-{}", nonce().map_err(observe)?);
        let ttl = BudgetId::PyFinalMyceliumA13MacosManagedOffersTtlSecondsL107C77
            .span(Entries::ZERO)
            .map_err(|_| ObservationError::new("budget_unavailable"))?;
        let raw = host
            .guarded(
                node,
                &json!(run),
                &json!(deployment),
                |mailbox, binding, now| {
                    if binding != &expected {
                        return Err(OwnerError("managed_control_member_stale"));
                    }
                    let raw = mailbox
                        .publish_stage(
                            StagePublication {
                                binding,
                                id: &id,
                                archive,
                                ttl,
                                preposition: None,
                            },
                            || now,
                        )
                        .map_err(owner_error)?;
                    self.published.insert(
                        id.clone(),
                        Offer {
                            offer_id: id.clone(),
                            raw: raw.clone(),
                        },
                    );
                    Ok(raw)
                },
            )
            .map_err(observe)?;
        Ok(Offer { offer_id: id, raw })
    }
    fn retire_offer(&mut self, offer: &Offer) -> Result<Value, ObservationError> {
        self.offer_effect(offer, |m, now| {
            m.retire_stage(&offer.raw, || now).map_err(owner_error)
        })
        .map_err(observe)
    }
    fn request_stage_cleanup(&mut self, offer: &Offer) -> Result<(), ObservationError> {
        self.offer_effect(offer, |m, now| {
            m.request_stage_cleanup(&offer.raw, || now)
                .map_err(owner_error)
        })
        .map_err(observe)
    }
    fn rebuild_observation(
        &mut self,
        _: &mut Option<ObservationContext>,
        _: &str,
        _: &str,
        _: &[Value],
    ) -> Result<String, owner_run::chain::PortError> {
        Err(owner_run::chain::PortError::Failed {
            code: Some("NP-L10-observation-rebuild".into()),
            text: "ManagedServingWorkflow source/stop/cleanup is not mounted".into(),
        })
    }
}
