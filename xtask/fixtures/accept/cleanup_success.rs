// SPDX-License-Identifier: AGPL-3.0-only
fn cleanup() -> Result<(), E> {
    if let Err(error) = stop() { record_error("STOP_FAILED", &error, "stop"); return Err(error); }
    Ok(())
}
fn close() -> Result<(), E> {
    match worker.join() { Ok(()) => (), Err(error) => return Err(error) }
    record_success();
    Ok(())
}
fn telemetry() -> Result<(), E> {
    if let Err(error) = publish_metric() { record_error("METRIC_FAILED", &error, "telemetry"); }
    Ok(())
}
