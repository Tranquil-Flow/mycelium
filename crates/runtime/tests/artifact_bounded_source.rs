// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "iroh-adapter")]
//! Byte-store controls only, never native proof or model qualification.
use runtime::{
    artifact_journal::{DurableConfig, DurableObjects},
    iroh_artifact::ArtifactError,
    iroh_artifact_source::ArtifactObjects,
};
use std::{
    fs,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};
#[test]
fn bounded_verification_checks_each_read_and_does_not_cache_interrupted_scans() {
    let root = std::env::temp_dir().join(format!("b7c-bounded-source-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let cap = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
        .bytes()
        .unwrap()
        .get() as usize;
    let bytes = vec![42; cap + 1];
    let digest = protocol::artifact_grant::range_digest(&bytes);
    let path = root.join(&digest[7..]);
    fs::write(&path, &bytes).unwrap();
    let objects = DurableObjects::open(DurableConfig {
        objects: root.clone(),
        state_dir: root.join("journal"),
        fence_capacity: 1,
        clock_skew_ms: 0,
        now: Arc::new(|| 0),
    })
    .unwrap();
    let calls = AtomicUsize::new(0);
    assert_eq!(
        objects
            .verify_bounded(&digest, &|| {
                if calls.fetch_add(1, Ordering::SeqCst) == 2 {
                    Err(ArtifactError("test_cancel".into()))
                } else {
                    Ok(())
                }
            })
            .unwrap_err()
            .0,
        "test_cancel"
    );
    assert_eq!(calls.load(Ordering::SeqCst), 3);
    fs::write(&path, b"changed after cancellation").unwrap();
    assert!(
        objects.verified_size(&digest).is_none(),
        "cancelled scan must not publish a cached size"
    );
    assert_eq!(
        objects.verify_bounded(&digest, &|| Ok(())).unwrap_err().0,
        "artifact_source_chunk_unverified"
    );
    fs::write(&path, &bytes).unwrap();
    assert_eq!(
        objects.verify_bounded(&digest, &|| Ok(())).unwrap(),
        bytes.len() as u64
    );
    assert_eq!(objects.verified_size(&digest), Some(bytes.len() as u64));
    assert_eq!(
        objects
            .verify_bounded("sha256:../../elsewhere", &|| Ok(()))
            .unwrap_err()
            .0,
        "artifact_object_digest_invalid"
    );
    drop(objects);
    fs::remove_dir_all(root).unwrap();
}
