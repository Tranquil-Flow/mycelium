// SPDX-License-Identifier: AGPL-3.0-only
//! EP2G: real owner Work/Epoch/carrier/gateway; controlled admission and compute.
//! No native-worker reap or physical-node claim.
use super::*;

#[test]
fn ep2g_stop_during_held_dial_closes_partial_peers_and_cleans_other_member() {
    let mut swarm = swarm(2);
    let plan = swarm.serving_plan();
    let (entered, held) = mpsc::channel();
    let (release, gate) = mpsc::channel();
    let mut scripts = stage_scripts();
    scripts[1].setup_hold = Some((entered, gate));
    let (mut work, members) = swarm.serving(plan, scripts);
    let proposal = json!({"max_wall_seconds":60});
    work.start_serving(&proposal).unwrap();
    let staged = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(staged["state"], "awaiting_activation", "{staged}");
    ActivationPort::activate(&mut work, &proposal).unwrap();
    held.recv_timeout(span(B::WorkerStartup)).unwrap();
    let began = Instant::now();
    let bound = stop_allowance(60, 2) + span(B::OwnerPoll) * 2;
    let (done, thread) = stop(work);
    let result = done.recv_timeout(bound);
    let elapsed = began.elapsed();
    // Release only AFTER recording the bounded observation. Join even on RED.
    release.send(()).unwrap();
    let mut work = thread.join().unwrap();
    let state = result
        .as_ref()
        .map(|r| r.1.clone())
        .unwrap_or_else(|_| work.state());
    drop(work.take_members());
    eprintln!("EP2G held dial elapsed={elapsed:?} state={state}");
    assert!(
        result.is_ok(),
        "Stop remained in a held dial past {bound:?}"
    );
    assert_eq!(state["state"], "stopped");
    assert!(
        state["member_cleanup"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["node_id"] == state["transferred"][0]["node_id"] && r["discarded"] == true),
        "healthy member cleanup: {state}"
    );
    let first = members[0].0.lock().unwrap();
    assert!(first.served.is_some(), "partial peer's serve ended");
    assert_eq!(first.commands.last().map(String::as_str), Some("discard"));
    assert!(
        members[1]
            .0
            .lock()
            .unwrap()
            .compute
            .contains(&("partial_closed", 1)),
        "owned partial peer closed before rescue"
    );
}

#[test]
fn ep2g_stop_held_gateway_and_member_close_share_the_first_stop_end() {
    use std::io::{Read, Write};
    let mut swarm = swarm(2);
    let plan = swarm.serving_plan();
    let (entered, closing) = mpsc::channel();
    let (release, gate) = mpsc::channel();
    let mut scripts = stage_scripts();
    scripts[1].close_hold = Some((entered, gate));
    let (mut work, members) = swarm.serving(plan, scripts);
    activate(&mut work);
    let gateway = work.gateway().unwrap();
    let bound = stop_allowance(60, 2);
    let first_stop = Instant::now();
    let signal = work.stopper();
    signal();
    // Model the host spending most of the ORIGINAL allowance before teardown.
    // This timeout is intentional time consumption, not readiness synchronization.
    let (keep, wait) = mpsc::channel::<()>();
    assert!(matches!(
        wait.recv_timeout(bound - span(B::RouterClose)),
        Err(mpsc::RecvTimeoutError::Timeout)
    ));
    drop(keep);
    let mut silent = std::net::TcpStream::connect(gateway_addr(&gateway)).unwrap();
    silent.write_all(b"GET /v1/inference HTTP/1.1\r\n").unwrap();
    // A subsequent round-trip is an accept-loop milestone for the held socket.
    let (status, _) = sse_client::http(
        gateway_addr(&gateway),
        "GET",
        "/health",
        &bearer(&gateway),
        "",
    );
    assert!(status > 0);
    signal(); // Repeated Stop MUST NOT renew the end.
    let (done, thread) = stop(work);
    let result = done.recv_timeout(
        (first_stop + bound + span(B::OwnerPoll) * 2).saturating_duration_since(Instant::now()),
    );
    let elapsed = first_stop.elapsed();
    // Rescue the controlled member only after measuring; never count rescue as success.
    release.send(()).unwrap();
    silent.shutdown(std::net::Shutdown::Both).unwrap();
    let mut work = thread.join().unwrap();
    let state = result
        .as_ref()
        .map(|r| r.1.clone())
        .unwrap_or_else(|_| work.state());
    assert!(closing.recv_timeout(span(B::RouterClose)).is_ok());
    drop(work.take_members());
    eprintln!("EP2G held close elapsed={elapsed:?} state={state}");
    assert!(
        result.is_ok(),
        "held gateway/close renewed first Stop allowance: {elapsed:?} > {bound:?}"
    );
    assert_eq!(
        state["cleanup_error"], "owner_target_cleanup_unproven",
        "{state}"
    );
    assert!(
        state["member_cleanup"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["node_id"] == state["transferred"][0]["node_id"] && r["discarded"] == true),
        "unrelated member not cleaned: {state}"
    );
    assert!(members[0].0.lock().unwrap().served.is_some());
    silent.set_read_timeout(Some(span(B::OwnerPoll))).unwrap();
    let mut byte = [0];
    assert_eq!(silent.read(&mut byte).unwrap_or(0), 0);
    assert!(
        std::net::TcpStream::connect(gateway_addr(&gateway)).is_err(),
        "owned gateway joined"
    );
}
