// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB patch 3 (REVIEW XB2-06): the window's `/api/network/reach` action is one action
//! transaction, like every other control action (`api::actions::Control`, v0.1.4's `app.guard`):
//! its role and run checks, load and save happen as one piece, so two overlapping changes (or a
//! change and Start) never interleave and none of them is lost.
use super::*;
use crate::desktop_host::DesktopServices;
use desktop::services::{ServiceRequest, Services as _};
use std::{num::NonZeroUsize, sync::mpsc, time::Duration};

/// One `/api/network/reach` request through the window's action path; its answer arrives on the
/// returned channel.
fn reach_request(services: &DesktopServices, body: Value) -> mpsc::Receiver<(u16, Value)> {
    let (tx, rx) = mpsc::sync_channel(1);
    services.submit(
        ServiceRequest {
            method: "POST".into(),
            target: crate::packaged_work::reach::ACTION.into(),
            headers: vec![
                ("x-mycelium-action".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&body).unwrap(),
        },
        Box::new(move |reply| {
            tx.send((
                reply.status,
                serde_json::from_slice(&reply.body).unwrap_or(Value::Null),
            ))
            .unwrap_or_default();
        }),
    );
    rx
}

/// Two overlapping changes: the first is held between its load and its save (until the second
/// one answered, at most 1 s). Without one transaction the second completes inside that window
/// and the first then saves its stale load over it (a lost update); with it the second waits for
/// the first's whole transaction, and the stored result is the two changes in order.
#[test]
fn two_overlapping_reach_changes_run_one_transaction_after_the_other() {
    let root = std::env::temp_dir().join(format!(
        "xb3-reach-action-{}",
        crate::service::random_id().unwrap()
    ));
    fs::create_dir(&root).unwrap();
    let dirs = crate::packaged::prepare_dirs(&root).unwrap();
    let node = NodeService::new(
        Arc::new(NoNative),
        "reach-action".into(),
        crate::native::development_qualification().unwrap(),
    );
    api::actions::Backend::publish(&mut node.clone()).unwrap();
    let services = DesktopServices::new(node, NonZeroUsize::new(4).unwrap(), &dirs.private);
    let network = services.network();
    let (second_answered, answered) = mpsc::channel::<()>();
    let (loaded, first_loaded) = mpsc::channel::<()>();
    network.reach.hold_after_load(Box::new(move || {
        loaded.send(()).unwrap_or_default();
        answered.recv_timeout(Duration::from_secs(1)).unwrap_or_default();
    }));
    let first = reach_request(&services, json!({"portmapping": true}));
    first_loaded
        .recv_timeout(Duration::from_secs(30))
        .expect("the first change loaded");
    let second = reach_request(&services, json!({"portmapping": false}));
    let early = second.recv_timeout(Duration::from_millis(500));
    if early.is_ok() {
        second_answered.send(()).unwrap_or_default();
    }
    let first = first.recv_timeout(Duration::from_secs(30)).unwrap();
    assert_eq!(first.0, 200, "{}", first.1);
    assert!(
        early.is_err(),
        "the second change answered inside the first one's transaction: {early:?}"
    );
    let second = second.recv_timeout(Duration::from_secs(30)).unwrap();
    assert_eq!(second.0, 200, "{}", second.1);
    let mut status = json!({});
    crate::packaged_network::status_overlay(&mut status, None, &network);
    assert_eq!(
        status["network_reach"]["portmapping"], false,
        "the later change stands: {status}"
    );
    std::fs::remove_dir_all(&root).ok();
}
