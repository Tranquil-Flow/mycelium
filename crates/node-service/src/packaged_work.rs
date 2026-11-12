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
/// Explicit transport policy; no environment-selected interface or public relay fallback.
#[derive(Clone, Default)]
pub struct NetworkChoice(pub Option<(IpAddr, LanPolicy)>);
impl NetworkChoice {
    pub fn allows(&self, address: std::net::SocketAddr) -> bool {
        match &self.0 {
            None => address.ip().is_loopback(),
            Some((_, policy)) => {
                policy.allows(address.ip())
                    && policy
                        .peer_hosts()
                        .is_none_or(|hosts| hosts.contains(&address.ip()))
            }
        }
    }
}
#[cfg(test)]
type ReceiveHook = Box<dyn FnOnce(&work::Operation) + Send>;
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
}
impl Service {
    #[cfg(all(test, feature = "desktop-shell"))]
    pub(crate) fn on_receive(&self, hook: ReceiveHook) {
        *self.shared.receive_hook.lock().unwrap() = Some(hook);
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
            stop: AtomicBool::new(false),
        });
        shared.live()?; // refusal precedes sockets, threads, custody and native work
        let (send, ready) = mpsc::sync_channel(1);
        let worker = shared.clone();
        let thread = std::thread::Builder::new()
            .name("mycelium-packaged-work".into())
            .spawn(move || drive(worker, requests, send, network))
            .map_err(|_| "member_work_thread_failed")?;
        let mut service = Self {
            shared,
            address: None,
            carrier: None,
            thread: Some(thread),
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
        let joined = self
            .thread
            .take()
            .map(|t| {
                t.join()
                    .map_err(|_| "member_work_panicked".to_owned())
                    .and_then(|r| r)
            })
            .unwrap_or(Ok(()));
        self.address = None;
        cancel.and(input).and(joined)
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
                    operations(&shared, requests, &side, &signer, &status)
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
) -> Result<()> {
    let mut work = work::Work::new(shared.root.clone());
    let result = (|| {
        loop {
            if shared.stop.load(Ordering::SeqCst) {
                break;
            }
            shared.live()?;
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
    let cleanup = work.discard().map(drop);
    if let Err(e) = &cleanup {
        eprintln!("member_work_custody_cleanup_failed:{e}");
    }
    result.and(cleanup)
}
