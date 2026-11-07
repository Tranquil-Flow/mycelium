// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(all(feature = "metal", target_os = "macos"))]
use budgets::WallExpiry;
use node_service::{
    control::{Ids, Session, Wire},
    enrollment::*,
    iroh_frames::{self, Clock},
    receipts, service,
};
use serde_json::json;
use std::os::unix::fs::PermissionsExt;
fn private_dir(name: &str) -> std::path::PathBuf {
    let root = std::env::temp_dir()
        .canonicalize()
        .unwrap()
        .join(format!("{name}-{}", service::random_id().unwrap()));
    std::fs::create_dir(&root).unwrap();
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
    root
}
#[test]
fn even_the_pinned_owner_cannot_enable_a_member_qualification_clock_over_node_control() {
    let root = private_dir("slice-clock");
    let owner_state = private_dir("slice-clock-owner");
    let owner = receipts::identity("owner").unwrap();
    let member = identity(&root).unwrap();
    let now = WallExpiry::from_unix_millis(receipts::wall_ms().unwrap());
    LocalMembership::approve(
        &root,
        Invitation {
            swarm: "clock-test".into(),
            owner_node: "owner".into(),
            owner_endpoint: owner.endpoint_id().into(),
            owner_key: owner.public_key_record(),
            member_node: "b-member".into(),
            member_key: member.public_key_record(),
            nonce: "clock-test".into(),
        },
        node::Caps {
            storage_bytes: 1,
            memory_bytes: 1,
            wall_ms: 1,
            commands: 1,
        },
        true,
        now,
    )
    .unwrap();
    let rt = iroh_frames::runtime().unwrap();
    let clock = Clock::new(1);
    let mut host =
        iroh_frames::bind_persisted(&rt, &clock, &owner_state, &iroh_frames::OWNER_CHANNELS)
            .unwrap();
    let launch =
        serde_json::to_string(&json!({"owner":iroh_frames::address(&host).unwrap()})).unwrap();
    let state = root.clone();
    let child = std::thread::spawn(move || {
        node_service::native::member(&launch, &state, false).map_err(|e| e.to_string())
    });
    let wire = Wire {
        host: &host,
        clock: &clock,
        runtime: &rt,
        signer: &owner,
        watch: None,
    };
    let mut session = Session::owner(
        Ids {
            request_id: "clock-test".into(),
            generation: 1,
        },
        member.public_key_record(),
    );
    // The signed, pinned owner asks the member to run a fast-forwarded clock.
    wire.serve(
        &mut session,
        "register",
        None,
        budgets::BudgetId::WorkerStartup,
        |_, _| {
            Ok(json!({"node":"b-member","owner_key":owner.public_key_record(),"swarm_id":"clock-test",
                "request_id":"clock-test","generation":1,"qualification_clock":true,
                "qualification_now":now.unix_millis()}))
        },
    )
    .unwrap();
    assert_eq!(
        child.join().unwrap().unwrap_err(),
        "qualification_clock_not_admitted"
    );
    iroh_frames::close(&mut host, &clock).unwrap();
    drop(host);
    drop(rt);
    std::fs::remove_dir_all(root).unwrap();
    std::fs::remove_dir_all(owner_state).unwrap();
}
