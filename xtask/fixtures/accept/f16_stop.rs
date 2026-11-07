// SPDX-License-Identifier: AGPL-3.0-only
// MODEL corrected idiom: preserve stop failure and require proof before success.
// mycelium_a13_macos/demo_chain.py:1350-1384 @ f5bfb709.
fn retire_route(stop_wait: Duration) -> Result<(), E> {
    stop(route, stop_wait)?;
    route.join().map_err(Error::Join)?;
    require_cleanup_receipts()?;
    Ok(())
}
