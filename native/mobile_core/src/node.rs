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
                let _ = t.spawn_accept(tx).await;
            }
        });
        self.rt.spawn(serve_inbox(rx, self.shared.clone(), self.transport.clone()));
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
            (s.membership.clone(), s.route.as_ref().map(|r| r.assignment.assignment_id.clone()))
        };
        let Some(m) = m.filter(|m| !m.needs_resume) else { return Ok(false) };
        let message_id = format!("route-{}", &new_incarnation().map_err(|e| e.0)?[4..]);
        let env = route_assignment::request(&m.cfg, &self.identity, &message_id, m.generation, applied.as_deref(), now_s())
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
                generation: m.generation,
                request_message_id: &message_id,
                now: now_s(),
            },
        )
        .map_err(|e| e.0)?;
        self.shared.lock().unwrap().status.route_assignment_error = None;
        let Some((assignment, delivered)) = verified else {
            drop_route(&self.shared, &self.transport).await;
            return Ok(false);
        };
        if applied.as_deref() == Some(assignment.assignment_id.as_str()) {
            return Ok(false);
        }
        let digest = delivered.pack_digest().to_owned();
        // Apply only a stage that is loaded: a v2 manifest first needs every
        // pinned tensor promoted in the artifact store (digest-verified).
        let stage: Box<dyn StageRuntime> = match delivered {
            DeliveredStage::Gpt2(stage) => stage,
            DeliveredStage::Qwen2 { manifest, envelope } => {
                Box::new(self.load_v2_stage(&assignment, &manifest, &envelope, &m.pinned)?)
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

    /// Persist the seed-signed v2 manifest, then load the stage from the store
    /// once every tensor is promoted (each object re-verified, golden checked).
    /// Until then the assignment is not applied (`acquisition_pending`).
    fn load_v2_stage(
        &self,
        assignment: &RouteAssignment,
        manifest: &crate::stage_manifest::StageManifest,
        envelope: &Value,
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
            // reported at once; acquisition itself is the artifact client's job.
            store.admit(manifest, None).map_err(|e| e.0)?;
            set_state("acquisition_pending");
            return Err("route_assignment_stage_artifacts_pending".into());
        }
        if let Some(Some(available)) = current_platform_metrics(&self.shared).get("memory_available_bytes").copied() {
            manifest.admit_memory(available as u64).map_err(|e| e.0)?;
        }
        let stage = store.load_stage(manifest).map_err(|e| e.0)?;
        let runtime = Qwen2Runtime::new(&assignment.assignment_id, &manifest.stage_id, stage).map_err(|e| e.0)?;
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

/// Execute admitted HopHeader frames carrying MYAC float32 activations on the
/// loaded stage and forward the result to the configured next peer.
async fn serve_inbox(mut rx: mpsc::Receiver<Inbound>, shared: Arc<Mutex<Shared>>, transport: NodeTransport) {
    while let Some(inbound) = rx.recv().await {
        drop_expired_route(&shared, &transport).await;
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
}
