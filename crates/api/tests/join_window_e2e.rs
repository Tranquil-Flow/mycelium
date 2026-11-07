// SPDX-License-Identifier: AGPL-3.0-only
//! Round-15 end to end on loopback, Rust member against Rust owner + RU-025 seed: the owner collapse
//! (`pair_request` -> `approve_pairing` -> `accept_pairing` over the owner's own pinned local TLS), the
//! owner's pairing window behind a loopback relay stand-in, and the member's `JoinWindowClient`
//! (link probe, signed join report, submit, owner approval, poll, handoff, seed join). Every piece
//! used here is replayed against the executed oracle in its own test file; this test proves they
//! compose. The relay's seed hop and the cloudflared front are not part of it (relay.rs / network_start.rs).
use api::{
    capability,
    handoff::{self, Coordinator as HandoffCoordinator, Entropy},
    join_client::JoinWindowClient,
    pairing_windows::PairingWindows,
    peer::{self, JoinEnv},
    persistent::identity_fingerprint,
    preferences::private_state,
    seed::SeedOptions,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

fn ms(millis: u64) -> Duration {
    Duration::from_millis(millis)
}
fn secs(seconds: u64) -> Duration {
    Duration::from_secs(seconds)
}
fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}
fn scratch(tag: &str) -> PathBuf {
    let base = std::fs::canonicalize(std::env::temp_dir()).expect("tmp");
    let dir = base.join(format!("e2e-{tag}-{}-{}", std::process::id(), now() as u64));
    std::fs::create_dir_all(&dir).expect("dir");
    std::fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .expect("mode");
    dir
}
fn private(dir: &Path, name: &str) -> PathBuf {
    let path = dir.join(name);
    std::fs::create_dir_all(&path).expect("dir");
    std::fs::set_permissions(&path, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .expect("mode");
    path
}
fn random_hex(n: usize) -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(1);
    let seed = COUNTER.fetch_add(1, Ordering::SeqCst) ^ (now() * 1e6) as u64;
    (0..n)
        .map(|i| {
            format!(
                "{:02x}",
                (seed.wrapping_mul(2654435761).wrapping_add(i as u64 * 40503)) & 0xff
            )
        })
        .collect()
}
/// `build_pair_request` of the headless host: a signed request, the retained pending record and the consent.
fn pair_request(dir: &Path, signer: &Identity, node: &str) -> Value {
    let issued = now();
    let secret = [7u8; 32];
    let statement = json!({
        "protocol": "mycelium.pairing_request.v1", "node_id": node,
        "identity_endpoint_id": signer.endpoint_id(), "identity_key_digest": signer.verification_key_digest(),
        "identity_verification_key": signer.public_key_record(), "peer_class": "mac_mlx_iroh",
        "invite_protocol": "mycelium.invite_bundle.v1",
        "recipient_encryption_key": URL_SAFE_NO_PAD.encode(handoff::crypto::x25519_public(&secret)),
        "pairing_nonce": format!("pairing-{}", random_hex(24)),
        "issued_at": issued, "expires_at": issued + 300.0,
    });
    let mut request = statement.clone();
    request["signature"] = signer.sign(&statement).expect("sign");
    let consent = json!({
        "cooperative_peer_visibility_acknowledged": true, "resource_use_acknowledged": true,
        "contribution_enabled": false,
    });
    let limit = private_state::private_json_limit().expect("limit");
    private_state::store(
        &dir.join("consent.json"),
        &json!({"protocol": "mycelium.remote_mac_consent.v1", "consent": consent}),
        limit,
    )
    .expect("consent");
    private_state::store(
        &dir.join("pairing.pending.json"),
        &json!({
            "protocol": "mycelium.remote_mac_pairing_pending.v1", "request": request,
            "recipient_private_key": URL_SAFE_NO_PAD.encode(secret),
            "consent_digest": protocol::canonical::digest(&consent).expect("digest"),
        }),
        limit,
    )
    .expect("pending");
    request
}

/// A loopback stand-in for the relay front: `/pairing-window/*` and `/link-probe/*` to the owner's handlers.
fn relay_stand_in(
    windows: Arc<PairingWindows>,
    stop: Arc<AtomicBool>,
) -> (u16, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    listener.set_nonblocking(true).expect("nonblocking");
    let port = listener.local_addr().expect("addr").port();
    let handle = std::thread::spawn(move || {
        while !stop.load(Ordering::SeqCst) {
            let Ok((mut stream, _)) = listener.accept() else {
                std::thread::sleep(ms(5));
                continue;
            };
            stream.set_nonblocking(false).expect("blocking");
            stream.set_read_timeout(Some(secs(5))).expect("timeout");
            serve(&mut stream, &windows);
        }
    });
    (port, handle)
}
fn serve(stream: &mut TcpStream, windows: &PairingWindows) {
    let mut raw = Vec::new();
    let mut chunk = [0u8; 8192];
    let (head_end, length) = loop {
        let n = stream.read(&mut chunk).unwrap_or(0);
        if n == 0 {
            return;
        }
        raw.extend_from_slice(&chunk[..n]);
        if let Some(at) = raw.windows(4).position(|w| w == b"\r\n\r\n") {
            let head = String::from_utf8_lossy(&raw[..at]).to_ascii_lowercase();
            let length = head
                .lines()
                .find_map(|l| {
                    l.strip_prefix("content-length:")
                        .and_then(|v| v.trim().parse::<usize>().ok())
                })
                .unwrap_or(0);
            break (at + 4, length);
        }
    };
    while raw.len() < head_end + length {
        let n = stream.read(&mut chunk).unwrap_or(0);
        if n == 0 {
            break;
        }
        raw.extend_from_slice(&chunk[..n]);
    }
    let head = String::from_utf8_lossy(&raw[..head_end]).into_owned();
    let mut first = head.lines().next().unwrap_or_default().split(' ');
    let (method, path) = (
        first.next().unwrap_or_default(),
        first.next().unwrap_or_default(),
    );
    let body = &raw[head_end..head_end + length];
    assert!(
        head.contains(api::join_client::USER_AGENT),
        "the Mycelium User-Agent is sent: {head}"
    );
    let (status, payload) = if path.starts_with("/pairing-window/") {
        windows.handle(method, path, body)
    } else if let Some(kib) = path.strip_prefix("/link-probe/v1/") {
        match capability::link_probe_response(
            &json!(kib.parse::<u64>().unwrap_or(u64::MAX)),
            body.len(),
        ) {
            Ok(payload) => (200, payload),
            Err(_) => (400, br#"{"error":"link_probe_invalid"}"#.to_vec()),
        }
    } else {
        (404, br#"{"error":"route_unknown"}"#.to_vec())
    };
    let reply = format!(
        "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        payload.len()
    );
    stream
        .write_all(reply.as_bytes())
        .and_then(|()| stream.write_all(&payload))
        .unwrap_or_default();
}

#[test]
fn rust_member_joins_a_rust_owner_through_the_pairing_window_and_the_seed() {
    let base = scratch("join");
    let owner = private(&base, "owner");
    // ---- the owner: seed (RU-025 crate) over local TLS
    let material = seed::tls::ensure_local_tls_material(&owner.join("tls")).expect("tls");
    let seed_signer = Identity::from_secret("endpoint-seed", &[11u8; 32]).expect("seed key");
    let owner_signer = Identity::from_secret("endpoint-owner", &[12u8; 32]).expect("owner key");
    let swarm = "sw-e2e0123456789abcdef";
    let options = seed::Options::new(swarm, "seed-e2e", "boot-e2e").expect("options");
    let ids = AtomicU64::new(1);
    let coordinator = Arc::new(
        seed::Coordinator::open(
            &owner.join("seed.sqlite"),
            seed_signer,
            options,
            Box::new(now),
            Box::new(move || {
                format!(
                    "{:08x}-0000-4000-8000-{:012x}",
                    ids.fetch_add(1, Ordering::SeqCst),
                    7
                )
            }),
        )
        .expect("coordinator"),
    );
    let server =
        seed::server::Server::bind_tls(coordinator.clone(), &material, None).expect("server");
    let ca = std::fs::read(&material.ca_certificate).expect("ca");
    let seed_url = server.base_url().to_owned();
    let handoff_coordinator = || HandoffCoordinator {
        signer: Identity::from_secret("endpoint-seed", &[11u8; 32]).expect("seed key"),
        swarm_id: swarm.to_owned(),
        seed_url: seed_url.clone(),
    };
    let quota = json!({"storage_bytes": 2_000_000_000u64, "network_bytes_per_day": 2_000_000_000u64, "background_allowed": false});
    // ---- (2) owner collapse: this Mac is member 1 of its own swarm
    let owner_member = private(&owner, "member");
    let owner_request = pair_request(&owner_member, &owner_signer, "node-owner1");
    let collapse = handoff::approve_pairing(
        &owner_member,
        true,
        Some(&handoff_coordinator()),
        &owner_request,
        &json!(120),
        &quota,
        now(),
        &Entropy::os,
    )
    .expect("owner approves itself");
    let own_options = SeedOptions {
        ca_pem: Some(ca.clone()),
        connect_to: Some(server.address()),
        server_name: Some("127.0.0.1".into()),
    };
    let hex = |n: usize| random_hex(n);
    let env = JoinEnv {
        signer: &owner_signer,
        clock: &now,
        random_hex: &hex,
        seed: &own_options,
    };
    let mut collapsed = peer::accept_handoff(&owner_member, true, &collapse.to_value(), &env)
        .expect("owner collapse accepts");
    let owner_row = coordinator.member("node-owner1").expect("owner member row");
    assert_eq!(
        owner_row["lifecycle_state"], "NEW",
        "a joined member is NEW until it heartbeats: {owner_row}"
    );
    // `_renew_locked` after the join: the first forced RUNNING heartbeat (what the oracle's /api/create answers)
    peer::renew(&owner_member, true, collapsed.session.as_mut(), &env).expect("first renew");
    let owner_row = coordinator.member("node-owner1").expect("owner member row");
    assert_eq!(owner_row["lifecycle_state"], "RUNNING", "{owner_row}");
    assert_eq!(owner_row["generation"], 1);
    assert_eq!(coordinator.members(None).expect("members").len(), 1);

    // ---- (1) the owner's window behind the relay stand-in
    let windows = Arc::new(PairingWindows::system());
    let stop = Arc::new(AtomicBool::new(false));
    let (port, relay) = relay_stand_in(windows.clone(), stop.clone());
    let origin = "https://v02-test-e2e.mycelium.now";
    let opened = windows
        .open(
            &json!(origin),
            &json!(coordinator.signer().verification_key_digest()),
            &json!(300),
        )
        .expect("window opens");
    let id = opened.get("window_id").map(|j| j.to_value()).expect("id");
    let link = opened.get("join_link").map(|j| j.to_value()).expect("link");
    let (parsed, _) =
        api::join_link::parse_join_text(&link, &json!(now())).expect("the owner's own link parses");
    assert_eq!(parsed.origin, origin);
    assert_eq!(Some(parsed.window_id.as_str()), id.as_str());
    assert_eq!(
        parsed.seed_key_digest.as_deref(),
        Some(coordinator.signer().verification_key_digest().as_str())
    );

    // ---- (3) the member: probe the relay link, sign the join report, submit
    let member = private(&base, "member");
    let member_signer = Identity::from_secret("endpoint-member", &[13u8; 32]).expect("member key");
    let request = pair_request(&member, &member_signer, "node-member1");
    let mut client = JoinWindowClient::new(origin, api::join_client::default_timeout())
        .expect("client")
        .with_test_base_url(&format!("http://127.0.0.1:{port}"))
        .expect("test base");
    let started = std::time::Instant::now();
    let mut clock = move || started.elapsed().as_secs_f64();
    let link_measure = capability::measure_link(
        &mut |kib, up| client.probe(kib, up),
        3,
        256 * 1024,
        &mut clock,
    )
    .expect("three RTT rounds and a bulk upload and download through the stand-in");
    let device = json!({
        "chip": "Apple M4 Pro", "memory_bytes": 51_539_607_552u64, "available_storage_bytes": 400_000_000_000u64,
        "os_version": "26.5.1", "mlx": {"available": false, "device": null},
        "power": "unknown", "thermal": "unknown", "network": "unknown",
    });
    let report = capability::signed_report(
        &member_signer,
        "node-member1",
        "join",
        None,
        &device,
        &link_measure,
        now(),
        capability::report_ttl_seconds(),
        None,
    )
    .expect("signed report");
    let answer = client
        .submit(&parsed.window_id, &parsed.code, &request, &report, &quota)
        .expect("the owner's window accepts the claim");
    let fingerprint = identity_fingerprint(&request["identity_key_digest"]).expect("fingerprint");
    assert_eq!(answer["state"], "waiting_for_owner");
    assert_eq!(answer["fingerprint"], fingerprint.as_str());
    let listed = windows.list().expect("list").to_value();
    assert_eq!(listed[0]["state"], "claimed");
    assert_eq!(listed[0]["pending"]["node_id"], "node-member1");
    assert_eq!(
        listed[0]["pending"]["quota"]["storage_bytes"],
        2_000_000_000u64
    );
    let digest = listed[0]["pending"]["request_digest"]
        .as_str()
        .expect("digest")
        .to_owned();
    assert_eq!(
        client
            .poll(&parsed.window_id, &parsed.code, &digest)
            .expect("poll"),
        json!({"state": "waiting_for_owner"})
    );

    // ---- the owner approves with one click: the issue closure is `approve_pairing` on the real coordinator
    let approved_dir = private(&owner, "approvals");
    let mut issue = |req: &Value, q: &Value| {
        handoff::approve_pairing(
            &approved_dir,
            true,
            Some(&handoff_coordinator()),
            req,
            &json!(120),
            q,
            now(),
            &Entropy::os,
        )
    };
    let view = windows
        .approve(&id, &json!(digest), &mut issue)
        .expect("approve")
        .to_value();
    assert_eq!(view["state"], "approved");

    // ---- the member collects the handoff and joins the seed (Web-PKI replaced by the pinned local CA)
    let polled = client
        .poll(&parsed.window_id, &parsed.code, &digest)
        .expect("poll approves");
    assert_eq!(polled["state"], "approved");
    assert_eq!(
        polled["handoff"]["protocol"],
        "mycelium.encrypted_invitation_handoff.v1"
    );
    let digest_pinned = parsed.seed_key_digest.clone().expect("pinned link");
    let member_options = SeedOptions {
        ca_pem: Some(ca),
        connect_to: None,
        server_name: None,
    };
    let member_env = JoinEnv {
        signer: &member_signer,
        clock: &now,
        random_hex: &hex,
        seed: &member_options,
    };
    let joined = peer::accept_handoff(&member, true, &polled["handoff"], &member_env)
        .expect("the member joins");
    assert!(
        joined.session.is_some(),
        "a first acceptance keeps its live session"
    );
    assert_eq!(
        joined
            .session
            .as_ref()
            .and_then(api::member::Session::seed_key_digest),
        Some(digest_pinned.as_str()),
        "the session is pinned to the seed key the owner's link carried"
    );
    // ---- the owner's seed now holds two members
    let rows = coordinator.members(None).expect("members");
    assert_eq!(rows.len(), 2, "{rows:?}");
    let row = coordinator.member("node-member1").expect("member row");
    assert_eq!(row["generation"], 1);
    assert_eq!(row["peer_class"], "mac_mlx_iroh");
    assert_eq!(
        windows.list().expect("list").to_value()[0]["state"],
        "delivered"
    );
    // ---- a second claim on a delivered window is refused and the code lockout is the oracle's
    let again = client.submit(&parsed.window_id, "WRONGCODE", &request, &report, &quota);
    assert_eq!(
        again.expect_err("wrong code").code,
        "pairing_window_code_invalid"
    );

    stop.store(true, Ordering::SeqCst);
    relay.join().expect("relay stand-in joins");
    server.close();
    std::fs::remove_dir_all(&base).expect("scratch removed");
}
