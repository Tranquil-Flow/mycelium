// SPDX-License-Identifier: AGPL-3.0-only
//! The owner-authority seams of a desktop `persistent::Host` (S1 create): one local identity, the
//! owner's swarm record and seed runtime, the owner's own membership (`/api/create`'s
//! `create_swarm` -> `pair_request` -> `approve_pairing` -> `accept_pairing`, collapsed onto this
//! Mac's own seed), authority start/stop, the pairing request/approve/accept seams and the owner's
//! pairing-window store.
//!
//! Moved unchanged from the headless L3 launcher (`examples/headless.rs`), which now calls this
//! module, so the launcher's journeys and the packaged executable run one copy of the code.
//! v0.1.4 @ f4e366e9: app.py:1589-1622 (`/api/create`), app.py:1687-1723 (`_owner_window_action`),
//! authority.py:383-451 (`start`, `create_swarm`), authority.py:491-545 (`_start_runtime`, seed).
//!
//! A host keeps the [`Authority`] in a [`Cell`]; every seam takes the cell's lock for one step at a
//! time, sees in that one acquisition what the host keeps beside the authority ([`HostView`]), and
//! never holds it across a seed round trip (the headless core is shared with its status, renewal
//! loop and relay routes). [`renew_owner`] is one pass of the owner's renewal loop; each host runs
//! that loop for its own lifetime.
use crate::{
    actions::Action,
    authority_view::{Member, PersistedAuthority},
    handoff::{self, Coordinator, Entropy, crypto},
    jobj,
    pairing_windows::PairingWindows,
    preferences::private_state,
    run_exchange::RunExchange,
    schema::{PublicError, Raised},
    seed::SeedOptions,
    wire::Json,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use budgets::{BudgetId, Entries};
use protocol::identity::Identity;
use serde_json::{Map, Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    net::{Ipv4Addr, SocketAddr, TcpListener},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

pub const SWARM_FILE: &str = "owner-swarm.json";
/// `authority._network_configuration()`: the public origin the owner chose, kept across restarts.
pub const NETWORK_FILE: &str = "owner-network.json";
pub const IDENTITY_FILE: &str = "target-identity.json";
pub const SEED_IDENTITY_FILE: &str = "seed-identity.json";
pub const PENDING_FILE: &str = "pairing.pending.json";
pub const CONSENT_FILE: &str = "consent.json";
pub const CONSENT_PROTOCOL: &str = "mycelium.remote_mac_consent.v1";
pub const HANDOFF_FILE: &str = "pairing.handoff.json";
pub const PENDING_PROTOCOL: &str = "mycelium.remote_mac_pairing_pending.v1";
/// `target_app.CONSENT`: the target's fixed peer consent.
const PEER_CONSENT: [(&str, bool); 3] = [
    ("cooperative_peer_visibility_acknowledged", true),
    ("resource_use_acknowledged", true),
    ("contribution_enabled", false),
];
const REQUEST_FIELDS: [&str; 12] = [
    "protocol",
    "node_id",
    "identity_endpoint_id",
    "identity_key_digest",
    "identity_verification_key",
    "peer_class",
    "invite_protocol",
    "recipient_encryption_key",
    "pairing_nonce",
    "issued_at",
    "expires_at",
    "signature",
];

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}
pub fn random(n: usize) -> Result<Vec<u8>, PublicError> {
    let mut bytes = vec![0; n];
    fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| PublicError::new(503, "native_control_unavailable"))?;
    Ok(bytes)
}
pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
/// `authority._MAX_PRIVATE_JSON_BYTES` from the budget registry; an unreadable registry yields 0,
/// which `private_state` refuses as `authority_state_size_limit_invalid`.
pub fn cap() -> u64 {
    private_state::private_json_limit().unwrap_or(0)
}
fn failure() -> PublicError {
    PublicError::new(409, "action_not_completed")
}

pub fn identity(root: &Path) -> Result<(Identity, String, String), PublicError> {
    identity_in(root, IDENTITY_FILE)
}
pub fn identity_in(root: &Path, file: &str) -> Result<(Identity, String, String), PublicError> {
    let path = root.join(file);
    let stored = private_state::load(&path, 4096).map_err(|e| PublicError::new(409, e.0))?;
    let record = match stored {
        Some(map) => Value::Object(map),
        None => {
            let secret = hex(&random(32)?);
            let endpoint = format!("endpoint-{}", hex(&random(8)?));
            let record = json!({"secret": secret, "endpoint_id": endpoint});
            private_state::store(&path, &record, 4096).map_err(|e| PublicError::new(409, e.0))?;
            record
        }
    };
    let text = record["secret"].as_str().ok_or_else(failure)?;
    let mut secret = [0u8; 32];
    for (i, byte) in secret.iter_mut().enumerate() {
        *byte = u8::from_str_radix(text.get(2 * i..2 * i + 2).ok_or_else(failure)?, 16)
            .map_err(|_| failure())?;
    }
    let endpoint = record["endpoint_id"]
        .as_str()
        .ok_or_else(failure)?
        .to_owned();
    let id = Identity::from_secret(&endpoint, &secret).map_err(|_| failure())?;
    // `RemoteMacMember`: the node id IS the key (`remote-mac-<24 hex of the key digest>`); a resume
    // refuses any other (`peer_identity_mismatch`). Older state files carry an unused random `node_id`.
    let digest = id.verification_key_digest();
    let node = format!(
        "remote-mac-{}",
        &digest.strip_prefix("sha256:").unwrap_or(&digest)[..24]
    );
    Ok((id, endpoint, node))
}
/// The request's keys in `new_pairing_request`'s statement order.
fn ordered_request(request: &Value) -> Json {
    Json::Obj(
        REQUEST_FIELDS
            .iter()
            .filter_map(|k| request.get(*k).map(|v| ((*k).to_owned(), Json::from(v))))
            .collect(),
    )
}
fn peer_consent() -> Value {
    Value::Object(
        PEER_CONSENT
            .iter()
            .map(|(k, v)| ((*k).to_owned(), Value::Bool(*v)))
            .collect(),
    )
}
/// The owner's seed address and signer (the coordinator `mint_invite` signs with): a loopback
/// HTTPS address nothing listens on - this host has no seed server (`peer_transport_unreachable`
/// is then the honest answer of every target that tries to join it).
pub fn coordinator(root: &Path, origin: Option<&str>) -> Result<Coordinator, PublicError> {
    let record = private_state::load(&root.join(SWARM_FILE), cap())
        .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))?
        .ok_or_else(|| {
            PublicError::new(409, "authority_swarm_missing").raised_by(Raised::Authority)
        })?;
    let (signer, _, _) = identity_in(root, SEED_IDENTITY_FILE)?;
    let port = match record.get("seed_port").and_then(Value::as_u64) {
        Some(port) => port,
        None => {
            let free = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
                .and_then(|l| l.local_addr())
                .map_err(|_| failure())?
                .port();
            let mut updated = record.clone();
            updated.insert("seed_port".into(), json!(free));
            private_state::store(&root.join(SWARM_FILE), &Value::Object(updated), cap())
                .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))?;
            u64::from(free)
        }
    };
    Ok(Coordinator {
        signer,
        swarm_id: record["swarm_id"].as_str().unwrap_or_default().to_owned(),
        seed_url: origin.map_or_else(|| format!("https://127.0.0.1:{port}"), str::to_owned),
    })
}

/// `authority.pair_request()` over `dir` (the pending request and consent files), signed by the
/// identity under `identity_root`. The recipient key is a fresh random 32-byte value.
pub fn build_pair_request(dir: &Path, identity_root: &Path) -> Result<Json, PublicError> {
    let root = dir;
    let clock = now();
    let pending = root.join(PENDING_FILE);
    if let Some(saved) = private_state::load(&pending, cap())
        .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))?
    {
        let request = Value::Object(saved)["request"].clone();
        if request["expires_at"].as_f64().is_some_and(|t| t >= clock) {
            return Ok(ordered_request(&request));
        }
    }
    let (id, endpoint, node) = identity(identity_root)?;
    let recipient_secret: [u8; 32] = random(32)?.try_into().map_err(|_| failure())?;
    let statement = json!({
        "protocol": "mycelium.pairing_request.v1", "node_id": node,
        "identity_endpoint_id": endpoint, "identity_key_digest": id.verification_key_digest(),
        "identity_verification_key": id.public_key_record(), "peer_class": "mac_mlx_iroh",
        "invite_protocol": "mycelium.invite_bundle.v1",
        "recipient_encryption_key": URL_SAFE_NO_PAD.encode(crypto::x25519_public(&recipient_secret)),
        "pairing_nonce": format!("pairing-{}", hex(&random(24)?)),
        "issued_at": clock, "expires_at": clock + 300.0,
    });
    let mut request = statement.clone();
    request["signature"] = id.sign(&statement).map_err(|_| failure())?;
    let consent_digest = protocol::canonical::digest(&peer_consent()).map_err(|_| failure())?;
    // `_save_consent`: the approved consent is retained beside the pending request.
    private_state::store(
        &root.join(CONSENT_FILE),
        &json!({"protocol": CONSENT_PROTOCOL, "consent": peer_consent()}),
        cap(),
    )
    .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))?;
    let record = json!({
        "protocol": PENDING_PROTOCOL, "request": request,
        "recipient_private_key": URL_SAFE_NO_PAD.encode(recipient_secret),
        "consent_digest": consent_digest,
    });
    private_state::store(&pending, &record, cap())
        .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))?;
    Ok(ordered_request(&request))
}

/// The managed control owner a host may bind to one seed runtime (the headless launcher's
/// observation mount). Closing the seed closes it first.
pub trait ManagedSlot: Send + Sync {
    /// Release the bound owner; the code says why its cleanup is unproven.
    fn close(&self) -> Result<(), &'static str>;
}
/// A seed runtime that never carries a managed owner.
pub enum Unmanaged {}
impl ManagedSlot for Unmanaged {
    fn close(&self) -> Result<(), &'static str> {
        match *self {}
    }
}

/// The owner's seed coordinator with its local TLS server (`DemoAuthority.seed_server`): the
/// RU-025 crate `seed`, bound to a free loopback port, answering for the swarm's `seed_url`.
pub struct SeedRuntime<M: ManagedSlot = Unmanaged> {
    pub coordinator: Arc<::seed::Coordinator>,
    pub exchange: Arc<RunExchange>,
    pub clock_offsets: Arc<crate::clock_offset::PeerClockOffsets>,
    pub capability_reports: Mutex<BTreeMap<String, Value>>,
    server: Mutex<Option<::seed::server::Server>>,
    pub ca_pem: Vec<u8>,
    pub address: SocketAddr,
    pub seed_url: String,
    pub clock: crate::managed_carrier::HostClock,
    pub managed_path: PathBuf,
    pub managed_init: Mutex<()>,
    pub managed_closed: Mutex<bool>,
    pub managed: Mutex<Option<Arc<M>>>,
    /// The target-side managed-control routes this seed serves to joined members; the owner's
    /// managed owner binds its mailbox here (`crate::managed_routes`).
    pub mailbox_routes: Arc<crate::managed_routes::DispatchSlot>,
}
impl<M: ManagedSlot> SeedRuntime<M> {
    /// How this process reaches its own seed: the local CA, the loopback port, the loopback name.
    pub fn options(&self) -> SeedOptions {
        SeedOptions {
            ca_pem: Some(self.ca_pem.clone()),
            connect_to: Some(self.address),
            server_name: Some("127.0.0.1".into()),
        }
    }
    pub fn close(&self) {
        if let Ok(mut closed) = self.managed_closed.lock() {
            *closed = true;
        }
        self.mailbox_routes.unbind();
        let managed = self.managed.lock().ok().and_then(|m| m.clone());
        if let Some(managed) = managed
            && let Err(error) = managed.close()
        {
            eprintln!("managed owner cleanup unproven: {error}");
        }
        let taken = self.server.lock().ok().and_then(|mut s| s.take());
        if let Some(server) = taken {
            server.close();
        }
    }
}
fn seed_failure(error: &::seed::SeedError) -> PublicError {
    eprintln!("seed: {}", error.code);
    PublicError::new(409, "action_not_completed").raised_by(Raised::Authority)
}
fn uuid4() -> String {
    let mut raw = random(16).unwrap_or_else(|_| vec![0; 16]);
    raw[6] = (raw[6] & 0x0f) | 0x40;
    raw[8] = (raw[8] & 0x3f) | 0x80;
    let h = hex(&raw);
    format!(
        "{}-{}-{}-{}-{}",
        &h[..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..]
    )
}
/// `DemoAuthority.create_swarm`'s seed half: TLS material, the coordinator over its database, the
/// server, and the seed url (the relay origin when one is configured, else the server's own).
pub fn start_seed<M: ManagedSlot>(
    root: &Path,
    swarm_id: &str,
    origin: Option<&str>,
) -> Result<SeedRuntime<M>, PublicError> {
    let dir = root.join("seed-rs");
    fs::create_dir_all(&dir).map_err(|_| failure())?;
    fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .map_err(|_| failure())?;
    let material = ::seed::tls::ensure_local_tls_material(&dir.join("tls")).map_err(|e| {
        eprintln!("seed tls: {}", e.0);
        failure()
    })?;
    let (signer, _, _) = identity_in(root, SEED_IDENTITY_FILE)?;
    let options = ::seed::Options::new(
        swarm_id,
        &format!("seed-{swarm_id}"),
        &format!("boot-{}", hex(&random(8)?)),
    )
    .map_err(|e| seed_failure(&e))?;
    let coordinator = Arc::new(
        ::seed::Coordinator::open(
            &dir.join("seed.sqlite"),
            signer,
            options,
            Box::new(now),
            Box::new(uuid4),
        )
        .map_err(|e| seed_failure(&e))?,
    );
    // The oracle's loopback seed keeps its port across restarts (`_network_configuration`); the seed
    // crate binds a fresh one (seam SE-L10-SEED-PORT), so a swarm that has no public origin keeps its
    // FIRST url as the url it answers for. This process still reaches it through `SeedOptions`.
    let url_file = dir.join("seed-url.json");
    let kept = private_state::load(&url_file, cap())
        .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))?
        .and_then(|m| m.get("seed_url").and_then(Value::as_str).map(str::to_owned));
    let answers_for = origin.map(str::to_owned).or(kept);
    let exchange = Arc::new(RunExchange::new(Box::new(now)));
    let clock_offsets =
        Arc::new(crate::clock_offset::PeerClockOffsets::new(Box::new(now)).map_err(|_| failure())?);
    let observer = clock_offsets.clone();
    let routes = Arc::new(crate::run_exchange_routes::SeedRoutes::new(
        exchange.clone(),
        Arc::new(move |node, signed, rtt| {
            let rtt = rtt.map(Value::from);
            observer.observe(node, &Value::from(signed), None, rtt.as_ref());
        }),
        BudgetId::ManagedControlExchangeResponseTtl
            .span(Entries::ZERO)
            .map_err(|_| failure())?,
    ));
    let mailbox_routes = Arc::new(crate::managed_routes::DispatchSlot::default());
    let routes = Arc::new(crate::managed_routes::Routes(vec![
        routes,
        Arc::new(crate::managed_routes::MailboxRoutes(mailbox_routes.clone())),
    ]));
    let server = ::seed::server::Server::bind_tls_on_with_routes(
        coordinator.clone(),
        &material,
        answers_for.as_deref(),
        None,
        routes,
    )
    .map_err(|e| seed_failure(&e))?;
    if answers_for.is_none() {
        private_state::store(&url_file, &json!({"seed_url": server.base_url()}), cap())
            .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))?;
    }
    let ca_pem = fs::read(&material.ca_certificate).map_err(|_| failure())?;
    Ok(SeedRuntime {
        seed_url: coordinator
            .seed_url()
            .unwrap_or_else(|| server.base_url().to_owned()),
        address: server.address(),
        coordinator,
        exchange,
        clock_offsets,
        capability_reports: Mutex::new(BTreeMap::new()),
        server: Mutex::new(Some(server)),
        ca_pem,
        clock: crate::managed_carrier::HostClock::default(),
        managed_path: dir.join("managed"),
        managed_init: Mutex::new(()),
        managed_closed: Mutex::new(false),
        managed: Mutex::new(None),
        mailbox_routes,
    })
}
/// `/api/create` on a fresh swarm: `pair_request` -> `approve_pairing(ttl_seconds=120)` ->
/// `accept_pairing`, all against this Mac's own seed over its pinned local TLS (owner collapse).
fn owner_collapse<M: ManagedSlot>(
    root: &Path,
    runtime: &SeedRuntime<M>,
    swarm_id: &str,
    storage: &Value,
) -> Result<Option<crate::member::Session>, PublicError> {
    let dir = root.join("authority").join("member");
    fs::create_dir_all(&dir).map_err(|_| failure())?;
    fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .map_err(|_| failure())?;
    let request = build_pair_request(&dir, root)?;
    let (seed_signer, _, _) = identity_in(root, SEED_IDENTITY_FILE)?;
    let coordinator = Coordinator {
        signer: seed_signer,
        swarm_id: swarm_id.to_owned(),
        seed_url: runtime.seed_url.clone(),
    };
    let quota = json!({"storage_bytes": storage, "network_bytes_per_day": storage, "background_allowed": false});
    let handoff = handoff::approve_pairing(
        &dir,
        true,
        Some(&coordinator),
        &request.to_value(),
        &json!(120),
        &quota,
        now(),
        &Entropy::os,
    )?;
    let (signer, _, _) = identity(root)?;
    let options = runtime.options();
    let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
    let env = crate::peer::JoinEnv {
        signer: &signer,
        clock: &now,
        random_hex: &random_hex,
        seed: &options,
    };
    let mut accepted = crate::peer::accept_handoff(&dir, true, &handoff.to_value(), &env)?;
    // `return self._renew_locked()`: the first forced RUNNING heartbeat; "enrollment is already durably
    // accepted; callers can renew later" - a failure here does not undo the join.
    if let Some(session) = accepted.session.as_mut()
        && let Err(error) = crate::peer::renew(&dir, true, Some(session), &env)
    {
        eprintln!("owner renew: {}", error.code);
    }
    Ok(accepted.session)
}

/// The authority state a host keeps: `DemoAuthority` and the `DemoApp` fields its seams read.
pub struct Authority<M: ManagedSlot = Unmanaged> {
    pub root: PathBuf,
    /// `installation_role == 'target'`.
    pub target: bool,
    /// `DemoApp.running`.
    pub running: bool,
    /// The owner's one-member swarm record (`owner-swarm.json`).
    pub swarm: Option<Value>,
    /// An owner authority persisted in the v0.1.3/v0.1.4 layout (`authority/authority.json` +
    /// `authority/seed/state.sqlite3`), read-only: nothing here resumes it (no seed coordinator).
    pub legacy: Option<PersistedAuthority>,
    /// `authority._network_configuration()['public_origin']`.
    pub configured_origin: Option<String>,
    /// `DemoApp.pairing_windows`: the owner's in-memory window store (shared with the relay's routes).
    pub windows: Arc<PairingWindows>,
    /// The owner's seed coordinator and its local TLS server (`DemoAuthority.seed_server`).
    pub seed: Option<Arc<SeedRuntime<M>>>,
    /// The owner's own live membership (`DemoAuthority.member`), renewed by `DemoApp._renew_loop`.
    pub owner_session: Option<crate::member::Session>,
    /// `RemoteMacMember._member`: the live membership session a first acceptance leaves.
    pub session: Option<crate::member::Session>,
    /// `(generation, lease_expires_at)` of `session` while the renewal loop has it out on the wire, so the
    /// status never flickers to "absent" during a heartbeat.
    pub session_view: Option<(Option<u64>, Option<f64>)>,
}
impl<M: ManagedSlot> Authority<M> {
    /// The authority part of `DemoApp.__init__` over `root`: the installation role, the owner's
    /// swarm record, a persisted v0.1.x authority and the configured public origin. Nothing runs
    /// yet (an owner starts `running`, a target does not).
    pub fn load(root: PathBuf) -> Result<Self, &'static str> {
        // Admit the trusted app root once, before handing paths to any subsystem.
        // Ancestor aliases (notably macOS /var) are valid; a root symlink itself
        // and links planted below the admitted physical root remain forbidden.
        fs::create_dir_all(&root).map_err(|_| "state_root_invalid")?;
        if fs::symlink_metadata(&root)
            .map_err(|_| "state_root_invalid")?
            .file_type()
            .is_symlink()
        {
            return Err("state_root_invalid");
        }
        let root = root.canonicalize().map_err(|_| "state_root_invalid")?;
        let role =
            private_state::load(&root.join("installation-role.json"), 4096).map_err(|e| e.0)?;
        let target = role.is_some_and(|m| m.get("role") == Some(&json!("target")));
        let swarm = private_state::load(&root.join(SWARM_FILE), cap())
            .map_err(|e| e.0)?
            .map(Value::Object);
        let legacy = if target {
            None
        } else {
            PersistedAuthority::load(&root).map_err(|e| e.code)?
        };
        let configured_origin = private_state::load(&root.join(NETWORK_FILE), cap())
            .map_err(|e| e.0)?
            .and_then(|m| {
                m.get("public_origin")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            });
        Ok(Self {
            running: !target,
            configured_origin,
            legacy,
            root,
            target,
            swarm,
            session: None,
            session_view: None,
            windows: Arc::new(PairingWindows::system()),
            seed: None,
            owner_session: None,
        })
    }
    /// The owner authority this process answers for: a persisted v0.1.x one, else the one-member swarm
    /// record this host creates (a RUNNING member whose lease nothing can lapse: it is this Mac).
    pub fn owner_authority(&self) -> Option<PersistedAuthority> {
        if self.target {
            return None;
        }
        if let Some(legacy) = &self.legacy {
            return Some(legacy.clone());
        }
        let record = self.swarm.as_ref()?;
        let node = record["node_id"].as_str()?.to_owned();
        if let Some(seed) = &self.seed {
            let members = seed
                .coordinator
                .members(None)
                .unwrap_or_default()
                .iter()
                .filter_map(|m| {
                    Some(Member {
                        node_id: m["node_id"].as_str()?.to_owned(),
                        generation: m["generation"].as_u64()?,
                        lifecycle_state: m["lifecycle_state"].as_str()?.to_owned(),
                        lease_expires_at: m["lease_expires_at"].as_f64()?,
                    })
                })
                .collect::<Vec<_>>();
            if !members.is_empty() {
                return Some(PersistedAuthority {
                    swarm_id: record["swarm_id"].as_str()?.to_owned(),
                    target_node_id: node,
                    contribution_enabled: false,
                    members,
                });
            }
        }
        Some(PersistedAuthority {
            swarm_id: record["swarm_id"].as_str()?.to_owned(),
            target_node_id: node.clone(),
            contribution_enabled: false,
            members: vec![Member {
                node_id: node,
                generation: 1,
                lifecycle_state: "RUNNING".into(),
                lease_expires_at: f64::MAX,
            }],
        })
    }
}

/// `public_relay.status().get('public_origin')`: the origin a host's public relay answers on.
pub trait RelayOrigin {
    fn origin(&self) -> Option<String>;
}
impl RelayOrigin for crate::relay::OwnerRelay {
    fn origin(&self) -> Option<String> {
        crate::relay::OwnerRelay::origin(self)
    }
}
/// A host without a public relay.
struct NoRelay;
impl RelayOrigin for NoRelay {
    fn origin(&self) -> Option<String> {
        None
    }
}
/// What a seam sees under ONE acquisition of its host's [`Cell`]: the [`Authority`] (its fields
/// read as the view's own, `Deref`) and what the host keeps beside it - `DemoApp.closed`, the
/// member's `_left` and `public_relay` - as the headless core's lock gave them to these seams.
pub struct HostView<'a, M: ManagedSlot = Unmanaged> {
    authority: &'a mut Authority<M>,
    relay: &'a dyn RelayOrigin,
    closed: bool,
    left: bool,
}
impl<'a, M: ManagedSlot> HostView<'a, M> {
    pub fn new(
        authority: &'a mut Authority<M>,
        relay: &'a dyn RelayOrigin,
        closed: bool,
        left: bool,
    ) -> Self {
        Self {
            authority,
            relay,
            closed,
            left,
        }
    }
    /// An authority with nothing beside it: no relay, and a lifetime its host ends itself.
    pub fn bare(authority: &'a mut Authority<M>) -> Self {
        Self::new(authority, &NoRelay, false, false)
    }
}
impl<M: ManagedSlot> std::ops::Deref for HostView<'_, M> {
    type Target = Authority<M>;
    fn deref(&self) -> &Authority<M> {
        self.authority
    }
}
impl<M: ManagedSlot> std::ops::DerefMut for HostView<'_, M> {
    fn deref_mut(&mut self) -> &mut Authority<M> {
        self.authority
    }
}

/// Where a host keeps its [`Authority`] and what else the seams ask it.
pub trait Cell {
    type Managed: ManagedSlot;
    /// Run `f` on the state as one snapshot; a poisoned lock still yields it.
    fn with<R>(&self, f: impl FnOnce(&mut HostView<'_, Self::Managed>) -> R) -> R;
    /// Run `f` on the state as one snapshot, `None` when the lock is poisoned.
    fn try_with<R>(&self, f: impl FnOnce(&mut HostView<'_, Self::Managed>) -> R) -> Option<R>;
    /// The seed options of a joining member: the public Web-PKI path.
    fn member_seed_options(&self) -> SeedOptions {
        SeedOptions::default()
    }
}
/// An authority behind its own lock: no relay, nothing closes or leaves beside it (its host's
/// own loop ends with the host), the public seed path for a joining member.
impl<M: ManagedSlot> Cell for Mutex<Authority<M>> {
    type Managed = M;
    fn with<R>(&self, f: impl FnOnce(&mut HostView<'_, M>) -> R) -> R {
        match self.lock() {
            Ok(mut authority) => f(&mut HostView::bare(&mut authority)),
            Err(poisoned) => f(&mut HostView::bare(&mut poisoned.into_inner())),
        }
    }
    fn try_with<R>(&self, f: impl FnOnce(&mut HostView<'_, M>) -> R) -> Option<R> {
        self.lock()
            .ok()
            .map(|mut authority| f(&mut HostView::bare(&mut authority)))
    }
}

/// A swarm record WITH the seed state this host wrote beside it (`seed-rs/seed.sqlite`) and no seed running
/// yet: the owner has something to resume. A bare record (state from before the seed existed) has nothing
/// to bring back, and generating the local TLS material at startup would delay the launcher by seconds.
pub fn resumable(cell: &impl Cell) -> bool {
    cell.try_with(|c| {
        !c.target
            && c.swarm.is_some()
            && c.seed.is_none()
            && c.root.join("seed-rs").join("seed.sqlite").is_file()
    })
    .unwrap_or(false)
}
/// `DemoAuthority._start_runtime` for a swarm that exists (a restart, `/api/start`, a new origin): the seed
/// over its own database and local TLS material, bound to the configured public origin, then the owner's
/// own membership resumes through it (`RemoteMacMember.start` -> `_resume`: generation + 1, renewed lease).
pub fn open_owner_seed(cell: &impl Cell) -> Result<(), PublicError> {
    let (root, record, origin) = cell
        .try_with(|c| (c.root.clone(), c.swarm.clone(), c.configured_origin.clone()))
        .ok_or_else(failure)?;
    let Some(record) = record else {
        return cell.try_with(|c| c.running = true).ok_or_else(failure);
    };
    let swarm_id = record["swarm_id"].as_str().ok_or_else(failure)?.to_owned();
    let runtime = Arc::new(start_seed(&root, &swarm_id, origin.as_deref())?);
    let dir = root.join("authority").join("member");
    let (signer, _, _) = identity(&root)?;
    let options = runtime.options();
    let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
    let env = crate::peer::JoinEnv {
        signer: &signer,
        clock: &now,
        random_hex: &random_hex,
        seed: &options,
    };
    let mut started = match crate::peer::start(&dir, &signer, true, &env) {
        Ok(started) => started,
        Err(failed) => {
            runtime.close();
            return Err(failed.error);
        }
    };
    // `_renew_locked` after the resume: the forced RUNNING heartbeat (a failure leaves the resume standing).
    if let Some(session) = started.session.as_mut()
        && let Err(error) = crate::peer::renew(&dir, true, Some(session), &env)
    {
        eprintln!("owner renew: {}", error.code);
    }
    cell.try_with(|c| {
        c.seed = Some(runtime);
        c.owner_session = started.session;
        c.running = true;
    })
    .ok_or_else(failure)
}

/// `authority.status().get('swarm_created')`.
pub fn swarm_created(cell: &impl Cell) -> bool {
    cell.with(|c| c.owner_authority().is_some())
}
/// `authority.status().get('membership_state')`.
pub fn membership_state(cell: &impl Cell) -> Option<String> {
    Some(cell.with(|c| {
        c.owner_authority()
            .map_or("absent", |view| {
                view.status(true, now())
                    .get("membership_state")
                    .and_then(|s| {
                        if let Json::Str(s) = s {
                            Some(s.as_str())
                        } else {
                            None
                        }
                    })
                    .map_or(
                        "absent",
                        |s| if s == "revoked" { "revoked" } else { "current" },
                    )
            })
            .into()
    }))
}
/// `app.running`.
pub fn running(cell: &impl Cell) -> bool {
    cell.with(|c| c.running)
}
/// `self.authority.stop(); self.running = False`: the seed runtime closes, the membership is retained.
pub fn stop_authority(cell: &impl Cell) {
    let seed = cell.with(|c| {
        c.running = false;
        c.owner_session = None;
        c.seed.take()
    });
    if let Some(seed) = seed {
        seed.close();
    }
}
/// `self.authority.start(); self.running = True`: an owner with a swarm brings its seed back.
pub fn start_authority(cell: &impl Cell) -> Result<(), PublicError> {
    if resumable(cell) {
        return open_owner_seed(cell);
    }
    cell.with(|c| c.running = true);
    Ok(())
}
/// One pass of `DemoApp._renew_loop`: `authority.renew()` for a running owner (the session is taken out of
/// the core while the heartbeat is on the wire, so no core lock is held across the seed round trip).
/// `renewal_unavailable` is only a status field in the oracle; here the next pass simply tries again.
pub fn renew_owner(cell: &impl Cell) {
    let Some(Some((root, runtime, session))) = cell.try_with(|c| {
        if c.closed || !c.running || c.left || c.target {
            return None;
        }
        let (Some(runtime), Some(session)) = (c.seed.clone(), c.owner_session.take()) else {
            return None;
        };
        Some((c.root.clone(), runtime, session))
    }) else {
        return;
    };
    let mut session = session;
    let dir = root.join("authority").join("member");
    let Ok((signer, _, _)) = identity(&root) else {
        return;
    };
    let options = runtime.options();
    let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
    let env = crate::peer::JoinEnv {
        signer: &signer,
        clock: &now,
        random_hex: &random_hex,
        seed: &options,
    };
    if let Err(error) = crate::peer::renew(&dir, true, Some(&mut session), &env) {
        eprintln!("owner renew: {}", error.code);
    }
    cell.try_with(|c| {
        if c.owner_session.is_none() && c.seed.is_some() {
            c.owner_session = Some(session);
        }
    });
}
/// Target side of `authority.pair_request()`: one signed, recipient-keyed request, reused
/// until it expires (authority.py:841-871).
pub fn pair_request(cell: &impl Cell) -> Result<Json, PublicError> {
    let (root, target) = cell.with(|c| (c.root.clone(), c.target));
    if !target {
        return Err(PublicError::new(409, "authority_swarm_missing").raised_by(Raised::Authority));
    }
    build_pair_request(&root, &root)
}
/// Owner side of `/api/pair/approve`: the oracle's approvals store and encrypted handoff.
pub fn pair_approve(
    cell: &impl Cell,
    request: &Value,
    ttl_seconds: &Value,
    quota: &Value,
) -> Result<Json, PublicError> {
    let (root, started, swarm, bound) = cell.with(|c| {
        (
            c.root.clone(),
            c.running,
            c.owner_authority().is_some(),
            c.seed.as_ref().map(|s| s.seed_url.clone()),
        )
    });
    let coordinator = if swarm {
        Some(coordinator(&root, bound.as_deref())?)
    } else {
        None
    };
    handoff::approve_pairing(
        &root,
        started,
        coordinator.as_ref(),
        request,
        ttl_seconds,
        quota,
        now(),
        &Entropy::os,
    )
}
/// Target side of `/api/pair/accept`: `RemoteMacMember.accept_handoff` (`peer`) over the
/// pinned seed HTTPS client; a first acceptance keeps its live membership session.
pub fn pair_accept(cell: &impl Cell, handoff_value: &Value) -> Result<Json, PublicError> {
    let (root, target, running) = cell.with(|c| (c.root.clone(), c.target, c.running));
    if !target {
        return Err(failure());
    }
    let (signer, _, _) = identity(&root)?;
    let options = cell.member_seed_options();
    let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
    let env = crate::peer::JoinEnv {
        signer: &signer,
        clock: &now,
        random_hex: &random_hex,
        seed: &options,
    };
    let accepted = crate::peer::accept_handoff(&root, running, handoff_value, &env)?;
    if let Some(session) = accepted.session {
        cell.with(|c| c.session = Some(session));
    }
    Ok(accepted.answer)
}
/// `RemoteMacMember.membership_binding()` of the live membership session.
pub fn membership_binding(cell: &impl Cell) -> Option<crate::member::Binding> {
    cell.with(|c| crate::peer::membership_binding(c.session.as_ref()))
}
/// The owner side of `/api/create`: the one-member swarm record (authority.create_swarm →
/// pair_request → approve_pairing → accept_pairing collapse to "this Mac is member 1").
pub fn create_owner_json(
    cell: &impl Cell,
    consent: &Map<String, Value>,
) -> Result<Json, PublicError> {
    let root = cell.with(|c| c.root.clone());
    // `current.get('swarm_created')`: a current member is answered with `authority.status()`
    // (no `members`); anything but `unenrolled` cannot re-enrol (app.py:1604-1608)
    if let Some(view) = cell.with(|c| c.owner_authority()) {
        let status = view.authority_status(true, now());
        let state = match status.get("membership_state") {
            Some(Json::Str(s)) => s.clone(),
            _ => String::new(),
        };
        return match state.as_str() {
            "current" => Ok(status),
            "unenrolled" => {
                cell.with(|c| c.running = true);
                Ok(view.membership_result())
            }
            _ => Err(PublicError::new(400, "membership_cannot_reenroll")),
        };
    }
    let id = hex(&random(10)?);
    let (_, _, owner_node) = identity(&root)?;
    let swarm_id = format!("sw-{}", &id[..20]);
    let record = json!({"swarm_id": swarm_id, "node_id": owner_node, "created_at": now()});
    private_state::store(&root.join(SWARM_FILE), &record, cap())
        .map_err(|e| PublicError::new(409, e.0))?;
    // `authority.create_swarm`: the seed (on the relay origin when one is configured), then the owner joins
    // its own swarm through it (`pair_request` -> `approve_pairing` -> `accept_pairing`).
    let origin = cell.with(|c| c.configured_origin.clone());
    let runtime = Arc::new(start_seed(&root, &swarm_id, origin.as_deref())?);
    let storage = consent.get("storage_bytes").cloned().unwrap_or(Value::Null);
    let session = match owner_collapse(&root, &runtime, &swarm_id, &storage) {
        Ok(session) => session,
        Err(error) => {
            runtime.close();
            fs::remove_file(root.join(SWARM_FILE)).unwrap_or_default();
            return Err(error);
        }
    };
    cell.with(|c| {
        c.swarm = Some(record);
        c.seed = Some(runtime);
        c.owner_session = session;
        c.running = true;
        Ok(c.owner_authority()
            .map_or(Json::Null, |view| view.membership_result()))
    })
}
/// `authority.coordinator is not None`.
pub fn coordinator_ready(cell: &impl Cell) -> bool {
    cell.with(|c| c.seed.is_some())
}
/// `len([w for w in pairing_windows.list() if w['state'] in {'open','claimed','approved'}])`.
pub fn live_pairing_windows(cell: &impl Cell) -> usize {
    cell.with(|c| c.windows.live_count())
}
/// `authority._network_configuration()['public_origin']`, when configured.
pub fn configured_public_origin(cell: &impl Cell) -> Option<String> {
    cell.with(|c| c.configured_origin.clone())
}
/// `_owner_window_action` for `/api/pair/window/{open,list,approve,revoke}` once the persistent
/// backend has answered its own part (a code asked for without a relay is its pending window).
pub fn owner_window(
    cell: &impl Cell,
    action: Action,
    body: &Map<String, Value>,
) -> Result<Json, PublicError> {
    let (windows, seed, origin) =
        cell.with(|c| (c.windows.clone(), c.seed.clone(), c.relay.origin()));
    match action {
        Action::PairWindowList => Ok(jobj! {"windows" => windows.list()?}),
        Action::PairWindowRevoke => windows.revoke(&body["window_id"]),
        Action::PairWindowOpen => {
            let (Some(seed), Some(origin)) = (seed, origin) else {
                return Err(PublicError::new(400, "pairing_window_swarm_required"));
            };
            // `_bind_public_routes` ran when the relay started; a seed bound to another address is stale.
            if seed.seed_url != origin {
                return Err(PublicError::new(400, "pairing_window_origin_stale"));
            }
            windows.open(
                &Value::String(origin),
                &Value::String(seed.coordinator.signer().verification_key_digest()),
                &body["ttl_seconds"],
            )
        }
        Action::PairWindowApprove => {
            let mut issue = |request: &Value, quota: &Value| -> Result<Json, PublicError> {
                handoff::validate_quota(quota)?;
                let expires = request["expires_at"].as_f64().unwrap_or(f64::NAN);
                let remaining = (expires - now()).trunc() as i64 - 1;
                if remaining < 30 {
                    return Err(PublicError::new(409, "pairing_request_expiring")
                        .raised_by(Raised::Pairing));
                }
                pair_approve(cell, request, &json!(remaining.min(120)), quota)
            };
            windows.approve(&body["window_id"], &body["request_digest"], &mut issue)
        }
        _ => Err(failure()),
    }
}
