// SPDX-License-Identifier: AGPL-3.0-only
//! Repeated production-reader controls; only tiny inert objects, no native work.
use super::*;
use std::{
    fs,
    os::unix::fs::{MetadataExt, symlink},
    sync::atomic::{AtomicUsize, Ordering},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Root(PathBuf);
impl Drop for Root {
    fn drop(&mut self) {
        if let Err(e) = fs::remove_dir_all(&self.0) {
            eprintln!("source_test_cleanup_failed:{e}");
        }
    }
}
fn source() -> (Root, DurableObjects, String, Grant) {
    let path = std::env::temp_dir().join(format!(
        "s4cb3-source-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir(&path).unwrap();
    let root = Root(path.canonicalize().unwrap());
    fs::create_dir(root.0.join("stage-shards")).unwrap();
    let data = b"0123456789abcdef";
    let digest = range_digest(data);
    let file = root.0.join("stage-shards/stage.safetensors");
    fs::write(&file, data).unwrap();
    let mut pins = PinnedObjects::new();
    pins.admit(&digest[7..], &file, Some(data.len() as u64))
        .unwrap();
    let objects = root.0.join("objects");
    fs::create_dir(&objects).unwrap();
    symlink(&file, objects.join(&digest[7..])).unwrap();
    let store = DurableObjects::open(DurableConfig {
        objects,
        state_dir: root.0.join("journal"),
        fence_capacity: 16,
        clock_skew_ms: 0,
        now: Arc::new(|| 0),
    })
    .unwrap()
    .with_pinned(Some(Arc::new(pins)));
    assert_eq!(
        store.verify_bounded(&digest, &|| Ok(())).unwrap(),
        data.len() as u64
    );
    let grant = Grant {
        grant_id: "custody".into(),
        recipient_node_id: "member".into(),
        recipient_endpoint_id: "0".repeat(64),
        recipient_key_digest: range_digest(b"key"),
        assignment_id: "run".into(),
        manifest_pack_digest: range_digest(b"manifest"),
        allowed_object_digests: vec![digest.clone()],
        sources: BTreeMap::new(),
        source_addrs: BTreeMap::new(),
        maximum_total_bytes: data.len() as u64,
        maximum_bytes_per_second: 1 << 30,
        maximum_chunk_bytes: 8,
        not_before_unix_ms: 0,
        expires_at_unix_ms: 0,
        envelope: Value::Null,
    };
    (root, store, digest, grant)
}
fn chunk(
    store: &DurableObjects,
    digest: &str,
    grant: &Grant,
    offset: u64,
    id: &str,
) -> Result<AdmittedChunk, ArtifactError> {
    store.admit_and_read(
        grant,
        &ChunkRequest {
            request_id: id.into(),
            source_endpoint_id: "source".into(),
            object_digest: digest.into(),
            offset_bytes: offset,
            length_bytes: 8,
        },
        id,
        0,
        0,
    )
}
#[test]
fn repeated_reads_use_original_objects_and_charge_each_chunk_once() {
    let (root, store, digest, grant) = source();
    assert_eq!(
        chunk(&store, &digest, &grant, 0, "first").unwrap().bytes,
        b"01234567"
    );
    assert_eq!(
        chunk(&store, &digest, &grant, 8, "second").unwrap().bytes,
        b"89abcdef"
    );
    assert_eq!(store.lock().grants[&grant.grant_id].0, 16);
    assert_eq!(
        fs::metadata(root.0.join("stage-shards/stage.safetensors"))
            .unwrap()
            .nlink(),
        1
    );
    assert_eq!(
        chunk(&store, &digest, &grant, 0, "third").err().unwrap().0,
        "artifact_grant_budget_exhausted"
    );
}
#[test]
fn repeated_read_refuses_final_file_replacement_even_with_identical_bytes() {
    let (root, store, digest, grant) = source();
    assert_eq!(
        chunk(&store, &digest, &grant, 0, "first").unwrap().bytes,
        b"01234567"
    );
    let path = root.0.join("stage-shards/stage.safetensors");
    fs::rename(&path, root.0.join("original")).unwrap();
    fs::write(path, b"0123456789abcdef").unwrap();
    assert_eq!(
        chunk(&store, &digest, &grant, 8, "second").err().unwrap().0,
        "artifact_source_identity_changed"
    );
    assert!(
        store.verified_size(&digest).is_none(),
        "cached digest never bypasses custody"
    );
}
#[test]
fn repeated_read_refuses_replaced_parent_and_symlink_escape() {
    for link in [false, true] {
        let (root, store, digest, grant) = source();
        assert_eq!(
            chunk(&store, &digest, &grant, 0, "first").unwrap().bytes,
            b"01234567"
        );
        let path = root.0.join("stage-shards");
        let escaped = root.0.join("private-escape");
        fs::rename(&path, &escaped).unwrap();
        if link {
            symlink(&escaped, &path).unwrap();
        } else {
            fs::create_dir(&path).unwrap();
            fs::write(path.join("stage.safetensors"), b"0123456789abcdef").unwrap();
        }
        assert_eq!(
            chunk(&store, &digest, &grant, 8, "second").err().unwrap().0,
            "artifact_source_identity_changed"
        );
    }
}
#[test]
fn source_admission_preserves_size_single_link_and_no_follow_policy() {
    let (root, _store, digest, _grant) = source();
    let file = root.0.join("stage-shards/stage.safetensors");
    let mut pins = PinnedObjects::new();
    assert!(pins.admit(&digest[7..], &file, Some(1)).is_err());
    fs::hard_link(&file, root.0.join("extra-link")).unwrap();
    assert!(pins.admit(&digest[7..], &file, Some(16)).is_err());
    fs::remove_file(root.0.join("extra-link")).unwrap();
    symlink(&file, root.0.join("file-symlink")).unwrap();
    assert!(
        pins.admit(&digest[7..], &root.0.join("file-symlink"), Some(16))
            .is_err()
    );
}
