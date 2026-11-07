// SPDX-License-Identifier: AGPL-3.0-only
//! Deterministic held real-carrier cancellation, not a native receipt fixture.
use api::managed_carrier::{HostClock, LocalMailboxCarrier, TargetFence};
use budgets::{BudgetId as B, Entries};
use std::{
    sync::{Arc, mpsc},
    time::Duration,
};
use transport::Clock;
#[test]
fn target_stop_wakes_held_real_carrier_and_fences_late_response() {
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let carrier = Arc::new(
        LocalMailboxCarrier::start(move |_| {
            entered_tx.send(()).unwrap();
            release_rx
                .recv()
                .map_err(|_| runtime::managed_owner::OwnerError("fixture_released"))?;
            Ok(b"late-transport-bytes-not-a-native-receipt".to_vec())
        })
        .unwrap(),
    );
    let fence = TargetFence::default();
    let f = fence.clone();
    let c = carrier.clone();
    let clock = HostClock::default();
    let allowance = B::PyMyceliumA13MacosManagedControlManagedcontrolclientInitTimeoutL1933
        .span(Entries::ZERO)
        .unwrap();
    let patience = Duration::from_millis(allowance.millis());
    let (done_tx, done_rx) = mpsc::sync_channel(1);
    let caller = std::thread::spawn(move || {
        let end = clock.monotonic().checked_add(allowance).unwrap();
        let result = c.call_target(&clock, b"held", end, &f);
        done_tx
            .send((result, end.remaining(clock.monotonic()).unwrap()))
            .unwrap();
    });
    let entered = entered_rx.recv_timeout(patience);
    let cancelled = fence.cancel();
    let result = done_rx.recv_timeout(patience);
    // Always release before assertion/join, including a broken cancellation control.
    match release_tx.send(()) {
        Ok(()) => {}
        Err(_) => eprintln!("held carrier already released"),
    }
    caller.join().unwrap();
    carrier.close().unwrap();
    entered.unwrap();
    cancelled.unwrap();
    let (result, remaining) = result.unwrap();
    assert_ne!(
        remaining,
        budgets::Span::ZERO,
        "cancellation must wake before the unchanged absolute call deadline"
    );
    assert!(
        result.is_err(),
        "Stop must wake a held call and reject its later bytes"
    );
    assert!(fence.check().is_err());
}
