// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "iroh-loopback")]
use budgets::BudgetId as B;
use node_service::{
    activation,
    iroh_frames::{self, Clock, Frames},
};
use serde_json::{Value, json};
use stage_contract::{Activation, ByteOrder, DType, Layout};
fn header() -> Value {
    json!({"request_id":"request","phase":"PREFILL","token_index":0,"hop_index":0,"source_placement_id":"owner","destination_placement_id":"member","idempotency_key":"request-1-0","path_id":"path-1","path_attempt":0,"topology_version":1})
}
fn roundtrip(wrong_binding: bool) {
    let rt = iroh_frames::runtime().unwrap();
    let clock = Clock::new(1);
    let mut owner = iroh_frames::bind(&rt, &clock).unwrap();
    let owner_address = iroh_frames::address(&owner).unwrap();
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    let (member_done, member_completed) = std::sync::mpsc::sync_channel(1);
    let (owner_done, owner_completed) = std::sync::mpsc::sync_channel(1);
    let wait = std::time::Duration::from_millis(
        B::WorkerStartup
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    );
    let task = std::thread::spawn(move || {
        let rt = iroh_frames::runtime().unwrap();
        let clock = Clock::new(1);
        let mut member = iroh_frames::bind(&rt, &clock).unwrap();
        tx.send(iroh_frames::address(&member).unwrap()).unwrap();
        let peer = iroh_frames::peer(&owner_address).unwrap();
        let conn = member
            .accept(&clock, clock.deadline(B::WorkerStartup).unwrap())
            .unwrap();
        let mut frames = Frames::new(conn, &rt, &clock, &peer, 1).unwrap();
        let mut expected = header();
        if wrong_binding {
            expected["topology_version"] = json!(2);
        }
        let input = frames.receive(&expected);
        if wrong_binding {
            assert!(input.is_err());
        } else {
            frames.send(&input.unwrap()).unwrap();
        }
        // Keep the endpoint alive until both exchange callers consumed their
        // ACK/refusal. Writing an ACK does not prove its peer has read it.
        member_done.send(()).unwrap();
        owner_completed.recv_timeout(wait).unwrap();
        frames.close().unwrap();
        iroh_frames::close(&mut member, &clock).unwrap();
    });
    let address = rx
        .recv_timeout(std::time::Duration::from_millis(
            B::WorkerStartup
                .span(budgets::Entries::ZERO)
                .unwrap()
                .millis(),
        ))
        .unwrap();
    let peer = iroh_frames::peer(&address).unwrap();
    let conn = owner
        .connect(&peer, &clock, clock.deadline(B::WorkerStartup).unwrap())
        .unwrap();
    let mut frames = Frames::new(conn, &rt, &clock, &peer, 1).unwrap();
    let activation = Activation {
        shape: [1, 1, 2],
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes: vec![0, 0, 128, 63, 0, 0, 0, 192],
    };
    let raw = activation::frame(&header(), &activation).unwrap();
    let sent = frames.send(&raw);
    if wrong_binding {
        assert!(sent.is_err());
    } else {
        sent.unwrap();
        assert_eq!(frames.receive(&header()).unwrap(), raw);
    }
    member_completed.recv_timeout(wait).unwrap();
    owner_done.send(()).unwrap();
    frames.close().unwrap();
    iroh_frames::close(&mut owner, &clock).unwrap();
    task.join().unwrap();
}
#[test]
fn authenticated_router_carrier_preserves_native_activation_bytes() {
    roundtrip(false);
}
#[test]
fn authenticated_router_carrier_rejects_wrong_activation_generation() {
    roundtrip(true);
}
#[test]
fn provisioning_refuses_nonloopback_destinations_before_connect() {
    let rt = iroh_frames::runtime().unwrap();
    let clock = Clock::new(1);
    let mut host = iroh_frames::bind(&rt, &clock).unwrap();
    let mut address = iroh_frames::address(&host).unwrap();
    address["addresses"] = json!(["192.0.2.1:12345"]);
    assert!(iroh_frames::peer(&address).is_err());
    iroh_frames::close(&mut host, &clock).unwrap();
}
