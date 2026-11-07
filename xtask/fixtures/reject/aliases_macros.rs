// SPDX-License-Identifier: AGPL-3.0-only
use std::time::Duration as Delay;
use std::thread::spawn as launch;
use tokio as runtime;
fn run() {
    let wait = Delay::from_secs(30); // expect: TM001
    launch(worker); // expect: SE004
    runtime::spawn(async { work().await }); // expect: SE004
    assert_eq!(remaining, Duration::from_secs(30)); // expect: TM001
    boundary! { let _ = stop(); } // expect: SE001
    if let Err(error) = work() && should_stop {} // expect: SE003
}
