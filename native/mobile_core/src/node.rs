// SPDX-License-Identifier: AGPL-3.0-or-later
//! `NodeCore`: the one facade the UI shells call (Tauri commands today; the same
//! owned-type API is UniFFI-ready for thin native shells). All network, crypto
//! and inference work runs on the core's own tokio runtime, never on the UI
//! thread; every call returns in bounded time and status is a cheap snapshot.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::{Value, json};
use tokio::runtime::Runtime;
use tokio::sync::mpsc;

use crate::evidence;
use crate::identity::Identity;
use crate::membership::{self, MemberConfig};
use crate::onboarding::{self, PairingSession};
use crate::artifact::ArtifactStore;
use crate::qwen2::Qwen2Runtime;
use crate::route_assignment::{self, DeliveredStage, RouteAssignment};
use crate::runtime::{Gpt2BlockStage, REFERENCE_BACKEND, StageCall, StagePhase, StageRuntime};
use crate::transport::{Inbound, NodeTransport, Reachability};
use crate::wire;

/// The peer class this build may claim. Only the Android native class has an
/// owner-side pairing contract today; an iOS build reports its own class and
/// refuses to pair rather than claim to be Android (Apple families never
/// inherit another platform's qualification).
#[cfg(not(target_os = "ios"))]
pub const PEER_CLASS: &str = onboarding::ANDROID_PEER_CLASS;
#[cfg(target_os = "ios")]
pub const PEER_CLASS: &str = "ios_native_iroh";

fn require_registered_class() -> Result<(), NodeError> {
    if PEER_CLASS == onboarding::ANDROID_PEER_CLASS {
        Ok(())
    } else {
        Err(nerr(format!("platform_peer_class_unregistered:{PEER_CLASS}")))
    }
}
pub const PAIRING_CHANNEL_PROTOCOL: &str = "mycelium.pairing_channel.v1";
/// Membership timing. The defaults are the product cadence; tests shorten them
/// against a short-lease seed.
#[derive(Debug, Clone, Copy)]
pub struct NodeTiming {
    /// Scheduled heartbeat cadence while the lease is held.
    pub heartbeat_interval: Duration,
    /// Bound on every seed HTTP exchange.
    pub http_timeout: Duration,
    /// First retry delay after a failed heartbeat or resume; doubles up to `retry_max`.
    pub retry_min: Duration,
    pub retry_max: Duration,
}

impl Default for NodeTiming {
    fn default() -> Self {
        Self {
            heartbeat_interval: Duration::from_secs(30),
            http_timeout: Duration::from_secs(15),
            retry_min: Duration::from_secs(5),
            retry_max: Duration::from_secs(60),
        }
    }
}

/// Seed refusals after which a held membership can only come back through
/// `/seed/resume` (fresh incarnation, next generation).
const RESUMABLE_REFUSALS: [&str; 2] = ["seed_member_lease_expired", "seed_message_mismatch"];
/// Seed refusals that end the membership for good: the owner revoked or never
/// enrolled this member, so the node stops asking instead of retrying.
const TERMINAL_RESUME_REFUSALS: [&str; 4] = [
    "seed_resume_member_revoked",
    "seed_member_unknown",
    "seed_resume_identity_mismatch",
    "seed_resume_generation_stale",
];
/// Native runtime observations are sealed on this cadence into app-private
/// `evidence/`, keeping the newest `EVIDENCE_RETAINED` envelopes.
const EVIDENCE_INTERVAL: Duration = Duration::from_secs(60);
const EVIDENCE_RETAINED: u64 = 64;

/// A release Android build must carry its own build identity
/// (`mycelium_mobile_runtime_build.v1`, recomputable from the source commit by
/// `scripts/run_a12_mobile_physical_gate.py --build-commit`); a release without
/// one would bind every observation to the generic core version and is refused
/// at compile time.
#[cfg(all(target_os = "android", not(debug_assertions)))]
const fn release_build_digest_present(digest: Option<&str>) -> bool {
    match digest {
        Some(d) => d.len() == 71,
        None => false,
    }
}
#[cfg(all(target_os = "android", not(debug_assertions)))]
const _: () = assert!(
    release_build_digest_present(option_env!("MYCELIUM_NODE_BUILD_DIGEST")),
    "release android builds must set MYCELIUM_NODE_BUILD_DIGEST (sha256:<64 hex>)"
);

/// The build identity the node app binds its observations to: an explicit
/// build digest when the release pipeline sets one, else this core's version.
pub fn runtime_build_digest() -> String {
    match option_env!("MYCELIUM_NODE_BUILD_DIGEST") {
        Some(digest) if digest.len() == 71 && digest.starts_with("sha256:") => digest.to_owned(),
        _ => crate::canonical::sha256_prefixed(
            format!("mycelium-mobile-core:{}", env!("CARGO_PKG_VERSION")).as_bytes(),
        ),
    }
}

pub fn now_s() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// Seconds on this process's monotonic clock: the stage node's relay clock
/// (a wall-clock step never ages or revives a tombstone).
pub fn monotonic_s() -> f64 {
    static START: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    START.get_or_init(std::time::Instant::now).elapsed().as_secs_f64()
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct NodeError(pub String);

fn nerr(code: impl Into<String>) -> NodeError {
    NodeError(code.into())
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct NodeStatus {
    pub phase: String,
    pub endpoint_id: String,
    pub relay_online: bool,
    pub swarm_id: Option<String>,
    pub member_generation: Option<u64>,
    pub lease_expires_at: Option<f64>,
    /// True only while the seed-signed lease is unexpired: the node is a member
    /// the swarm can see. Serving without it is not serving.
    pub lease_fresh: bool,
    pub heartbeats_sent: u64,
    pub last_heartbeat_at: Option<f64>,
    pub last_error: Option<String>,
    pub stage_loaded: Option<String>,
    pub frames_received: u64,
    pub frames_executed: u64,
    pub last_execute_ms: Option<f64>,
    pub peer_class: String,
    pub runtime_backend: String,
    pub route_ready: bool,
    /// The owner-issued, seed-signed route assignment this node runs (stage
    /// loaded and route peer allow-listed), if any.
    pub route_assignment: Option<String>,
    pub route_assignment_error: Option<String>,
    /// v2 (digest-pinned) stage artifacts: `acquisition_pending` while the
    /// assigned tensors are not all promoted in the local store, `loaded` once
    /// the stage was loaded from verified objects and passed its golden.
    pub artifact_state: Option<String>,
    /// The owner of this device chose to serve (persisted across restarts, so a
    /// restarted app restores serving instead of silently dropping it).
    pub serving_intent: bool,
}

/// Where the last sealed observation window ended, so each observation covers
/// exactly the runtime work since the previous one.
struct ObservationCursor {
    window_started_at: f64,
    frames_at_window_start: u64,
    evidence_generation: u64,
}

/// The durable membership the supervisor keeps alive.
#[derive(Clone)]
struct ActiveMembership {
    cfg: MemberConfig,
    generation: u64,
    seed_url: String,
    pinned: Value,
    /// The lease was lost (or this is a fresh process): only a resume can restore it.
    needs_resume: bool,
    /// An unanswered resume (incarnation, exact envelope). The seed replays a
    /// committed acceptance only for the byte-identical request, so a retry after
    /// a lost reply resends it unchanged instead of minting another incarnation.
    pending_resume: Option<(String, Value)>,
}

struct Shared {
    observation: ObservationCursor,
    membership: Option<ActiveMembership>,
    supervisor_running: bool,
    status: NodeStatus,
    session: Option<PairingSession>,
    invitation: Option<Value>,
    stage: Option<Box<dyn StageRuntime>>,
    forward: Option<(iroh::EndpointAddr, u64)>,
    route: Option<AppliedRoute>,
    /// The latest OS readings the app shell sampled, and when.
    shell_metrics: Option<(evidence::Metrics, f64)>,
    /// The background A2 acquisition of a v2 manifest's objects, if running.
    acquisition: Option<Acquisition>,
    /// Router participation of a v2 (Qwen2) stage: locked paths, hop ledger
    /// and path tables. A route change forgets the live paths and the ledger
    /// but keeps the tables (fail closed: no cancelled attempt revives).
    stage_node: Option<crate::stage_node::StageNode>,
    /// The route controller's session (`mycelium.node_control.v1`).
    control: Option<crate::node_control::ControlNode>,
}

/// One background acquisition: which manifest, and how to stop it.
struct Acquisition {
    pack_digest: String,
    cancel: Arc<std::sync::atomic::AtomicBool>,
    done: Arc<std::sync::atomic::AtomicBool>,
}

/// Ranges per A2 chunk request from the phone.
const ACQUISITION_CHUNK_BYTES: usize = 1 << 20;

/// Why acquisition must pause now (A12 §8: thermal, battery), from the owner's
/// invitation quota and the current platform readings.
/// Fails closed: a missing quota or a reading the gate cannot see pauses too.
fn acquisition_pause(quota: &Value, metrics: &evidence::Metrics) -> Option<String> {
    let (Some(ceiling), Some(floor)) = (quota["thermal_ceiling_c"].as_f64(), quota["battery_floor_percent"].as_f64()) else {
        return Some("acquisition_quota_unknown".into());
    };
    let reading = |name: &str| metrics.get(name).copied().flatten().filter(|v| v.is_finite());
    if reading("thermal_max_celsius").is_none() {
        return Some("platform_reading_unknown:thermal_max_celsius".into());
    }
    if let Some(code) = thermal_refusal(Some(ceiling), metrics) {
        return Some(code);
    }
    let Some(level) = reading("battery_level_percent") else {
        return Some("platform_reading_unknown:battery_level_percent".into());
    };
    let charging = reading("charging").is_some_and(|c| c >= 1.0);
    (level < floor && !charging).then(|| format!("battery_below_floor:{level}<{floor}"))
}

/// The applied route already runs this exact assignment and manifest (an
/// assignment id alone is not enough: the stage must be the signed one).
fn already_applied(applied: Option<&(String, String)>, assignment: &RouteAssignment) -> bool {
    applied.is_some_and(|(id, digest)| *id == assignment.assignment_id && *digest == assignment.stage_pack_digest)
}

/// Stop a running acquisition unless it is for `keep`; its state is cleared
/// under the same lock its outcome is recorded under.
fn cancel_acquisition_in(shared: &Arc<Mutex<Shared>>, keep: Option<&str>) {
    let mut s = shared.lock().unwrap();
    if s.acquisition.as_ref().is_some_and(|a| Some(a.pack_digest.as_str()) != keep) {
        let old = s.acquisition.take().unwrap();
        old.cancel.store(true, std::sync::atomic::Ordering::SeqCst);
        s.status.artifact_state = None;
    }
}

/// Record an acquisition's outcome unless it was cancelled; the check and
/// the write happen under the lock cancellation takes, so a late outcome of a
/// cancelled task can never land.
fn record_acquisition_outcome(shared: &Arc<Mutex<Shared>>, cancel: &std::sync::atomic::AtomicBool, state: String) {
    let mut s = shared.lock().unwrap();
    if !cancel.load(std::sync::atomic::Ordering::SeqCst) {
        s.status.artifact_state = Some(state);
    }
}

/// The seed-delivered assignment currently applied to the stage and transport.
struct AppliedRoute {
    assignment: RouteAssignment,
    peer: iroh::EndpointId,
}

pub struct NodeCore {
    rt: Runtime,
    data_dir: PathBuf,
    identity: Arc<Identity>,
    transport: NodeTransport,
    shared: Arc<Mutex<Shared>>,
    http: reqwest::Client,
    incarnation: String,
    timing: NodeTiming,
}

fn load_or_create_secret(path: &Path) -> Result<[u8; 32], NodeError> {
    if let Ok(bytes) = std::fs::read(path) {
        return bytes.try_into().map_err(|_| nerr("identity_store_corrupt"));
    }
    let mut secret = [0u8; 32];
    getrandom::getrandom(&mut secret).map_err(|_| nerr("randomness_unavailable"))?;
    write_private(path, &secret)?;
    Ok(secret)
}

fn write_private(path: &Path, bytes: &[u8]) -> Result<(), NodeError> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, bytes).map_err(|_| nerr("identity_store_unwritable"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
    }
    std::fs::rename(&tmp, path).map_err(|_| nerr("identity_store_unwritable"))
}

impl NodeCore {
    /// Open the durable identity under the app-private `data_dir` and bind the
    /// embedded iroh endpoint. The membership signer's endpoint id is the iroh
    /// EndpointID, so the signed profile binds the transport identity.
    pub fn open(data_dir: &Path, reach: Reachability) -> Result<Self, NodeError> {
        Self::open_with_timing(data_dir, reach, NodeTiming::default())
    }

    pub fn open_with_timing(data_dir: &Path, reach: Reachability, timing: NodeTiming) -> Result<Self, NodeError> {
        std::fs::create_dir_all(data_dir).map_err(|_| nerr("data_dir_unwritable"))?;
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .thread_name("mycelium-core")
            .enable_all()
            .build()
            .map_err(|_| nerr("runtime_start_failed"))?;
        let iroh_secret = load_or_create_secret(&data_dir.join("iroh.key"))?;
        let member_secret = load_or_create_secret(&data_dir.join("member.key"))?;
        let transport = rt.block_on(NodeTransport::bind(&iroh_secret, reach)).map_err(|e| nerr(e.0))?;
        let identity = Identity::from_secret(&transport.endpoint_id(), &member_secret)
            .map_err(|e| nerr(e.to_string()))?;
        let status = NodeStatus {
            phase: "unpaired".into(),
            endpoint_id: transport.endpoint_id(),
            peer_class: PEER_CLASS.into(),
            runtime_backend: REFERENCE_BACKEND.into(),
            ..Default::default()
        };
        let http = reqwest::Client::builder().timeout(timing.http_timeout).build().map_err(|_| nerr("http_client_failed"))?;
        let core = Self {
            rt,
            data_dir: data_dir.to_owned(),
            identity: Arc::new(identity),
            transport,
            shared: Arc::new(Mutex::new(Shared {
                observation: ObservationCursor { window_started_at: now_s(), frames_at_window_start: 0, evidence_generation: 0 },
                status,
                membership: None,
                supervisor_running: false,
                session: None,
                invitation: None,
                stage: None,
                forward: None,
                route: None,
                shell_metrics: None,
                acquisition: None,
                stage_node: None,
                control: None,
            })),
            http,
            incarnation: new_incarnation()?,
            timing,
        };
        core.shared.lock().unwrap().status.serving_intent = read_serving_intent(&core.data_dir);
        if let Ok(raw) = std::fs::read(core.data_dir.join("invitation.json"))
            && let Ok(inv) = serde_json::from_slice::<Value>(&raw)
        {
            let mut s = core.shared.lock().unwrap();
            s.invitation = Some(inv);
            s.status.phase = "paired".into();
        }
        core.spawn_background();
        Ok(core)
    }

    fn spawn_background(&self) {
        let transport = self.transport.clone();
        let shared = self.shared.clone();
        // Re-evaluated continuously: on mobile the OS may block the network while
        // the app is backgrounded and restore it when serving starts.
        self.rt.spawn(async move {
            loop {
                let online = transport.online(Duration::from_secs(5)).await;
                shared.lock().unwrap().status.relay_online = online;
                tokio::time::sleep(Duration::from_secs(10)).await;
            }
        });
        let (tx, rx) = mpsc::channel(64);
        self.rt.spawn({
            let t = self.transport.clone();
            async move {
                // Python transport semantics: acknowledge after dispatch.
                let _ = t.spawn_accept_dispatch(tx).await;
            }
        });
        self.rt.spawn(serve_inbox(rx, self.shared.clone(), self.transport.clone()));
        // The route controller's commands (plan §4.2), answered off the
        // compute path so `health` answers while a stage runs.
        let (control_tx, control_rx) = mpsc::channel(8);
        self.rt.spawn({
            let t = self.transport.clone();
            async move { t.set_control_handler(Some(control_tx)).await }
        });
        self.rt.spawn(serve_control(control_rx, self.shared.clone(), self.transport.clone(), self.identity.clone(), self.incarnation.clone()));
        // Seal native runtime observations into app-private evidence/ on a fixed
        // cadence for the lifetime of the node (the app owns one core).
        let (data_dir, shared, identity) = (self.data_dir.clone(), self.shared.clone(), self.identity.clone());
        self.rt.spawn(async move {
            loop {
                tokio::time::sleep(EVIDENCE_INTERVAL).await;
                let (d, s, i) = (data_dir.clone(), shared.clone(), identity.clone());
                let _ = tokio::task::spawn_blocking(move || seal_observation(&d, &s, &i)).await;
            }
        });
    }


    pub fn status(&self) -> NodeStatus {
        let mut status = self.shared.lock().unwrap().status.clone();
        status.lease_fresh = lease_is_fresh(&status, now_s());
        status
    }

    /// Seal one member-signed `mycelium.mobile_runtime_observation.v1` covering the
    /// runtime work since the previous observation: the core's own counters plus
    /// the platform metrics the OS exposes to the app (unknown otherwise).
    /// Only the registered Android class observes; an Apple build refuses (A12
    /// Apple mobile is deferred).
    /// Write the sealed observations as one owner-private export bundle under
    /// `out_dir` (the app cache the FileProvider shares from); returns its path and
    /// the number of observations it carries.
    pub fn export_evidence(&self, out_dir: &Path) -> Result<(PathBuf, usize), NodeError> {
        let bundle = evidence::export_bundle(&self.data_dir.join("evidence"), &runtime_build_digest())
            .map_err(|e| nerr(e.0))?;
        let count = bundle["observations"].as_array().map_or(0, Vec::len);
        std::fs::create_dir_all(out_dir).map_err(|_| nerr("mobile_evidence_export_unwritable"))?;
        let path = out_dir.join(format!("mycelium-evidence-{}.json", (now_s() * 1000.0) as u64));
        write_private(&path, &serde_json::to_vec(&bundle).unwrap_or_default())?;
        Ok((path, count))
    }

    pub fn runtime_observation(&self, platform: &evidence::Metrics) -> Result<Value, NodeError> {
        observe_runtime(&self.shared, &self.identity, platform)
    }

    /// Record the OS readings the app shell sampled (battery, charging, battery
    /// temperature, thermal status); sealed observations use them while fresh.
    pub fn set_platform_metrics(&self, sample: evidence::Metrics) {
        self.shared.lock().unwrap().shell_metrics = Some((sample, now_s()));
    }

    /// The platform metrics the next sealed observation carries.
    pub fn platform_metrics(&self) -> evidence::Metrics {
        current_platform_metrics(&self.shared)
    }
}

fn current_platform_metrics(shared: &Arc<Mutex<Shared>>) -> evidence::Metrics {
    let shell = {
        let s = shared.lock().unwrap();
        s.shell_metrics
            .as_ref()
            .filter(|(_, at)| now_s() - at <= evidence::SHELL_SAMPLE_MAX_AGE_S)
            .map(|(m, _)| m.clone())
    };
    evidence::merge_platform_metrics(evidence::platform_metrics(Path::new("/")), Some(&shell.unwrap_or_default()))
}

fn observe_runtime(
    shared: &Arc<Mutex<Shared>>,
    identity: &Identity,
    platform: &evidence::Metrics,
) -> Result<Value, NodeError> {
        require_registered_class()?;
        let now = now_s();
        let (mut metrics, duration_ms, samples, profile_generation, generation) = {
            let mut s = shared.lock().unwrap();
            let frames = s.status.frames_executed;
            let delta = frames.saturating_sub(s.observation.frames_at_window_start);
            let duration_ms = ((now - s.observation.window_started_at) * 1000.0).max(1.0);
            s.observation.window_started_at = now;
            s.observation.frames_at_window_start = frames;
            s.observation.evidence_generation += 1;
            let mut metrics = evidence::Metrics::new();
            metrics.insert("frames_executed_window".into(), Some(delta as f64));
            metrics.insert("last_execute_ms".into(), s.status.last_execute_ms);
            metrics.insert("relay_online".into(), Some(if s.status.relay_online { 1.0 } else { 0.0 }));
            metrics.insert("stage_loaded".into(), Some(if s.status.stage_loaded.is_some() { 1.0 } else { 0.0 }));
            metrics.insert("heartbeats_sent".into(), Some(s.status.heartbeats_sent as f64));
            metrics.insert(
                "lifecycle_member".into(),
                Some(if lease_is_fresh(&s.status, now) { 1.0 } else { 0.0 }),
            );
            (metrics, duration_ms, 1 + delta, s.status.member_generation.unwrap_or(1), s.observation.evidence_generation)
        };
        for (name, value) in platform {
            metrics.entry(name.clone()).or_insert(*value);
        }
        let build = runtime_build_digest();
        let document = evidence::runtime_observation(&evidence::Observation {
            platform_family: "android",
            runtime_build_digest: &build,
            profile_generation: profile_generation.max(1),
            evidence_generation: generation,
            observed_at_unix_ms: now * 1000.0,
            measurement_duration_ms: duration_ms,
            sample_count: samples,
            authority: evidence::OBSERVATION_AUTHORITY,
            metrics: &metrics,
        })
        .map_err(|e| nerr(e.0))?;
        evidence::sign_observation(identity, &document).map_err(|e| nerr(e.0))
}

fn seal_observation(data_dir: &Path, shared: &Arc<Mutex<Shared>>, identity: &Identity) {
        let dir = data_dir.join("evidence");
        if std::fs::create_dir_all(&dir).is_err() {
            return;
        }
        let Ok(envelope) = observe_runtime(shared, identity, &current_platform_metrics(shared)) else {
            return;
        };
        let generation = envelope["message"]["evidence_generation"].as_u64().unwrap_or(0);
        let bytes = serde_json::to_vec(&envelope).unwrap_or_default();
        let _ = write_private(&dir.join(format!("runtime-observation-{:06}.json", generation % 1_000_000)), &bytes);
        if generation > EVIDENCE_RETAINED {
            let _ = std::fs::remove_file(dir.join(format!("runtime-observation-{:06}.json", (generation - EVIDENCE_RETAINED) % 1_000_000)));
        }
}

impl NodeCore {

    fn fail(&self, e: NodeError) -> NodeError {
        self.shared.lock().unwrap().status.last_error = Some(e.0.clone());
        e
    }

    /// Scan result of the owner's invite QR (a pairing channel): create the
    /// signed pairing request, post it to the channel and wait for the encrypted
    /// handoff. One scan pairs the device.
    pub fn pair_with_channel_qr(&self, qr: &str, device_ref: &str) -> Result<NodeStatus, NodeError> {
        require_registered_class().map_err(|e| self.fail(e))?;
        let channel = onboarding::decode_qr(qr).map_err(|e| self.fail(nerr(e.to_string())))?;
        if channel["protocol"] != PAIRING_CHANNEL_PROTOCOL {
            return Err(self.fail(nerr("pairing_channel_invalid")));
        }
        let url = channel["pairing_url"].as_str().ok_or_else(|| self.fail(nerr("pairing_channel_invalid")))?.to_owned();
        let session = PairingSession::new(&self.identity, device_ref, now_s(), 300, None)
            .map_err(|e| self.fail(nerr(e.to_string())))?;
        let request = session.request.clone();
        // A failed attempt puts the node back where it was (paired, joined, ...),
        // never stuck in `pairing`.
        let previous = std::mem::replace(&mut self.shared.lock().unwrap().status.phase, "pairing".into());
        let abandon = |e: NodeError| {
            self.shared.lock().unwrap().status.phase = previous.clone();
            self.fail(e)
        };
        let http = self.http.clone();
        let handoff: Value = self
            .rt
            .block_on(async move {
                let resp = http.post(&url).json(&json!({"request": request})).send().await?;
                resp.error_for_status()?.json::<Value>().await
            })
            .map_err(|e| abandon(nerr(format!("pairing_channel_unreachable:{}", e.without_url()))))?;
        let handoff = handoff.get("handoff").cloned().unwrap_or(handoff);
        let payload = session.decrypt(&handoff, now_s()).map_err(|e| abandon(nerr(e.to_string())))?;
        write_private(&self.data_dir.join("invitation.json"), &serde_json::to_vec(&payload).unwrap())
            .map_err(abandon)?;
        let mut s = self.shared.lock().unwrap();
        s.invitation = Some(payload);
        s.status.phase = "paired".into();
        s.status.last_error = None;
        Ok(s.status.clone())
    }

    /// Two-QR fallback: show this device QR to the owner console.
    pub fn begin_pairing(&self, device_ref: &str) -> Result<String, NodeError> {
        require_registered_class()?;
        let session = PairingSession::new(&self.identity, device_ref, now_s(), 300, None)
            .map_err(|e| nerr(e.to_string()))?;
        let qr = session.request_qr().map_err(|e| nerr(e.to_string()))?;
        let mut s = self.shared.lock().unwrap();
        s.session = Some(session);
        s.status.phase = "pairing".into();
        Ok(qr)
    }

    /// Join the swarm with the decrypted invitation: verify the seed's signed
    /// identity against the pinned key, sign and post the join request, verify
    /// the signed acceptance, then keep the lease alive with heartbeats.
    pub fn join(&self) -> Result<NodeStatus, NodeError> {
        let invitation = self.shared.lock().unwrap().invitation.clone().ok_or_else(|| nerr("not_paired"))?;
        let invite = onboarding::verify_invite_bundle(&invitation["invite_bundle"], now_s())
            .map_err(|e| self.fail(nerr(e.to_string())))?;
        let seed_url = invite.seed_url().trim_end_matches('/').to_owned();
        let pinned = invite.seed_key_record.clone();
        let token = invitation["invite_bundle"]["token"].as_str().unwrap_or_default().to_owned();
        let http = self.http.clone();
        let identity_env: Value = self
            .rt
            .block_on(async { http.get(format!("{seed_url}/seed/identity")).send().await?.json::<Value>().await })
            .map_err(|e| self.fail(nerr(format!("seed_unreachable:{}", e.without_url()))))?;
        let seed = membership::verify_seed_identity(&identity_env, &pinned, now_s()).map_err(|e| self.fail(nerr(e.0)))?;
        let cfg = MemberConfig {
            swarm_id: invite.swarm_id().to_owned(),
            node_id: format!("node-{}", &self.identity.verification_key_digest()[7..23]),
            seed_node_id: seed["seed_node_id"].as_str().unwrap_or_default().to_owned(),
            incarnation: self.incarnation.clone(),
            software_version: format!("mycelium-node-app/{}", env!("CARGO_PKG_VERSION")),
            peer_class: PEER_CLASS.into(),
            runtime_capability: json!({
                "runtime_backend": REFERENCE_BACKEND,
                "transport": "iroh",
                "activation_protocol": membership::ROUTER_ACTIVATION_PROTOCOL,
            }),
            ttl_seconds: 60.0,
        };
        let join = membership::join_request(
            &cfg,
            &self.identity,
            &format!("join-{}", &self.incarnation[4..]),
            invite.nonce(),
            &self.transport.advertised_addrs(),
            now_s(),
        )
        .map_err(|e| self.fail(nerr(e.0)))?;
        let http = self.http.clone();
        let body = json!({"protocol": membership::SEED_JOIN_HTTP_PROTOCOL, "invite_token": token, "join_envelope": join});
        let url = format!("{seed_url}/seed/join");
        let (status_code, reply): (u16, Value) = self
            .rt
            .block_on(async move {
                let resp = http.post(url).json(&body).send().await?;
                let code = resp.status().as_u16();
                Ok::<_, reqwest::Error>((code, resp.json::<Value>().await.unwrap_or(Value::Null)))
            })
            .map_err(|e| self.fail(nerr(format!("seed_unreachable:{}", e.without_url()))))?;
        if status_code != 200 {
            let code = reply["error"]["code"].as_str().unwrap_or("seed_join_refused");
            return Err(self.fail(nerr(format!("seed_join_refused:{code}"))));
        }
        let acceptance = membership::verify_seed_envelope(&reply, &pinned, membership::JOIN_ACCEPTANCE_PROTOCOL)
            .map_err(|e| self.fail(nerr(e.0)))?;
        let generation = acceptance["membership_generation"].as_u64().unwrap_or(0);
        self.enter_membership(cfg, generation, seed_url, pinned, acceptance["lease_expires_at"].as_f64())?;
        Ok(self.status())
    }

    /// Record the durable membership and keep it alive.
    fn enter_membership(
        &self,
        cfg: MemberConfig,
        generation: u64,
        seed_url: String,
        pinned: Value,
        lease_expires_at: Option<f64>,
    ) -> Result<(), NodeError> {
        persist_membership(&self.data_dir, &cfg, generation, &seed_url, &pinned)?;
        {
            let mut s = self.shared.lock().unwrap();
            s.status.phase = "joined".into();
            s.status.swarm_id = Some(cfg.swarm_id.clone());
            s.status.member_generation = Some(generation);
            s.status.lease_expires_at = lease_expires_at;
            s.status.last_error = None;
            s.membership = Some(ActiveMembership {
                cfg,
                generation,
                seed_url,
                pinned,
                needs_resume: false,
                pending_resume: None,
            });
        }
        self.ensure_supervisor();
        Ok(())
    }

    /// After a restart: resume the enrolled member with a fresh incarnation (no
    /// invite is consumed). Returns at once; the membership supervisor keeps
    /// retrying with backoff until the seed answers, so a cold start before the
    /// network is up still comes back. Old KV/request state is gone with the
    /// process.
    pub fn resume(&self) -> Result<NodeStatus, NodeError> {
        let raw = std::fs::read(self.data_dir.join("membership.json")).map_err(|_| nerr("not_enrolled"))?;
        let record: Value = serde_json::from_slice(&raw).map_err(|_| nerr("membership_store_corrupt"))?;
        let cfg: MemberConfig =
            serde_json::from_value(record["cfg"].clone()).map_err(|_| nerr("membership_store_corrupt"))?;
        let generation = record["generation"].as_u64().unwrap_or(0);
        let seed_url = record["seed_url"].as_str().unwrap_or_default().to_owned();
        {
            let mut s = self.shared.lock().unwrap();
            s.status.phase = "reconnecting".into();
            s.status.swarm_id = Some(cfg.swarm_id.clone());
            s.status.member_generation = Some(generation);
            s.membership = Some(ActiveMembership {
                cfg,
                generation,
                seed_url,
                pinned: record["seed_key"].clone(),
                needs_resume: true,
                pending_resume: None,
            });
        }
        self.ensure_supervisor();
        Ok(self.status())
    }

    /// One supervisor per core owns heartbeats and resumes for the current membership.
    fn ensure_supervisor(&self) {
        {
            let mut s = self.shared.lock().unwrap();
            if s.supervisor_running {
                return;
            }
            s.supervisor_running = true;
        }
        self.rt.spawn(supervise_membership(Supervisor {
            identity: self.identity.clone(),
            http: self.http.clone(),
            shared: self.shared.clone(),
            transport: self.transport.clone(),
            data_dir: self.data_dir.clone(),
            timing: self.timing,
        }));
    }

    pub fn is_enrolled(&self) -> bool {
        self.data_dir.join("membership.json").exists()
    }

    /// Persist whether the user chose to serve. A restarted app reads it back
    /// and restores serving after resuming the membership.
    pub fn set_serving_intent(&self, serving: bool) -> Result<NodeStatus, NodeError> {
        let record = json!({"protocol": SERVING_INTENT_PROTOCOL, "serving": serving});
        write_private(&self.data_dir.join(SERVING_INTENT_FILE), &serde_json::to_vec(&record).unwrap())
            .map_err(|_| self.fail(nerr("serving_intent_unwritable")))?;
        let mut s = self.shared.lock().unwrap();
        s.status.serving_intent = serving;
        Ok(s.status.clone())
    }

    pub fn serving_intent(&self) -> bool {
        self.shared.lock().unwrap().status.serving_intent
    }

    /// Load one assignment-local stage pack (digest-verified by the runtime).
    pub fn load_stage(&self, pack: &Value) -> Result<NodeStatus, NodeError> {
        let stage = Gpt2BlockStage::from_document(pack).map_err(|e| self.fail(nerr(e.0)))?;
        let mut s = self.shared.lock().unwrap();
        s.status.stage_loaded = Some(stage.pack_digest().to_owned());
        s.stage = Some(Box::new(stage));
        Ok(s.status.clone())
    }

    /// Allow-list a route peer and set where executed activations are forwarded.
    pub fn configure_route_peer(&self, endpoint_id: &str, generation: u64, forward_to: bool) -> Result<(), NodeError> {
        let id = crate::transport::parse_endpoint_id(endpoint_id).map_err(|e| nerr(e.0))?;
        self.rt.block_on(self.transport.configure_peer(id, generation));
        if forward_to {
            self.shared.lock().unwrap().forward = Some((iroh::EndpointAddr::from(id), generation));
        }
        Ok(())
    }

    /// Allow exactly these route controllers (iroh endpoint ids) on the
    /// control ALPN; every other endpoint is refused before any command is
    /// read. The controller set comes from the seed-signed assignment
    /// (M4-host proposal); an empty set refuses all control.
    pub fn set_route_controllers(&self, endpoint_ids: &[String]) -> Result<(), NodeError> {
        let ids = endpoint_ids.iter().map(|e| crate::transport::parse_endpoint_id(e).map_err(|e| nerr(e.0))).collect::<Result<Vec<_>, _>>()?;
        self.rt.block_on(self.transport.configure_controllers(ids));
        Ok(())
    }

    /// Drain: stop admitting work and release local request/KV state.
    pub fn drain(&self) -> NodeStatus {
        let mut s = self.shared.lock().unwrap();
        if let Some(stage) = s.stage.as_mut() {
            stage.release_all();
        }
        s.status.phase = if s.status.member_generation.is_some() { "drained".into() } else { s.status.phase.clone() };
        s.status.clone()
    }

    pub fn shutdown(self) {
        let t = self.transport.clone();
        self.rt.block_on(async move { t.close().await });
    }
}

const SERVING_INTENT_FILE: &str = "serving.json";
const SERVING_INTENT_PROTOCOL: &str = "mycelium.node_serving_intent.v1";

/// Missing or unreadable intent means "not serving": never serve by accident.
fn read_serving_intent(data_dir: &Path) -> bool {
    std::fs::read(data_dir.join(SERVING_INTENT_FILE))
        .ok()
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
        .is_some_and(|v| v["protocol"] == SERVING_INTENT_PROTOCOL && v["serving"] == Value::Bool(true))
}

/// Refuse work at or above the owner's thermal ceiling (`quota.thermal_ceiling_c`
/// from the invitation), judged on `thermal_max_celsius`. Unknown temperature
/// or no ceiling refuses nothing.
fn thermal_refusal(ceiling: Option<f64>, metrics: &evidence::Metrics) -> Option<String> {
    let ceiling = ceiling.filter(|c| c.is_finite())?;
    let hottest = metrics.get("thermal_max_celsius").copied().flatten()?;
    (hottest >= ceiling).then(|| format!("thermal_ceiling_reached:{hottest}>={ceiling}"))
}

fn lease_is_fresh(status: &NodeStatus, now: f64) -> bool {
    matches!(status.phase.as_str(), "joined" | "drained") && status.lease_expires_at.is_some_and(|t| now < t)
}

fn new_incarnation() -> Result<String, NodeError> {
    let mut raw = [0u8; 8];
    getrandom::getrandom(&mut raw).map_err(|_| nerr("randomness_unavailable"))?;
    Ok(format!("inc-{}", hex::encode(raw)))
}

fn persist_membership(
    data_dir: &Path,
    cfg: &MemberConfig,
    generation: u64,
    seed_url: &str,
    pinned: &Value,
) -> Result<(), NodeError> {
    let record = json!({"cfg": cfg, "generation": generation, "seed_url": seed_url, "seed_key": pinned});
    write_private(&data_dir.join("membership.json"), &serde_json::to_vec(&record).unwrap())
}

/// What one supervisor step decided.
enum Step {
    /// The lease is held (renewed or freshly resumed).
    Held,
    /// Transient failure (network, seed busy): try again after the backoff.
    Retry(String),
    /// The seed no longer honours this incarnation: resume now.
    Resume(String),
    /// The membership is over (revoked, unknown, mismatched): stop.
    Ended(String),
}

struct Supervisor {
    identity: Arc<Identity>,
    http: reqwest::Client,
    shared: Arc<Mutex<Shared>>,
    transport: NodeTransport,
    data_dir: PathBuf,
    timing: NodeTiming,
}

/// Keep the membership alive: heartbeat on the product cadence while the lease
/// is held; once the seed refuses the lease (or it lapses locally while the
/// network is down) resume with a fresh incarnation, retrying with bounded
/// exponential backoff until the network and seed answer again.
async fn supervise_membership(sv: Supervisor) {
    let mut sequence = 0u64;
    let mut delay = sv.timing.retry_min;
    loop {
        let Some(m) = sv.shared.lock().unwrap().membership.clone() else { break };
        let step = if m.needs_resume {
            let step = sv.resume_once(m).await;
            if matches!(step, Step::Held) {
                sequence = 0;
            }
            step
        } else {
            sequence += 1;
            sv.heartbeat_once(&m, sequence).await
        };
        let held = matches!(step, Step::Held);
        let pause = {
            let mut s = sv.shared.lock().unwrap();
            match step {
                Step::Held => {
                    delay = sv.timing.retry_min;
                    Some(sv.timing.heartbeat_interval)
                }
                Step::Resume(code) => {
                    s.status.last_error = Some(code);
                    s.status.phase = "reconnecting".into();
                    if let Some(m) = s.membership.as_mut() {
                        m.needs_resume = true;
                    }
                    None
                }
                Step::Retry(code) => {
                    s.status.last_error = Some(code);
                    let lapsed = s.status.lease_expires_at.is_none_or(|t| now_s() >= t);
                    if lapsed && let Some(m) = s.membership.as_mut() {
                        // Heartbeats cannot renew a lapsed lease; go straight to resume.
                        m.needs_resume = true;
                        s.status.phase = "reconnecting".into();
                    }
                    let pause = delay;
                    delay = (delay * 2).min(sv.timing.retry_max);
                    Some(pause)
                }
                Step::Ended(code) => {
                    s.status.phase =
                        if code.ends_with("seed_resume_member_revoked") { "revoked" } else { "membership_lost" }.into();
                    s.status.last_error = Some(code);
                    s.membership = None;
                    break;
                }
            }
        };
        if held {
            // The lease is held: pick up (or drop) the owner's route assignment.
            sv.sync_route().await;
        }
        if let Some(pause) = pause {
            tokio::time::sleep(pause).await;
        }
    }
    // The membership is over: no route peer is admitted on its behalf any more.
    let old = {
        let mut s = sv.shared.lock().unwrap();
        s.supervisor_running = false;
        clear_route(&mut s)
    };
    if let Some(old) = old {
        sv.transport.remove_peer(old.peer).await;
    }
}

/// Drop the applied route and withdraw its peer from the allow-list.
async fn drop_route(shared: &Arc<Mutex<Shared>>, transport: &NodeTransport) {
    let old = clear_route(&mut shared.lock().unwrap());
    if let Some(old) = old {
        transport.remove_peer(old.peer).await;
    }
}

/// An assignment past its signed expiry is never served, even when the seed
/// cannot be reached to say so.
async fn drop_expired_route(shared: &Arc<Mutex<Shared>>, transport: &NodeTransport) -> bool {
    let expired = shared.lock().unwrap().route.as_ref().is_some_and(|r| now_s() >= r.assignment.expires_at);
    if expired {
        drop_route(shared, transport).await;
        shared.lock().unwrap().status.route_assignment_error = Some("route_assignment_expired".into());
    }
    expired
}

/// Forget the applied route: stage unloaded, nothing forwarded.
fn clear_route(s: &mut Shared) -> Option<AppliedRoute> {
    let old = s.route.take();
    if let Some(stage_node) = s.stage_node.as_mut() {
        stage_node.clear();
    }
    if old.is_some() {
        s.stage = None;
        s.forward = None;
        s.status.stage_loaded = None;
        if s.status.artifact_state.as_deref() == Some("loaded") {
            s.status.artifact_state = None;
        }
        s.status.route_assignment = None;
    }
    old
}

impl Supervisor {
    async fn post(&self, path: &str, seed_url: &str, body: &Value) -> Result<(u16, Value), String> {
        let resp = self
            .http
            .post(format!("{seed_url}{path}"))
            .json(body)
            .send()
            .await
            .map_err(|e| format!("seed_unreachable:{}", e.without_url()))?;
        let code = resp.status().as_u16();
        Ok((code, resp.json::<Value>().await.unwrap_or(Value::Null)))
    }

    async fn heartbeat_once(&self, m: &ActiveMembership, sequence: u64) -> Step {
        let env = match membership::heartbeat(
            &m.cfg,
            &self.identity,
            &format!("hb-{}-{sequence}", &m.cfg.incarnation[4..]),
            m.generation,
            sequence,
            "RUNNING",
            0,
            now_s(),
        ) {
            Ok(env) => env,
            Err(e) => return Step::Ended(e.0.into()),
        };
        let body = json!({"protocol": membership::SEED_MEMBER_HTTP_PROTOCOL, "envelope": env});
        let (code, reply) = match self.post("/seed/message", &m.seed_url, &body).await {
            Ok(answer) => answer,
            Err(e) => return Step::Retry(e),
        };
        if !(200..300).contains(&code) {
            let refusal = reply["error"]["code"].as_str().unwrap_or("unknown");
            let error = format!("heartbeat_refused:{refusal}");
            return if RESUMABLE_REFUSALS.contains(&refusal) { Step::Resume(error) } else { Step::Retry(error) };
        }
        match membership::verify_seed_envelope(&reply, &m.pinned, membership::LEASE_RENEWAL_PROTOCOL) {
            Ok(renewal) => {
                let mut s = self.shared.lock().unwrap();
                s.status.heartbeats_sent += 1;
                s.status.last_heartbeat_at = Some(now_s());
                s.status.lease_expires_at = renewal["lease_expires_at"].as_f64();
                s.status.last_error = None;
                if s.status.phase == "reconnecting" {
                    s.status.phase = "joined".into();
                }
                Step::Held
            }
            Err(e) => Step::Retry(format!("lease_renewal_invalid:{}", e.0)),
        }
    }

    /// Ask the seed for this member's route assignment and apply what it signed:
    /// load the digest-verified stage, allow-list the route peer at the route
    /// generation and forward executed activations to it. A newly applied
    /// assignment is reported back at once so the owner sees it running.
    ///
    /// Fails closed: an answer that does not verify (signature, recipient,
    /// generation, request binding, stage digest) or a seed refusal drops the
    /// applied route; only an unreachable seed keeps a still-unexpired route.
    async fn sync_route(&self) {
        for _ in 0..2 {
            match self.route_exchange().await {
                Ok(true) => continue,
                Ok(false) => break,
                Err(code) => {
                    let transient = code.starts_with("seed_unreachable");
                    self.shared.lock().unwrap().status.route_assignment_error = Some(code);
                    if !transient {
                        drop_route(&self.shared, &self.transport).await;
                    }
                    break;
                }
            }
        }
        drop_expired_route(&self.shared, &self.transport).await;
    }

    /// One request/answer; `Ok(true)` when the applied route changed.
    async fn route_exchange(&self) -> Result<bool, String> {
        let (m, applied) = {
            let s = self.shared.lock().unwrap();
            (
                s.membership.clone(),
                s.route.as_ref().map(|r| (r.assignment.assignment_id.clone(), r.assignment.stage_pack_digest.clone())),
            )
        };
        let Some(m) = m.filter(|m| !m.needs_resume) else { return Ok(false) };
        let message_id = format!("route-{}", &new_incarnation().map_err(|e| e.0)?[4..]);
        let env = route_assignment::request(&m.cfg, &self.identity, &message_id, m.generation, applied.as_ref().map(|(id, _)| id.as_str()), now_s())
            .map_err(|e| e.0)?;
        let body = json!({"protocol": membership::SEED_MEMBER_HTTP_PROTOCOL, "envelope": env});
        let (code, reply) = self.post("/seed/message", &m.seed_url, &body).await?;
        if !(200..300).contains(&code) {
            return Err(format!("route_assignment_refused:{}", reply["error"]["code"].as_str().unwrap_or("unknown")));
        }
        let verified = route_assignment::verify_delivery(
            &reply,
            &route_assignment::Expected {
                pinned_seed_key: &m.pinned,
                swarm_id: &m.cfg.swarm_id,
                seed_node_id: &m.cfg.seed_node_id,
                node_id: &m.cfg.node_id,
                endpoint_id: self.identity.endpoint_id(),
                member_key_digest: &self.identity.verification_key_digest(),
                generation: m.generation,
                request_message_id: &message_id,
                now: now_s(),
            },
        )
        .map_err(|e| e.0)?;
        self.shared.lock().unwrap().status.route_assignment_error = None;
        let Some((assignment, delivered)) = verified else {
            self.cancel_acquisition(None);
            drop_route(&self.shared, &self.transport).await;
            return Ok(false);
        };
        if already_applied(applied.as_ref(), &assignment) {
            return Ok(false);
        }
        let digest = delivered.pack_digest().to_owned();
        // Apply only a stage that is loaded: a v2 manifest first needs every
        // pinned tensor promoted in the artifact store (digest-verified).
        let stage: Box<dyn StageRuntime> = match delivered {
            DeliveredStage::Gpt2(stage) => stage,
            DeliveredStage::Qwen2 { manifest, envelope, grant } => {
                Box::new(self.load_v2_stage(&assignment, &manifest, &envelope, &grant, &m.pinned)?)
            }
        };
        let peer = crate::transport::parse_endpoint_id(&assignment.peer_endpoint_id).map_err(|e| e.0)?;
        self.transport.configure_peer(peer, assignment.route_generation).await;
        let old = {
            let mut s = self.shared.lock().unwrap();
            s.status.stage_loaded = Some(digest);
            s.stage = Some(stage);
            s.forward = assignment.forward_to.then(|| (iroh::EndpointAddr::from(peer), assignment.route_generation));
            s.status.route_assignment = Some(assignment.assignment_id.clone());
            s.route.replace(AppliedRoute { assignment, peer })
        };
        if let Some(old) = old
            && old.peer != peer
        {
            self.transport.remove_peer(old.peer).await;
        }
        Ok(true)
    }

    /// Stop a running acquisition unless it is for `keep` (a pack digest).
    fn cancel_acquisition(&self, keep: Option<&str>) {
        cancel_acquisition_in(&self.shared, keep);
    }

    /// Start (once per manifest) the background A2-over-iroh acquisition of
    /// the manifest's objects from the grant's sources, with the node's own
    /// endpoint and member key. It pauses on the owner's thermal ceiling and
    /// battery floor, stops when cancelled, and reports `artifact_state`; the
    /// next route sync applies the assignment once every object is promoted.
    fn ensure_acquisition(&self, manifest: &crate::stage_manifest::StageManifest, grant: &crate::artifact_grant::Grant, quota: u64) -> Result<(), String> {
        use std::sync::atomic::{AtomicBool, Ordering};
        let network_per_day = {
            let s = self.shared.lock().unwrap();
            s.invitation.as_ref().and_then(|inv| inv["quota"]["network_bytes_per_day"].as_u64())
        }
        .ok_or("artifact_network_quota_unknown")?;
        self.cancel_acquisition(Some(&manifest.pack_digest));
        {
            let s = self.shared.lock().unwrap();
            if s.acquisition.as_ref().is_some_and(|a| !a.done.load(Ordering::SeqCst)) {
                return Ok(());
            }
        }
        // Endpoint ids resolve through discovery; the grant's signed direct
        // addresses are hints (LAN or loopback sources).
        let sources: Vec<iroh::EndpointAddr> = grant
            .sources
            .keys()
            .map(|id| {
                let mut addr = iroh::EndpointAddr::from(crate::transport::parse_endpoint_id(id).map_err(|e| e.0)?);
                for hint in grant.source_addrs.get(id).into_iter().flatten() {
                    addr = addr.with_ip_addr(*hint);
                }
                Ok::<_, String>(addr)
            })
            .collect::<Result<_, _>>()?;
        let mut client = crate::artifact_net::IrohChunkSource::new(self.transport.endpoint(), self.identity.clone(), grant.clone(), sources).map_err(|e| e.0)?;
        let (cancel, done) = (Arc::new(AtomicBool::new(false)), Arc::new(AtomicBool::new(false)));
        let (shared, root, manifest) = (self.shared.clone(), self.data_dir.join("artifacts"), manifest.clone());
        let (task_cancel, task_done) = (cancel.clone(), done.clone());
        {
            let mut s = self.shared.lock().unwrap();
            s.acquisition = Some(Acquisition { pack_digest: manifest.pack_digest.clone(), cancel, done });
            s.status.artifact_state = Some("acquiring".into());
        }
        tokio::task::spawn_blocking(move || {
            let mut gate = || {
                if task_cancel.load(Ordering::SeqCst) {
                    return Err(crate::artifact::ArtifactError("artifact_acquisition_cancelled".into()));
                }
                let quota = shared.lock().unwrap().invitation.as_ref().map(|inv| inv["quota"].clone()).unwrap_or(Value::Null);
                match acquisition_pause(&quota, &current_platform_metrics(&shared)) {
                    Some(code) => Err(crate::artifact::ArtifactError(code)),
                    None => Ok(()),
                }
            };
            // The owner's daily network quota is spent per received byte and
            // persisted; free space on the store volume bounds admission.
            let outcome = ArtifactStore::open(&root, quota).and_then(|store| {
                let mut budget = crate::artifact::NetworkBudget::open(&root, network_per_day, crate::artifact::utc_day());
                let mut metered = crate::artifact::MeteredSource { inner: &mut client, budget: &mut budget, day: &crate::artifact::utc_day };
                store.acquire_with(&manifest, &mut metered, ACQUISITION_CHUNK_BYTES, crate::artifact::available_bytes(&root), &mut gate)
            });
            let state = match outcome {
                Ok(_) => "promoted".to_owned(),
                Err(e) => format!("paused:{}", e.0),
            };
            record_acquisition_outcome(&shared, &task_cancel, state);
            task_done.store(true, Ordering::SeqCst);
        });
        Ok(())
    }

    /// Persist the seed-signed v2 manifest, then load the stage from the store
    /// once every tensor is promoted (each object re-verified, golden checked).
    /// Until then the assignment is not applied: acquisition runs in the
    /// background under the seed's grant.
    fn load_v2_stage(
        &self,
        assignment: &RouteAssignment,
        manifest: &crate::stage_manifest::StageManifest,
        envelope: &Value,
        grant: &crate::artifact_grant::Grant,
        pinned: &Value,
    ) -> Result<Qwen2Runtime, String> {
        let quota = {
            let s = self.shared.lock().unwrap();
            s.invitation.as_ref().and_then(|inv| inv["quota"]["storage_bytes"].as_u64())
        }
        .ok_or("artifact_storage_quota_unknown")?;
        let store = ArtifactStore::open(&self.data_dir.join("artifacts"), quota).map_err(|e| e.0)?;
        store.store_manifest(envelope, pinned).map_err(|e| e.0)?;
        let set_state = |state: &str| self.shared.lock().unwrap().status.artifact_state = Some(state.into());
        if !store.is_complete(manifest) {
            // Admission is checked now so an assignment that can never fit is
            // reported at once, then the objects are fetched in the background.
            store.admit(manifest, crate::artifact::available_bytes(store.root())).map_err(|e| e.0)?;
            self.ensure_acquisition(manifest, grant, quota)?;
            return Err("route_assignment_stage_artifacts_pending".into());
        }
        if let Some(Some(available)) = current_platform_metrics(&self.shared).get("memory_available_bytes").copied() {
            manifest.admit_memory(available as u64).map_err(|e| e.0)?;
        }
        // Objects re-verified against this manifest's digests and the golden
        // run now: nothing cached from another assignment can satisfy it.
        let stage = store.load_stage_for_serving(manifest).map_err(|e| e.0)?;
        let runtime = Qwen2Runtime::new(&assignment.assignment_id, &manifest.stage_id, stage).map_err(|e| e.0)?;
        self.shared.lock().unwrap().acquisition = None;
        set_state("loaded");
        Ok(runtime)
    }

    async fn resume_once(&self, m: ActiveMembership) -> Step {
        let (incarnation, env) = match m.pending_resume.clone() {
            Some(pending) => pending,
            None => {
                let incarnation = match new_incarnation() {
                    Ok(i) => i,
                    Err(e) => return Step::Retry(e.0),
                };
                let mut cfg = m.cfg.clone();
                let previous = std::mem::replace(&mut cfg.incarnation, incarnation.clone());
                match membership::resume_request(
                    &cfg,
                    &self.identity,
                    &format!("resume-{}", &incarnation[4..]),
                    &previous,
                    m.generation,
                    &self.transport.advertised_addrs(),
                    now_s(),
                ) {
                    Ok(env) => (incarnation, env),
                    Err(e) => return Step::Ended(e.0.into()),
                }
            }
        };
        if let Some(active) = self.shared.lock().unwrap().membership.as_mut() {
            active.pending_resume = Some((incarnation.clone(), env.clone()));
        }
        let body = json!({"protocol": membership::SEED_RESUME_HTTP_PROTOCOL, "resume_envelope": env});
        let (code, reply) = match self.post("/seed/resume", &m.seed_url, &body).await {
            Ok(answer) => answer,
            Err(e) => return Step::Retry(e),
        };
        if code != 200 {
            let refusal = reply["error"]["code"].as_str().unwrap_or("seed_resume_refused").to_owned();
            if refusal == "membership_message_expired"
                && let Some(active) = self.shared.lock().unwrap().membership.as_mut()
            {
                // Never committed (the seed would have replayed it): mint a fresh request.
                active.pending_resume = None;
            }
            let error = format!("seed_resume_refused:{refusal}");
            return if TERMINAL_RESUME_REFUSALS.contains(&refusal.as_str()) {
                Step::Ended(error)
            } else {
                Step::Retry(error)
            };
        }
        let acceptance =
            match membership::verify_seed_envelope(&reply, &m.pinned, membership::RESUME_ACCEPTANCE_PROTOCOL) {
                Ok(a) => a,
                Err(e) => return Step::Retry(format!("resume_acceptance_invalid:{}", e.0)),
            };
        let generation = acceptance["membership_generation"].as_u64().unwrap_or(0);
        let mut cfg = m.cfg;
        cfg.incarnation = incarnation;
        if let Err(e) = persist_membership(&self.data_dir, &cfg, generation, &m.seed_url, &m.pinned) {
            return Step::Retry(e.0);
        }
        let mut s = self.shared.lock().unwrap();
        // A new generation fences everything the previous one computed: no
        // cached replay result may answer a frame of the new generation.
        if let Some(stage) = s.stage.as_mut() {
            stage.release_all();
        }
        s.status.phase = "joined".into();
        s.status.member_generation = Some(generation);
        s.status.lease_expires_at = acceptance["lease_expires_at"].as_f64();
        s.status.last_error = None;
        s.membership = Some(ActiveMembership {
            cfg,
            generation,
            seed_url: m.seed_url,
            pinned: m.pinned,
            needs_resume: false,
            pending_resume: None,
        });
        Step::Held
    }
}

/// Dispatch admitted Router frames. A v2 (Qwen2) stage is a stage-only
/// Router participant ([`crate::stage_node::StageNode`]): every frame is
/// decided by the node, the verdict acknowledges (or refuses) the delivery,
/// and the frames it sends go to the peers its locked path names. A v1 (GPT-2
/// reference) stage keeps the device-checkpoint demo forwarding.
async fn serve_inbox(mut rx: mpsc::Receiver<Inbound>, shared: Arc<Mutex<Shared>>, transport: NodeTransport) {
    while let Some(mut inbound) = rx.recv().await {
        drop_expired_route(&shared, &transport).await;
        let v2 = shared.lock().unwrap().stage.as_ref().is_some_and(|stage| stage.backend() == crate::qwen2::QWEN2_BACKEND);
        if v2 {
            let reply = inbound.reply.take();
            let shared_task = shared.clone();
            let step = tokio::task::spawn_blocking(move || stage_step(&shared_task, &inbound)).await;
            let verdict = match step {
                Ok(Ok(step)) => {
                    for send in step.sends {
                        tokio::spawn(send_with_retry(transport.clone(), shared.clone(), send));
                    }
                    if step.ack { Ok(()) } else { Err(if step.reason.is_empty() { "refused".to_owned() } else { step.reason }) }
                }
                Ok(Err(code)) => Err(code),
                Err(_) => Err("router_dispatch_failed".to_owned()),
            };
            if let Some(reply) = reply {
                let _ = reply.send(verdict);
            }
            continue;
        }
        if let Some(reply) = inbound.reply.take() {
            // The demo path acknowledges on receipt, as before.
            let _ = reply.send(Ok(()));
        }
        // The owner's quota bounds how hot the device may run while serving.
        let ceiling = {
            let s = shared.lock().unwrap();
            s.invitation.as_ref().and_then(|inv| inv["quota"]["thermal_ceiling_c"].as_f64())
        };
        let hot = thermal_refusal(ceiling, &current_platform_metrics(&shared));
        let started = std::time::Instant::now();
        let outcome = {
            let mut s = shared.lock().unwrap();
            s.status.frames_received += 1;
            if let Some(code) = hot {
                s.status.last_error = Some(code);
                None
            } else if inbound.frame.message_type != "HopHeader" {
                None
            } else {
                let body = &inbound.frame.body;
                let result = wire::Activation::decode(&inbound.frame.payload)
                    .map_err(|e| e.0.to_string())
                    .and_then(|a| a.to_flat_f32().map_err(|e| e.0.to_string()))
                    .and_then(|(sequence, _, hidden)| {
                        let phase = match body["phase"].as_str() {
                            Some(p) => StagePhase::parse(p).map_err(|e| e.0)?,
                            None => StagePhase::Prefill,
                        };
                        let call = StageCall {
                            request_id: body["request_id"].as_str().unwrap_or(""),
                            path_id: body["path_id"].as_str().unwrap_or(""),
                            path_attempt: body["path_attempt"].as_u64().unwrap_or(0),
                            phase,
                            token_index: body["token_index"].as_i64().unwrap_or(-1),
                            position: body["position"].as_u64().unwrap_or(0),
                            sequence,
                            hidden: &hidden,
                        };
                        match s.stage.as_mut() {
                            Some(stage) => stage.execute(&call).map_err(|e| e.0),
                            None => Err("stage_not_loaded".into()),
                        }
                    });
                match result {
                    Ok(out) => {
                        s.status.frames_executed += 1;
                        s.status.last_execute_ms = Some(started.elapsed().as_secs_f64() * 1000.0);
                        Some((inbound.frame.body.clone(), out, s.forward.clone()))
                    }
                    Err(code) => {
                        s.status.last_error = Some(code);
                        None
                    }
                }
            }
        };
        if let Some((mut body, out, Some((addr, generation)))) = outcome {
            if let Some(h) = body["hop_index"].as_u64() {
                body["hop_index"] = json!(h + 1);
            }
            let payload = wire::Activation::from_flat_f32(out.sequence, out.hidden_size, &out.values).encode().unwrap_or_default();
            if let Ok(frame) = wire::encode_frame("HopHeader", &body, &payload)
                && let Err(e) = transport.send(addr, generation, &frame).await
            {
                shared.lock().unwrap().status.last_error = Some(format!("forward_failed:{}", e.0));
            }
        }
    }
}

/// The node as the control channel sees it, for one command.
struct NodeControlEnv<'a> {
    s: &'a mut Shared,
    endpoint_addr: Value,
    install: Option<(Vec<crate::node_control::PeerSpec>, u64)>,
    stop: bool,
}

impl crate::node_control::ControlEnv for NodeControlEnv<'_> {
    fn endpoint_addr(&self) -> Value {
        self.endpoint_addr.clone()
    }
    fn stage(&self) -> Option<crate::node_control::StageView> {
        let route = self.s.route.as_ref()?;
        let stage = self.s.stage.as_ref()?;
        Some(crate::node_control::StageView {
            assignment_id: route.assignment.assignment_id.clone(),
            stage_id: stage.stage_id().to_owned(),
            pack_digest: route.assignment.stage_pack_digest.clone(),
            backend: stage.backend().to_owned(),
        })
    }
    fn install_peers(&mut self, peers: &[crate::node_control::PeerSpec], local_generation: u64) -> Result<(), String> {
        for p in peers {
            serde_json::from_value::<iroh::EndpointAddr>(p.endpoint_addr.clone()).map_err(|_| "invalid_peer_binding".to_owned())?;
        }
        self.install = Some((peers.to_vec(), local_generation));
        Ok(())
    }
    fn health(&self) -> Value {
        json!({
            "stage_loaded": self.s.stage.is_some(),
            "frames_received": self.s.status.frames_received,
            "frames_executed": self.s.status.frames_executed,
            "last_error": self.s.status.last_error,
        })
    }
    fn host_resources(&self) -> Value {
        let backend = self.s.stage.as_ref().map(|stage| stage.backend()).unwrap_or(crate::qwen2::QWEN2_BACKEND);
        let now_ms = (now_s() * 1000.0) as u64;
        json!({
            "protocol": "mycelium.host_resource_snapshot.v1",
            "observed_at_unix_ms": now_ms,
            "valid_until_unix_ms": now_ms + 120_000,
            "backend": backend,
            "supported_architectures": ["qwen2"],
            "supported_dtypes": ["float32"],
            "supported_quantizations": ["int8-weight-only"],
            "decode_modes_by_architecture": {"qwen2": [crate::node_control::REPLAY_DECODE_MODE]},
        })
    }
    fn monotonic_ns(&self) -> u64 {
        (monotonic_s() * 1e9) as u64
    }
    fn stop(&mut self) {
        self.stop = true;
    }
}

/// Answer the route controller's commands. Each command runs on the blocking
/// pool under the shared lock (never behind a stage run: the stage step takes
/// the same lock only around its own decision), then its transport effect
/// (route peers, drain) is applied.
async fn serve_control(
    mut rx: mpsc::Receiver<crate::transport::ControlRequest>,
    shared: Arc<Mutex<Shared>>,
    transport: NodeTransport,
    identity: Arc<Identity>,
    incarnation: String,
) {
    while let Some(request) = rx.recv().await {
        let (shared_task, identity_task, incarnation_task) = (shared.clone(), identity.clone(), incarnation.clone());
        let endpoint_addr = serde_json::to_value(transport.addr()).unwrap_or(Value::Null);
        let endpoint_id = transport.endpoint_id();
        let outcome = tokio::task::spawn_blocking(move || {
            let mut guard = shared_task.lock().unwrap();
            let s = &mut *guard;
            let node_id = s.membership.as_ref().map(|m| m.cfg.node_id.clone()).unwrap_or_default();
            // A stopped session ends; the next controller opens a new one.
            let mut control = s.control.take().filter(|c| !c.stopped()).unwrap_or_else(|| {
                crate::node_control::ControlNode::new(crate::node_control::ControlIdentity {
                    node_id: node_id.clone(),
                    host_id: node_id.clone(),
                    process_id: incarnation_task.clone(),
                    endpoint_id: endpoint_id.clone(),
                    boot_id: incarnation_task.clone(),
                })
            });
            let mut env = NodeControlEnv { s, endpoint_addr, install: None, stop: false };
            let response = control.handle(&request.from.to_string(), &request.command, &identity_task, &mut env);
            let (install, stop) = (env.install.take(), env.stop);
            s.control = Some(control);
            (response, install, stop)
        })
        .await;
        let Ok((response, install, stop)) = outcome else { continue };
        if let Some((peers, _local_generation)) = install {
            let bindings = peers
                .into_iter()
                .filter_map(|p| {
                    let addr = serde_json::from_value::<iroh::EndpointAddr>(p.endpoint_addr).ok()?;
                    Some(crate::transport::PeerBinding { generation: p.generation, node_id: p.node_id, addr })
                })
                .collect();
            transport.configure_route_peers(bindings).await;
        }
        if stop {
            let mut s = shared.lock().unwrap();
            if let Some(stage_node) = s.stage_node.as_mut() {
                stage_node.clear();
            }
        }
        let _ = request.reply.send(response);
    }
}

/// One Router frame through the stage node, under the shared lock, on the
/// blocking pool (the stage run is CPU work). The owner's thermal ceiling
/// turns an admitted hop into a `thermal_ceiling_reached` FailureReport.
fn stage_step(shared: &Arc<Mutex<Shared>>, inbound: &Inbound) -> Result<crate::stage_node::Step, String> {
    let ceiling = {
        let s = shared.lock().unwrap();
        s.invitation.as_ref().and_then(|inv| inv["quota"]["thermal_ceiling_c"].as_f64())
    };
    let hot = thermal_refusal(ceiling, &current_platform_metrics(shared));
    let mut guard = shared.lock().unwrap();
    let s = &mut *guard;
    s.status.frames_received += 1;
    if s.stage_node.is_none() {
        let local = s.membership.as_ref().map(|m| m.cfg.node_id.clone()).ok_or("not_a_member")?;
        s.stage_node = Some(crate::stage_node::StageNode::new(&local));
    }
    let stage_node = s.stage_node.as_mut().expect("installed above");
    let stage = s.stage.as_mut().ok_or("stage_not_loaded")?;
    let started = std::time::Instant::now();
    let mut executed = false;
    let step = stage_node.on_frame_at(monotonic_s(), &inbound.from_node, &inbound.frame, &mut |path, hop, payload| {
        executed = true;
        if hot.is_some() {
            return Err("thermal_ceiling_reached".into());
        }
        let activation = wire::Activation::decode(payload).map_err(|e| e.0.to_string())?;
        let (sequence, hidden_size, hidden) = activation.to_flat_f32().map_err(|e| e.0.to_string())?;
        if hidden_size != path.hidden_size {
            return Err("request_hidden_shape_invalid".into());
        }
        let call = StageCall {
            request_id: &hop.request_id,
            path_id: &hop.path_id,
            path_attempt: hop.path_attempt.max(0) as u64,
            phase: StagePhase::parse(&hop.phase).map_err(|e| e.0)?,
            token_index: hop.token_index,
            position: hop.position as u64,
            sequence,
            hidden: &hidden,
        };
        let out = stage.execute(&call).map_err(|e| e.0)?;
        wire::Activation::from_flat_f32(out.sequence, out.hidden_size, &out.values).encode().map_err(|e| e.0.to_string())
    });
    if let Some((request_id, path_id)) = &step.released {
        stage.release_path(request_id, path_id);
    }
    if executed {
        if step.disposition == Some("FORWARDED") {
            s.status.frames_executed += 1;
            s.status.last_execute_ms = Some(started.elapsed().as_secs_f64() * 1000.0);
        } else if !step.reason.is_empty() {
            s.status.last_error = Some(step.reason.clone());
        }
    }
    Ok(step)
}

/// Deliver one frame the stage node sent to its peer node, retrying like the
/// sidecar's outbound worker until acknowledged, refused for rotation or
/// replay collision, or out of attempts.
async fn send_with_retry(transport: NodeTransport, shared: Arc<Mutex<Shared>>, send: crate::stage_node::Send) {
    let mut delay = Duration::from_millis(250);
    for _ in 0..5 {
        let Some(peer) = transport.peer_for_node(&send.to).await else {
            shared.lock().unwrap().status.last_error = Some(format!("route_peer_unknown:{}", send.to));
            return;
        };
        match transport.send(peer.addr.clone(), peer.generation, &send.frame).await {
            Ok(_) => return,
            Err(e) if e.0 == "peer_rotated" || e.0 == "replay_collision" => {
                shared.lock().unwrap().status.last_error = Some(format!("forward_refused:{}", e.0));
                return;
            }
            Err(e) => shared.lock().unwrap().status.last_error = Some(format!("forward_failed:{}", e.0)),
        }
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(Duration::from_secs(4));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frames_are_refused_at_the_owner_thermal_ceiling_only() {
        let mut m = evidence::Metrics::new();
        m.insert("thermal_max_celsius".into(), Some(44.9));
        assert_eq!(thermal_refusal(Some(45.0), &m), None);
        m.insert("thermal_max_celsius".into(), Some(45.0));
        assert_eq!(thermal_refusal(Some(45.0), &m).as_deref(), Some("thermal_ceiling_reached:45>=45"));
        assert_eq!(thermal_refusal(None, &m), None);
        m.insert("thermal_max_celsius".into(), None);
        assert_eq!(thermal_refusal(Some(45.0), &m), None);
    }

    #[test]
    fn acquisition_pauses_on_the_thermal_ceiling_or_below_the_battery_floor_unless_charging() {
        let quota = serde_json::json!({"thermal_ceiling_c": 45.0, "battery_floor_percent": 30.0});
        let mut m = evidence::Metrics::new();
        // Readings the gate cannot see pause it (fail closed), as does a missing quota.
        assert_eq!(acquisition_pause(&quota, &m).as_deref(), Some("platform_reading_unknown:thermal_max_celsius"));
        m.insert("thermal_max_celsius".into(), Some(30.0));
        assert_eq!(acquisition_pause(&quota, &m).as_deref(), Some("platform_reading_unknown:battery_level_percent"));
        m.insert("battery_level_percent".into(), Some(80.0));
        assert_eq!(acquisition_pause(&Value::Null, &m).as_deref(), Some("acquisition_quota_unknown"));
        m.insert("thermal_max_celsius".into(), Some(46.0));
        assert!(acquisition_pause(&quota, &m).unwrap().starts_with("thermal_ceiling_reached"));
        m.insert("thermal_max_celsius".into(), Some(40.0));
        m.insert("battery_level_percent".into(), Some(25.0));
        m.insert("charging".into(), Some(0.0));
        assert_eq!(acquisition_pause(&quota, &m).as_deref(), Some("battery_below_floor:25<30"));
        m.insert("charging".into(), Some(1.0));
        assert_eq!(acquisition_pause(&quota, &m), None);
        m.insert("charging".into(), Some(0.0));
        m.insert("battery_level_percent".into(), Some(31.0));
        assert_eq!(acquisition_pause(&quota, &m), None);
    }

    #[test]
    fn an_applied_route_short_circuits_only_for_the_same_assignment_and_manifest() {
        let a = RouteAssignment {
            assignment_id: "route-1".into(),
            stage_pack_digest: "sha256:aa".into(),
            peer_endpoint_id: "p".into(),
            route_generation: 1,
            forward_to: true,
            expires_at: 1.0,
        };
        assert!(already_applied(Some(&("route-1".into(), "sha256:aa".into())), &a));
        assert!(!already_applied(Some(&("route-1".into(), "sha256:bb".into())), &a), "same id, other manifest");
        assert!(!already_applied(Some(&("route-2".into(), "sha256:aa".into())), &a));
        assert!(!already_applied(None, &a));
    }

    #[test]
    fn a_cancelled_acquisition_never_reports_its_outcome() {
        use std::sync::atomic::AtomicBool;
        let dir = std::env::temp_dir().join(format!("mycelium-node-acq-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let core = NodeCore::open(&dir, Reachability::LocalOnly).unwrap();
        let shared = core.shared.clone();
        let cancel = Arc::new(AtomicBool::new(false));
        shared.lock().unwrap().acquisition =
            Some(Acquisition { pack_digest: "sha256:aa".into(), cancel: cancel.clone(), done: Arc::new(AtomicBool::new(false)) });
        shared.lock().unwrap().status.artifact_state = Some("promoted".into());
        cancel_acquisition_in(&shared, None);
        assert!(shared.lock().unwrap().acquisition.is_none());
        assert_eq!(shared.lock().unwrap().status.artifact_state, None, "a cancelled acquisition leaves no state");
        record_acquisition_outcome(&shared, &cancel, "promoted".into());
        assert_eq!(shared.lock().unwrap().status.artifact_state, None, "a late outcome of a cancelled task is dropped");
        let live = Arc::new(AtomicBool::new(false));
        record_acquisition_outcome(&shared, &live, "promoted".into());
        assert_eq!(shared.lock().unwrap().status.artifact_state.as_deref(), Some("promoted"));
        core.shutdown();
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The golden stage's output: `0.5 * ((i % 17) - 8) / 8` per value.
    struct GoldenStage {
        released: Arc<Mutex<Vec<(String, String)>>>,
    }

    impl StageRuntime for GoldenStage {
        fn backend(&self) -> &'static str {
            crate::qwen2::QWEN2_BACKEND
        }
        fn assignment_id(&self) -> &str {
            "assignment-golden"
        }
        fn stage_id(&self) -> &str {
            "stage-golden"
        }
        fn hidden_size(&self) -> usize {
            8
        }
        fn execute(&mut self, call: &StageCall<'_>) -> Result<crate::runtime::StageOutput, crate::runtime::RuntimeError> {
            let values = (0..call.sequence * 8).map(|i| 0.5 * (((i % 17) as f32) - 8.0) / 8.0).collect();
            Ok(crate::runtime::StageOutput { sequence: call.sequence, hidden_size: 8, values })
        }
        fn release_all(&mut self) -> usize {
            0
        }
        fn release_path(&mut self, request_id: &str, path_id: &str) -> usize {
            self.released.lock().unwrap().push((request_id.to_owned(), path_id.to_owned()));
            1
        }
    }

    fn golden(name: &str) -> Vec<u8> {
        std::fs::read(format!("{}/../../contracts/phone-stage-wire-golden/{name}", env!("CARGO_MANIFEST_DIR"))).unwrap()
    }

    /// A v2 stage served end to end over real loopback iroh endpoints: the
    /// entry's frames reach `serve_inbox` through the dispatch transport, the
    /// stage node's verdict is the transport acknowledgement (a refused
    /// registration is returned to the sender), the forwarded hop reaches the
    /// final node byte for byte, a hot device answers with a
    /// `thermal_ceiling_reached` FailureReport to the entry, a cancellation
    /// releases the runtime's replay results, and a route change keeps the
    /// path tables.
    #[tokio::test(flavor = "multi_thread")]
    async fn serve_inbox_runs_the_stage_node_over_the_transport() {
        use crate::transport::{PeerBinding, Reachability, parse_endpoint_id};
        let phone = NodeTransport::bind(&[21; 32], Reachability::LocalOnly).await.unwrap();
        let entry = NodeTransport::bind(&[22; 32], Reachability::LocalOnly).await.unwrap();
        let last = NodeTransport::bind(&[23; 32], Reachability::LocalOnly).await.unwrap();
        let id = |t: &NodeTransport| parse_endpoint_id(&t.endpoint_id()).unwrap();
        phone
            .configure_route_peers(vec![
                PeerBinding { generation: 1, node_id: "node-mac-entry".into(), addr: entry.addr() },
                PeerBinding { generation: 1, node_id: "node-mac-final".into(), addr: last.addr() },
            ])
            .await;
        entry.configure_peer(id(&phone), 1).await;
        last.configure_peer(id(&phone), 1).await;
        let (entry_tx, mut entry_rx) = mpsc::channel(8);
        let (last_tx, mut last_rx) = mpsc::channel(8);
        let _entry_accept = entry.spawn_accept(entry_tx);
        let _last_accept = last.spawn_accept(last_tx);

        let released = Arc::new(Mutex::new(Vec::new()));
        let shared = Arc::new(Mutex::new(Shared {
            observation: ObservationCursor { window_started_at: now_s(), frames_at_window_start: 0, evidence_generation: 0 },
            membership: None,
            supervisor_running: false,
            status: NodeStatus::default(),
            session: None,
            invitation: Some(json!({"quota": {"thermal_ceiling_c": 45.0}})),
            stage: Some(Box::new(GoldenStage { released: released.clone() })),
            forward: None,
            route: None,
            shell_metrics: None,
            acquisition: None,
            stage_node: Some(crate::stage_node::StageNode::new("node-phone")),
            control: None,
        }));
        let (tx, rx) = mpsc::channel(8);
        let _accept = phone.spawn_accept_dispatch(tx);
        tokio::spawn(serve_inbox(rx, shared.clone(), phone.clone()));
        let quiet = |rx: &mut mpsc::Receiver<crate::transport::Inbound>| rx.try_recv().is_err();

        // Registration: accepted from the entry; a manifest the final node
        // relays for a path the phone does not hold is refused to the sender.
        entry.send(phone.addr(), 1, &golden("01-manifest-locked.bin")).await.unwrap();
        let mut other = wire::decode_frame(&golden("01-manifest-locked.bin")).unwrap();
        other.body["path_id"] = json!("path-other");
        other.body["manifest"]["path_id"] = json!("path-other");
        other.body["build"]["path_id"] = json!("path-other");
        let other = wire::encode_frame("ManifestLocked", &other.body, &other.payload).unwrap();
        assert_eq!(last.send(phone.addr(), 1, &other).await.unwrap_err().0, "manifest_registration_rejected");

        // A hop runs and its forward reaches the final node byte for byte.
        entry.send(phone.addr(), 1, &golden("02-hop-prefill-in.bin")).await.unwrap();
        let forwarded = tokio::time::timeout(Duration::from_secs(10), last_rx.recv()).await.unwrap().unwrap();
        assert_eq!(forwarded.from_node, id(&phone).to_string());
        assert_eq!(forwarded.frame, wire::decode_frame(&golden("03-hop-prefill-forward.bin")).unwrap());
        // A redelivery is answered from the ledger: nothing runs or is resent.
        entry.send(phone.addr(), 1, &golden("02-hop-prefill-in.bin")).await.unwrap();
        tokio::time::sleep(Duration::from_millis(300)).await;
        assert!(quiet(&mut last_rx));
        assert_eq!(shared.lock().unwrap().status.frames_executed, 1);

        // Hot device: the admitted hop becomes a FailureReport to the entry.
        let mut hot = evidence::Metrics::new();
        hot.insert("battery_temperature_celsius".into(), Some(50.0));
        shared.lock().unwrap().shell_metrics = Some((hot, now_s()));
        entry.send(phone.addr(), 1, &golden("04-hop-decode-in.bin")).await.unwrap();
        let report = tokio::time::timeout(Duration::from_secs(10), entry_rx.recv())
            .await
            .unwrap_or_else(|_| {
                let hot = thermal_refusal(Some(45.0), &current_platform_metrics(&shared));
                let s = shared.lock().unwrap();
                panic!("no FailureReport; last_error={:?} received={} executed={} hot={hot:?}", s.status.last_error, s.status.frames_received, s.status.frames_executed)
            })
            .unwrap();
        assert_eq!(report.frame.message_type, "FailureReport");
        assert_eq!(report.frame.body["reason"], "thermal_ceiling_reached");
        assert_eq!(report.frame.body["node_id"], "node-phone");
        assert!(quiet(&mut last_rx));

        // The entry's cancellation releases the runtime's replay results.
        entry.send(phone.addr(), 1, &golden("08-path-cancellation.bin")).await.unwrap();
        assert_eq!(*released.lock().unwrap(), vec![("request-phone-golden".to_owned(), "path-phone-golden".to_owned())]);
        // A route change forgets live paths but never revives the cancelled attempt.
        clear_route(&mut shared.lock().unwrap());
        assert_eq!(entry.send(phone.addr(), 1, &golden("01-manifest-locked.bin")).await.unwrap_err().0, "manifest_registration_rejected");
    }

    /// The control channel over a real loopback iroh connection: only an
    /// allow-listed controller is answered; `hello`, `configure` (bound to the
    /// phone's applied stage) and `start` return phone-signed observations,
    /// and `start` installs the route peers the stage node forwards to.
    #[tokio::test(flavor = "multi_thread")]
    async fn the_control_channel_answers_only_the_route_controller() {
        use crate::transport::{Reachability, parse_endpoint_id};
        let phone = NodeTransport::bind(&[31; 32], Reachability::LocalOnly).await.unwrap();
        let controller = NodeTransport::bind(&[32; 32], Reachability::LocalOnly).await.unwrap();
        let stranger = NodeTransport::bind(&[33; 32], Reachability::LocalOnly).await.unwrap();
        let entry = NodeTransport::bind(&[34; 32], Reachability::LocalOnly).await.unwrap();
        let identity = Arc::new(Identity::from_secret(&phone.endpoint_id(), &[35; 32]).unwrap());
        let assignment = RouteAssignment {
            assignment_id: "assignment-1".into(),
            stage_pack_digest: "sha256:pack".into(),
            peer_endpoint_id: entry.endpoint_id(),
            route_generation: 1,
            forward_to: false,
            expires_at: now_s() + 3600.0,
        };
        let shared = Arc::new(Mutex::new(Shared {
            observation: ObservationCursor { window_started_at: now_s(), frames_at_window_start: 0, evidence_generation: 0 },
            membership: None,
            supervisor_running: false,
            status: NodeStatus::default(),
            session: None,
            invitation: None,
            stage: Some(Box::new(GoldenStage { released: Arc::default() })),
            forward: None,
            route: Some(AppliedRoute { assignment, peer: parse_endpoint_id(&entry.endpoint_id()).unwrap() }),
            shell_metrics: None,
            acquisition: None,
            stage_node: None,
            control: Some(crate::node_control::ControlNode::new(crate::node_control::ControlIdentity {
                node_id: "node-phone".into(),
                host_id: "host-phone".into(),
                process_id: "process-1".into(),
                endpoint_id: phone.endpoint_id(),
                boot_id: "boot-1".into(),
            })),
        }));
        let (dispatch_tx, _dispatch_rx) = mpsc::channel(8);
        let _accept = phone.spawn_accept_dispatch(dispatch_tx);
        let (control_tx, control_rx) = mpsc::channel(8);
        phone.set_control_handler(Some(control_tx)).await;
        phone.configure_controllers([parse_endpoint_id(&controller.endpoint_id()).unwrap()]).await;
        tokio::spawn(serve_control(control_rx, shared.clone(), phone.clone(), identity.clone(), "inc-1".into()));

        let command = |command: &str, payload: Value| {
            serde_json::to_vec(&json!({"protocol": crate::node_control::NODE_CONTROL_PROTOCOL, "command_id": format!("c-{command}"), "run_id": "run-1", "deployment_id": "dep-1", "command": command, "payload": payload})).unwrap()
        };
        // Not the route controller: refused before any command is read.
        assert!(stranger.control_call(phone.addr(), &command("hello", json!({}))).await.is_err());
        let call = |bytes: Vec<u8>| {
            let (controller, addr) = (controller.clone(), phone.addr());
            async move { serde_json::from_slice::<Value>(&controller.control_call(addr, &bytes).await.unwrap()).unwrap() }
        };
        let hello = call(command("hello", json!({}))).await;
        assert_eq!((hello["ok"].clone(), hello["result"]["endpoint_id"].clone()), (json!(true), json!(phone.endpoint_id())));
        let configure = json!({"assignment_id": "assignment-1", "stage_id": "stage-golden", "pack_digest": "sha256:pack", "manifest_digest": "sha256:graph", "load_generation": 1});
        let configured = call(command("configure", configure)).await;
        assert_eq!(configured["ok"], true, "{configured}");
        let result = &configured["result"];
        crate::identity::verify(&result["observation"], &result["signature"], &result["verification_key"]).unwrap();
        assert_eq!(result["observation"]["details"]["endpoint_addr"]["id"], phone.endpoint_id());
        let entry_addr = serde_json::to_value(entry.addr()).unwrap();
        let start = json!({"peer": {"node_id": "node-mac-entry", "endpoint_id": entry.endpoint_id(), "endpoint_addr": entry_addr, "generation": 4}, "local_generation": 2});
        let started = call(command("start", start)).await;
        assert_eq!(started["result"]["observation"]["event"], "started", "{started}");
        assert_eq!(phone.peer_for_node("node-mac-entry").await.unwrap().generation, 4);
        let refused = call(command("infer_start", json!({}))).await;
        assert_eq!(refused["error"]["code"], "stage_only_node");
    }
}
