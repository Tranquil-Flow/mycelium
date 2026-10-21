// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "net")]
//! A2-over-iroh end to end on loopback (no relays): a phone acquires a v2
//! manifest's objects from an artifact source under a seed grant, every range
//! receipt-verified, then loads the stage and passes its golden. A source
//! refuses a peer that is not the grant's recipient, a spent grant budget and
//! an object whose content does not match its name.

use std::path::PathBuf;
use std::sync::Arc;

use base64::Engine;
use mycelium_mobile_core::artifact::ArtifactStore;
use mycelium_mobile_core::artifact_grant::{self as ag, Grant};
use mycelium_mobile_core::artifact_net::{ArtifactSource, IrohChunkSource};
use mycelium_mobile_core::identity::Identity;
use mycelium_mobile_core::safetensors;
use mycelium_mobile_core::stage_manifest::StageManifest;
use mycelium_mobile_core::transport::Reachability;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// `MYCELIUM_MOBILE_FIXTURES` points a cross-compiled binary at pushed fixtures.
fn fixture(name: &str) -> Value {
    let dir = std::env::var("MYCELIUM_MOBILE_FIXTURES").unwrap_or_else(|_| format!("{}/tests/fixtures", env!("CARGO_MANIFEST_DIR")));
    let path = format!("{dir}/{name}");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn scratch(tag: &str) -> PathBuf {
    let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let dir = std::env::temp_dir().join(format!("mycelium-artifact-net-{tag}-{}-{n}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Content-addressed objects of the synthetic Qwen2 file, as a source holds them.
fn export_objects(root: &std::path::Path) {
    let file = base64::engine::general_purpose::STANDARD.decode(fixture("qwen2_synthetic.json")["safetensors_b64"].as_str().unwrap()).unwrap();
    let header = safetensors::parse_file(&file).unwrap();
    for name in header.tensors.keys() {
        let (s, e) = header.range(name).unwrap();
        let bytes = &file[s as usize..e as usize];
        std::fs::write(root.join(hex::encode(Sha256::digest(bytes))), bytes).unwrap();
    }
}

fn iroh_id(secret: [u8; 32]) -> String {
    iroh::SecretKey::from_bytes(&secret).public().to_string()
}

fn now_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64
}

/// A seed-signed grant (the seed issues these in Python; same shape).
fn grant(seed: &Identity, recipient: &str, member: &Identity, manifest: &StageManifest, source: &ArtifactSource, total: Option<u64>) -> Grant {
    let mut objects: Vec<String> = manifest.tensors.iter().map(|t| t.sha256.clone()).collect();
    objects.sort();
    objects.dedup();
    let now = now_ms();
    let message = json!({
        "protocol": ag::GRANT_PROTOCOL,
        "grant_id": "grant-net-1",
        "swarm_id": "swarm-a",
        "seed_node_id": "seed-node",
        "recipient_node_id": "node-phone",
        "recipient_endpoint_id": recipient,
        "recipient_key_digest": member.verification_key_digest(),
        "assignment_id": "route-1",
        "manifest_pack_digest": manifest.pack_digest,
        "allowed_object_digests": objects,
        "authorized_sources": [{"endpoint_id": source.endpoint_id(), "verification_key_digest": source.key_digest(), "direct_addrs": []}],
        "maximum_total_bytes": total.unwrap_or(manifest.total_bytes()),
        "maximum_bytes_per_second": 64u64 << 20,
        "maximum_chunk_bytes": 4u64 << 20,
        "issued_at_unix_ms": now,
        "not_before_unix_ms": now,
        "expires_at_unix_ms": now + 900_000,
    });
    let env = json!({
        "protocol": "mycelium.membership.signed_message.v1",
        "signature": seed.sign(&message).unwrap(),
        "verification_key": seed.public_key_record(),
        "message": message,
    });
    ag::verify_grant(&env, &seed.public_key_record(), now).unwrap()
}

struct World {
    seed: Identity,
    member: Arc<Identity>,
    phone_secret: [u8; 32],
    manifest: StageManifest,
    source: ArtifactSource,
    objects: PathBuf,
}

async fn world(tag: &str) -> World {
    let seed = Identity::from_secret("seed-endpoint", &[0x22; 32]).unwrap();
    let phone_secret = [0x41; 32];
    let member = Arc::new(Identity::from_secret(&iroh_id(phone_secret), &[0x11; 32]).unwrap());
    let manifest = StageManifest::from_document(&fixture("qwen2_stage_manifest_v2.json")["manifest"]).unwrap();
    let objects = scratch(&format!("{tag}-objects"));
    export_objects(&objects);
    let source = ArtifactSource::bind(&[0x51; 32], &[0x33; 32], seed.public_key_record(), objects.clone(), &scratch("state"), Reachability::LocalOnly).await.unwrap();
    drop(source.spawn_accept()); // detached accept loop
    World { seed, member, phone_secret, manifest, source, objects }
}

async fn acquire(w: &World, g: Grant, chunk: usize, secret: [u8; 32]) -> (Result<u64, String>, PathBuf) {
    let client = IrohChunkSource::bind(&secret, w.member.clone(), g, vec![w.source.addr()], Reachability::LocalOnly).await.unwrap();
    let manifest = w.manifest.clone();
    let root = scratch("store");
    let store_root = root.clone();
    let result = tokio::task::spawn_blocking(move || {
        let mut client = client;
        let store = ArtifactStore::open(&store_root, 1 << 30).unwrap();
        let report = store.acquire(&manifest, &mut client, chunk, None).map_err(|e| e.0)?;
        store.load_stage(&manifest).map_err(|e| e.0)?; // digest re-verify + golden
        Ok(report.fetched_bytes)
    })
    .await
    .unwrap();
    (result, root)
}

#[tokio::test(flavor = "multi_thread")]
async fn a_phone_acquires_its_objects_under_the_grant_and_passes_the_golden() {
    let w = world("ok").await;
    let g = grant(&w.seed, &iroh_id(w.phone_secret), &w.member, &w.manifest, &w.source, None);
    let (result, root) = acquire(&w, g, 1000, w.phone_secret).await;
    assert_eq!(result.unwrap(), w.manifest.total_bytes());
    std::fs::remove_dir_all(root).unwrap();
    std::fs::remove_dir_all(&w.objects).unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn a_source_refuses_another_peer_a_spent_budget_and_unverified_objects() {
    let w = world("refuse").await;
    let phone = iroh_id(w.phone_secret);
    // The grant names the phone, but a different iroh endpoint presents it.
    let g = grant(&w.seed, &phone, &w.member, &w.manifest, &w.source, None);
    let (result, root) = acquire(&w, g, 4096, [0x42; 32]).await;
    assert_eq!(result.unwrap_err(), "artifact_source_refused:artifact_recipient_transport_mismatch");
    std::fs::remove_dir_all(root).unwrap();
    // A budget smaller than the objects: the source stops serving.
    let g = grant(&w.seed, &phone, &w.member, &w.manifest, &w.source, Some(1000));
    let (result, root) = acquire(&w, g, 512, w.phone_secret).await;
    assert_eq!(result.unwrap_err(), "artifact_source_refused:artifact_grant_budget_exhausted");
    std::fs::remove_dir_all(root).unwrap();
    // An object whose bytes no longer match its name is never served.
    let victim = w.objects.join(w.manifest.tensors[0].hex());
    let mut bytes = std::fs::read(&victim).unwrap();
    bytes[0] ^= 1;
    std::fs::write(&victim, &bytes).unwrap();
    let source2 = ArtifactSource::bind(&[0x52; 32], &[0x34; 32], w.seed.public_key_record(), w.objects.clone(), &scratch("state"), Reachability::LocalOnly).await.unwrap();
    drop(source2.spawn_accept()); // detached accept loop
    let w2 = World { source: source2, ..w };
    let g = grant(&w2.seed, &phone, &w2.member, &w2.manifest, &w2.source, None);
    let (result, root) = acquire(&w2, g, 4096, w2.phone_secret).await;
    assert_eq!(result.unwrap_err(), "artifact_source_refused:artifact_object_unavailable");
    std::fs::remove_dir_all(root).unwrap();
    std::fs::remove_dir_all(&w2.objects).unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn a_client_only_dials_sources_the_grant_authorizes() {
    let w = world("auth").await;
    let g = grant(&w.seed, &iroh_id(w.phone_secret), &w.member, &w.manifest, &w.source, None);
    let other = ArtifactSource::bind(&[0x53; 32], &[0x35; 32], w.seed.public_key_record(), w.objects.clone(), &scratch("state"), Reachability::LocalOnly).await.unwrap();
    let err = IrohChunkSource::bind(&[0x43; 32], w.member.clone(), g, vec![other.addr()], Reachability::LocalOnly).await.err().unwrap();
    assert_eq!(err.0, "artifact_source_not_authorized");
    std::fs::remove_dir_all(&w.objects).unwrap();
}

/// One raw request over the artifact ALPN; returns the response header.
async fn raw_request(endpoint: &iroh::Endpoint, to: iroh::EndpointAddr, env: &Value) -> Value {
    let connection = endpoint.connect(to, ag::ARTIFACT_CHUNK_ALPN).await.unwrap();
    let (mut send, mut recv) = connection.open_bi().await.unwrap();
    send.write_all(&serde_json::to_vec(env).unwrap()).await.unwrap();
    send.finish().unwrap();
    let raw = recv.read_to_end(1 << 20).await.unwrap();
    let n = u32::from_be_bytes(raw[..4].try_into().unwrap()) as usize;
    serde_json::from_slice(&raw[4..4 + n]).unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn the_replay_fence_and_budget_survive_a_source_restart() {
    let seed = Identity::from_secret("seed-endpoint", &[0x22; 32]).unwrap();
    let phone_secret = [0x44; 32];
    let member = Identity::from_secret(&iroh_id(phone_secret), &[0x11; 32]).unwrap();
    let manifest = StageManifest::from_document(&fixture("qwen2_stage_manifest_v2.json")["manifest"]).unwrap();
    let objects = scratch("durable-objects");
    export_objects(&objects);
    let state = scratch("durable-state");
    let source = ArtifactSource::bind(&[0x54; 32], &[0x36; 32], seed.public_key_record(), objects.clone(), &state, Reachability::LocalOnly).await.unwrap();
    drop(source.spawn_accept()); // detached accept loop
    let g = grant(&seed, &iroh_id(phone_secret), &member, &manifest, &source, Some(40));
    let phone = iroh::Endpoint::builder(iroh::endpoint::presets::Minimal)
        .relay_mode(iroh::RelayMode::Disabled)
        .clear_ip_transports()
        .bind_addr(std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .secret_key(iroh::SecretKey::from_bytes(&phone_secret))
        .bind()
        .await
        .unwrap();
    let digest = manifest.tensors[0].sha256.clone();
    let req = |id: &str| ag::sign_chunk_request(&member, &g, id, &format!("nonce-{id}"), &source.endpoint_id(), &digest, 0, 16, now_ms()).unwrap();
    let first = req("r1");
    assert_eq!(raw_request(&phone, source.addr(), &first).await["status"], "ok");
    assert_eq!(raw_request(&phone, source.addr(), &first).await["code"], "artifact_chunk_request_replay");
    assert_eq!(raw_request(&phone, source.addr(), &req("r2")).await["status"], "ok");
    source.close().await;

    // Same keys and state dir: the fence and the 32 of 40 budget bytes persist.
    let restarted = ArtifactSource::bind(&[0x54; 32], &[0x36; 32], seed.public_key_record(), objects.clone(), &state, Reachability::LocalOnly).await.unwrap();
    drop(restarted.spawn_accept()); // detached accept loop
    assert_eq!(raw_request(&phone, restarted.addr(), &first).await["code"], "artifact_chunk_request_replay");
    assert_eq!(raw_request(&phone, restarted.addr(), &req("r3")).await["code"], "artifact_grant_budget_exhausted");
    // A torn final line is not an admission and does not break the replay.
    let journal = restarted.journal_path().unwrap();
    std::fs::OpenOptions::new().append(true).open(&journal).and_then(|mut f| std::io::Write::write_all(&mut f, b"{\"g\":\"grant-net-1\",\"e\":")).unwrap();
    restarted.close().await;
    let again = ArtifactSource::bind(&[0x54; 32], &[0x36; 32], seed.public_key_record(), objects.clone(), &state, Reachability::LocalOnly).await.unwrap();
    assert_eq!(std::fs::read_to_string(&journal).unwrap().lines().count(), 3, "compacted to the genesis header and the two admissions");
    again.close().await;
    std::fs::remove_dir_all(objects).unwrap();
    std::fs::remove_dir_all(state).unwrap();
}

// ---- fail-closed ledger state (security review: "fail-open state drift") ----

struct Bed {
    seed: Identity,
    member: Identity,
    phone_secret: [u8; 32],
    manifest: StageManifest,
    objects: PathBuf,
    state: PathBuf,
    phone: iroh::Endpoint,
}

async fn bed(tag: &str, phone_byte: u8) -> Bed {
    let seed = Identity::from_secret("seed-endpoint", &[0x22; 32]).unwrap();
    let phone_secret = [phone_byte; 32];
    let member = Identity::from_secret(&iroh_id(phone_secret), &[0x11; 32]).unwrap();
    let manifest = StageManifest::from_document(&fixture("qwen2_stage_manifest_v2.json")["manifest"]).unwrap();
    let objects = scratch(&format!("{tag}-objects"));
    export_objects(&objects);
    let state = scratch(&format!("{tag}-state"));
    let phone = iroh::Endpoint::builder(iroh::endpoint::presets::Minimal)
        .relay_mode(iroh::RelayMode::Disabled)
        .clear_ip_transports()
        .bind_addr(std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .secret_key(iroh::SecretKey::from_bytes(&phone_secret))
        .bind()
        .await
        .unwrap();
    Bed { seed, member, phone_secret, manifest, objects, state, phone }
}

impl Bed {
    async fn source(&self, key: u8) -> Result<ArtifactSource, String> {
        ArtifactSource::bind(&[key; 32], &[key ^ 0x80; 32], self.seed.public_key_record(), self.objects.clone(), &self.state, Reachability::LocalOnly)
            .await
            .map_err(|e| e.0)
    }

    fn request(&self, g: &Grant, source: &ArtifactSource, id: &str) -> Value {
        ag::sign_chunk_request(&self.member, g, id, &format!("nonce-{id}"), &source.endpoint_id(), &self.manifest.tensors[0].sha256, 0, 16, now_ms()).unwrap()
    }

    fn cleanup(&self) {
        let _ = std::fs::remove_dir_all(&self.objects);
        let _ = std::fs::remove_dir_all(&self.state);
    }
}

/// (3) A cleared ledger (new process, emptied dir) must not reopen old grants:
/// a grant that predates the ledger's genesis is refused, not started fresh.
#[tokio::test(flavor = "multi_thread")]
async fn a_cleared_ledger_refuses_grants_that_predate_it() {
    let b = bed("cleared", 0x61).await;
    let source = b.source(0x62).await.unwrap();
    drop(source.spawn_accept()); // detached accept loop
    let g = grant(&b.seed, &iroh_id(b.phone_secret), &b.member, &b.manifest, &source, None);
    let first = b.request(&g, &source, "r1");
    assert_eq!(raw_request(&b.phone, source.addr(), &first).await["status"], "ok");
    source.close().await;
    std::fs::remove_dir_all(&b.state).unwrap(); // the fence and budget are gone
    std::thread::sleep(std::time::Duration::from_millis(5));
    let reborn = b.source(0x62).await.unwrap();
    drop(reborn.spawn_accept()); // detached accept loop
    assert_eq!(raw_request(&b.phone, reborn.addr(), &first).await["code"], "artifact_source_ledger_reset");
    assert_eq!(raw_request(&b.phone, reborn.addr(), &b.request(&g, &reborn, "r2")).await["code"], "artifact_source_ledger_reset");
    // A grant issued after the new genesis is served.
    std::thread::sleep(std::time::Duration::from_millis(5));
    let fresh = grant(&b.seed, &iroh_id(b.phone_secret), &b.member, &b.manifest, &reborn, None);
    assert_eq!(raw_request(&b.phone, reborn.addr(), &b.request(&fresh, &reborn, "r3")).await["status"], "ok");
    reborn.close().await;
    b.cleanup();
}

/// (3) An unreadable or corrupt ledger fails the bind instead of being
/// silently replaced by an empty one.
#[cfg(unix)]
#[tokio::test(flavor = "multi_thread")]
async fn an_unreadable_or_corrupt_ledger_fails_closed() {
    use std::os::unix::fs::PermissionsExt;
    let b = bed("corrupt", 0x63).await;
    let source = b.source(0x64).await.unwrap();
    let journal = source.journal_path().unwrap();
    source.close().await;
    std::fs::set_permissions(&journal, std::fs::Permissions::from_mode(0o000)).unwrap();
    assert_eq!(b.source(0x64).await.err().unwrap(), "artifact_source_ledger_unavailable");
    std::fs::set_permissions(&journal, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut text = std::fs::read_to_string(&journal).unwrap();
    text.push_str("{not json\n{\"g\":\"grant-net-1\",\"e\":1,\"f\":\"x\",\"n\":1,\"x\":1}\n");
    std::fs::write(&journal, &text).unwrap();
    assert_eq!(b.source(0x64).await.err().unwrap(), "artifact_source_ledger_corrupt");
    std::fs::write(&journal, b"").unwrap(); // header gone
    assert_eq!(b.source(0x64).await.err().unwrap(), "artifact_source_ledger_corrupt");
    b.cleanup();
}

/// (1) The fence is never evicted while a fenced request could still be
/// replayed: at capacity the source refuses new requests instead.
#[tokio::test(flavor = "multi_thread")]
async fn a_full_fence_refuses_rather_than_forgetting_live_requests() {
    let b = bed("fence", 0x65).await;
    let source = b.source(0x66).await.unwrap();
    source.set_fence_capacity(2);
    drop(source.spawn_accept()); // detached accept loop
    let g = grant(&b.seed, &iroh_id(b.phone_secret), &b.member, &b.manifest, &source, None);
    let first = b.request(&g, &source, "r1");
    assert_eq!(raw_request(&b.phone, source.addr(), &first).await["status"], "ok");
    assert_eq!(raw_request(&b.phone, source.addr(), &b.request(&g, &source, "r2")).await["status"], "ok");
    assert_eq!(raw_request(&b.phone, source.addr(), &b.request(&g, &source, "r3")).await["code"], "artifact_source_busy");
    assert_eq!(raw_request(&b.phone, source.addr(), &first).await["code"], "artifact_chunk_request_replay");
    source.close().await;
    b.cleanup();
}

/// (2) Concurrent requests for one grant cannot race past its budget.
#[tokio::test(flavor = "multi_thread")]
async fn concurrent_requests_cannot_overspend_a_grant() {
    let b = bed("race", 0x67).await;
    // Allow eight connections from the one peer so the budget check itself is raced.
    let limits = mycelium_mobile_core::artifact_net::ArtifactLimits { connections_per_peer: 8, ..Default::default() };
    let source = b.source(0x68).await.unwrap().with_limits(limits);
    drop(source.spawn_accept()); // detached accept loop
    let g = grant(&b.seed, &iroh_id(b.phone_secret), &b.member, &b.manifest, &source, Some(48));
    let mut tasks = Vec::new();
    for i in 0..8 {
        let (phone, addr, env) = (b.phone.clone(), source.addr(), b.request(&g, &source, &format!("c{i}")));
        tasks.push(tokio::spawn(async move { raw_request(&phone, addr, &env).await }));
    }
    let mut ok = 0;
    for t in tasks {
        let h = t.await.unwrap();
        if h["status"] == "ok" {
            ok += 1;
        } else {
            assert_eq!(h["code"], "artifact_grant_budget_exhausted");
        }
    }
    assert_eq!(ok, 3, "exactly 48 / 16 requests are served");
    let lines = std::fs::read_to_string(source.journal_path().unwrap()).unwrap().lines().count();
    assert_eq!(lines, 1 + 3, "genesis + one durable admission per served request");
    source.close().await;
    b.cleanup();
}

/// (4) A receipt that does not cover the bytes received never lets those
/// bytes into the store: the partial stays empty and nothing is promoted.
#[tokio::test(flavor = "multi_thread")]
async fn a_bad_receipt_leaves_no_trusted_bytes_on_the_phone() {
    let b = bed("receipt", 0x69).await;
    // A rogue "source" holding the authorized source keys that flips one byte
    // after signing an honest receipt.
    let source_secret = [0x6a; 32];
    let signer = Identity::from_secret(&iroh_id(source_secret), &[0x6a ^ 0x80; 32]).unwrap();
    let rogue = iroh::Endpoint::builder(iroh::endpoint::presets::Minimal)
        .relay_mode(iroh::RelayMode::Disabled)
        .clear_ip_transports()
        .bind_addr(std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .secret_key(iroh::SecretKey::from_bytes(&source_secret))
        .alpns(vec![ag::ARTIFACT_CHUNK_ALPN.to_vec()])
        .bind()
        .await
        .unwrap();
    let honest = b.source(0x6b).await.unwrap(); // only used to shape the grant
    let mut g = grant(&b.seed, &iroh_id(b.phone_secret), &b.member, &b.manifest, &honest, None);
    g.sources = [(iroh_id(source_secret), signer.verification_key_digest())].into_iter().collect();
    let objects = b.objects.clone();
    let rogue_task = rogue.clone();
    let grant_for_rogue = g.clone();
    tokio::spawn(async move {
        while let Some(incoming) = rogue_task.accept().await {
            let Ok(conn) = incoming.await else { continue };
            while let Ok((mut send, mut recv)) = conn.accept_bi().await {
                let raw = recv.read_to_end(1 << 20).await.unwrap();
                let env: Value = serde_json::from_slice(&raw).unwrap();
                let m = &env["message"];
                let req = ag::ChunkRequest {
                    request_id: m["request_id"].as_str().unwrap().into(),
                    source_endpoint_id: m["source_endpoint_id"].as_str().unwrap().into(),
                    object_digest: m["object_digest"].as_str().unwrap().into(),
                    offset_bytes: m["offset_bytes"].as_u64().unwrap(),
                    length_bytes: m["length_bytes"].as_u64().unwrap(),
                };
                let all = std::fs::read(objects.join(&req.object_digest[7..])).unwrap();
                let mut data = all[req.offset_bytes as usize..(req.offset_bytes + req.length_bytes) as usize].to_vec();
                let receipt = ag::sign_chunk_receipt(&signer, &req, &grant_for_rogue, &data, now_ms()).unwrap();
                data[0] ^= 1; // the bytes sent differ from the bytes receipted
                let header = serde_json::to_vec(&json!({"protocol": "mycelium.mobile_artifact_chunk_response.v1", "status": "ok", "receipt": receipt})).unwrap();
                let mut out = (header.len() as u32).to_be_bytes().to_vec();
                out.extend_from_slice(&header);
                out.extend_from_slice(&data);
                send.write_all(&out).await.unwrap();
                send.finish().unwrap();
            }
        }
    });
    let client = IrohChunkSource::new(b.phone.clone(), Arc::new(Identity::from_secret(&iroh_id(b.phone_secret), &[0x11; 32]).unwrap()), g, vec![rogue.addr()]).unwrap();
    let manifest = b.manifest.clone();
    let root = scratch("receipt-store");
    let store_root = root.clone();
    let err = tokio::task::spawn_blocking(move || {
        let mut client = client;
        let store = ArtifactStore::open(&store_root, 1 << 30).unwrap();
        store.acquire(&manifest, &mut client, 1024, None).unwrap_err().0
    })
    .await
    .unwrap();
    assert_eq!(err, "artifact_chunk_receipt_mismatch");
    let partial_bytes: u64 = std::fs::read_dir(root.join("partial")).unwrap().flatten().map(|e| e.metadata().unwrap().len()).sum();
    assert_eq!(partial_bytes, 0, "no unverified byte was written");
    assert_eq!(std::fs::read_dir(root.join("objects")).unwrap().count(), 0);
    honest.close().await;
    std::fs::remove_dir_all(root).unwrap();
    b.cleanup();
}

// ---- denial of service: bounded serving (the sidecar also carries Router traffic) ----

async fn closed_within(conn: &iroh::endpoint::Connection, within: std::time::Duration) -> bool {
    tokio::time::timeout(within, conn.closed()).await.is_ok()
}

#[tokio::test(flavor = "multi_thread")]
async fn connections_are_bounded_per_peer_and_in_total_and_half_open_streams_are_dropped() {
    use mycelium_mobile_core::artifact_net::ArtifactLimits;
    let b = bed("dos", 0x6c).await;
    let limits = ArtifactLimits { connections: 3, connections_per_peer: 2, read_timeout: std::time::Duration::from_millis(300), idle_timeout: std::time::Duration::from_secs(20) };
    let source = b.source(0x6d).await.unwrap().with_limits(limits);
    drop(source.spawn_accept()); // detached accept loop
    let short = std::time::Duration::from_millis(1500);
    // Per peer: the third connection from one peer is closed at once.
    let first = b.phone.connect(source.addr(), ag::ARTIFACT_CHUNK_ALPN).await.unwrap();
    let second = b.phone.connect(source.addr(), ag::ARTIFACT_CHUNK_ALPN).await.unwrap();
    let third = b.phone.connect(source.addr(), ag::ARTIFACT_CHUNK_ALPN).await.unwrap();
    assert!(closed_within(&third, short).await, "a peer's third connection is refused");
    assert!(!closed_within(&first, std::time::Duration::from_millis(300)).await);
    // In total: one more peer fits (3), the next is refused.
    let other = bed("dos2", 0x6e).await;
    let fourth = other.phone.connect(source.addr(), ag::ARTIFACT_CHUNK_ALPN).await.unwrap();
    assert!(!closed_within(&fourth, std::time::Duration::from_millis(300)).await);
    let third_peer = bed("dos3", 0x6f).await;
    let fifth = third_peer.phone.connect(source.addr(), ag::ARTIFACT_CHUNK_ALPN).await.unwrap();
    assert!(closed_within(&fifth, short).await, "over the total the connection is refused");
    // A half-open request (never finished) loses its connection after the
    // read deadline, which frees the peer's slot for a new connection.
    let (mut send, _recv) = second.open_bi().await.unwrap();
    send.write_all(b"{\"partial\":").await.unwrap();
    assert!(closed_within(&second, std::time::Duration::from_secs(3)).await, "a slow request is dropped");
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    let again = b.phone.connect(source.addr(), ag::ARTIFACT_CHUNK_ALPN).await.unwrap();
    assert!(!closed_within(&again, std::time::Duration::from_millis(300)).await, "the freed slot is reusable");
    source.close().await;
    for bed in [&b, &other, &third_peer] {
        bed.cleanup();
    }
}

/// A request naming another recipient is refused before any signature check
/// or disk IO: even with a broken signature the answer is the transport
/// mismatch, not the signature failure that full verification would give.
#[tokio::test(flavor = "multi_thread")]
async fn a_request_for_another_recipient_is_refused_before_crypto_or_disk() {
    let b = bed("pre", 0x75).await;
    let source = b.source(0x76).await.unwrap();
    drop(source.spawn_accept()); // detached accept loop
    let g = grant(&b.seed, &"f".repeat(64), &b.member, &b.manifest, &source, None);
    let mut env = b.request(&g, &source, "r1");
    env["signature"]["signature"] = json!("A".repeat(86) + "==");
    assert_eq!(raw_request(&b.phone, source.addr(), &env).await["code"], "artifact_recipient_transport_mismatch");
    source.close().await;
    b.cleanup();
}
