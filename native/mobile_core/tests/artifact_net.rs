// SPDX-License-Identifier: AGPL-3.0-or-later
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
        "authorized_sources": [{"endpoint_id": source.endpoint_id(), "verification_key_digest": source.key_digest()}],
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
    let source = ArtifactSource::bind(&[0x51; 32], &[0x33; 32], seed.public_key_record(), objects.clone(), Reachability::LocalOnly).await.unwrap();
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
    let source2 = ArtifactSource::bind(&[0x52; 32], &[0x34; 32], w.seed.public_key_record(), w.objects.clone(), Reachability::LocalOnly).await.unwrap();
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
    let other = ArtifactSource::bind(&[0x53; 32], &[0x35; 32], w.seed.public_key_record(), w.objects.clone(), Reachability::LocalOnly).await.unwrap();
    let err = IrohChunkSource::bind(&[0x43; 32], w.member.clone(), g, vec![other.addr()], Reachability::LocalOnly).await.err().unwrap();
    assert_eq!(err.0, "artifact_source_not_authorized");
    std::fs::remove_dir_all(&w.objects).unwrap();
}
