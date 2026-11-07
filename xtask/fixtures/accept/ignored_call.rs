// SPDX-License-Identifier: AGPL-3.0-only
fn run() -> Result<(), E> {
    stop()?;
    worker.join().map_err(Error::Join)?;
    let _ = 12; // A value, not a call.
    Ok(())
}
