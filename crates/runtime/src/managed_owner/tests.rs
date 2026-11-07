// SPDX-License-Identifier: AGPL-3.0-only
//! Fault injection through the real private atomic writer's byte bound.
use super::*;
use std::os::unix::fs::PermissionsExt;
#[test]
fn temporary_removal_preserves_write_outcome_and_rejects_other_errno() {
    use rustix::io::Errno;
    let write_failed = OwnerError("authority_state_write_failed");
    for removal in [Ok(()), Err(Errno::NOENT)] {
        assert_eq!(finish_atomic_save(Ok(()), removal), Ok(()));
        assert_eq!(
            finish_atomic_save(Err(write_failed), removal),
            Err(write_failed)
        );
    }
    for errno in [Errno::ACCESS, Errno::IO, Errno::ISDIR, Errno::INTR] {
        assert_eq!(
            finish_atomic_save(Ok(()), Err(errno)),
            Err(write_failed),
            "unexpected cleanup errno {errno:?} must refuse"
        );
        assert_eq!(
            finish_atomic_save(Err(write_failed), Err(errno)),
            Err(write_failed)
        );
    }
}
#[test]
fn failed_intent_save_consumes_reserved_sequence_and_never_reaches_io() {
    let root =
        std::env::temp_dir().join(format!("managed-owner-intent-write-{}", std::process::id()));
    std::fs::create_dir(&root).unwrap();
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
    struct Cleanup(std::path::PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }
    let _cleanup = Cleanup(root.clone());
    let fixture: Value = serde_json::from_str(include_str!(
        "../../tests/fixtures/managed-owner-oracle.json"
    ))
    .unwrap();
    let case = |name: &str| {
        fixture["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == name)
            .unwrap()
    };
    let intent = &case("uncertain_enqueue")["journal"]["commands"]["hello-1"];
    struct NoIo {
        identity: Identity,
        nonce: u64,
    }
    impl transport::Clock for NoIo {
        fn wall_seconds(&self) -> f64 {
            1000.0
        }
        fn monotonic(&self) -> budgets::MonoInstant {
            budgets::MonoInstant::new(budgets::ClockDomain(1), 100000)
        }
    }
    impl OwnerHost for NoIo {
        fn signer(&self) -> &Identity {
            &self.identity
        }
        fn current_member(&mut self, _: &str) -> Result<Value> {
            Ok(
                json!({"node_id":"member","endpoint_id":"member-endpoint","verification_key_digest":format!("sha256:{}","a".repeat(64)),"generation":1,"incarnation":"inc-1","lifecycle_state":"RUNNING","peer_class":"mac_mlx_iroh","lease_expires_at":1000000.0}),
            )
        }
        fn nonce(&mut self) -> Result<String> {
            self.nonce += 1;
            Ok(format!("{:032x}", self.nonce))
        }
        fn exchange(&mut self, _: &Value, _: budgets::MonoInstant) -> Result<Value> {
            panic!("IO after unpersisted intent")
        }
        fn wait(&mut self, _: Span, _: budgets::MonoInstant) -> Result<()> {
            panic!("wait after unpersisted intent")
        }
    }
    let identity = Identity::from_secret("owner-endpoint", &[7; 32]).unwrap();
    let mut host = NoIo { identity, nonce: 0 };
    let mut owner = ManagedControlOwner::open(
        &root,
        host.signer(),
        "swarm",
        "owner",
        Limits::registered().unwrap(),
    )
    .unwrap();
    let reserved = &case("write_failure_2")["journal"];
    owner.limits.max_bytes = canonical::to_bytes(reserved).unwrap().len() as u64;
    let command = &intent["enqueue_request"]["envelope"]["statement"]["native_command"];
    let deadline = budgets::MonoInstant::new(budgets::ClockDomain(1), 130000);
    assert_eq!(
        owner
            .exchange(&mut host, &mut intent["binding"].clone(), command, deadline)
            .unwrap_err()
            .0,
        "managed_owner_state_unavailable"
    );
    assert_eq!(owner.guard().unwrap_err().0, "managed_owner_closed");
    drop(owner);
    let actual: Value =
        serde_json::from_slice(&std::fs::read(root.join("owner.json")).unwrap()).unwrap();
    assert_eq!(actual, *reserved);
    let mut reopened = ManagedControlOwner::open(
        &root,
        host.signer(),
        "swarm",
        "owner",
        Limits::registered().unwrap(),
    )
    .unwrap();
    let prepared = reopened
        .prepare_enqueue(
            host.signer(),
            &intent["binding"],
            command,
            RequestMeta {
                request_id: "retry-request".into(),
                nonce: "retry-nonce".into(),
                issued_at: 1000.0,
                ttl: Span::from_millis(30000),
            },
        )
        .unwrap();
    assert_eq!(
        prepared["envelope"]["statement"]["sequence"],
        reserved["sequence_ceiling"]
    );
}
