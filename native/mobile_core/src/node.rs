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

use crate::identity::Identity;
use crate::membership::{self, MemberConfig};
use crate::onboarding::{self, PairingSession};
use crate::runtime::{Gpt2BlockStage, REFERENCE_BACKEND, StageRuntime};
use crate::transport::{Inbound, NodeTransport, Reachability};
use crate::wire;

pub const PEER_CLASS: &str = onboarding::ANDROID_PEER_CLASS;
pub const PAIRING_CHANNEL_PROTOCOL: &str = "mycelium.pairing_channel.v1";
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(30);
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);

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
}

struct Shared {
    status: NodeStatus,
    session: Option<PairingSession>,
    invitation: Option<Value>,
    stage: Option<Box<dyn StageRuntime>>,
    forward: Option<(iroh::EndpointAddr, u64)>,
}

pub struct NodeCore {
    rt: Runtime,
    data_dir: PathBuf,
    identity: Arc<Identity>,
    transport: NodeTransport,
    shared: Arc<Mutex<Shared>>,
    http: reqwest::Client,
    incarnation: String,
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
        let mut incarnation_raw = [0u8; 8];
        getrandom::getrandom(&mut incarnation_raw).map_err(|_| nerr("randomness_unavailable"))?;
        let status = NodeStatus {
            phase: "unpaired".into(),
            endpoint_id: transport.endpoint_id(),
            peer_class: PEER_CLASS.into(),
            runtime_backend: REFERENCE_BACKEND.into(),
            ..Default::default()
        };
        let http = reqwest::Client::builder().timeout(HTTP_TIMEOUT).build().map_err(|_| nerr("http_client_failed"))?;
        let core = Self {
            rt,
            data_dir: data_dir.to_owned(),
            identity: Arc::new(identity),
            transport,
            shared: Arc::new(Mutex::new(Shared { status, session: None, invitation: None, stage: None, forward: None })),
            http,
            incarnation: format!("inc-{}", hex::encode(incarnation_raw)),
        };
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
        self.rt.spawn(async move {
            let online = transport.online(Duration::from_secs(20)).await;
            shared.lock().unwrap().status.relay_online = online;
        });
        let (tx, rx) = mpsc::channel(64);
        self.rt.spawn({
            let t = self.transport.clone();
            async move {
                let _ = t.spawn_accept(tx).await;
            }
        });
        self.rt.spawn(serve_inbox(rx, self.shared.clone(), self.transport.clone()));
    }

    pub fn status(&self) -> NodeStatus {
        self.shared.lock().unwrap().status.clone()
    }

    fn fail(&self, e: NodeError) -> NodeError {
        self.shared.lock().unwrap().status.last_error = Some(e.0.clone());
        e
    }

    /// Scan result of the owner's invite QR (a pairing channel): create the
    /// signed pairing request, post it to the channel and wait for the encrypted
    /// handoff. One scan pairs the device.
    pub fn pair_with_channel_qr(&self, qr: &str, device_ref: &str) -> Result<NodeStatus, NodeError> {
        let channel = onboarding::decode_qr(qr).map_err(|e| self.fail(nerr(e.to_string())))?;
        if channel["protocol"] != PAIRING_CHANNEL_PROTOCOL {
            return Err(self.fail(nerr("pairing_channel_invalid")));
        }
        let url = channel["pairing_url"].as_str().ok_or_else(|| nerr("pairing_channel_invalid"))?.to_owned();
        let session = PairingSession::new(&self.identity, device_ref, now_s(), 300, None)
            .map_err(|e| self.fail(nerr(e.to_string())))?;
        let request = session.request.clone();
        self.shared.lock().unwrap().status.phase = "pairing".into();
        let http = self.http.clone();
        let handoff: Value = self
            .rt
            .block_on(async move {
                let resp = http.post(&url).json(&json!({"request": request})).send().await?;
                resp.error_for_status()?.json::<Value>().await
            })
            .map_err(|e| self.fail(nerr(format!("pairing_channel_unreachable:{}", e.without_url()))))?;
        let handoff = handoff.get("handoff").cloned().unwrap_or(handoff);
        let payload = session.decrypt(&handoff, now_s()).map_err(|e| self.fail(nerr(e.to_string())))?;
        write_private(&self.data_dir.join("invitation.json"), &serde_json::to_vec(&payload).unwrap())?;
        let mut s = self.shared.lock().unwrap();
        s.invitation = Some(payload);
        s.status.phase = "paired".into();
        s.status.last_error = None;
        Ok(s.status.clone())
    }

    /// Two-QR fallback: show this device QR to the owner console.
    pub fn begin_pairing(&self, device_ref: &str) -> Result<String, NodeError> {
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
        let record = json!({"cfg": cfg, "generation": generation, "seed_url": seed_url, "seed_key": pinned});
        write_private(&self.data_dir.join("membership.json"), &serde_json::to_vec(&record).unwrap())?;
        {
            let mut s = self.shared.lock().unwrap();
            s.status.phase = "joined".into();
            s.status.swarm_id = Some(cfg.swarm_id.clone());
            s.status.member_generation = Some(generation);
            s.status.lease_expires_at = lease_expires_at;
            s.status.last_error = None;
        }
        self.rt.spawn(heartbeat_loop(
            cfg,
            self.identity.clone(),
            generation,
            seed_url,
            pinned,
            self.http.clone(),
            self.shared.clone(),
        ));
        Ok(())
    }

    /// After a restart: resume the enrolled member with a fresh incarnation
    /// (no invite is consumed). Old KV/request state is gone with the process.
    pub fn resume(&self) -> Result<NodeStatus, NodeError> {
        let raw = std::fs::read(self.data_dir.join("membership.json")).map_err(|_| nerr("not_enrolled"))?;
        let record: Value = serde_json::from_slice(&raw).map_err(|_| nerr("membership_store_corrupt"))?;
        let mut cfg: MemberConfig =
            serde_json::from_value(record["cfg"].clone()).map_err(|_| nerr("membership_store_corrupt"))?;
        let previous = std::mem::replace(&mut cfg.incarnation, self.incarnation.clone());
        let generation = record["generation"].as_u64().unwrap_or(0);
        let seed_url = record["seed_url"].as_str().unwrap_or_default().to_owned();
        let pinned = record["seed_key"].clone();
        let env = membership::resume_request(
            &cfg,
            &self.identity,
            &format!("resume-{}", &self.incarnation[4..]),
            &previous,
            generation,
            &self.transport.advertised_addrs(),
            now_s(),
        )
        .map_err(|e| self.fail(nerr(e.0)))?;
        let http = self.http.clone();
        let body = json!({"protocol": membership::SEED_RESUME_HTTP_PROTOCOL, "resume_envelope": env});
        let url = format!("{seed_url}/seed/resume");
        let (code, reply): (u16, Value) = self
            .rt
            .block_on(async move {
                let resp = http.post(url).json(&body).send().await?;
                let code = resp.status().as_u16();
                Ok::<_, reqwest::Error>((code, resp.json::<Value>().await.unwrap_or(Value::Null)))
            })
            .map_err(|e| self.fail(nerr(format!("seed_unreachable:{}", e.without_url()))))?;
        if code != 200 {
            let c = reply["error"]["code"].as_str().unwrap_or("seed_resume_refused");
            return Err(self.fail(nerr(format!("seed_resume_refused:{c}"))));
        }
        let acceptance = membership::verify_seed_envelope(&reply, &pinned, membership::RESUME_ACCEPTANCE_PROTOCOL)
            .map_err(|e| self.fail(nerr(e.0)))?;
        let generation = acceptance["membership_generation"].as_u64().unwrap_or(0);
        self.enter_membership(cfg, generation, seed_url, pinned, acceptance["lease_expires_at"].as_f64())?;
        Ok(self.status())
    }

    pub fn is_enrolled(&self) -> bool {
        self.data_dir.join("membership.json").exists()
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

async fn heartbeat_loop(
    cfg: MemberConfig,
    identity: Arc<Identity>,
    generation: u64,
    seed_url: String,
    pinned: Value,
    http: reqwest::Client,
    shared: Arc<Mutex<Shared>>,
) {
    let mut sequence = 0u64;
    loop {
        sequence += 1;
        let active = shared.lock().unwrap().status.frames_received;
        let env = match membership::heartbeat(
            &cfg,
            &identity,
            &format!("hb-{}-{sequence}", &cfg.incarnation[4..]),
            generation,
            sequence,
            "RUNNING",
            0,
            now_s(),
        ) {
            Ok(env) => env,
            Err(e) => {
                shared.lock().unwrap().status.last_error = Some(e.0.into());
                return;
            }
        };
        let _ = active;
        let body = json!({"protocol": membership::SEED_MEMBER_HTTP_PROTOCOL, "envelope": env});
        let result = async {
            let resp = http.post(format!("{seed_url}/seed/message")).json(&body).send().await?;
            let ok = resp.status().is_success();
            Ok::<_, reqwest::Error>((ok, resp.json::<Value>().await.unwrap_or(Value::Null)))
        }
        .await;
        {
            let mut s = shared.lock().unwrap();
            match result {
                Ok((true, reply)) => match membership::verify_seed_envelope(&reply, &pinned, membership::LEASE_RENEWAL_PROTOCOL) {
                    Ok(renewal) => {
                        s.status.heartbeats_sent += 1;
                        s.status.last_heartbeat_at = Some(now_s());
                        s.status.lease_expires_at = renewal["lease_expires_at"].as_f64();
                        s.status.last_error = None;
                    }
                    Err(e) => s.status.last_error = Some(format!("lease_renewal_invalid:{}", e.0)),
                },
                Ok((false, reply)) => {
                    s.status.last_error =
                        Some(format!("heartbeat_refused:{}", reply["error"]["code"].as_str().unwrap_or("unknown")))
                }
                Err(e) => s.status.last_error = Some(format!("seed_unreachable:{}", e.without_url())),
            }
        }
        tokio::time::sleep(HEARTBEAT_INTERVAL).await;
    }
}

/// Execute admitted HopHeader frames carrying MYAC float32 activations on the
/// loaded stage and forward the result to the configured next peer.
async fn serve_inbox(mut rx: mpsc::Receiver<Inbound>, shared: Arc<Mutex<Shared>>, transport: NodeTransport) {
    while let Some(inbound) = rx.recv().await {
        let started = std::time::Instant::now();
        let outcome = {
            let mut s = shared.lock().unwrap();
            s.status.frames_received += 1;
            if inbound.frame.message_type != "HopHeader" {
                None
            } else {
                let request_id = inbound.frame.body["request_id"].as_str().unwrap_or("").to_owned();
                let result = wire::Activation::decode(&inbound.frame.payload)
                    .map_err(|e| e.0.to_string())
                    .and_then(|a| a.to_rows_f64().map_err(|e| e.0.to_string()))
                    .and_then(|rows| match s.stage.as_mut() {
                        Some(stage) => stage.execute(&request_id, &rows).map_err(|e| e.0),
                        None => Err("stage_not_loaded".into()),
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
            let payload = wire::Activation::from_rows_f32(&out).encode().unwrap_or_default();
            if let Ok(frame) = wire::encode_frame("HopHeader", &body, &payload)
                && let Err(e) = transport.send(addr, generation, &frame).await
            {
                shared.lock().unwrap().status.last_error = Some(format!("forward_failed:{}", e.0));
            }
        }
    }
}
