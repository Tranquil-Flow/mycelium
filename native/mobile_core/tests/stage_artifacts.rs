// SPDX-License-Identifier: AGPL-3.0-only
//! v2 stage manifests and the digest-verified per-tensor artifact store
//! (plan M2): Python-authored manifest fixtures verify byte-for-byte in Rust,
//! acquisition resumes and promotes atomically, every corruption fails closed,
//! and a stage loaded from the store passes its host golden self-check.
//!
//! The real-weight tests run when `MYCELIUM_QWEN05_SNAPSHOT` names a local
//! Qwen2.5-0.5B snapshot directory, or (on a device) when
//! `MYCELIUM_QWEN05_OBJECTS` names a directory holding a v2 `manifest.json`
//! and its exported `objects/<sha256 hex>` (`qwen_stage_manifest.py export`).
//! `MYCELIUM_MOBILE_FIXTURES` points a cross-compiled binary at pushed fixtures.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use mycelium_mobile_core::artifact::{ArtifactError, ArtifactStore, ChunkSource};
use mycelium_mobile_core::canonical;
use mycelium_mobile_core::qwen2::{Qwen2Config, Qwen2Stage, SafetensorsBytes};
use mycelium_mobile_core::safetensors;
use mycelium_mobile_core::stage_manifest::{self, StageManifest, golden_input, output_digest};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn fixture(name: &str) -> Value {
    let dir = std::env::var("MYCELIUM_MOBILE_FIXTURES").unwrap_or_else(|_| format!("{}/tests/fixtures", env!("CARGO_MANIFEST_DIR")));
    let path = format!("{dir}/{name}");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn scratch(tag: &str) -> PathBuf {
    let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let dir = std::env::temp_dir().join(format!("mycelium-artifacts-{tag}-{}-{n}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn synthetic_bytes() -> Vec<u8> {
    STANDARD.decode(fixture("qwen2_synthetic.json")["safetensors_b64"].as_str().unwrap()).unwrap()
}

/// Serves raw tensor ranges out of an in-memory Safetensors file by digest,
/// optionally failing, stalling or corrupting to exercise the store.
struct MemorySource {
    objects: HashMap<String, Vec<u8>>,
    fail_after_bytes: Option<u64>,
    served: u64,
    corrupt: bool,
    oversize: bool,
    stall: bool,
}

impl MemorySource {
    fn new(file: &[u8]) -> Self {
        let header = safetensors::parse_file(file).unwrap();
        let mut objects = HashMap::new();
        for name in header.tensors.keys() {
            let (s, e) = header.range(name).unwrap();
            let bytes = file[s as usize..e as usize].to_vec();
            objects.insert(format!("sha256:{}", hex::encode(Sha256::digest(&bytes))), bytes);
        }
        Self { objects, fail_after_bytes: None, served: 0, corrupt: false, oversize: false, stall: false }
    }
}

impl ChunkSource for MemorySource {
    fn fetch(&mut self, sha256: &str, offset: u64, max_len: usize) -> Result<Vec<u8>, ArtifactError> {
        if self.fail_after_bytes.is_some_and(|limit| self.served >= limit) {
            return Err(ArtifactError("source_unreachable".into()));
        }
        if self.stall {
            return Ok(Vec::new());
        }
        let object = self.objects.get(sha256).ok_or_else(|| ArtifactError("artifact_chunk_not_advertised".into()))?;
        let start = offset as usize;
        let end = (start + max_len).min(object.len());
        let mut chunk = object[start..end].to_vec();
        if self.oversize {
            chunk.push(0);
        }
        if self.corrupt && start == 0 {
            chunk[0] ^= 0x40;
        }
        self.served += chunk.len() as u64;
        Ok(chunk)
    }
}

fn manifest() -> StageManifest {
    StageManifest::from_document(&fixture("qwen2_stage_manifest_v2.json")["manifest"]).unwrap()
}

fn redigest(mut doc: Value) -> Value {
    let map = doc.as_object_mut().unwrap();
    map.remove("pack_digest");
    let digest = canonical::sha256_prefixed(&canonical::to_ascii_bytes(&Value::Object(map.clone())).unwrap());
    map.insert("pack_digest".into(), json!(digest));
    doc
}

#[test]
fn python_manifest_verifies_with_identical_canonical_bytes_and_signature() {
    let f = fixture("qwen2_stage_manifest_v2.json");
    let m = StageManifest::from_document(&f["manifest"]).unwrap();
    assert_eq!(m.pack_digest, f["manifest"]["pack_digest"].as_str().unwrap());
    assert_eq!(canonical::sha256_prefixed(&canonical::to_ascii_bytes(&f["manifest"]).unwrap()), f["canonical_ascii_sha256"].as_str().unwrap());
    assert_eq!(m.config.hidden_size, 64);
    assert_eq!(m.config.head_dim, 16);
    assert_eq!(m.config.rms_norm_eps, 1e-6);
    assert_eq!(m.config.rope_theta, 1e6);
    assert_eq!(m.tensors.len(), 24);
    assert_eq!(m.golden.as_ref().unwrap().seq, 3);
    assert_eq!(m.source_sha256, fixture("qwen2_synthetic.json")["safetensors_sha256"].as_str().unwrap());
    let signed = StageManifest::verify_signed(&f["signed"], &f["seed_key"]).unwrap();
    assert_eq!(signed, m);
    // Every tensor digest equals the raw bytes in the synthetic Safetensors file.
    let source = MemorySource::new(&synthetic_bytes());
    for t in &m.tensors {
        assert_eq!(source.objects[&t.sha256].len() as u64, t.size_bytes, "{}", t.name);
    }
    assert_eq!(m.total_bytes(), source.objects.values().map(|v| v.len() as u64).sum::<u64>());
}

#[test]
fn manifest_tampering_fails_closed_with_the_python_codes() {
    let doc = fixture("qwen2_stage_manifest_v2.json")["manifest"].clone();
    let code = |d: Value| StageManifest::from_document(&d).unwrap_err().0;
    let mut t = doc.clone();
    t["tensors"][3]["sha256"] = json!(format!("sha256:{}", "0".repeat(64)));
    assert_eq!(code(t), "stage_manifest_digest_mismatch");
    for (field, value, expected) in [
        ("backend", json!("mobile-rust-reference"), "stage_manifest_backend_unsupported"),
        ("quantizer", json!("mlx_int8"), "stage_manifest_quantizer_unsupported"),
        ("activation_dtype", json!("float16"), "stage_manifest_activation_dtype_unsupported"),
        ("route_ready", json!(true), "stage_manifest_route_ready_invalid"),
        ("end_layer_exclusive", json!(3), "stage_manifest_layer_range_invalid"),
        ("resolved_commit", json!("main"), "stage_manifest_identity_invalid"),
    ] {
        let mut d = doc.clone();
        d[field] = value;
        assert_eq!(code(redigest(d)), expected, "{field}");
    }
    let mut d = doc.clone();
    d["tensors"][0]["shape"] = json!([1, 64]);
    assert_eq!(code(redigest(d)), "stage_manifest_tensor_shape_invalid");
    let mut d = doc.clone();
    d["tensors"][0]["dtype"] = json!("F16");
    assert_eq!(code(redigest(d)), "stage_manifest_tensor_dtype_unsupported");
    let mut d = doc.clone();
    d["architecture"]["rope_theta"] = json!(1000000);
    assert_eq!(code(redigest(d)), "stage_manifest_architecture_invalid");
    let mut d = doc.clone();
    d["tensors"].as_array_mut().unwrap().swap(0, 1);
    assert_eq!(code(redigest(d)), "stage_manifest_tensors_invalid");
    let mut d = doc.clone();
    d["golden"]["seq"] = json!(65);
    assert_eq!(code(redigest(d)), "stage_manifest_golden_invalid");
    let mut d = doc;
    d["extra"] = json!(1);
    assert_eq!(code(d), "stage_manifest_fields_invalid");
}

#[test]
fn golden_input_matches_python_bit_for_bit() {
    let g = &fixture("qwen2_stage_manifest_v2.json")["golden_input"];
    let python = STANDARD.decode(g["f32_le_b64"].as_str().unwrap()).unwrap();
    let ours: Vec<u8> = golden_input(42, 2, 8).iter().flat_map(|v| v.to_le_bytes()).collect();
    assert_eq!(ours, python);
}

#[test]
fn acquire_promote_and_load_passes_the_host_golden() {
    let root = scratch("load");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let m = manifest();
    assert!(!store.is_complete(&m));
    assert_eq!(store.missing_bytes(&m), m.total_bytes());
    let file = synthetic_bytes();
    let mut src = MemorySource::new(&file);
    let report = store.acquire(&m, &mut src, 1000, Some(10 << 30)).unwrap();
    assert_eq!(report.promoted_objects, 24);
    assert_eq!(report.fetched_bytes, m.total_bytes());
    assert_eq!(report.resumed_bytes, 0);
    assert!(store.is_complete(&m));
    assert_eq!(std::fs::read_dir(root.join("partial")).unwrap().count(), 0);
    // A second acquisition fetches nothing.
    let again = store.acquire(&m, &mut src, 1000, None).unwrap();
    assert_eq!((again.reused_objects, again.fetched_bytes), (24, 0));
    // Loaded from the store == loaded straight from the file, bit for bit, and
    // the manifest's golden (computed by the host build) is reproduced.
    let stage = store.load_stage(&m).unwrap();
    let mut direct = SafetensorsBytes::new(&file).unwrap();
    let reference = Qwen2Stage::load(m.config.clone(), 0, 2, &mut direct).unwrap();
    let x = golden_input(7, 5, 64);
    assert_eq!(output_digest(&stage.forward(5, &x).unwrap()), output_digest(&reference.forward(5, &x).unwrap()));
    assert_eq!(stage_manifest::golden_digest(&stage, 42, 3).unwrap(), m.golden.as_ref().unwrap().output_digest);
    assert_eq!(stage.resident_bytes() as u64, m.resident_bytes());
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn an_interrupted_acquisition_resumes_from_the_partial() {
    let root = scratch("resume");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let m = manifest();
    let file = synthetic_bytes();
    let mut src = MemorySource::new(&file);
    src.fail_after_bytes = Some(20_000);
    assert_eq!(store.acquire(&m, &mut src, 3000, None).unwrap_err().0, "source_unreachable");
    let served = src.served;
    let partial_bytes: u64 = std::fs::read_dir(root.join("partial")).unwrap().flatten().map(|e| e.metadata().unwrap().len()).sum();
    assert!(partial_bytes > 0);
    // "Reboot": reopening keeps the partial and the promoted objects.
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let promoted_first: u64 = m.tensors.iter().filter(|t| store.has_object(t)).map(|t| t.size_bytes).sum();
    assert_eq!(promoted_first + partial_bytes, served, "every fetched byte is either promoted or kept");
    let still_missing = store.missing_bytes(&m);
    assert_eq!(still_missing, m.total_bytes() - served);
    let mut src = MemorySource::new(&file);
    let report = store.acquire(&m, &mut src, 3000, None).unwrap();
    assert_eq!(report.resumed_bytes, partial_bytes);
    assert_eq!(report.fetched_bytes, still_missing, "nothing already held is fetched again");
    assert!(store.is_complete(&m));
    store.load_stage(&m).unwrap();
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn corrupt_oversized_stalled_or_overlong_bytes_never_become_objects() {
    let root = scratch("corrupt");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let m = manifest();
    let file = synthetic_bytes();
    let objects = |r: &Path| std::fs::read_dir(r.join("objects")).unwrap().count();

    let mut src = MemorySource::new(&file);
    src.corrupt = true;
    assert_eq!(store.acquire(&m, &mut src, 1 << 20, None).unwrap_err().0, "artifact_digest_mismatch");
    assert_eq!(objects(&root), 0);
    assert_eq!(std::fs::read_dir(root.join("partial")).unwrap().count(), 0, "a bad partial is deleted");

    let mut src = MemorySource::new(&file);
    src.oversize = true;
    assert_eq!(store.acquire(&m, &mut src, 64, None).unwrap_err().0, "artifact_chunk_oversized");
    let mut src = MemorySource::new(&file);
    src.stall = true;
    assert_eq!(store.acquire(&m, &mut src, 64, None).unwrap_err().0, "artifact_source_stalled");
    assert_eq!(objects(&root), 0);

    // A partial longer than its object is discarded, not trusted.
    let first = &m.tensors[0];
    std::fs::write(root.join("partial").join(first.hex()), vec![7u8; first.size_bytes as usize + 10]).unwrap();
    let mut src = MemorySource::new(&file);
    let report = store.acquire(&m, &mut src, 1 << 20, None).unwrap();
    assert_eq!(report.fetched_bytes, m.total_bytes());
    assert!(store.is_complete(&m));
    assert_eq!(store.acquire(&m, &mut src, 0, None).unwrap_err().0, "artifact_chunk_size_invalid");
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_tampered_object_on_disk_is_refused_at_load_and_refetched() {
    let root = scratch("tamper");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let m = manifest();
    let file = synthetic_bytes();
    store.acquire(&m, &mut MemorySource::new(&file), 1 << 20, None).unwrap();
    let victim = root.join("objects").join(m.tensors[5].hex());
    let mut bytes = std::fs::read(&victim).unwrap();
    bytes[0] ^= 1;
    std::fs::write(&victim, &bytes).unwrap();
    assert_eq!(store.load_stage(&m).unwrap_err().0, "artifact_object_digest_mismatch");
    assert!(!victim.exists(), "the corrupt object is dropped");
    assert!(!store.is_complete(&m));
    let report = store.acquire(&m, &mut MemorySource::new(&file), 1 << 20, None).unwrap();
    assert_eq!(report.promoted_objects, 1);
    store.load_stage(&m).unwrap();
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_wrong_golden_fails_the_self_check() {
    let root = scratch("golden");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let mut doc = fixture("qwen2_stage_manifest_v2.json")["manifest"].clone();
    doc["golden"]["seed"] = json!(43);
    let m = StageManifest::from_document(&redigest(doc)).unwrap();
    store.acquire(&m, &mut MemorySource::new(&synthetic_bytes()), 1 << 20, None).unwrap();
    assert_eq!(store.load_stage(&m).unwrap_err().0, "stage_golden_mismatch");
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn storage_admission_quota_free_space_and_memory() {
    let root = scratch("quota");
    let m = manifest();
    let store = ArtifactStore::open(&root, m.total_bytes() - 1).unwrap();
    let mut src = MemorySource::new(&synthetic_bytes());
    assert_eq!(store.acquire(&m, &mut src, 4096, None).unwrap_err().0, "artifact_quota_exceeded");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    assert_eq!(store.acquire(&m, &mut src, 4096, Some(1 << 20)).unwrap_err().0, "artifact_disk_space_insufficient");
    assert_eq!(std::fs::read_dir(root.join("partial")).unwrap().count(), 0, "admission precedes any write");
    assert!(m.admit_memory(4 * m.resident_bytes()).is_ok());
    assert_eq!(m.admit_memory(4 * m.resident_bytes() - 4).unwrap_err().0, "stage_memory_insufficient");
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn manifests_persist_atomically_and_boot_cleanup_and_retain_tidy_the_store() {
    let root = scratch("persist");
    let f = fixture("qwen2_stage_manifest_v2.json");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let m = store.store_manifest(&f["signed"], &f["seed_key"]).unwrap();
    assert_eq!(store.load_manifest(&m.pack_digest, &f["seed_key"]).unwrap(), m);
    let other_key = json!({"algorithm": "ed25519", "encoding": "base64", "verification_key": "x", "verification_key_digest": "y"});
    assert!(store.store_manifest(&f["signed"], &other_key).is_err());
    // A persisted manifest edited on disk is refused at boot.
    let path = root.join("manifests").join(format!("{}.json", &m.pack_digest[7..]));
    let mut env: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    env["message"]["stage_id"] = json!("evil");
    std::fs::write(&path, serde_json::to_vec(&env).unwrap()).unwrap();
    assert!(store.load_manifest(&m.pack_digest, &f["seed_key"]).is_err());
    store.store_manifest(&f["signed"], &f["seed_key"]).unwrap();

    let file = synthetic_bytes();
    let mut src = MemorySource::new(&file);
    src.fail_after_bytes = Some(5000);
    let _ = store.acquire(&m, &mut src, 1000, None);
    std::fs::write(root.join("tmp").join("half-written.json"), b"{").unwrap();
    std::fs::write(root.join("objects").join("not-a-digest"), b"junk").unwrap();
    std::fs::create_dir(root.join("partial").join("dir")).unwrap();
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    assert_eq!(std::fs::read_dir(root.join("tmp")).unwrap().count(), 0);
    assert!(!root.join("objects").join("not-a-digest").exists());
    assert!(!root.join("partial").join("dir").exists());
    assert!(std::fs::read_dir(root.join("partial")).unwrap().count() > 0, "partials survive boot");
    store.acquire(&m, &mut MemorySource::new(&file), 1000, None).unwrap();
    assert!(store.used_bytes() >= m.total_bytes());
    assert!(store.retain(&[&m]) == 0);
    let freed = store.retain(&[]);
    assert!(freed >= m.total_bytes());
    assert_eq!(store.used_bytes(), 0);
    assert!(store.load_manifest(&m.pack_digest, &f["seed_key"]).is_err());
    std::fs::remove_dir_all(root).unwrap();
}

/// Real weights: a manifest built here from the snapshot's header for layers
/// 12..14 (same digest rule as the Python builder), objects fetched from the
/// file through the store, then the store-loaded stage must reproduce the host
/// golden computed by `examples/qwen2_stage_golden` (seed 42, seq 4).
#[test]
fn real_qwen05_two_layers_through_the_store() {
    let Some(dir) = std::env::var_os("MYCELIUM_QWEN05_SNAPSHOT").map(PathBuf::from) else {
        eprintln!("MYCELIUM_QWEN05_SNAPSHOT not set; skipping");
        return;
    };
    let file = std::fs::read(dir.join("model.safetensors")).unwrap();
    let header = safetensors::parse_file(&file).unwrap();
    let hf: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("config.json")).unwrap()).unwrap();
    let config = Qwen2Config::from_hf_config(&hf).unwrap();
    let tensors: Vec<Value> = stage_manifest::tensor_names(12, 14)
        .iter()
        .map(|name| {
            let entry = &header.tensors[name];
            let (s, e) = header.range(name).unwrap();
            json!({"name": name, "dtype": entry.dtype, "shape": entry.shape, "size_bytes": e - s,
                   "sha256": format!("sha256:{}", hex::encode(Sha256::digest(&file[s as usize..e as usize])))})
        })
        .collect();
    let doc = redigest(json!({
        "protocol": stage_manifest::MANIFEST_PROTOCOL,
        "stage_id": "qwen05-12-14",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "resolved_commit": "7ae557604adf67be50417f59c2c2f167def9a775",
        "source": {"file": "model.safetensors", "sha256": format!("sha256:{}", hex::encode(Sha256::digest(&file))), "size_bytes": file.len()},
        "architecture": {"model_type": "qwen2", "hidden_act": "silu", "hidden_size": config.hidden_size,
            "intermediate_size": config.intermediate_size, "num_attention_heads": config.num_attention_heads,
            "num_key_value_heads": config.num_key_value_heads, "head_dim": config.head_dim, "num_hidden_layers": 24,
            "rms_norm_eps": hf["rms_norm_eps"], "rope_theta": hf["rope_theta"]},
        "start_layer": 12,
        "end_layer_exclusive": 14,
        "backend": "mobile-rust-qwen2",
        "quantizer": stage_manifest::QUANTIZER,
        "activation_dtype": "float32",
        "tensors": tensors,
        "golden": {"protocol": stage_manifest::GOLDEN_PROTOCOL, "generator": stage_manifest::GOLDEN_GENERATOR, "seed": 42, "seq": 4,
                   "output_digest": "sha256:71de5cfde1c7046c4371ca6a47052b4070ff3effeb5ce8d967841ef9890c0e80"},
        "route_ready": false,
    }));
    let m = StageManifest::from_document(&doc).unwrap();
    assert!(m.source_sha256.starts_with("sha256:fdf756fa"));
    let root = scratch("real");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let report = store.acquire(&m, &mut MemorySource::new(&file), 4 << 20, None).unwrap();
    assert_eq!(report.promoted_objects, 24);
    let stage = store.load_stage(&m).unwrap(); // includes the golden self-check
    assert_eq!(stage.resident_bytes() as u64, m.resident_bytes());
    eprintln!("real 2-layer stage: {} raw bytes, {} resident bytes", m.total_bytes(), m.resident_bytes());
    std::fs::remove_dir_all(root).unwrap();
}

/// Serves object ranges from a directory of exported objects (the stand-in
/// for an artifact source on the device), optionally failing after a budget.
struct DirSource {
    dir: PathBuf,
    fail_after_bytes: Option<u64>,
    served: u64,
}

impl ChunkSource for DirSource {
    fn fetch(&mut self, sha256: &str, offset: u64, max_len: usize) -> Result<Vec<u8>, ArtifactError> {
        use std::io::{Read, Seek, SeekFrom};
        if self.fail_after_bytes.is_some_and(|limit| self.served >= limit) {
            return Err(ArtifactError("source_unreachable".into()));
        }
        let mut f = std::fs::File::open(self.dir.join(&sha256[7..])).map_err(|_| ArtifactError("artifact_chunk_not_advertised".into()))?;
        f.seek(SeekFrom::Start(offset)).unwrap();
        let mut buf = vec![0u8; max_len];
        let mut n = 0;
        while n < max_len {
            let r = f.read(&mut buf[n..]).unwrap();
            if r == 0 {
                break;
            }
            n += r;
        }
        buf.truncate(n);
        self.served += n as u64;
        Ok(buf)
    }
}

/// Device path: the layers 12..14 manifest (built by the Python builder with
/// the host golden) and its exported objects were pushed; acquire with an
/// interruption, resume, promote, reject a corrupted object, refetch it, then
/// load from the store and pass the host golden on this device.
#[test]
fn real_qwen05_objects_resume_reject_and_match_the_host_golden() {
    let Some(dir) = std::env::var_os("MYCELIUM_QWEN05_OBJECTS").map(PathBuf::from) else {
        eprintln!("MYCELIUM_QWEN05_OBJECTS not set; skipping");
        return;
    };
    let doc: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("manifest.json")).unwrap()).unwrap();
    let m = StageManifest::from_document(&doc).unwrap();
    assert_eq!(m.golden.as_ref().unwrap().output_digest, "sha256:71de5cfde1c7046c4371ca6a47052b4070ff3effeb5ce8d967841ef9890c0e80");
    let root = scratch("device");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let t = std::time::Instant::now();
    let mut src = DirSource { dir: dir.join("objects"), fail_after_bytes: Some(25_000_000), served: 0 };
    assert_eq!(store.acquire(&m, &mut src, 4 << 20, None).unwrap_err().0, "source_unreachable");
    let served = src.served;
    let partial: u64 = std::fs::read_dir(root.join("partial")).unwrap().flatten().map(|e| e.metadata().unwrap().len()).sum();
    let promoted: u64 = m.tensors.iter().filter(|t| store.has_object(t)).map(|t| t.size_bytes).sum();
    assert_eq!(promoted + partial, served);
    assert!(partial > 0, "the interruption left a partial to resume");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap(); // "reboot"
    let mut src = DirSource { dir: dir.join("objects"), fail_after_bytes: None, served: 0 };
    let report = store.acquire(&m, &mut src, 4 << 20, None).unwrap();
    assert_eq!(report.resumed_bytes, partial);
    assert_eq!(report.fetched_bytes, m.total_bytes() - served);
    assert!(store.is_complete(&m));
    let acquire_ms = t.elapsed().as_secs_f64() * 1e3;
    // Corrupt one promoted object on disk: refused at load, dropped, refetched.
    let victim = root.join("objects").join(m.tensors[3].hex());
    let mut bytes = std::fs::read(&victim).unwrap();
    bytes[100] ^= 0x10;
    std::fs::write(&victim, &bytes).unwrap();
    assert_eq!(store.load_stage(&m).unwrap_err().0, "artifact_object_digest_mismatch");
    assert!(!store.is_complete(&m));
    let again = store.acquire(&m, &mut DirSource { dir: dir.join("objects"), fail_after_bytes: None, served: 0 }, 4 << 20, None).unwrap();
    assert_eq!(again.promoted_objects, 1);
    let t = std::time::Instant::now();
    let stage = store.load_stage(&m).unwrap(); // includes the golden self-check
    let load_ms = t.elapsed().as_secs_f64() * 1e3;
    assert_eq!(stage.resident_bytes() as u64, m.resident_bytes());
    eprintln!(
        "device v2: {} bytes acquired (interrupted at {served}, resumed {partial}) in {acquire_ms:.0} ms; load+quantize+golden {load_ms:.0} ms; resident {}",
        m.total_bytes(),
        m.resident_bytes()
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_policy_gate_pauses_acquisition_and_a_later_call_resumes() {
    let root = scratch("gate");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let m = manifest();
    let file = synthetic_bytes();
    let mut calls = 0;
    let mut gate = || {
        calls += 1;
        if calls > 10 { Err(ArtifactError("thermal_ceiling_reached".into())) } else { Ok(()) }
    };
    let mut src = MemorySource::new(&file);
    let err = store.acquire_with(&m, &mut src, 1000, None, &mut gate).unwrap_err();
    assert_eq!(err.0, "thermal_ceiling_reached");
    assert_eq!(calls, 11, "the gate is asked before every chunk and stops at once");
    let missing = store.missing_bytes(&m);
    assert_eq!(missing, m.total_bytes() - src.served, "every admitted chunk was kept");
    let report = store.acquire(&m, &mut MemorySource::new(&file), 1000, None).unwrap();
    assert_eq!(report.fetched_bytes, missing);
    store.load_stage(&m).unwrap();
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn the_daily_network_budget_stops_acquisition_and_survives_a_restart() {
    use mycelium_mobile_core::artifact::{MeteredSource, NetworkBudget};
    let root = scratch("budget");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let m = manifest();
    let file = synthetic_bytes();
    let day = || 20_000u64;
    let mut budget = NetworkBudget::open(&root, 50_000, day());
    let mut inner = MemorySource::new(&file);
    let err = store.acquire(&m, &mut MeteredSource { inner: &mut inner, budget: &mut budget, day: &day }, 1000, None).unwrap_err();
    assert_eq!(err.0, "network_budget_exhausted");
    assert!(budget.used() <= 50_000 && budget.used() > 49_000);
    assert_eq!(budget.used(), inner.served);
    // A restart the same day keeps the count; the next day starts afresh.
    let mut same_day = NetworkBudget::open(&root, 50_000, day());
    assert_eq!(same_day.used(), inner.served);
    assert!(same_day.admit(day(), 1000).is_err());
    let next = || 20_001u64;
    let mut budget = NetworkBudget::open(&root, 1 << 30, next());
    assert_eq!(budget.used(), 0);
    let mut inner = MemorySource::new(&file);
    store.acquire(&m, &mut MeteredSource { inner: &mut inner, budget: &mut budget, day: &next }, 1000, None).unwrap();
    store.load_stage(&m).unwrap();
    std::fs::remove_dir_all(root).unwrap();
}

#[cfg(unix)]
#[test]
fn free_space_is_reported_for_the_store_volume() {
    let root = scratch("free");
    let free = mycelium_mobile_core::artifact::available_bytes(&root).unwrap();
    assert!(free > 0);
    assert!(mycelium_mobile_core::artifact::available_bytes(&root.join("missing")).is_none());
    std::fs::remove_dir_all(root).unwrap();
}

// ---- node review: fail-closed v2 apply state ----

#[test]
fn a_stage_is_served_only_with_a_golden_that_passed() {
    let root = scratch("serve-golden");
    let store = ArtifactStore::open(&root, 1 << 30).unwrap();
    let mut doc = fixture("qwen2_stage_manifest_v2.json")["manifest"].clone();
    doc["golden"] = Value::Null;
    let m = StageManifest::from_document(&redigest(doc)).unwrap();
    store.acquire(&m, &mut MemorySource::new(&synthetic_bytes()), 1 << 20, None).unwrap();
    assert_eq!(store.load_stage_for_serving(&m).unwrap_err().0, "stage_manifest_golden_required");
    let with = manifest();
    store.acquire(&with, &mut MemorySource::new(&synthetic_bytes()), 1 << 20, None).unwrap();
    store.load_stage_for_serving(&with).unwrap();
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_corrupt_or_unreadable_network_budget_record_fails_closed() {
    use mycelium_mobile_core::artifact::NetworkBudget;
    let root = scratch("budget-corrupt");
    std::fs::write(root.join("network-budget.json"), b"{not json").unwrap();
    let mut budget = NetworkBudget::open(&root, 1000, 7);
    assert_eq!(budget.used(), 1000, "an unreadable count is treated as the day's quota spent");
    assert_eq!(budget.admit(7, 1).unwrap_err().0, "network_budget_exhausted");
    // The next day starts afresh, and a clean record from another day is not trusted for today.
    assert!(budget.admit(8, 1).is_ok());
    std::fs::write(root.join("network-budget.json"), br#"{"day":6,"bytes":999}"#).unwrap();
    assert_eq!(NetworkBudget::open(&root, 1000, 7).used(), 0);
    std::fs::remove_dir_all(root).unwrap();
}
