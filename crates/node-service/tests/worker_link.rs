// SPDX-License-Identifier: AGPL-3.0-only
//! The private link between a member AGENT (signer) and its NATIVE WORKER (RU-020): a worker's
//! death is a typed `Closed` the agent survives, and nothing else on the link is silent.
use node_service::worker_link::{Link, LinkError};
use serde_json::json;
use std::{io::Write, os::unix::net::UnixStream, thread, time::Duration};

/// A registered L1 span as a `Duration` (no literal time in the tests).
fn budget(id: budgets::BudgetId) -> Duration {
    node_service::service::duration(id)
}
/// Allowance for a frame whose peer is expected to answer promptly.
fn frame_wait() -> Duration {
    budget(budgets::BudgetId::WorkerStartup)
}
fn pair() -> (Link, Link) {
    let (a, b) = UnixStream::pair().unwrap();
    let wait = budget(budgets::BudgetId::RegistryPoll);
    (Link::new(a, wait).unwrap(), Link::new(b, wait).unwrap())
}

#[test]
fn a_head_and_a_binary_payload_round_trip_exactly() {
    let (mut agent, mut worker) = pair();
    let payload: Vec<u8> = (0..=255).collect();
    let echo = thread::spawn(move || {
        let (head, body) = worker.receive().unwrap();
        worker.send(&head, &body).unwrap();
    });
    agent
        .send(&json!({"command":"step","tokens":[1,2,3]}), &payload)
        .unwrap();
    let (head, body) = agent.receive().unwrap();
    assert_eq!(head, json!({"command":"step","tokens":[1,2,3]}));
    assert_eq!(body, payload);
    echo.join().unwrap();
}

#[test]
fn an_empty_payload_is_a_frame_too() {
    let (mut agent, mut worker) = pair();
    agent.send(&json!({"command":"close"}), &[]).unwrap();
    let (head, body) = worker.receive().unwrap();
    assert_eq!(head["command"], "close");
    assert!(body.is_empty());
}

#[test]
fn a_dead_peer_is_closed_for_receive_and_for_send() {
    let (mut agent, worker) = pair();
    drop(worker);
    assert_eq!(agent.receive().unwrap_err(), LinkError::Closed);
    assert_eq!(
        agent.send(&json!({"command":"step"}), &[]).unwrap_err(),
        LinkError::Closed
    );
}

#[test]
fn a_peer_that_dies_mid_frame_is_closed_not_a_short_read() {
    let (a, mut raw) = UnixStream::pair().unwrap();
    let mut agent = Link::new(a, frame_wait()).unwrap();
    // A header that promises 100 bytes of head, then the peer dies after three.
    raw.write_all(&100u32.to_be_bytes()).unwrap();
    raw.write_all(b"abc").unwrap();
    drop(raw);
    assert_eq!(agent.receive().unwrap_err(), LinkError::Closed);
}

#[test]
fn an_oversized_declared_length_is_refused_before_it_is_read() {
    let (a, mut raw) = UnixStream::pair().unwrap();
    let mut agent = Link::new(a, frame_wait()).unwrap();
    raw.write_all(&u32::MAX.to_be_bytes()).unwrap();
    assert_eq!(
        agent.receive().unwrap_err(),
        LinkError::Frame("worker_link_head_too_large")
    );
    let (a, mut raw) = UnixStream::pair().unwrap();
    let mut agent = Link::new(a, frame_wait()).unwrap();
    let head = b"{}";
    raw.write_all(&(head.len() as u32).to_be_bytes()).unwrap();
    raw.write_all(head).unwrap();
    raw.write_all(&u32::MAX.to_be_bytes()).unwrap();
    assert_eq!(
        agent.receive().unwrap_err(),
        LinkError::Frame("worker_link_payload_too_large")
    );
}

#[test]
fn a_head_that_is_not_json_is_refused() {
    let (a, mut raw) = UnixStream::pair().unwrap();
    let mut agent = Link::new(a, frame_wait()).unwrap();
    raw.write_all(&3u32.to_be_bytes()).unwrap();
    raw.write_all(b"{{{").unwrap();
    raw.write_all(&0u32.to_be_bytes()).unwrap();
    assert_eq!(
        agent.receive().unwrap_err(),
        LinkError::Frame("worker_link_head_invalid")
    );
}

#[test]
fn a_silent_but_alive_peer_times_out_instead_of_hanging() {
    let (mut agent, _worker) = pair();
    assert_eq!(agent.receive().unwrap_err(), LinkError::Timeout);
}

#[test]
fn a_remote_error_reply_is_returned_as_its_code() {
    let (mut agent, mut worker) = pair();
    let peer = thread::spawn(move || {
        worker.receive().unwrap();
        worker
            .send(&json!({"error":"native_residue"}), &[])
            .unwrap();
    });
    let error = agent.request(&json!({"command":"close"}), &[]).unwrap_err();
    assert_eq!(error, LinkError::Remote("native_residue".into()));
    peer.join().unwrap();
}

#[test]
fn a_poll_reports_silence_without_losing_the_stream_and_then_the_frame() {
    let (mut agent, mut worker) = pair();
    let poll = budget(budgets::BudgetId::RegistryPoll) / 10;
    assert!(agent.poll(poll).unwrap().is_none());
    assert!(agent.poll(poll).unwrap().is_none());
    worker.send(&json!({"ok":true}), b"late").unwrap();
    let (head, body) = agent
        .poll(poll)
        .unwrap()
        .expect("the frame after the silence");
    assert_eq!(head, json!({"ok":true}));
    assert_eq!(body, b"late");
    drop(worker);
    assert_eq!(agent.poll(poll).unwrap_err(), LinkError::Closed);
}
