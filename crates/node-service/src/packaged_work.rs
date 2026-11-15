// SPDX-License-Identifier: AGPL-3.0-only
//! Owned work service for an already-enrolled packaged contributor. No pairing surface.
//! The control loop and serial Work use the same enrolled session; only this service owns them.
use api::{
    app_host,
    compiler_inputs::Error,
    joined_member::work::{self, Member},
    managed_carrier::HostClock,
    member::Session,
    member_control::joined,
    run_exchange::owner_pin::{OwnerPin, VerifiedAnnouncement},
};
use budgets::{BudgetId as B, Entries};
use protocol::identity::Identity;
use runtime::iroh_host::{LanPolicy, LoopbackHost};
use serde_json::{Value, json};
use std::{
    net::IpAddr,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread::JoinHandle,
    time::Duration,
};
use transport::Channel;
type Result<T> = std::result::Result<T, String>;
pub type SessionCell = Arc<Mutex<Option<Session>>>;
/// Explicit transport policy; no environment-selected interface or public relay fallback. The
/// addresses it admits are verified before they get here: a member's are signed by that member's
/// key while the seed currently enrolls it, a shard source's are in the seed-signed grant.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct NetworkChoice(pub Option<(IpAddr, LanPolicy)>);
/// One reading of this Mac's interfaces ([`api::lan_interface::this_mac`] in the app).
pub type InterfaceReader = Arc<dyn Fn() -> api::lan_interface::Selection + Send + Sync>;
/// Where the packaged work endpoints (member work service, the owner's local worker, the shard
/// source, the route and the owner's dials) bind, decided each time a work service or an owner run
/// STARTS, never once per process: Wi-Fi may come up or change network after launch. An owner run
/// reads it ONCE, at its start, and keeps that answer for the whole run.
#[derive(Clone)]
pub enum WorkNetwork {
    /// Exactly this choice, whatever this Mac's interfaces are (tests and explicit callers).
    Fixed(NetworkChoice),
    /// The app's own: this Mac's private LAN address ([`api::lan_interface::this_mac`]) under
    /// [`LanPolicy::private_only`] (exact pins, no relay, no address lookup); 127.0.0.1 only
    /// (`NetworkMode::LocalOnly`) with the rule's named reason when it finds no single network.
    ThisMac,
    /// The app's rule over another reading of the interfaces, bound under `policy`: a test that
    /// must see when, and how often, a run reads the interfaces.
    Read(InterfaceReader, LanPolicy),
}
impl std::fmt::Debug for WorkNetwork {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Fixed(choice) => f.debug_tuple("Fixed").field(choice).finish(),
            Self::ThisMac => f.write_str("ThisMac"),
            Self::Read(_, policy) => f.debug_tuple("Read").field(policy).finish(),
        }
    }
}
impl PartialEq for WorkNetwork {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Fixed(a), Self::Fixed(b)) => a == b,
            (Self::ThisMac, Self::ThisMac) => true,
            (Self::Read(a, p), Self::Read(b, q)) => Arc::ptr_eq(a, b) && p == q,
            _ => false,
        }
    }
}
impl Eq for WorkNetwork {}
impl Default for WorkNetwork {
    /// The app's own ([`WorkNetwork::ThisMac`]).
    fn default() -> Self {
        Self::ThisMac
    }
}
/// One resolution of the work transport.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    pub choice: NetworkChoice,
    /// Log-only transport name (`lan:<ip>` / `local_only:<reason>`).
    pub transport: String,
    /// Why this Mac's own reading left the transport on 127.0.0.1 (the rule's
    /// [`api::lan_interface::Selection::LocalOnly`] reason). `None` on the LAN, and for a choice
    /// a caller fixed (which is deliberate, never a failed reading).
    pub local_only: Option<&'static str>,
}
impl WorkNetwork {
    /// 127.0.0.1 only, whatever the interfaces (`NetworkMode::LocalOnly`).
    pub fn loopback() -> Self {
        Self::Fixed(NetworkChoice::default())
    }
    /// This start's choice, reading this Mac's interfaces now when it is [`WorkNetwork::ThisMac`].
    pub fn resolve(&self) -> (NetworkChoice, String) {
        self.resolve_with(api::lan_interface::this_mac)
    }
    /// [`WorkNetwork::resolve`] over a given reading of the interfaces (the pure rule's answer).
    /// Answers the choice and the log-only transport name (`lan:<ip>` / `local_only:<reason>`).
    pub fn resolve_with(
        &self,
        read: impl FnOnce() -> api::lan_interface::Selection,
    ) -> (NetworkChoice, String) {
        let resolved = self.resolution_with(read);
        (resolved.choice, resolved.transport)
    }
    /// This start's full resolution, with the reason when the reading found no single network.
    pub fn resolution(&self) -> Resolved {
        self.resolution_with(api::lan_interface::this_mac)
    }
    fn resolution_with(&self, read: impl FnOnce() -> api::lan_interface::Selection) -> Resolved {
        let (selection, policy) = match self {
            Self::Fixed(choice) => {
                let transport = match &choice.0 {
                    Some((ip, _)) => format!("lan:{ip}"),
                    None => "local_only:explicit".into(),
                };
                return Resolved {
                    choice: choice.clone(),
                    transport,
                    local_only: None,
                };
            }
            Self::ThisMac => (read(), LanPolicy::private_only()),
            Self::Read(reader, policy) => (reader(), policy.clone()),
        };
        match selection {
            api::lan_interface::Selection::Lan(ip) => Resolved {
                choice: NetworkChoice(Some((ip, policy))),
                transport: format!("lan:{ip}"),
                local_only: None,
            },
            api::lan_interface::Selection::LocalOnly(reason) => Resolved {
                choice: NetworkChoice::default(),
                transport: format!("local_only:{reason}"),
                local_only: Some(reason),
            },
        }
    }
}
/// The refusal (code, plain message) of an owner run whose own reading of this Mac's interfaces
/// found no single private network (`reason`, an [`api::lan_interface`] refusal) while a joined
/// Mac is in the run: it is refused before anything is dialed.
pub fn owner_network_refusal(reason: &str) -> (&'static str, String) {
    use api::lan_interface::{
        AMBIGUOUS_PRIVATE_INTERFACES, NO_PRIVATE_ADDRESS, ONLY_BRIDGE_INTERFACES,
    };
    let why = match reason {
        NO_PRIVATE_ADDRESS => "it has no private network address",
        AMBIGUOUS_PRIVATE_INTERFACES => {
            "it is on more than one private network and cannot tell which one the joined Mac \
             shares (its default route is a VPN, or there is none)"
        }
        ONLY_BRIDGE_INTERFACES => {
            "its only private network is one it hosts for virtual machines or Internet Sharing, \
             which the joined Mac is not on"
        }
        _ => "it could not read its network interfaces",
    };
    (
        "owner_work_network_unavailable",
        format!(
            "This Mac cannot work with the joined Mac: {why} ({reason}). Connect both Macs to \
             the same Wi-Fi or Ethernet network, turn off any VPN on this Mac, and start again."
        ),
    )
}
impl NetworkChoice {
    /// A joined Mac's advertised work address this run's transport may dial: loopback for a
    /// loopback-only run; otherwise an address the policy admits, of the bound address's family
    /// (the run holds ONE socket of that family, which cannot reach the other one).
    pub fn allows(&self, address: std::net::SocketAddr) -> bool {
        match &self.0 {
            None => address.ip().is_loopback(),
            Some((bound, policy)) => {
                bound.is_ipv4() == address.is_ipv4()
                    && policy.allows(address.ip())
                    && policy
                        .peer_hosts()
                        .is_none_or(|hosts| hosts.contains(&address.ip()))
            }
        }
    }
    /// The refusal (code, plain message) for a joined Mac's advertised work address this run's
    /// transport does not admit ([`NetworkChoice::allows`] false), named before any dial.
    pub fn address_refusal(&self, refused: std::net::SocketAddr) -> (&'static str, String) {
        let ip = refused.ip();
        let again = "Connect both Macs to the same Wi-Fi or Ethernet network, turn off any VPN \
                     on the joined Mac, and start again.";
        match &self.0 {
            Some((bound, _)) if ip.is_loopback() => (
                "member_work_network_mismatch",
                format!(
                    "The joined Mac offers its work only on its own loopback address ({ip}): it \
                     found no single private network to use. This Mac works on {bound}. {again}"
                ),
            ),
            Some((bound, _)) if bound.is_ipv4() != ip.is_ipv4() => (
                "member_work_family_mismatch",
                format!(
                    "The joined Mac offers its work only at {ip} and this Mac works on {bound}: \
                     one uses IPv4 and the other IPv6, so they cannot reach each other. {again}"
                ),
            ),
            None if !ip.is_loopback() => (
                "member_work_network_mismatch",
                format!(
                    "The joined Mac offers its work on the network at {ip}, but this Mac's work \
                     transport is on its own loopback address only. {again}"
                ),
            ),
            _ => (
                "member_work_address_refused",
                format!(
                    "The joined Mac offers its work at {ip}, which is not a private network \
                     address this Mac may use. {again}"
                ),
            ),
        }
    }
}
#[cfg(test)]
type ReceiveHook = Box<dyn FnOnce(&work::Operation) + Send>;
/// Test-only (lane FLK patch 3): the operation loop's next idle admission (`shared.live()` at the
/// top of `operations`) waits at `go` after signalling `arrived`, then reports its answer on
/// `admitted`. A test thereby orders that admission after whatever it does in between, with no
/// sleep: the loop cannot sample the work-session cell while it is held.
#[cfg(test)]
pub(crate) struct AdmissionHold {
    arrived: mpsc::Sender<()>,
    go: mpsc::Receiver<()>,
    admitted: mpsc::Sender<Result<()>>,
}
struct Shared {
    root: PathBuf,
    session: SessionCell,
    signer: Arc<Identity>,
    identity: Value,
    announcement: Mutex<VerifiedAnnouncement>,
    pins: work::Pins,
    dispatcher: Arc<work::Dispatcher>,
    status: work::Status,
    #[cfg(test)]
    receive_hook: Mutex<Option<ReceiveHook>>,
    #[cfg(test)]
    admission_hold: Mutex<Option<AdmissionHold>>,
    /// Test-only fault injection: panic immediately before the custody handoff.
    #[cfg(test)]
    panic_handoff: AtomicBool,
    stop: AtomicBool,
}
impl Shared {
    fn live(&self) -> Result<()> {
        if self.stop.load(Ordering::SeqCst) {
            return Err("member_work_closed".into());
        }
        self.current(&self.signer).map_err(|e| e.0.to_owned())?;
        let mut work = work::Work::new(self.root.clone());
        work.announced(
            &*self
                .announcement
                .lock()
                .map_err(|_| "member_announcement_poisoned")?,
        );
        work.admit_share(Some(self))?;
        Ok(())
    }
}
impl Member for Shared {
    fn session(&self) -> &SessionCell {
        &self.session
    }
    fn current(&self, signer: &Identity) -> std::result::Result<Value, Error> {
        if self.stop.load(Ordering::SeqCst) {
            return Err(Error("member_work_closed"));
        }
        let held = self.session.lock().map_err(|_| Error("session_poisoned"))?;
        let row = held
            .as_ref()
            .ok_or(Error("not_joined"))?
            .shard_identity(signer, app_host::now())
            .map_err(|_| Error("joined_shard_member_changed"))?;
        if row != self.identity {
            return Err(Error("joined_shard_member_changed"));
        }
        Ok(row)
    }
}
/// One custody owner. Closing cancels independently of the blocking operation, closes input,
/// joins the worker and its control loop, then reports any unproven cleanup.
pub struct Service {
    shared: Arc<Shared>,
    address: Option<iroh::EndpointAddr>,
    carrier: Option<Identity>,
    thread: Option<JoinHandle<Result<()>>>,
    /// The member-side custody object the operation loop hands back at its exit. Kept until
    /// `discard` PROVES the owned stage released; a failed close retains this exact owner so a
    /// later `close` retries only its unresolved custody instead of reporting success.
    cleanup: Arc<Mutex<Option<work::Work>>>,
    /// The loop unwound before its custody handoff, so the exact `work::Work` is gone and no
    /// later close can prove its stage released. Sticky: every close stays an error.
    unprovable: bool,
}
impl Service {
    #[cfg(all(test, feature = "desktop-shell"))]
    pub(crate) fn on_receive(&self, hook: ReceiveHook) {
        *self.shared.receive_hook.lock().unwrap() = Some(hook);
    }
    /// Test-only (lane FLK patch 3): hold the operation loop at its next idle admission. Returns
    /// (`arrived`: the loop is held before `live()`, `go`: let it admit, `admitted`: its answer).
    #[cfg(all(test, feature = "desktop-shell"))]
    pub(crate) fn hold_next_admission(
        &self,
    ) -> (
        mpsc::Receiver<()>,
        mpsc::Sender<()>,
        mpsc::Receiver<Result<()>>,
    ) {
        let (arrived, at_admission) = mpsc::channel();
        let (go, released) = mpsc::channel();
        let (admitted, answer) = mpsc::channel();
        *self.shared.admission_hold.lock().unwrap() = Some(AdmissionHold {
            arrived,
            go: released,
            admitted,
        });
        (at_admission, go, answer)
    }
    /// Test-only: the member loop panics immediately before handing its custody object back,
    /// so a regression exercises the real unwind instead of a simulated error return.
    #[cfg(all(test, feature = "desktop-shell"))]
    pub(crate) fn on_handoff_panic(&self) {
        self.shared.panic_handoff.store(true, Ordering::SeqCst);
    }
    pub fn start(
        root: PathBuf,
        session: SessionCell,
        signer: Arc<Identity>,
        announcement: VerifiedAnnouncement,
    ) -> Result<Self> {
        Self::on_network(
            root,
            session,
            signer,
            announcement,
            NetworkChoice::default(),
        )
    }
    pub fn on_network(
        root: PathBuf,
        session: SessionCell,
        signer: Arc<Identity>,
        announcement: VerifiedAnnouncement,
        network: NetworkChoice,
    ) -> Result<Self> {
        let identity = session
            .lock()
            .map_err(|_| "session_poisoned")?
            .as_ref()
            .ok_or("not_joined")?
            .shard_identity(&signer, app_host::now())
            .map_err(|_| "joined_shard_identity_mismatch")?;
        let pin = OwnerPin::from_announcement(&announcement).map_err(|e| e.0.to_owned())?;
        if identity["seed_key_digest"] != pin.seed_key_digest() {
            return Err("owner_pin_announcement_unverified".into());
        }
        let pins = work::Pins::new();
        pins.repin(&announcement).map_err(|e| e.0.to_owned())?;
        let (dispatcher, requests) = work::Dispatcher::new()?;
        let shared = Arc::new(Shared {
            root,
            session,
            signer,
            identity,
            announcement: Mutex::new(announcement),
            pins,
            dispatcher,
            status: work::Live::new(
                json!({"joined":true,"native_owned":false,"shard_owned":false,"route_ready":false}),
            ),
            #[cfg(test)]
            receive_hook: Mutex::new(None),
            #[cfg(test)]
            admission_hold: Mutex::new(None),
            #[cfg(test)]
            panic_handoff: AtomicBool::new(false),
            stop: AtomicBool::new(false),
        });
        shared.live()?; // refusal precedes sockets, threads, custody and native work
        let (send, ready) = mpsc::sync_channel(1);
        let worker = shared.clone();
        let cleanup = Arc::new(Mutex::new(None));
        let handed = cleanup.clone();
        let thread = std::thread::Builder::new()
            .name("mycelium-packaged-work".into())
            .spawn(move || drive(worker, requests, send, network, handed))
            .map_err(|_| "member_work_thread_failed")?;
        let mut service = Self {
            shared,
            address: None,
            carrier: None,
            thread: Some(thread),
            cleanup,
            unprovable: false,
        };
        let (address, endpoint) = ready
            .recv_timeout(duration(B::RouterClose)?)
            .map_err(|_| "member_work_start_failed")??;
        service.carrier = Some(
            Identity::from_secret(&endpoint, &service.shared.signer.secret_bytes())
                .map_err(|_| "member_work_identity_invalid")?,
        );
        service.address = Some(address);
        Ok(service)
    }
    /// The existing report format, projected only from real resident custody. Never claims a
    /// descriptor, native proof or serving route. Observation reports remain on their old path.
    pub fn report(&self) -> Result<Option<Value>> {
        self.shared.live()?;
        let announcement = self
            .shared
            .announcement
            .lock()
            .map_err(|_| "member_announcement_poisoned")?;
        let a = announcement.announcement();
        if a["phase"] != "work" {
            return Ok(None);
        }
        let s = self
            .shared
            .status
            .lock()
            .map_err(|_| "member_status_poisoned")?;
        let state = if s["last_error"].is_string() {
            "error"
        } else if s["native_owned"] == true {
            "work_running"
        } else if s["shard_owned"] == true {
            "slice_ready"
        } else {
            "idle"
        };
        Ok(Some(json!({"protocol":api::run_exchange::MEMBER_REPORT,
            "run_id":a["run_id"],"deployment_id":a["deployment_id"],"state":state,
            "detail":s["last_error"],"model":{"model_id":a["model"]["model_id"],"resolved_commit":a["model"]["resolved_commit"]},
            "progress":null,"descriptor":null,"issued_at":app_host::now()})))
    }
    pub fn registration(&self) -> Result<Value> {
        self.shared.live()?;
        let held = self.shared.session.lock().map_err(|_| "session_poisoned")?;
        joined::announce(
            held.as_ref().ok_or("not_joined")?,
            &self.shared.signer,
            self.carrier.as_ref().ok_or("member_work_closed")?,
            self.address.as_ref().ok_or("member_work_closed")?,
            &*self
                .shared
                .announcement
                .lock()
                .map_err(|_| "member_announcement_poisoned")?,
            app_host::now(),
        )
        .map_err(|e| e.code.to_owned())
    }
    /// Share and owner pin move as one snapshot. An identity/run change closes the old service;
    /// its links cannot silently acquire authority over the new run.
    pub fn update(&mut self, announcement: VerifiedAnnouncement) -> Result<()> {
        let old = self
            .shared
            .announcement
            .lock()
            .map_err(|_| "member_announcement_poisoned")?
            .clone();
        let same = OwnerPin::from_announcement(&announcement).ok()
            == OwnerPin::from_announcement(&old).ok()
            && announcement.announcement()["run_id"] == old.announcement()["run_id"]
            && announcement.announcement()["deployment_id"] == old.announcement()["deployment_id"];
        if !same {
            self.close()?;
            return Err("member_work_run_changed".into());
        }
        if announcement.work_limits() != old.work_limits() {
            self.shared.dispatcher.cancel_active()?;
        }
        *self
            .shared
            .announcement
            .lock()
            .map_err(|_| "member_announcement_poisoned")? = announcement;
        if let Err(code) = self.shared.live() {
            self.close()?;
            return Err(code);
        }
        Ok(())
    }
    /// Standing acceptance is preflight only: no owner-authored consent and no native launch.
    /// The resident dispatcher repeats this admission when the signed prepare command arrives.
    pub fn preview(&self, offer: &Value, limits: &Value, policy: &Value) -> Result<Value> {
        self.shared.live()?;
        let saved = api::preferences::resource_policy::read(&self.shared.root)
            .map_err(|e| e.code.to_owned())?;
        if saved.as_ref() != Some(policy) {
            return Err("resource_policy_changed".into());
        }
        let mut work = work::Work::new(self.shared.root.clone());
        work.announced(
            &*self
                .shared
                .announcement
                .lock()
                .map_err(|_| "member_announcement_poisoned")?,
        );
        let admitted = work
            .admit_share_for(Some(&*self.shared), offer, true)?
            .ok_or("member_share_unannounced")?;
        if &admitted != limits {
            return Err("member_share_limits_mismatch".into());
        }
        Ok(
            json!({"protocol":"mycelium.packaged_member_work_preview.v1", "offer":offer, "limits":limits}),
        )
    }
    pub fn accept(&self, preview: &Value, limits: &Value, policy: &Value) -> Result<()> {
        if self.preview(&preview["offer"], limits, policy)? != *preview {
            return Err("member_work_preview_changed".into());
        }
        Ok(())
    }
    pub fn close(&mut self) -> Result<()> {
        self.shared.stop.store(true, Ordering::SeqCst);
        let cancel = self.shared.dispatcher.cancel_active();
        let input = self.shared.dispatcher.close_input();
        let joined = match self.thread.take() {
            None => Ok(()),
            Some(t) => match t.join() {
                Ok(result) => result,
                Err(_) => {
                    // The loop unwound before its custody handoff: the exact `work::Work` was
                    // dropped unproven. Never let the now-empty cleanup slot read as success.
                    self.unprovable = true;
                    Err("member_work_panicked".to_owned())
                }
            },
        };
        // The loop hands its custody object back here. Retry (or finish) its ONE discard:
        // an unproven retire keeps the same Work for the next close; it is never dropped
        // into a success while the stage bytes are still owned.
        let cleanup = self.discard();
        self.address = None;
        let outcome = cancel.and(input).and(joined).and(cleanup);
        if self.unprovable {
            return Err(outcome
                .err()
                .unwrap_or_else(|| "member_work_unprovable".to_owned()));
        }
        outcome
    }
    /// True while a failed close still owns member-side custody (a `close` retries it).
    /// An unprovable Service stays pending forever: its Work was dropped by the unwind, so no
    /// later close may be treated as a successful release.
    #[cfg(any(feature = "desktop-shell", feature = "packaged"))]
    pub(crate) fn cleanup_pending(&self) -> bool {
        self.unprovable
            || self
                .cleanup
                .lock()
                .map(|slot| slot.is_some())
                .unwrap_or(true)
    }
    fn discard(&self) -> Result<()> {
        let mut slot = self
            .cleanup
            .lock()
            .map_err(|_| "member_work_cleanup_poisoned".to_owned())?;
        let Some(work) = slot.as_mut() else {
            return Ok(());
        };
        match work.discard() {
            Ok(_) => {
                *slot = None;
                Ok(())
            }
            Err(code) => Err(code),
        }
    }
}
impl Drop for Service {
    fn drop(&mut self) {
        if let Err(e) = self.close() {
            eprintln!("member_work_cleanup_failed:{e}");
        }
    }
}
fn duration(id: B) -> Result<Duration> {
    Ok(Duration::from_millis(
        id.span(Entries::ZERO)
            .map_err(|_| "budget_unavailable")?
            .millis(),
    ))
}
fn bind<'r>(
    rt: &'r tokio::runtime::Runtime,
    clock: &HostClock,
    network: &NetworkChoice,
    channels: &[Channel],
) -> Result<LoopbackHost<'r>> {
    let until = work::until(clock, B::RouterClose)?;
    let secret = iroh::SecretKey::generate();
    match &network.0 {
        Some((ip, policy)) => {
            LoopbackHost::bind_lan(rt, clock, until, secret, channels, *ip, policy.clone())
        }
        None => LoopbackHost::bind_protocols(rt, clock, until, secret, channels),
    }
    .map_err(|e| format!("member_work_bind_failed:{e:?}"))
}
fn drive(
    shared: Arc<Shared>,
    requests: mpsc::Receiver<Result<work::Operation>>,
    ready: mpsc::SyncSender<Result<(iroh::EndpointAddr, String)>>,
    network: NetworkChoice,
    cleanup: Arc<Mutex<Option<work::Work>>>,
) -> Result<()> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .map_err(|_| "member_work_runtime_failed")?;
    let clock = HostClock::default();
    let mut carrier = bind(
        &rt,
        &clock,
        &network,
        &[Channel::ArtifactChunk, Channel::Router],
    )?;
    let outcome = (|| {
        let mut control = bind(&rt, &clock, &network, &[Channel::NodeControl])?;
        let outcome = (|| {
            let address = control
                .address()
                .map_err(|_| "member_work_address_unavailable")?;
            let endpoint = carrier
                .address()
                .map_err(|_| "member_work_address_unavailable")?
                .id;
            let signer =
                Identity::from_secret(&endpoint.to_string(), &shared.signer.secret_bytes())
                    .map_err(|_| "member_work_identity_invalid")?;
            let trust = work::OwnerTrust::Announced(shared.pins.clone());
            let status = shared.status.clone();
            std::thread::scope(|scope| -> Result<()> {
                let control_thread = std::thread::Builder::new()
                    .name("mycelium-packaged-work-control".into())
                    .spawn_scoped(scope, || {
                        work::control_loop(
                            &rt,
                            &clock,
                            &mut control,
                            &trust,
                            address.id,
                            &shared.dispatcher,
                            &shared.signer,
                            &status,
                            &shared.stop,
                        )
                    })
                    .map_err(|_| "member_work_control_thread_failed")?;
                let result = (|| {
                    ready
                        .send(Ok((address.clone(), endpoint.to_string())))
                        .map_err(|_| "member_work_starter_gone")?;
                    let side = work::Carrier {
                        host: &carrier,
                        runtime: &rt,
                        clock: &clock,
                        endpoint,
                        lan: network.0.as_ref().map(|(_, p)| p),
                    };
                    operations(&shared, requests, &side, &signer, &status, &cleanup)
                })();
                shared.stop.store(true, Ordering::SeqCst);
                let control_result = control_thread
                    .join()
                    .map_err(|_| "member_work_control_panicked")?;
                result.and(control_result)
            })
        })();
        let closed = control
            .close(&clock, work::until(&clock, B::RouterClose)?)
            .map_err(|_| "member_work_control_close_failed".into());
        outcome.and(closed)
    })();
    let closed = carrier
        .close(&clock, work::until(&clock, B::RouterClose)?)
        .map_err(|_| "member_work_carrier_close_failed".into());
    outcome.and(closed)
}
fn operations(
    shared: &Shared,
    requests: mpsc::Receiver<Result<work::Operation>>,
    side: &work::Carrier<'_, '_>,
    carrier: &Identity,
    status: &work::Status,
    cleanup: &Mutex<Option<work::Work>>,
) -> Result<()> {
    let mut work = work::Work::new(shared.root.clone());
    let result = (|| {
        loop {
            if shared.stop.load(Ordering::SeqCst) {
                break;
            }
            #[cfg(test)]
            let hold = shared
                .admission_hold
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .take();
            #[cfg(test)]
            if let Some(hold) = &hold {
                hold.arrived.send(()).unwrap_or(());
                hold.go.recv().unwrap_or(());
            }
            let admitted = shared.live();
            #[cfg(test)]
            if let Some(hold) = hold {
                hold.admitted.send(admitted.clone()).unwrap_or(());
            }
            admitted?;
            let operation = match requests.recv_timeout(duration(B::OwnerPoll)?) {
                Ok(operation) => operation?,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            };
            let command = operation.request["command"].as_str().unwrap_or_default();
            work.announced(
                &*shared
                    .announcement
                    .lock()
                    .map_err(|_| "member_announcement_poisoned")?,
            );
            let result =
                shared
                    .live()
                    .and_then(|()| operation.check())
                    .and_then(|()| match command {
                        "carrier_address" => {
                            let address = side.host.address().map_err(|_| "member_work_address_unavailable".to_owned())?;
                            let sockets: Vec<_> = address.ip_addrs().copied().collect();
                            let held = shared.session.lock().map_err(|_| "session_poisoned".to_owned())?;
                            let advert = api::joined_shard::carrier::advertise(held.as_ref().ok_or("not_joined")?, &shared.signer, carrier, &sockets, app_host::now()).map_err(|e| e.0.to_owned())?;
                            Ok(json!({"endpoint_id":address.id.to_string(),"socket":sockets.first().ok_or("carrier_address_missing")?,"sockets":sockets,"advert":advert}))
                        }
                        "consent_status" => work::consent_status(&shared.root),
                        "consent" | "grant_consent" => Err("member_local_consent_required".into()),
                        "pair_request" | "accept" => Err("member_already_joined".into()),
                        "receive" => {
                            #[cfg(test)]
                            if let Some(hook) = shared.receive_hook.lock().unwrap().take() { hook(&operation); }
                            work.receive(Some(shared), side, &shared.signer, carrier, &operation)
                        }
                        "prepare_native" => {
                            work.prepare_native(Some(shared), &shared.signer, &operation)
                        }
                        "native_step" => work.native_step(Some(shared), &shared.signer, &operation),
                        "serve" => work.serve(Some(shared), side, &shared.signer, &operation),
                        "native_close" => work.native_close(),
                        "discard" | "close" => work.discard(),
                        _ => Err("member_work_command_refused".into()),
                    });
            let out = match result {
                Ok(v) => json!({"ok":true,"result":v}),
                Err(code) => json!({"ok":false,"code":code}),
            };
            work.record(status, true, command, &operation, &out);
            status.lock().map_err(|_| "member_status_poisoned")?["last_error"] =
                if out["ok"] == false {
                    out["code"].clone()
                } else {
                    Value::Null
                };
            shared.dispatcher.complete(&operation, out, |_| {
                Err("member_work_origin_invalid".into())
            })?;
            if command == "close" {
                break;
            }
        }
        Ok(())
    })();
    // Test-only: panic immediately before the handoff so a regression exercises the real
    // unwind that drops `work` when the custody object has not been handed back yet.
    #[cfg(test)]
    if shared.panic_handoff.swap(false, Ordering::SeqCst) {
        panic!("member_work_handoff_panic");
    }
    // Hand the exact custody object to the owner of `close`, not to the thread's end: only
    // the Service may decide that discard is proven (and retry it with after a fault).
    *cleanup.lock().unwrap_or_else(|p| p.into_inner()) = Some(work);
    result
}
