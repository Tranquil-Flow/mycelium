// SPDX-License-Identifier: AGPL-3.0-only
use std::time::Duration as Delay;
use std::thread::spawn as launch;
fn run(seconds: u64) -> Result<(), E> {
    let wait = Delay::from_secs(seconds);
    let handle = launch(worker);
    handle.join().map_err(Error::Join)?;
    assert_eq!(wait, Duration::from_secs(seconds));
    boundary! { stop()?; }
    if let Err(error) = work() && should_stop { return Err(error); }
    Ok(())
}
