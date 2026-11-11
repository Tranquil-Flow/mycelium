// SPDX-License-Identifier: AGPL-3.0-only
//! The join flow and the joined member's own pieces that BOTH desktop hosts compose: the headless
//! L3 launcher (`examples/headless.rs`) and the packaged executable (`node-service`'s packaged
//! host). Moved unchanged from the launcher, which now calls this module, so the two hosts run one
//! copy. v0.1.4 @ f4e366e9:
//!
//! * the owner side behind the relay: `DemoApp._public_route` (app.py:1045-1068: pairing windows,
//!   member-report inbox `MemberReports.submit`, link probe) and the relay's verified-TLS hop into
//!   the owner's seed (`proxy.bind_seed(authority.seed_server)`);
//! * the member side of `/api/join/window[/poll]` (app.py:1742-1813): `_join_client`,
//!   `_measure_and_sign` (capability.py `observe_device`, `measure_link`, standing_consent
//!   `planning_caps`), `peer.handoff_seed_key_digest`, `peer.owner_origin`, the automatic join
//!   report (`_auto_report`, app.py:1138-1163);
//! * the joined Mac's status pieces: `TargetApplication.status()` (peer.py:395-455,
//!   target_app.py:330-343) and `_join_flow_status` (app.py:1214-1237);
//! * `authority.start()` of a joined target (`RemoteMacMember.start`, app.py:613-626) and the
//!   durable half of a member leave's "no owned compute" proof.
use crate::{
    app_host::{
        self, Authority, CONSENT_FILE, Cell, HANDOFF_FILE, IDENTITY_FILE, ManagedSlot, PENDING_FILE,
    },
    app_host::{SeedRuntime, cap, hex, identity, now, random},
    capability, handoff, jobj,
    join_client::JoinWindowClient,
    pairing_windows::{PAIRING_WINDOW_PREFIX, PairingWindows},
    persistent::JoinAccepted,
    preferences::{private_state, resource_policy, standing_consent},
    relay::{BackendError, BackendReply, LocalAnswer, LocalHandler, SeedBackend},
    schema::PublicError,
    seed::SeedOptions,
    windows::PendingWindows,
    wire::Json,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::TcpStream,
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

/// `_public_route`'s link probe bound: at most this many probes per window (app.py:1063-1064).
/// SE-L10-RELAY-BOUNDS (L1b): not in the budget registry yet.
pub const PROBE_LIMIT: usize = 64;
/// The probe window in seconds (`now - t < 10`, app.py:1063). SE-L10-RELAY-BOUNDS.
pub const PROBE_WINDOW_SECONDS: u64 = 10;

fn failure() -> PublicError {
    PublicError::new(409, "action_not_completed")
}
fn span(id: BudgetId) -> Duration {
    id.span(Entries::new(0))
        .map_or(Duration::ZERO, |v| Duration::from_millis(v.millis()))
}

/// The relay's verified-TLS hop into the owner's seed: HTTPS to the local server, pinned to the local CA.
pub struct SeedHop<M: ManagedSlot>(pub Arc<SeedRuntime<M>>);
impl<M: ManagedSlot> SeedBackend for SeedHop<M> {
    fn request(
        &self,
        method: &str,
        path: &str,
        content_type: &str,
        body: &[u8],
    ) -> Result<BackendReply, BackendError> {
        use rustls::{ClientConfig, ClientConnection, RootCertStore, StreamOwned};
        use rustls_pki_types::{CertificateDer, ServerName, pem::PemObject};
        let mut roots = RootCertStore::empty();
        for cert in CertificateDer::pem_slice_iter(&self.0.ca_pem) {
            roots
                .add(cert.map_err(|_| BackendError::Unavailable)?)
                .map_err(|_| BackendError::Unavailable)?;
        }
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let config = ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .map_err(|_| BackendError::Unavailable)?
            .with_root_certificates(roots)
            .with_no_client_auth();
        let timeout = span(BudgetId::PyMyceliumA13MacosPublicRelayRelayhandlerForwardTimeoutL115);
        let tcp = TcpStream::connect_timeout(&self.0.address, timeout)
            .map_err(|_| BackendError::Unavailable)?;
        tcp.set_read_timeout(Some(timeout))
            .and_then(|()| tcp.set_write_timeout(Some(timeout)))
            .map_err(|_| BackendError::Unavailable)?;
        let name =
            ServerName::try_from("127.0.0.1".to_owned()).map_err(|_| BackendError::Unavailable)?;
        let connection =
            ClientConnection::new(Arc::new(config), name).map_err(|_| BackendError::Unavailable)?;
        let mut stream = StreamOwned::new(connection, tcp);
        let head = format!(
            "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            self.0.address.port(),
            body.len()
        );
        let mut wire = head.into_bytes();
        wire.extend_from_slice(body);
        stream
            .write_all(&wire)
            .and_then(|()| stream.flush())
            .map_err(|_| BackendError::CertificateRejected)?;
        let mut reply = Vec::new();
        let mut chunk = [0u8; 16 * 1024];
        loop {
            match stream.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => reply.extend_from_slice(&chunk[..n]),
                Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => break,
                Err(_) => return Err(BackendError::Unavailable),
            }
        }
        let split = reply
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .ok_or(BackendError::Unavailable)?;
        let head = String::from_utf8_lossy(&reply[..split]).into_owned();
        let status = head
            .lines()
            .next()
            .and_then(|l| l.split(' ').nth(1))
            .and_then(|c| c.parse::<u16>().ok())
            .ok_or(BackendError::Unavailable)?;
        let content_type = head
            .lines()
            .filter_map(|l| l.split_once(':'))
            .find(|(k, _)| k.eq_ignore_ascii_case("content-type"))
            .map(|(_, v)| v.trim().to_owned())
            .unwrap_or_default();
        Ok(BackendReply {
            status,
            content_type,
            body: reply[split + 4..].to_vec(),
        })
    }
}
/// The relay's seed hop while the swarm has no seed server: the address `coordinator()` hands out
/// is a loopback port nothing listens on, so a request through the relay is answered `502
/// relay_backend_unavailable`, exactly what the relay does when the seed is gone.
pub struct NoSeedServer;
impl SeedBackend for NoSeedServer {
    fn request(&self, _: &str, _: &str, _: &str, _: &[u8]) -> Result<BackendReply, BackendError> {
        Err(BackendError::Unavailable)
    }
}
/// The relay's hop into `seed` when the swarm has one, else [`NoSeedServer`] for an owner whose
/// swarm exists, else none (`proxy.bind_seed(self.authority.seed_server)`).
pub fn seed_backend<M: ManagedSlot + 'static>(
    seed: Option<Arc<SeedRuntime<M>>>,
    swarm: bool,
) -> Option<Arc<dyn SeedBackend>> {
    seed.map(|s| Arc::new(SeedHop(s)) as Arc<dyn SeedBackend>)
        .or_else(|| swarm.then(|| Arc::new(NoSeedServer) as Arc<dyn SeedBackend>))
}

/// What `_public_route` reads of its host at each request: the window store and the seed.
pub type RouteState<M> =
    Arc<dyn Fn() -> (Arc<PairingWindows>, Option<Arc<SeedRuntime<M>>>) + Send + Sync>;
/// `DemoApp._public_route` for the pairing window, the link probe and the member-report inbox.
pub fn public_route<M: ManagedSlot + 'static>(state: RouteState<M>) -> LocalHandler {
    let probes: Arc<Mutex<Vec<Instant>>> = Arc::new(Mutex::new(Vec::new()));
    let reply = |status: i64, value: Json| {
        Ok(LocalAnswer {
            status,
            raw: value.dumps(crate::wire::Text::Ascii),
            kind: None,
        })
    };
    Arc::new(move |method: &str, path: &str, body: &[u8]| {
        let (windows, seed) = state();
        if path.starts_with(PAIRING_WINDOW_PREFIX) {
            let (status, raw) = windows.handle(method, path, body);
            return Ok(LocalAnswer {
                status: i64::from(status),
                raw,
                kind: None,
            });
        }
        if path == capability::MEMBER_REPORT_PATH {
            if method != "POST" {
                return reply(405, jobj! {"error" => "method_not_allowed"});
            }
            let report = serde_json::from_slice::<Value>(body)
                .ok()
                .filter(Value::is_object);
            let Some(report) = report else {
                return reply(400, jobj! {"error" => "capability_report_invalid"});
            };
            let outcome = member_report(seed.as_deref(), &report);
            return match outcome {
                Ok(node) => reply(200, jobj! {"state" => "accepted", "node_id" => node}),
                Err(code) => {
                    let status = if matches!(
                        code,
                        "capability_report_member_not_current"
                            | "capability_report_expired"
                            | "capability_report_capacity"
                    ) {
                        409
                    } else {
                        400
                    };
                    reply(status, jobj! {"error" => code})
                }
            };
        }
        if let Some(kib) = path.strip_prefix(&format!("{}/", capability::LINK_PROBE_PATH)) {
            if method != "POST"
                || kib.is_empty()
                || kib.len() > 3
                || !kib.bytes().all(|b| b.is_ascii_digit())
            {
                return reply(404, jobj! {"error" => "route_unknown"});
            }
            let now = Instant::now();
            let mut times = probes.lock().map_err(|_| ())?;
            let window = Duration::from_secs(PROBE_WINDOW_SECONDS);
            times.retain(|t| now.duration_since(*t) < window);
            if times.len() >= PROBE_LIMIT {
                return reply(429, jobj! {"error" => "link_probe_rate_limited"});
            }
            times.push(now);
            return match capability::link_probe_response(
                &json!(kib.parse::<u64>().unwrap_or(u64::MAX)),
                body.len(),
            ) {
                Ok(raw) => Ok(LocalAnswer {
                    status: 200,
                    raw,
                    kind: None,
                }),
                Err(_) => reply(400, jobj! {"error" => "link_probe_invalid"}),
            };
        }
        reply(404, jobj! {"error" => "route_unknown"})
    })
}
/// `MemberReports.submit`: the owner inbox accepts the report of a CURRENT member only.
pub fn member_report<M: ManagedSlot>(
    seed: Option<&SeedRuntime<M>>,
    report: &Value,
) -> Result<String, &'static str> {
    let node = report["node_id"]
        .as_str()
        .ok_or("capability_report_invalid")?
        .to_owned();
    let member = seed
        .and_then(|s| s.coordinator.member(&node).ok())
        .ok_or("capability_report_member_not_current")?;
    let digest = member["verification_key_digest"]
        .as_str()
        .unwrap_or_default();
    let statement = capability::verify_report(
        report,
        now(),
        digest,
        &node,
        &["join", "pre_run", "periodic"],
    )
    .map_err(|e| e.code)?;
    let current = matches!(
        member["lifecycle_state"].as_str(),
        Some("NEW" | "CONFIGURED" | "RUNNING")
    );
    // `statement['generation'] != member['generation']`: a join report carries no generation yet.
    if statement["generation"] != member["generation"] && !statement["generation"].is_null()
        || !current
    {
        return Err("capability_report_member_not_current");
    }
    seed.ok_or("capability_report_member_not_current")?
        .capability_reports
        .lock()
        .map_err(|_| "capability_report_invalid")?
        .insert(node.clone(), statement);
    Ok(node)
}

/// `shutil.disk_usage(root).free` (`f_bavail * f_frsize`).
#[allow(unsafe_code)] // statvfs(3) into a zeroed struct for a valid path
pub fn free_bytes(root: &Path) -> u64 {
    let path = std::ffi::CString::new(root.to_string_lossy().as_bytes()).unwrap_or_default();
    // SAFETY: a zeroed statvfs is a valid out-parameter for statvfs(3).
    let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: `path` is NUL-terminated and `stat` is a valid, writable statvfs.
    let status = unsafe { libc::statvfs(path.as_ptr(), &mut stat) };
    if status == 0 {
        u64::from(stat.f_bavail) * stat.f_frsize
    } else {
        0
    }
}
/// `observe_device`: chip, RAM, free disk, OS version; MLX is not part of this build; power, thermal and
/// network are `unknown` (never invented).
pub fn observe_device(root: &Path) -> Value {
    #[allow(unsafe_code)] // sysctlbyname(3): the buffer is sized by the first call
    fn sysctl_text(name: &str) -> Option<String> {
        let name = std::ffi::CString::new(name).ok()?;
        let mut size = 0usize;
        // SAFETY: querying the size, then reading into a buffer of exactly that size.
        unsafe {
            if libc::sysctlbyname(
                name.as_ptr(),
                std::ptr::null_mut(),
                &mut size,
                std::ptr::null_mut(),
                0,
            ) != 0
            {
                return None;
            }
            let mut buffer = vec![0u8; size];
            if libc::sysctlbyname(
                name.as_ptr(),
                buffer.as_mut_ptr().cast(),
                &mut size,
                std::ptr::null_mut(),
                0,
            ) != 0
            {
                return None;
            }
            buffer.truncate(size);
            while buffer.last() == Some(&0) {
                buffer.pop();
            }
            String::from_utf8(buffer).ok()
        }
    }
    #[allow(unsafe_code)] // sysctlbyname(3) into a u64
    fn sysctl_u64(name: &str) -> Option<u64> {
        let name = std::ffi::CString::new(name).ok()?;
        let mut value = 0u64;
        let mut size = std::mem::size_of::<u64>();
        // SAFETY: `value` is a u64 and `size` says so.
        let status = unsafe {
            libc::sysctlbyname(
                name.as_ptr(),
                (&raw mut value).cast(),
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        };
        (status == 0).then_some(value)
    }
    let free = free_bytes(root);
    let printable = |t: Option<String>| {
        t.filter(|t| (1..=96).contains(&t.len()) && t.bytes().all(|b| (0x20..=0x7e).contains(&b)))
    };
    json!({
        "chip": printable(sysctl_text("machdep.cpu.brand_string")).unwrap_or_else(|| "unknown".into()),
        "memory_bytes": sysctl_u64("hw.memsize").unwrap_or(0),
        "available_storage_bytes": free,
        "os_version": printable(sysctl_text("kern.osproductversion")).unwrap_or_else(|| "unknown".into()),
        "mlx": {"available": false, "device": null},
        "power": "unknown", "thermal": "unknown", "network": "unknown",
    })
}
/// `planning_caps(root)`: known local limits for placement, never authorization.
pub fn planning_caps(root: &Path) -> Option<Value> {
    let mut caps = Map::new();
    if let Ok(Some(grant)) = standing_consent::load(root) {
        for key in ["storage_bytes", "max_memory_bytes", "max_wall_seconds"] {
            if let Some(v) = grant.get("caps").and_then(|c| c.get(key)) {
                caps.insert(key.to_owned(), v.clone());
            }
        }
    }
    if let Ok(Some(policy)) = resource_policy::read(root)
        && let Some(storage) = policy.get("storage_bytes").and_then(Value::as_i64)
    {
        let stored = caps
            .get("storage_bytes")
            .and_then(Value::as_i64)
            .unwrap_or(storage);
        caps.insert("storage_bytes".into(), json!(stored.min(storage)));
    }
    (!caps.is_empty()).then_some(Value::Object(caps))
}
/// The member's `JoinWindowClient` (`_join_client`); `test_base` is the oracle's
/// `_test_join_base_url` seam (the member's window/probe/report requests go to a loopback relay
/// proxy instead of the public HTTPS origin; app.py:506-513).
pub fn join_client(origin: &str, test_base: Option<&str>) -> Result<JoinWindowClient, PublicError> {
    let client = JoinWindowClient::new(origin, crate::join_client::default_timeout())?;
    match test_base {
        Some(base) => client.with_test_base_url(base),
        None => Ok(client),
    }
}
/// `_measure_and_sign(client, purpose)`.
pub fn measure_and_sign(
    root: &Path,
    client: &mut JoinWindowClient,
    purpose: &str,
) -> Result<Value, PublicError> {
    let started = Instant::now();
    let mut clock = move || started.elapsed().as_secs_f64();
    let link = capability::measure_link(
        &mut |kib, upload| client.probe(kib, upload),
        3,
        256 * 1024,
        &mut clock,
    )?;
    let device = observe_device(root);
    let (signer, _, node) = identity(root)?;
    let caps = planning_caps(root);
    capability::signed_report(
        &signer,
        &node,
        purpose,
        None,
        &device,
        &link,
        now(),
        capability::report_ttl_seconds(),
        caps.as_ref(),
    )
}
/// `RemoteMacMember.owner_origin()`: the seed url of the retained membership.
pub fn owner_origin(root: &Path) -> Option<String> {
    private_state::load(&root.join("membership.state.json"), cap())
        .ok()
        .flatten()
        .and_then(|m| m.get("seed_url").and_then(Value::as_str).map(str::to_owned))
}
/// `peer.handoff_seed_key_digest(handoff)`: decrypt locally and read the pinned seed key digest.
pub fn handoff_seed_key_digest(root: &Path, handoff_value: &Value) -> Result<String, PublicError> {
    let pending = private_state::load(&root.join(PENDING_FILE), cap())
        .map_err(|e| PublicError::new(409, e.0))?
        .ok_or_else(failure)?;
    let request = pending.get("request").cloned().unwrap_or(Value::Null);
    let key = pending
        .get("recipient_private_key")
        .and_then(Value::as_str)
        .and_then(|k| URL_SAFE_NO_PAD.decode(k).ok())
        .ok_or_else(failure)?;
    let payload =
        handoff::decrypt_invitation_handoff(&request, handoff_value, Some(&key), &json!(now()))?;
    Ok(payload["seed_key_digest"]
        .as_str()
        .unwrap_or_default()
        .to_owned())
}
/// `_auto_report(purpose)` (app.py:1138-1163) as the join answer carries it: the signed device +
/// relay-link report to the owner's inbox, never failing the caller (a failure is its code).
pub fn auto_report(root: &Path, test_base: Option<&str>, purpose: &str) -> Value {
    let mut state = "delivered";
    let mut error = Value::Null;
    let sent = (|| -> Result<(), PublicError> {
        let origin = owner_origin(root).ok_or(PublicError::new(400, "membership_required"))?;
        let mut client = join_client(&origin, test_base)?;
        let report = measure_and_sign(root, &mut client, purpose)?;
        client.member_report(&report).map(drop)
    })();
    if let Err(e) = sent {
        state = "failed";
        error = json!(e.code);
    }
    json!({"purpose": purpose, "at": now(), "state": state, "error": error, "seed_capability": "not_sent"})
}
/// The member's join: measure the relay link, sign the report, post the request to the owner's
/// window (`client.submit`, app.py:1763-1767); the owner's answer and the client's warnings.
pub fn join_submit(
    root: &Path,
    test_base: Option<&str>,
    link: &crate::persistent::JoinLink,
    request: &Value,
    quota: &Value,
) -> Result<(Value, Vec<Value>), PublicError> {
    let mut client = join_client(&link.origin, test_base)?;
    let report = measure_and_sign(root, &mut client, "join")?;
    let answer = client.submit(&link.window_id, &link.code, request, &report, quota)?;
    Ok((answer, client.warnings().to_vec()))
}
/// `JoinWindowClient(state.origin).poll(window_id, code=, digest=)` (app.py:1784-1785).
pub fn join_poll(
    state: &Map<String, Value>,
    test_base: Option<&str>,
) -> Result<Value, PublicError> {
    let text = |k: &str| {
        state
            .get(k)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned()
    };
    let mut client = join_client(&text("origin"), test_base)?;
    client.poll(&text("window_id"), &text("code"), &text("request_digest"))
}
/// The approved branch of `/api/join/window/poll` (app.py:1796-1813): seed pin check,
/// `accept_pairing`, membership binding, `running = True`, the automatic join report.
pub fn join_accept(
    cell: &impl Cell,
    handoff_value: &Value,
    pin: Option<&str>,
    test_base: Option<&str>,
) -> Result<JoinAccepted, PublicError> {
    let root = cell.with(|c| c.root.clone());
    if let Some(pin) = pin
        && handoff_seed_key_digest(&root, handoff_value)? != pin
    {
        return Err(PublicError::new(400, "join_seed_pin_mismatch"));
    }
    let joined = app_host::pair_accept(cell, handoff_value)?;
    let binding = app_host::membership_binding(cell).map(|b| {
        json!({"swarm_id": b.swarm_id, "node_id": b.node_id, "seed_key_digest": b.seed_key_digest})
    });
    cell.with(|c| c.running = true);
    let generation = cell.with(|c| c.session.as_ref().and_then(|s| s.generation()));
    let report = auto_report(&root, test_base, "join");
    // `self.last_report = record` (`_auto_report`, app.py:1161), shown in `join_flow.last_report`.
    cell.with(|c| c.recorded.last_report = Some(report.clone()));
    Ok(JoinAccepted {
        membership_state: joined
            .get("membership_state")
            .map_or(json!("current"), Json::to_value),
        generation: generation.map_or(Value::Null, Value::from),
        binding,
        report,
    })
}
/// `TargetApplication.status` = `peer.status()` plus the owner-shaped extras (peer.py:395-455,
/// target_app.py:330-343), in that dict order. The membership is the joined Mac's OWN
/// (`api::peer::status_membership`: retained / current / expired / a recorded disposition), and the
/// acknowledgements are the retained consent's (`_load_consent`).
pub fn target_status<M: ManagedSlot>(c: &Authority<M>) -> Json {
    let root = &c.root;
    let pairing_state = if root.join("accepted.json").exists() {
        "accepted"
    } else if root.join(HANDOFF_FILE).exists() {
        "handoff_retained"
    } else if root.join(PENDING_FILE).exists() {
        "pending"
    } else {
        "not_requested"
    };
    let consent = private_state::load(&root.join(CONSENT_FILE), cap())
        .ok()
        .flatten()
        .and_then(|record| record.get("consent").cloned())
        .unwrap_or(Value::Null);
    let acknowledged = |key: &str| consent[key] == true;
    let live = c.session.as_ref().map_or(c.session_view, |s| {
        Some((s.generation(), s.lease_expires_at()))
    });
    let (state, generation, lease_state) = crate::peer::status_membership(root, live, now());
    jobj! {
        "protocol" => "mycelium.demo_authority_status.v1",
        "started" => c.running,
        "identity_state" => if root.join(IDENTITY_FILE).exists() { "present" } else { "absent" },
        "pairing_state" => pairing_state,
        "membership_state" => state.as_str(),
        "membership_generation" => generation.map_or(Json::Null, Json::from),
        "lease_state" => lease_state,
        "cooperative_peer_visibility_acknowledged" => acknowledged("cooperative_peer_visibility_acknowledged"),
        "resource_use_acknowledged" => acknowledged("resource_use_acknowledged"),
        "contribution_enabled" => acknowledged("contribution_enabled"),
        "capability_state" => "not_reported",
        "serving_state" => "not_yet_serving",
        "route_ready" => false,
        "swarm_created" => false,
        "member_count" => 0u64,
        "native_control" => jobj! {
            "state" => "stopped", "model_load_authorized" => false, "work_admitted" => false,
        },
        "capability" => Json::Null,
    }
}
/// `self.authority.status()` as `DemoApp.status()` shows it (app.py:1251-1258): a joined target's
/// [`target_status`]; a stopped owner's retained membership; else the owner authority's status with
/// each member's clock offset view (`clock_offset.py` snapshot order), or the no-swarm status.
pub fn authority_status<M: ManagedSlot>(c: &Authority<M>) -> Json {
    if c.target {
        return target_status(c);
    }
    // `authority.stop()`: the seed runtime is closed, the retained membership is all that is left.
    if !c.running && c.legacy.is_none() && c.swarm.is_some() {
        let dir = c.root.join("authority").join("member");
        let generation = crate::peer::load_membership_state(&dir)
            .ok()
            .flatten()
            .and_then(|state| state.get("membership_generation").and_then(Value::as_u64));
        return crate::authority_view::retained_status(generation);
    }
    match c.owner_authority() {
        Some(view) => {
            let mut status = view.status(c.running, now());
            if let (Some(seed), Some(Json::Arr(mut members))) =
                (&c.seed, status.get("members").cloned())
            {
                for member in &mut members {
                    let Some(Json::Str(node)) = member.get("node_id") else {
                        continue;
                    };
                    let clock = seed.clock_offsets.view(node);
                    if clock["samples"].as_u64().is_some_and(|n| n > 0) {
                        // clock_offset.py snapshot insertion order; unknown peers retain the old null view.
                        let fields = [
                            "offset_seconds",
                            "rtt_seconds",
                            "samples",
                            "age_seconds",
                            "fresh",
                            "tolerance_seconds",
                            "exceeds_tolerance",
                        ];
                        member.set(
                            "clock",
                            Json::Obj(
                                fields
                                    .into_iter()
                                    .map(|field| (field.to_owned(), Json::from(&clock[field])))
                                    .collect(),
                            ),
                        );
                    }
                }
                status.set("members", Json::Arr(members));
            }
            status
        }
        None => crate::authority_view::no_swarm_status(c.running),
    }
}
/// `_join_flow_status()` (app.py:1214-1237): the standing disclosure (the join-consent document),
/// then the owner's windows, or the joined Mac's standing consent (with the acceptor's last offer),
/// the standing error, its pending join window and its last report, as the app `recorded` them.
pub fn join_flow_status(
    root: &Path,
    target: bool,
    windows: &PairingWindows,
    pending: &PendingWindows,
    recorded: &crate::app_status::Recorded,
) -> Json {
    let disclosure = jobj! {
        "text" => standing_consent::DISCLOSURE_TEXT, "digest" => standing_consent::DISCLOSURE_DIGEST,
    };
    if !target {
        return jobj! {
            "standing_disclosure" => disclosure,
            "pairing_windows" => windows.list().unwrap_or(Json::Arr(Vec::new())),
            "pending_windows" => pending.json(), "member_reports" => Json::Obj(Vec::new()),
        };
    }
    let optional = |value: &Option<Value>| value.as_ref().map_or(Json::Null, Json::from);
    let grant = standing_consent::load(root).ok().flatten();
    let join = private_state::load(&root.join("join-window.json"), cap())
        .ok()
        .flatten();
    jobj! {
        "standing_disclosure" => disclosure,
        "standing_consent" => match grant {
            None => Json::Null,
            Some(g) => jobj! {
                "state" => &g["state"], "caps" => &g["caps"], "granted_at" => &g["granted_at"],
                "withdrawn_at" => &g["withdrawn_at"], "last" => optional(&recorded.standing_last),
            },
        },
        "standing_error" => recorded.standing_error.as_deref().map_or(Json::Null, Json::from),
        "standing_error_http" => optional(&recorded.standing_error_http),
        "join_window" => match join {
            None => Json::Null,
            Some(j) => jobj! {
                "state" => "waiting_for_owner", "fingerprint" => &j["fingerprint"],
                "pinned" => !j["seed_key_digest"].is_null(),
            },
        },
        "last_report" => optional(&recorded.last_report),
    }
}
/// `authority.start()` of a joined target (`RemoteMacMember.start` -> `_resume`, app.py:613-626):
/// a retained membership resumes over `options` (generation + 1); a dead relay or a lapsed
/// membership leaves the app started but not running (`recovery_error`), anything else refuses.
pub fn resume_membership(cell: &impl Cell, options: &SeedOptions) -> Result<(), String> {
    let (target, root) = cell.with(|c| (c.target, c.root.clone()));
    if !target || !root.join("membership.state.json").exists() {
        return Ok(());
    }
    let (signer, _, _) = identity(&root).map_err(|e| e.code.to_owned())?;
    let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
    let env = crate::peer::JoinEnv {
        signer: &signer,
        clock: &now,
        random_hex: &random_hex,
        seed: options,
    };
    match crate::peer::start(&root, &signer, true, &env) {
        Ok(started) => {
            cell.with(|c| {
                c.session = started.session;
                c.running = true;
            });
            Ok(())
        }
        Err(failed) => match failed.error.code {
            "peer_transport_unreachable"
            | "seed_http_remote_error"
            | "peer_member_expired"
            | "peer_generation_stale"
            | "peer_member_unknown" => {
                cell.with(|c| c.running = false);
                Ok(())
            }
            code => Err(code.to_owned()),
        },
    }
}
/// The durable half of a member leave's "this Mac owns no compute" proof: no own-contributor or
/// native-bootstrap state on disk (unreadable paths and symlink residue refuse) and no cleanup
/// debt a previous leave recorded. A host adds what it holds in memory.
pub fn none_owned_on_disk(root: &Path) -> Result<bool, String> {
    let contributor = root.join("local-contributor");
    let native = contributor.join("native-bootstrap");
    for path in [&contributor, &native] {
        let absent_or_directory = match fs::symlink_metadata(path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => true,
            Err(_) => return Err("member_compute_ownership_unavailable".into()),
            Ok(metadata) => metadata.is_dir(),
        };
        if !absent_or_directory {
            return Ok(false);
        }
    }
    let native_empty = match fs::read_dir(&native) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => true,
        Err(_) => return Err("member_compute_ownership_unavailable".into()),
        Ok(mut entries) => entries.next().is_none(),
    };
    if !native_empty {
        return Ok(false);
    }
    let previous =
        private_state::load(&root.join("leave-state.json"), cap()).map_err(|e| e.0.to_owned())?;
    // A removed stage is not enough to retire already-recorded cleanup debt.
    Ok(previous.is_none_or(|record| {
        record.get("steps").is_some_and(Value::is_array) && !crate::leave::cleanup_pending(&record)
    }))
}
